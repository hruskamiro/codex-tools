"""Optional voice CLI, discovery, model installation and process ownership."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_tools import cli, voice


class Download(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data) if length is None else length)}


class VoiceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / 'state'
        self.server = self.root / 'bin/whisper-server'
        self.server.parent.mkdir()
        self.server.write_text('#!/bin/sh\nexit 0\n')
        self.server.chmod(0o700)
        self.model = self.root / 'ggml-small.bin'
        self.model.write_bytes(b'lmgg' + b'fixture' * 20)

    def run_voice(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = voice.main(['--state-dir', str(self.state), *args])
        return result, output.getvalue()

    def test_cli_dispatches_voice_only_when_requested(self):
        with patch.object(voice, 'main', return_value=7) as main:
            self.assertEqual(cli.main(['voice', 'doctor']), 7)
        main.assert_called_once_with(['doctor'])

    def test_init_configure_and_diagnostics_never_start_a_process_or_expose_key(self):
        with patch.object(voice.subprocess, 'Popen') as process:
            self.assertEqual(self.run_voice('init')[0], 0)
            identity = (self.state / 'identity.json').read_bytes()
            self.assertEqual(self.run_voice('init')[0], 1)
            self.assertEqual((self.state / 'identity.json').read_bytes(), identity)
            self.assertEqual(self.run_voice('configure', '--whisper-bin', str(self.server), '--model', str(self.model), '--cpu')[0], 0)
            with patch.object(voice, 'backend_ready', return_value=False):
                code, output = self.run_voice('doctor', '--json')
            self.assertEqual(code, 0)
            details = json.loads(output)
            self.assertTrue(details['managed_engine_configured'])
            self.assertFalse(details['engine_ready'])
            token = json.loads(identity)['token']
            self.assertNotIn(token, output)
            self.assertEqual(self.run_voice('token')[1].strip(), token)
            process.assert_not_called()
        self.assertEqual((self.state / 'identity.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_discovers_iris_server_and_model_without_importing_iris(self):
        config_home = self.root / 'config'
        (config_home / 'iris').mkdir(parents=True)
        (config_home / 'iris/config.json').write_text(json.dumps({'whisper_cpp': {
            'binary_path': str(self.server.with_name('whisper-cli')),
            'model_path': str(self.model),
        }}))
        actual_which = voice.shutil.which
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config_home)}), \
             patch.object(voice.shutil, 'which', side_effect=lambda p: None if p == 'whisper-server' else actual_which(p)):
            self.assertEqual(voice.find_server(), str(self.server))
            self.assertIn(str(self.model), voice.installed_models(self.state))
            self.assertEqual(voice.resolve_model('small', self.state), str(self.model))
        self.assertIsNone(voice.find_server('/missing/explicit/server'))

    def test_invalid_configuration_is_rejected_without_overwriting_saved_settings(self):
        self.assertEqual(self.run_voice('configure', '--whisper-bin', str(self.server), '--model', str(self.model))[0], 0)
        saved = (self.state / 'config.json').read_bytes()
        for arguments in [('--threads', '0'), ('--port', '8080'), ('--whisper-url', 'http://example.com:8080'), ('--model', '/missing/model')]:
            self.assertEqual(self.run_voice('configure', *arguments)[0], 1)
            self.assertEqual((self.state / 'config.json').read_bytes(), saved)

    def test_models_download_is_explicit_atomic_and_reuses_valid_files(self):
        data = b'lmgg' + b'model' * 30
        opener = Mock()
        opener.open.return_value = Download(data)
        with patch.object(voice, 'build_opener', return_value=opener):
            target = voice.download_model('small', self.state)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(voice.download_model('small', self.state), target)
            self.assertEqual(opener.open.call_count, 1)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin')
        opener.open.return_value = Download(b'lmgg' + b'incomplete' * 20, length=5000)
        with patch.object(voice, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError):
                voice.download_model('small', self.state, force=True)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(list(target.parent.glob('*.part')), [])
        with self.assertRaises(ValueError):
            voice.download_model('../../anything', self.state)

    def test_engine_command_binds_localhost_and_requires_explicit_model(self):
        settings = {'whisper_bin': str(self.server), 'model': str(self.model), 'cpu': True,
                    'whisper_url': 'http://127.0.0.1:18080', 'threads': 2}
        command = voice.engine_command(settings, self.state)
        self.assertEqual(command[:5], [str(self.server), '--host', '127.0.0.1', '--port', '18080'])
        self.assertIn('--no-gpu', command)
        self.assertEqual(command[command.index('--model') + 1], str(self.model))
        with self.assertRaises(ValueError):
            voice.engine_command({**settings, 'model': None}, self.state)

    def test_managed_engine_restarts_its_child_and_stops_only_that_process(self):
        script = self.root / 'fixture-engine.py'
        counter = self.root / 'starts'
        script.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nn=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\nif n==1:sys.exit(1)\ntime.sleep(60)\n')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        engine = voice.ManagedEngine([sys.executable, str(script), str(counter), '--port', str(port)], self.root)
        try:
            engine.start()
            deadline = time.monotonic() + 8
            while not counter.exists() or counter.read_text() != '2':
                if time.monotonic() > deadline:
                    self.fail('The managed engine was not restarted')
                time.sleep(.05)
            child = engine.process
        finally:
            engine.close()
        self.assertIsNotNone(child.poll())

    def test_doctor_does_not_create_state_or_load_a_model(self):
        with patch.object(voice, 'find_server', return_value=None), \
             patch.object(voice, 'installed_models', return_value=[]), \
             patch.object(voice, 'backend_ready', return_value=False):
            code, output = self.run_voice('doctor', '--json')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)['ready'])
        self.assertFalse(self.state.exists())

    def test_managed_engine_does_not_adopt_an_existing_listener(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            engine = voice.ManagedEngine(['fixture', '--port', str(listener.getsockname()[1])], self.root)
            with patch.object(engine, '_spawn') as spawn:
                with self.assertRaises(OSError):
                    engine.start()
                spawn.assert_not_called()

    def test_managed_engine_can_restart_after_recently_closed_connections(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.listen()
            with socket.create_connection(('127.0.0.1', port)) as client:
                accepted, _ = listener.accept()
                accepted.close()  # The server side can remain in TIME_WAIT.
                self.assertEqual(client.recv(1), b'')
        engine = voice.ManagedEngine([sys.executable, '-c', 'import time;time.sleep(60)', '--port', str(port)], self.root)
        try:
            engine.start()
            self.assertIsNone(engine.process.poll())
        finally:
            engine.close()

    def test_guided_setup_selects_an_existing_model_and_preserves_identity(self):
        self.assertEqual(self.run_voice('init')[0], 0)
        original = (self.state / 'identity.json').read_bytes()
        answers = ['1', '', '1', 'cpu', '2', '', '', 'yes']
        with patch.object(voice, 'find_server', return_value=str(self.server)), \
             patch.object(voice, 'installed_models', return_value=[str(self.model)]), \
             patch('builtins.input', side_effect=answers), \
             patch.object(voice, 'download_model') as download, \
             patch.object(voice.subprocess, 'Popen') as spawn:
            code, output = self.run_voice('configure', '--interactive')
        self.assertEqual(code, 0, output)
        settings = voice.load_settings(self.state)
        self.assertEqual(settings['model'], str(self.model))
        self.assertTrue(settings['manage_engine'])
        self.assertTrue(settings['cpu'])
        self.assertEqual(settings['threads'], 2)
        self.assertEqual((self.state / 'identity.json').read_bytes(), original)
        self.assertIn('ct voice serve', output)
        self.assertNotIn(json.loads(original)['token'], output)
        download.assert_not_called()
        spawn.assert_not_called()
        with patch.object(voice, 'serve', return_value=0) as serve:
            self.assertEqual(self.run_voice('serve')[0], 0)
            self.assertTrue(serve.call_args.kwargs['start_engine'])
            self.assertEqual(self.run_voice('serve', '--external-engine')[0], 0)
            self.assertFalse(serve.call_args.kwargs['start_engine'])

    def test_guided_external_setup_creates_identity_without_requiring_a_model(self):
        with patch('builtins.input', side_effect=['2', '', '', 'yes']), \
             patch.object(voice, 'download_model') as download:
            code, output = self.run_voice('configure', '--interactive')
        self.assertEqual(code, 0, output)
        self.assertTrue((self.state / 'identity.json').is_file())
        self.assertFalse(voice.load_settings(self.state)['manage_engine'])
        download.assert_not_called()

    def test_guided_setup_does_not_download_a_named_model_without_yes(self):
        with patch.object(voice, 'find_server', return_value=str(self.server)), \
             patch.object(voice, 'installed_models', return_value=[]), \
             patch('builtins.input', side_effect=['1', '', 'large', 'no', 'q']), \
             patch.object(voice, 'download_model') as download:
            code, output = self.run_voice('configure', '--interactive')
        self.assertEqual(code, 130, output)
        self.assertFalse(self.state.exists())
        download.assert_not_called()

    def test_guided_setup_cancel_keeps_existing_configuration_and_identity(self):
        self.assertEqual(self.run_voice('init')[0], 0)
        voice.write_settings(self.state, {'port': 8791})
        old_config = (self.state / 'config.json').read_bytes()
        old_identity = (self.state / 'identity.json').read_bytes()
        with patch('builtins.input', side_effect=['2', '', '', 'no']):
            code, _ = self.run_voice('configure', '--interactive')
        self.assertEqual(code, 130)
        self.assertEqual((self.state / 'config.json').read_bytes(), old_config)
        self.assertEqual((self.state / 'identity.json').read_bytes(), old_identity)

    def test_bare_configure_uses_terminal_and_is_explicit_when_noninteractive(self):
        with patch.object(voice.sys.stdin, 'isatty', return_value=True), \
             patch.object(voice, 'configure_interactively', return_value=0) as wizard:
            self.assertEqual(self.run_voice('configure')[0], 0)
            wizard.assert_called_once_with(self.state, {})
        with patch.object(voice.sys.stdin, 'isatty', return_value=False):
            code, output = self.run_voice('configure')
        self.assertEqual(code, 1)
        self.assertIn('--interactive', output)
        self.assertFalse(self.state.exists())

    def test_install_large_alias_selects_only_with_use_and_keeps_settings(self):
        data = b'lmgg' + b'fixture' * 20
        opener = Mock()
        opener.open.return_value = Download(data)
        with patch.object(voice, 'build_opener', return_value=opener), \
             patch.object(voice, 'resolve_model', return_value=None):
            code, output = self.run_voice('install', 'large')
        self.assertEqual(code, 0, output)
        self.assertIn('large-v3', output)
        self.assertTrue((self.state / 'models/ggml-large-v3.bin').exists())
        self.assertFalse((self.state / 'config.json').exists())
        self.assertIn('ggml-large-v3.bin', opener.open.call_args.args[0].full_url)
        voice.write_settings(self.state, {'port': 8799, 'cpu': True})
        with patch.object(voice, 'build_opener') as fetch:
            code, output = self.run_voice('install', 'large', '--use')
        self.assertEqual(code, 0, output)
        settings = voice.load_settings(self.state)
        self.assertEqual(Path(settings['model']).name, 'ggml-large-v3.bin')
        self.assertEqual(settings['port'], 8799)
        self.assertTrue(settings['cpu'])
        fetch.assert_not_called()

    def test_install_reuses_existing_iris_model_without_copying_or_downloading(self):
        with patch.object(voice, 'installed_models', return_value=[str(self.model)]), \
             patch.object(voice, 'download_model') as download:
            code, output = self.run_voice('install', 'small', '--use')
        self.assertEqual(code, 0, output)
        self.assertEqual(voice.load_settings(self.state)['model'], str(self.model))
        self.assertIn('Using installed', output)
        download.assert_not_called()

    def test_acceleration_is_reported_only_after_engine_verification(self):
        source = ("ggml_vulkan: 0 = AMD Radeon 890M Graphics (radv) | uma: 1\n"
                  "whisper_backend_init_gpu: using Vulkan0 backend\n")
        result = voice.acceleration_from_log(source)
        self.assertEqual(result['backend'], 'Vulkan0')
        self.assertEqual(result['device'], 'AMD Radeon 890M Graphics (radv)')
        self.assertTrue(result['verified'])
        self.assertEqual(voice.acceleration_from_log('whisper_model_load:      CPU total size = 42 MB')['backend'], 'CPU')
        self.assertFalse(voice.acceleration_from_log('use gpu = 1')['verified'])

    def test_legacy_state_directory_preserves_mobile_prototype_jobs(self):
        home = self.root / 'home'
        legacy = home / '.local/share/voice-jobs'
        legacy.mkdir(parents=True)
        (legacy / 'identity.json').write_text('{}')
        preferred = self.root / 'new-data'
        with patch.object(voice.Path, 'home', return_value=home), \
             patch.object(voice.paths, 'user_data_dir', return_value=preferred):
            self.assertEqual(voice.default_state_dir(), legacy)


if __name__ == '__main__':
    unittest.main()
