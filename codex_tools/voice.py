"""Optional durable voice service and whisper.cpp setup; independent of agents."""
from __future__ import annotations

import argparse
from contextlib import closing
import errno
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from codex_tools import paths
from codex_tools.voice_jobs import VoiceService, WhisperServer, make_handler, validate_backend
from http.server import ThreadingHTTPServer

MODEL_NAMES = (
    'tiny', 'tiny.en', 'tiny-q5_1', 'tiny.en-q5_1', 'tiny-q8_0',
    'base', 'base.en', 'base-q5_1', 'base.en-q5_1', 'base-q8_0',
    'small', 'small.en', 'small-q5_1', 'small.en-q5_1', 'small-q8_0',
    'medium', 'medium.en', 'medium-q5_0', 'medium.en-q5_0', 'medium-q8_0',
    'large-v1', 'large-v2', 'large-v2-q5_0', 'large-v2-q8_0',
    'large-v3', 'large-v3-q5_0', 'large-v3-turbo', 'large-v3-turbo-q5_0',
    'large-v3-turbo-q8_0',
)


MODEL_ALIASES = {'large': 'large-v3', 'turbo': 'large-v3-turbo'}
MODEL_CHOICES = (*MODEL_NAMES, *MODEL_ALIASES)


def canonical_model(name):
    return MODEL_ALIASES.get(name, name)


def default_state_dir():
    preferred = paths.user_data_dir() / 'voice'
    legacy = Path.home() / '.local/share/voice-jobs'
    # Preserve identity, accepted jobs and results from the mobile prototype.
    return legacy if not (preferred / 'identity.json').exists() and (legacy / 'identity.json').is_file() else preferred


def load_identity(root):
    value = json.loads((root / 'identity.json').read_text())
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in ('id', 'token')):
        raise ValueError('Invalid voice identity. Restore the original identity.json; do not rotate a key with pending jobs.')
    return value


def load_settings(root):
    config = root / 'config.json'
    value = json.loads(config.read_text()) if config.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Invalid voice configuration.')
    for name in ('whisper_bin', 'model', 'whisper_url'):
        if name in value and not isinstance(value[name], str):
            raise ValueError(f'Invalid voice configuration: {name} must be text.')
    for name, maximum in (('port', 65535), ('threads', 1024)):
        if name in value and (type(value[name]) is not int or not 1 <= value[name] <= maximum):
            raise ValueError(f'Invalid voice configuration: {name} is out of range.')
    for name in ('cpu', 'manage_engine'):
        if name in value and type(value[name]) is not bool:
            raise ValueError(f'Invalid voice configuration: {name} must be true or false.')
    validate_backend(value.get('whisper_url', 'http://127.0.0.1:8080'))
    return value


def write_settings(root, value):
    paths.ensure_private_dir(root)
    fd, name = tempfile.mkstemp(prefix='.config-', dir=root)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, root / 'config.json')
    finally:
        Path(name).unlink(missing_ok=True)


def iris_settings():
    config_home = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser()
    try:
        value = json.loads((config_home / 'iris/config.json').read_text()).get('whisper_cpp', {})
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def executable(path):
    if not path:
        return None
    found = shutil.which(str(Path(path).expanduser()))
    return str(Path(found).absolute()) if found else None


def find_server(configured=None):
    if configured:
        return executable(configured)  # An explicit bad path must not silently fall back.
    found = executable('whisper-server')
    if found:
        return found
    iris_binary = iris_settings().get('binary_path')
    candidates = ([Path(iris_binary).expanduser().with_name('whisper-server')] if isinstance(iris_binary, str) and iris_binary else [])
    candidates += [Path.home() / p for p in (
        '.local/bin/whisper-server', '.local/src/whisper.cpp/build/bin/whisper-server',
        'whisper.cpp/build/bin/whisper-server', 'src/whisper.cpp/build/bin/whisper-server',
    )]
    candidates += [Path('/opt/homebrew/bin/whisper-server'), Path('/usr/local/bin/whisper-server')]
    return next((found for p in candidates if (found := executable(p))), None)


