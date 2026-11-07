from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_tools import manager


class ProfileTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'source-manager'
        self.destination = self.root/'destination-manager'
        self.source_default = self.root/'source-home'
        self.destination_default = self.root/'destination-home'
        self.source_default.mkdir()
        self.destination_default.mkdir()
        self.bin = self.root/'bin'
        self.output = StringIO()
        self.errors = StringIO()
        self.source_args = ['--manager-root', str(self.source), '--default-home', str(self.source_default)]
        self.destination_args = ['--manager-root', str(self.destination), '--default-home', str(self.destination_default)]
        self.assertEqual(self.run_manager(self.source_args+['new', 'work']), 0)
        self.home = self.source/'profiles/work/home'
        (self.home/'auth.json').write_text('{"fixture":"first"}\n')
        (self.home/'config.toml').write_text('model = "fixture-model"\ncli_auth_credentials_store = "keyring"\n')

    def run_manager(self, args):
        with redirect_stdout(self.output), redirect_stderr(self.errors):
            return manager.main(args)

    def export(self, name='work', *, encrypted=False, conversations=False, filename='work.codex-profile'):
        path = self.root/filename
        args = self.source_args+['export', name, '--output', str(path)]
        if not encrypted:
            args.append('--no-passphrase')
        if conversations:
            args.append('--include-conversations')
        with patch.object(manager, 'prompt_export_passphrase', return_value='fixture-password') as prompt:
            self.assertEqual(self.run_manager(args), 0)
        self.assertEqual(prompt.call_count, int(encrypted))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return path

    def test_plaintext_transfer_runs_with_destination_home_and_updates_without_force(self):
        binary = self.root/'fixture-codex'
        binary.write_text('#!'+sys.executable+'\n'+'''
import json, os, sys
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
print(json.dumps({'home':str(home), 'args':sys.argv[1:], 'fixture':json.loads((home/'auth.json').read_text())['fixture']}))
''')
        binary.chmod(0o700)
        bundle = self.export()
        self.assertTrue(zipfile.is_zipfile(bundle))
        self.assertIn('Encryption: none', self.output.getvalue())
        with patch.object(manager, 'prompt_import_passphrase', side_effect=AssertionError('Unexpected passphrase prompt')):
            self.assertEqual(self.run_manager(self.destination_args+[
                'update', str(bundle), '--install', '--bin-dir', str(self.bin), '--codex-binary', str(binary)]), 0)
        home = self.destination/'profiles/work/home'
        wrapper = self.bin/'work'
        self.assertTrue(wrapper.is_file())
        self.assertFalse((self.bin/'codex-work').exists())
        self.assertEqual((home/'auth.json').stat().st_mode & 0o777, 0o600)
        self.assertIn('cli_auth_credentials_store = "file"', (home/'config.toml').read_text())
        (self.destination_default/'sessions/keep.jsonl').write_text('existing conversation\n')
        metadata = (home.parent/'profile.json').read_bytes()
        wrapper_bytes = wrapper.read_bytes()
        sessions = (home/'sessions').resolve()
        (self.home/'auth.json').write_text('{"fixture":"second"}\n')
        second = self.export(filename='new-login.codex-profile')
        with patch.object(manager, 'prompt_import_passphrase', side_effect=AssertionError('Unexpected passphrase prompt')):
            self.assertEqual(self.run_manager(self.destination_args+['update', str(second)]), 0)
            self.assertEqual(self.run_manager(self.destination_args+['update', str(second), '--install']), 0)
            wrapper.unlink()
            self.assertEqual(self.run_manager(self.destination_args+['update', str(second), '--install']), 0)
        self.assertEqual((home.parent/'profile.json').read_bytes(), metadata)
        self.assertEqual(wrapper.read_bytes(), wrapper_bytes)
        self.assertEqual((home/'sessions').resolve(), sessions)
        self.assertEqual((sessions/'keep.jsonl').read_text(), 'existing conversation\n')
        shutil.rmtree(self.source)
        shutil.rmtree(self.source_default)
        result = subprocess.run([str(wrapper), '--version'], capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value['home'], str(home))
        self.assertEqual(value['args'], ['--version'])
        self.assertEqual(value['fixture'], 'second')

    def test_encryption_stays_default_and_update_still_authenticates_it(self):
        bundle = self.export(encrypted=True)
        self.assertFalse(zipfile.is_zipfile(bundle))
        self.assertTrue(manager.profile_bundle.is_encrypted_file(bundle))
        self.assertEqual(self.run_manager(self.destination_args+['new', 'work', '--isolated']), 0)
        home = self.destination/'profiles/work/home'
        (home/'auth.json').write_text('{"fixture":"old"}')
        with patch.object(manager, 'prompt_import_passphrase', return_value='wrong-password'):
            self.assertEqual(self.run_manager(self.destination_args+['update', str(bundle)]), 2)
        self.assertEqual((home/'auth.json').read_text(), '{"fixture":"old"}')
        with patch.object(manager, 'prompt_import_passphrase', return_value='fixture-password'):
            self.assertEqual(self.run_manager(self.destination_args+['update', str(bundle)]), 0)
        self.assertEqual(json.loads((home/'auth.json').read_text()), {'fixture':'first'})
        self.assertEqual((home/'sessions').resolve(), self.destination/'stores/work-conversations/sessions')

    def test_plaintext_import_without_update_still_refuses_an_existing_profile(self):
        bundle = self.export()
        with patch.object(manager, 'prompt_import_passphrase', side_effect=AssertionError('Unexpected passphrase prompt')):
            self.assertEqual(self.run_manager(self.destination_args+['import', str(bundle)]), 0)
            self.assertEqual(self.run_manager(self.destination_args+['import', str(bundle)]), 2)
        self.assertIn('manager update', self.errors.getvalue())

    def test_update_rejects_unrelated_or_modified_wrappers_before_changing_login(self):
        bundle = self.export()
        for mode in ('unrelated', 'modified', 'different-name', 'different-directory'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                root = Path(temporary)
                args = ['--manager-root', str(root/'manager'), '--default-home', str(self.destination_default)]
                self.assertEqual(self.run_manager(args+['new', 'work']), 0)
                home = root/'manager/profiles/work/home'
                (home/'auth.json').write_text('old-login')
                original_config = (home/'config.toml').read_bytes() if (home/'config.toml').exists() else None
                directory = root/'bin'
                directory.mkdir()
                if mode == 'unrelated':
                    (directory/'work').write_text('unrelated command')
                else:
                    self.assertEqual(self.run_manager(args+['install', 'work', '--bin-dir', str(directory)]), 0)
                    if mode == 'modified':
                        (directory/'work').write_text('locally modified command')
                command = args+['update', str(bundle), '--install']
                if mode == 'unrelated':
                    command += ['--bin-dir', str(directory)]
                elif mode == 'different-name':
                    command += ['--wrapper', 'other']
                elif mode == 'different-directory':
                    command += ['--bin-dir', str(root/'other')]
                self.assertEqual(self.run_manager(command), 2)
                self.assertEqual((home/'auth.json').read_text(), 'old-login')
                if original_config is not None:
                    self.assertEqual((home/'config.toml').read_bytes(), original_config)

    def test_export_never_overwrites_and_refuses_invalid_plaintext_archives(self):
        bundle = self.export()
        before = bundle.read_bytes()
        self.assertEqual(self.run_manager(self.source_args+[
            'export', 'work', '--no-passphrase', '--output', str(bundle)]), 2)
        self.assertEqual(bundle.read_bytes(), before)
        for mode in ('invalid', 'traversal'):
            path = self.root/(mode+'.codex-profile')
            if mode == 'invalid':
                path.write_bytes(b'not a profile bundle')
            else:
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('manifest.json', json.dumps(dict(
                        format='codex-manager-profile', version=1, name='work',
                        includes=dict(auth=True, config=False, conversations=False))))
                    archive.writestr('profile/auth.json', '{"fixture":"invalid"}')
                    archive.writestr('../escaped', 'bad')
            with patch.object(manager, 'prompt_import_passphrase', side_effect=AssertionError('Unexpected passphrase prompt')):
                self.assertEqual(self.run_manager(self.destination_args+['update', str(path)]), 2)
            self.assertFalse((self.destination/'profiles/work').exists())
        self.assertFalse((self.root/'escaped').exists())

    def test_update_preserves_missing_bundle_config_and_custom_wrapper(self):
        (self.home/'config.toml').unlink()
        bundle = self.export()
        self.assertEqual(self.run_manager(self.destination_args+['new', 'work']), 0)
        home = self.destination/'profiles/work/home'
        (home/'config.toml').write_text('model = "local-model"\n')
        self.assertEqual(self.run_manager(self.destination_args+[
            'install', 'work', '--wrapper', 'cw', '--bin-dir', str(self.bin)]), 0)
        wrapper = self.bin/'cw'
        before = wrapper.read_bytes()
        self.assertEqual(self.run_manager(self.destination_args+['update', str(bundle), '--install']), 0)
        self.assertEqual(wrapper.read_bytes(), before)
        profile = manager.load_profile(self.destination, 'work', self.destination_default)
        self.assertEqual(profile['wrapper']['naming'], 'custom')
        self.assertIn('model = "local-model"', (home/'config.toml').read_text())

    def test_update_can_relocate_bundled_conversations_only_for_a_new_profile(self):
        (self.source_default/'sessions/source.jsonl').write_text('portable fixture\n')
        bundle = self.export(conversations=True)
        self.assertEqual(self.run_manager(self.destination_args+['update', str(bundle), '--name', 'codex-mom',
                                                                  '--install', '--bin-dir', str(self.bin)]), 0)
        home = self.destination/'profiles/codex-mom/home'
        store = (home/'sessions').resolve()
        self.assertEqual((store/'source.jsonl').read_text(), 'portable fixture\n')
        self.assertTrue(self.bin.joinpath('codex-mom').is_file())
        self.assertFalse(self.bin.joinpath('codex-codex-mom').exists())
        self.assertEqual(self.run_manager(self.destination_args+['update', str(bundle), '--name', 'codex-mom']), 2)
        self.assertEqual((store/'source.jsonl').read_text(), 'portable fixture\n')