def valid_model(path):
    try:
        with Path(path).open('rb') as handle:
            return handle.read(4) == b'lmgg' and Path(path).stat().st_size > 64
    except OSError:
        return False


def installed_models(root, server=None):
    candidates = []
    iris_model = iris_settings().get('model_path')
    if isinstance(iris_model, str) and iris_model:
        candidates.append(Path(iris_model).expanduser())
    directories = [root / 'models', Path.home() / '.cache/whisper.cpp']
    if server:
        # A normal source build places the executable at build/bin/whisper-server.
        directories.append(Path(server).parent.parent.parent / 'models')
    for directory in directories:
        if directory.is_dir():
            candidates.extend(sorted(directory.glob('ggml-*.bin')))
    return list(dict.fromkeys(str(p.absolute()) for p in candidates if valid_model(p)))


def resolve_model(value, root, server=None):
    value = canonical_model(value)
    if not value:
        return None
    expanded = Path(value).expanduser()
    if expanded.is_file():
        return str(expanded.absolute()) if valid_model(expanded) else None
    filename = f'ggml-{value}.bin'
    return next((p for p in installed_models(root, server) if Path(p).name == filename), None)


def download_model(name, root, *, force=False, progress=None):
    name = canonical_model(name)
    if name not in MODEL_NAMES:
        raise ValueError('Unknown model name. Use ct voice models list.')
    directory = root / 'models'
    paths.ensure_private_dir(directory)
    target = directory / f'ggml-{name}.bin'
    if target.exists() and not force:
        if not valid_model(target):
            raise ValueError('Existing model is incomplete or invalid; use --force to replace it.')
        return target
    fd, temporary = tempfile.mkstemp(prefix=f'.{name}-', suffix='.part', dir=directory)
    try:
        request = Request(f'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin',
                          headers={'User-Agent': 'codex-tools-voice'})
        with os.fdopen(fd, 'wb') as handle, build_opener().open(request, timeout=30) as response:
            expected = response.headers.get('Content-Length')
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 4 * 1024**3:
                    raise ValueError('Model exceeds the 4 GiB download limit.')
                handle.write(chunk)
                if progress is not None:
                    progress(size, int(expected) if expected is not None else None)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not None and size != int(expected):
            raise ValueError('Model download was incomplete; existing model was preserved.')
        if not valid_model(temporary):
            raise ValueError('Downloaded file is not a complete GGML model.')
        os.replace(temporary, target)
        return target
    finally:
        Path(temporary).unlink(missing_ok=True)



def initialize_identity(root):
    paths.ensure_private_dir(root)
    with os.fdopen(os.open(root / 'identity.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
        json.dump({'id': str(uuid.uuid4()), 'token': secrets.token_urlsafe(32)}, handle)
        handle.flush()
        os.fsync(handle.fileno())


def install_model(name, root, settings, *, force=False):
    name = canonical_model(name)
    existing = resolve_model(name, root, find_server(settings.get('whisper_bin')))
    if existing and not force:
        print(f'Using installed {name}: {existing}')
        return Path(existing)
    print(f'Downloading {name} from the whisper.cpp model repository...', flush=True)
    last_update = 0.0
    def progress(received, total):
        nonlocal last_update
        now = time.monotonic()
        if now - last_update >= .5 or received == total:
            amount = f'{received / 1024**2:.1f} MiB'
            if total:
                amount += f' / {total / 1024**2:.1f} MiB ({received / total:.0%})'
            print(f'  {amount}', file=sys.stderr, flush=True)
            last_update = now
    model = download_model(name, root, force=force, progress=progress)
    print(f'Installed {name}: {model}')
    return model


class ConfigurationCancelled(Exception):
    pass


def prompt_value(label, default=None):
    suffix = f' [{default}]' if default is not None else ''
    answer = input(f'{label}{suffix}: ').strip()
    if answer.lower() in ('q', 'quit'):
        raise ConfigurationCancelled()
    return answer or (str(default) if default is not None else '')


def prompt_choice(label, choices, default):
    while True:
        answer = prompt_value(label, default).lower()
        if answer in choices:
            return answer
        print('Choose ' + ', '.join(choices) + ', or q to cancel.')


def configure_interactively(root, settings):
    print('Voice setup (q cancels). Settings are saved at the end.')
    print('Voice has its own identity and connection; agent setup is independent.')
    if (root / 'identity.json').exists():
        load_identity(root)
    print('  1. Run a local whisper-server executable with the voice service')
    print('  2. Connect to an already running localhost whisper-server')
    default = '1' if settings.get('manage_engine', True) else '2'
    managed = prompt_choice('Engine', ('1', '2'), default) == '1'
    value = {**settings, 'manage_engine': managed}
    if managed:
        discovered = find_server(settings.get('whisper_bin'))
        print(f'whisper-server found: {discovered or "not found"}')
        while True:
            selected = prompt_value('whisper-server executable', discovered)
            server = find_server(selected) if selected else None
            if server:
                value['whisper_bin'] = server
                break
            print('Executable not found. Install whisper.cpp, then enter its whisper-server path.')
        available = installed_models(root, server)
        if available:
            print('Installed models:')
            for index, model in enumerate(available, 1):
                print(f'  {index}. {Path(model).name} ({Path(model).stat().st_size / 1024**2:.1f} MiB) - {model}')
        print('Enter a model number, file path, or download name (base, small, medium, large, turbo).')
        print('large means large-v3; .en models are English-only. Downloads require an explicit yes.')
        current = resolve_model(settings.get('model'), root, server)
        while True:
            selected = prompt_value('Model', current)
            if selected.isdigit() and 1 <= int(selected) <= len(available):
                selected = available[int(selected) - 1]
            model = resolve_model(selected, root, server)
            if model:
                value['model'] = model
                break
            name = canonical_model(selected)
            if name in MODEL_NAMES:
                if prompt_choice(f'Download {name} now? (yes/no)', ('yes', 'no', 'y', 'n'), 'no') in ('yes', 'y'):
                    value['model'] = str(install_model(name, root, value))
                    break
            else:
                print('Choose an installed model or a name from ct voice models list.')
        device = prompt_choice('Device (auto/cpu)', ('auto', 'cpu'), 'cpu' if settings.get('cpu') else 'auto')
        value['cpu'] = device == 'cpu'
        while True:
            threads = prompt_value('CPU threads', settings.get('threads', 4))
            if threads.isdigit() and 1 <= int(threads) <= 1024:
                value['threads'] = int(threads)
                break
            print('Enter a thread count from 1 to 1024.')
    while True:
        backend = prompt_value('Whisper engine URL', settings.get('whisper_url', 'http://127.0.0.1:8080'))
        try:
            value['whisper_url'] = validate_backend(backend)
            break
        except ValueError as exc:
            print(exc)
    while True:
        port = prompt_value('Voice job service port', settings.get('port', 8790))
        if port.isdigit() and 1 <= int(port) <= 65535 and int(port) != urlparse(backend).port:
            value['port'] = int(port)
            break
        print('Choose a port from 1 to 65535, different from the engine port.')
    print(f'\nVoice jobs: http://127.0.0.1:{value["port"]}')
    print(f'Engine: {value["whisper_url"]} ({"managed" if managed else "external"})')
    if managed:
        print(f'Model: {value["model"]}')
    if prompt_choice('Save this setup? (yes/no)', ('yes', 'no', 'y', 'n'), 'yes') not in ('yes', 'y'):
        raise ConfigurationCancelled()
    if not (root / 'identity.json').exists():
        initialize_identity(root)
    write_settings(root, value)
    print(f'Saved: {root / "config.json"}')
    print('Next: ct voice serve (runs in this terminal); ct voice status checks it.')
    print('For mobile pairing: ct voice token. Nothing has been started automatically.')
    return 0


def fetch_status(url, token=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    request = Request(url, headers=headers)
    with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
        return json.loads(response.read(65536))


def backend_ready(url):
    try:
        return fetch_status(validate_backend(url) + '/health').get('status') == 'ok'
    except (OSError, ValueError):
        return False


def engine_command(settings, root):
    server = find_server(settings.get('whisper_bin'))
    if not server:
        raise ValueError('whisper-server not found. Install whisper.cpp or set ct voice configure --whisper-bin /absolute/path/whisper-server.')
    model = resolve_model(settings.get('model'), root, server)
    if not model:
        raise ValueError('Whisper model not found/invalid. Download one with ct voice models download small, then ct voice configure --model small.')
    port = urlparse(validate_backend(settings.get('whisper_url', 'http://127.0.0.1:8080'))).port
    command = [server, '--host', '127.0.0.1', '--port', str(port), '--model', model,
               '--language', 'auto', '--threads', str(settings.get('threads', 4))]
    if settings.get('cpu', False):
        command.append('--no-gpu')
    return command


def acceleration_from_log(text):
    """Describe actual engine initialization, never equate 'auto' with GPU use."""
    match = re.search(r'whisper_backend_init_gpu: using (\S+) backend', text)
    if match:
        backend = match[1]
        device = backend
        if backend.startswith('Vulkan'):
            index = backend.removeprefix('Vulkan')
            found = re.search(r'ggml_vulkan: ' + re.escape(index) + r' = (.+?)(?: \||$)', text, re.MULTILINE)
            if found:
                device = found[1].strip()
        return {'backend': backend, 'device': device, 'verified': True}
    if re.search(r'whisper_model_load:\s+CPU total size', text):
        return {'backend': 'CPU', 'device': 'CPU', 'verified': True}
    return {'backend': None, 'device': 'Starting / not yet verified', 'verified': False}


class ManagedEngine:
    """Own only our child; restart it on failure, independently of mobile SSH."""
    def __init__(self, command, root):
        self.command = command
        self.root = root
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None
        self.log = None
        self.log_offset = 0

    def start(self):
        port = int(self.command[self.command.index('--port') + 1])
        with socket.socket() as probe:
            # Never take ownership of an existing server, even if it is healthy.
            # A bind probe misidentifies TIME_WAIT sockets from an engine that
            # just stopped as a live listener. Check an actual TCP connection.
            probe.settimeout(2)
            result = probe.connect_ex(('127.0.0.1', port))
            if result != errno.ECONNREFUSED:
                raise OSError(f'Whisper port {port} is occupied or unavailable; leave the existing process alone and use serve without --start-engine.')
        self.log = (self.root / 'whisper.log').open('ab')
        (self.root / 'whisper.log').chmod(0o600)
        try:
            self._spawn()
        except Exception:
            self.log.close()
            raise
        self.thread = threading.Thread(target=self._monitor, name='voice-engine', daemon=True)
        self.thread.start()

    def _spawn(self):
        self.log_offset = self.log.seek(0, os.SEEK_END)
        self.process = subprocess.Popen(self.command, cwd=self.root, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT, close_fds=True)

    def acceleration(self):
        if self.process is None or self.process.poll() is not None:
            return {'backend': None, 'device': 'Engine not running', 'verified': False}
        try:
            with (self.root / 'whisper.log').open('rb') as handle:
                handle.seek(self.log_offset)
                return acceleration_from_log(handle.read(512 * 1024).decode('utf-8', errors='replace'))
        except OSError:
            return {'backend': None, 'device': 'Not yet verified', 'verified': False}

    def _monitor(self):
        while not self.stop_event.wait(.25):
            if self.process.poll() is not None:
                if self.stop_event.wait(3):
                    return
                try:
                    self._spawn()
                except OSError:
                    # Keep accepted recordings queued while an installation is repaired.
                    continue

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()


def serve(root, settings, *, start_engine=False):
    identity = load_identity(root)
    port = settings.get('port', 8790)
    backend = validate_backend(settings.get('whisper_url', 'http://127.0.0.1:8080'))
    if port == urlparse(backend).port:
        raise ValueError('Voice job port and whisper-server port must differ.')
    # Hold the worker lock before starting a managed engine. A second instance
    # cannot start a second recognizer against the same durable queue.
    engine = None
    def engine_info():
        details = engine.acceleration() if engine else {'device': 'External engine', 'verified': False}
        model = settings.get('model')
        details['model'] = Path(model).stem.removeprefix('ggml-') if model and start_engine else 'Configured on external server'
        return details
    service = VoiceService(root, transcribe=WhisperServer(backend), host_id=identity['id'], engine_info=engine_info)
    handlers = {}
    try:
        with ThreadingHTTPServer(('127.0.0.1', port), make_handler(service, identity['token'])) as server:
            if start_engine:
                engine = ManagedEngine(engine_command(settings, root), root)
                engine.start()
            def stop_server(_signum, _frame):
                # shutdown() must run outside serve_forever's thread.
                threading.Thread(target=server.shutdown, daemon=True).start()
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, stop_server)
            print(f'Voice jobs: http://127.0.0.1:{port}; Whisper: {backend}', flush=True)
            server.serve_forever()
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        # Stop a managed recognizer first to unblock inference on shutdown;
        # unfinished work remains queued for the next service start.
        if engine:
            engine.close()
        service.close()
    return 0


def parser():
    result = argparse.ArgumentParser(prog='codex-tools voice', description=__doc__)
    result.add_argument('--state-dir', type=Path, default=None,
                        help='Private identity/config/job storage (default: Codex Tools voice data).')
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create the independent voice identity and pairing key, once.')
    commands.add_parser('token', help='Print the voice pairing key for the mobile app.')
    configure = commands.add_parser('configure', help='Locate whisper-server and save optional engine settings.')
    configure.add_argument('--interactive', action='store_true', help='Open the guided setup even when standard input is redirected.')
    mode = configure.add_mutually_exclusive_group()
    mode.add_argument('--manage-engine', action='store_true', dest='manage_engine', default=None, help='Have ct voice serve supervise the configured executable.')
    mode.add_argument('--external-engine', action='store_false', dest='manage_engine', help='Use a separately running recognizer.')
    configure.add_argument('--whisper-bin', help='whisper-server executable; otherwise discover PATH, Iris, and common installs.')
    configure.add_argument('--model', help='Existing GGML path or name of a downloaded/installed model.')
    configure.add_argument('--whisper-url', help='Existing/managed engine URL, http://127.0.0.1:PORT.')
    configure.add_argument('--port', type=int, help='Localhost voice job port (default 8790).')
    configure.add_argument('--threads', type=int)
    cpu = configure.add_mutually_exclusive_group()
    cpu.add_argument('--cpu', action='store_true', default=None, help='Force a managed whisper-server to CPU.')
    cpu.add_argument('--gpu', action='store_false', dest='cpu', help='Use acceleration supported by the installed binary.')
    for name in ('doctor', 'status'):
        command = commands.add_parser(name, help='Check local voice setup.' if name == 'doctor' else 'Check the running voice service and queue.')
        command.add_argument('--json', action='store_true')
    running = commands.add_parser('serve', help='Serve durable jobs on localhost; run independently of the agent relay.')
    serving_mode = running.add_mutually_exclusive_group()
    serving_mode.add_argument('--start-engine', action='store_true', default=None, help='Start/supervise whisper-server (overrides saved setup).')
    serving_mode.add_argument('--external-engine', action='store_false', dest='start_engine', help='Use an external recognizer (overrides saved setup).')
    running.add_argument('--port', type=int)
    running.add_argument('--whisper-url')
    models = commands.add_parser('models', help='Find existing models or explicitly download one.')
    model_commands = models.add_subparsers(dest='model_command', required=True)
    listing = model_commands.add_parser('list')
    listing.add_argument('--json', action='store_true')
    download = model_commands.add_parser('download')
    download.add_argument('name', choices=MODEL_CHOICES)
    download.add_argument('--force', action='store_true', help='Replace an existing model only after a complete validated download.')
    timing = commands.add_parser('benchmark', help='Measure CPU/GPU transcription with a temporary model process.')
    timing.add_argument('audio', type=Path, help='Mono 16 kHz 16-bit WAV, up to three minutes.')
    timing.add_argument('--model', help='Installed model name/path; defaults to the configured model.')
    timing.add_argument('--device', choices=('auto', 'cpu', 'gpu', 'both'), default='auto')
    timing.add_argument('--runs', type=int, default=3)
    timing.add_argument('--warmup', type=int, default=1)
    timing.add_argument('--language', default='', help='For example en; empty means automatic detection.')
    timing.add_argument('--json', action='store_true')
    install = commands.add_parser('install', help='Download a Whisper model; large=large-v3, turbo=large-v3-turbo.')
    install.add_argument('name', choices=MODEL_CHOICES)
    install.add_argument('--use', action='store_true', help='Also select this model for the next service start.')
    install.add_argument('--force', action='store_true', help='Download a fresh managed copy even if one is installed.')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    root = (args.state_dir or default_state_dir()).expanduser().absolute()
    try:
        if args.command == 'init':
            initialize_identity(root)
            print(f'Voice identity created in {root}. Use ct voice token for its pairing key.')
            return 0
        settings = load_settings(root)
        if args.command == 'benchmark':
            from codex_tools.voice_benchmark import benchmark
            result = benchmark(args.audio, root, settings, model=args.model, device=args.device,
                               runs=args.runs, warmup=args.warmup, language=args.language,
                               progress=lambda message: print(message, file=sys.stderr, flush=True))
            if args.json:
                print(json.dumps(result))
            else:
                print(f'Audio: {result["audio_seconds"]:.2f}s; loading measured separately')
                for case in result['cases']:
                    print(f'{case["model"]} | {case["backend"]} | {case["device"]} | {case["mean_seconds"]:.2f}s processing | {case["speed_vs_realtime"]:.1f}x real time | {case["startup_seconds"]:.2f}s load')
                print(result['note'])
            return 0
        if args.command == 'install':
            model = install_model(args.name, root, settings, force=args.force)
            if args.use:
                write_settings(root, {**settings, 'model': str(model)})
                print('Model selected. Restart an already running voice service to apply it.')
            else:
                print(f'To select it: ct voice configure --model {model}')
            print('For guided setup: ct voice configure')
            return 0
        if args.command == 'models':
            if args.model_command == 'download':
                install_model(args.name, root, settings, force=args.force)
            else:
                value = {'installed': installed_models(root, find_server(settings.get('whisper_bin'))), 'downloadable': MODEL_NAMES, 'aliases': MODEL_ALIASES}
                if args.json:
                    print(json.dumps(value))
                else:
                    print('Installed models:\n' + ('\n'.join(value['installed']) or '  None found.'))
                    print('Downloadable names: ' + ', '.join(MODEL_NAMES))
                    print('Aliases: large=large-v3, turbo=large-v3-turbo. Use ct voice install NAME.')
            return 0
        if args.command == 'configure':
            supplied = any(getattr(args, key) is not None for key in ('whisper_bin', 'model', 'whisper_url', 'port', 'threads', 'cpu', 'manage_engine'))
            if args.interactive or (not supplied and sys.stdin.isatty()):
                return configure_interactively(root, settings)
            if not supplied:
                raise ValueError('Guided setup needs a terminal. Use configure --interactive, or provide flags such as --model small.')
            value = {**settings}
            for name in ('model', 'whisper_url', 'port', 'threads', 'cpu', 'manage_engine'):
                if (selected := getattr(args, name)) is not None:
                    value[name] = selected
            server = find_server(args.whisper_bin or value.get('whisper_bin'))
            if args.whisper_bin and not server:
                raise ValueError('Configured whisper-server is not executable.')
            if server:
                value['whisper_bin'] = server
            if args.model:
                model = resolve_model(args.model, root, server)
                if not model:
                    raise ValueError('Model not found/invalid. Use ct voice models list or models download first.')
                value['model'] = model
            validate_backend(value.get('whisper_url', 'http://127.0.0.1:8080'))
            if not 1 <= value.get('port', 8790) <= 65535 or not 1 <= value.get('threads', 4) <= 1024:
                raise ValueError('Invalid service port or CPU thread count.')
            if value.get('port', 8790) == urlparse(value.get('whisper_url', 'http://127.0.0.1:8080')).port:
                raise ValueError('Voice job port and whisper-server port must differ.')
            write_settings(root, value)
            print(f'Voice configuration saved: {root / "config.json"}')
            print(f'whisper-server: {server or "not found; install whisper.cpp or configure --whisper-bin"}')
            print(f'Model: {value.get("model") or "not configured; see ct voice models list"}')
            return 0
        if args.command == 'token':
            print(load_identity(root)['token'])
            return 0
        if args.command == 'doctor':
            server = find_server(settings.get('whisper_bin'))
            model = resolve_model(settings.get('model'), root, server)
            backend = settings.get('whisper_url', 'http://127.0.0.1:8080')
            ready = backend_ready(backend)
            identity_ok = (root / 'identity.json').is_file()
            if identity_ok:
                load_identity(root)
            value = {'state_dir': str(root), 'identity_initialized': identity_ok,
                     'whisper_bin': server, 'model': model,
                     'installed_models': installed_models(root, server),
                     'whisper_url': backend, 'engine_ready': ready,
                     'managed_engine_configured': bool(server and model),
                     'ready': identity_ok and (ready or bool(server and model))}
            if args.json:
                print(json.dumps(value))
            else:
                for key, item in value.items():
                    print(f'{key}: {item}')
                if not server:
                    print('Install whisper.cpp (https://github.com/ggml-org/whisper.cpp), then configure --whisper-bin /absolute/path/whisper-server.')
            return 0 if value['ready'] else 1
        if args.command == 'status':
            identity = load_identity(root)
            url = f'http://127.0.0.1:{settings.get("port", 8790)}'
            result = fetch_status(url + '/v1/voice', identity['token'])
            if result.get('service') != 'voice-jobs' or result.get('host_id') != identity['id']:
                raise ValueError('The configured port is serving a different voice service.')
            result['url'] = url
            result['engine_ready'] = backend_ready(settings.get('whisper_url', 'http://127.0.0.1:8080'))
            result['queue'] = {}
            if (root / 'jobs.sqlite3').exists():
                with closing(sqlite3.connect((root / 'jobs.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                    result['queue'] = dict(db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status'))
                    columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
                    if {'transcription_seconds', 'queue_seconds'} <= columns:
                        count, processing, queued = db.execute("SELECT COUNT(*),AVG(transcription_seconds),AVG(queue_seconds) FROM (SELECT transcription_seconds,queue_seconds FROM jobs WHERE status='completed' AND transcription_seconds IS NOT NULL ORDER BY finished DESC LIMIT 20)").fetchone()
                        if count:
                            result['recent_timing'] = {'jobs': count, 'transcription_seconds': processing, 'queue_seconds': queued}
            if args.json:
                print(json.dumps(result))
            else:
                print(f'Voice running: {url}\nEngine ready: {result["engine_ready"]}\nModel: {result.get("model")}\nDevice: {result.get("device")} (verified: {result.get("device_verified", False)})\nJobs: {result["queue"]}')
                timing = result.get('recent_timing')
                if timing:
                    print(f'Recent {timing["jobs"]} jobs: {timing["transcription_seconds"]:.2f}s processing, {timing["queue_seconds"]:.2f}s queued (averages)')
            return 0
        if args.command == 'serve':
            for name in ('port', 'whisper_url'):
                if (selected := getattr(args, name)) is not None:
                    settings[name] = selected
            if not 1 <= settings.get('port', 8790) <= 65535:
                raise ValueError('Invalid voice service port.')
            managed = args.start_engine if args.start_engine is not None else settings.get('manage_engine', False)
            return serve(root, settings, start_engine=managed)
    except (ConfigurationCancelled, EOFError, KeyboardInterrupt):
        print('Voice setup/download canceled. Existing configuration was not changed.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
