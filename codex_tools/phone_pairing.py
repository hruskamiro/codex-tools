"""One-use, certificate-pinned invitations for native Agent Console clients."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import select
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from codex_tools import paths, relay_transport

MAX_BODY = 4096


def run(arguments, *, input=None):
    result = subprocess.run(arguments, input=input, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise ValueError("SSH operation failed; check existing SSH access and known host keys.")
    return result.stdout.strip()


def safe_target(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_@.\[\]:+-]{1,255}", value) or value.startswith('-'):
        raise ValueError("Use an SSH alias or user@host, without options, whitespace or shell commands.")
    return value


def target_arguments(target):
    safe_target(target)
    match = re.fullmatch(r'(.+\]|[^:]+):(\d+)', target)
    if match:
        if not 1 <= int(match[2]) <= 65535:
            raise ValueError('Invalid SSH port.')
        return ['-p', match[2], match[1]]
    return [target]


def ssh_options(target):
    output = run(['ssh', '-G', *target_arguments(target)])
    return dict(line.split(None, 1) for line in output.splitlines() if ' ' in line)


def ssh_route(target, seen=()):
    if target in seen or len(seen) >= 8:
        raise ValueError('Cyclic or excessive SSH jump route (maximum eight hosts).')
    settings = ssh_options(target)
    if settings.get('proxycommand', 'none') != 'none':
        raise ValueError('ProxyCommand cannot be exported; use named ProxyJump hosts instead.')
    result = []
    jumps = settings.get('proxyjump', 'none')
    if jumps != 'none':
        for jump in jumps.split(','):
            result.extend(ssh_route(jump, (*seen, target)))
    result.append((target, settings))
    if len(result) > 8 or len({item[0] for item in result}) != len(result):
        raise ValueError('Duplicate or excessive SSH jump hosts.')
    return result


def ssh_command(route, index, command):
    target, _ = route[index]
    arguments = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                 '-o', 'ConnectTimeout=10', '-o', 'ControlMaster=no', '-o', 'ControlPath=none']
    if index:
        arguments.extend(['-J', ','.join(item[0] for item in route[:index])])
    arguments.extend([*target_arguments(target), command])
    return arguments


def public_key(value):
    if not isinstance(value, str) or len(value) > 512 or '\n' in value or '\r' in value:
        raise ValueError('Expected a single Ed25519 public key.')
    fields = value.split()
    if len(fields) not in (2, 3) or fields[0] != 'ssh-ed25519':
        raise ValueError('Expected an Ed25519 public key, without authorized_keys options.')
    try:
        blob = base64.b64decode(fields[1], validate=True)
        prefix = b'\0\0\0\x0bssh-ed25519\0\0\0\x20'
        if len(blob) != len(prefix) + 32 or not blob.startswith(prefix):
            raise ValueError()
        key = serialization.load_ssh_public_key(f'ssh-ed25519 {fields[1]}'.encode())
    except Exception as exc:
        raise ValueError('Invalid Ed25519 public key.') from exc
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    return f'ssh-ed25519 {fields[1]}', fingerprint, key


def prepare(name, *, ssh=None, remote_port=None, remote_bin=None, service='relay', remote_state_dir=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('Use a connection name of up to 64 letters, digits, hyphens or underscores.')
    launcher = paths.user_config_dir() / 'ssh-launcher.json'
    settings = {}
    if launcher.exists():
        saved = json.loads(launcher.read_text())
        settings = saved.get('connections', {}).get(name, {})
    target = safe_target(ssh or settings.get('ssh', name))
    port = remote_port or (settings.get('remote_port', 8780) if service == 'relay' else 8790)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('Invalid service port.')
    executable = remote_bin or settings.get('remote_bin', 'ct')
    if not isinstance(executable, str) or not re.fullmatch(r'[A-Za-z0-9_./+-]{1,1024}', executable) or executable.startswith('-'):
        raise ValueError('Use a remote command name or executable path without shell commands.')
    route = ssh_route(target)
    hosts = []
    for index, (alias, host) in enumerate(route):
        # Read the server's Ed25519 host key through an already trusted SSH
        # connection. Never trust ssh-keyscan's unauthenticated output.
        key = run(ssh_command(route, index, 'cat /etc/ssh/ssh_host_ed25519_key.pub'))
        _, fingerprint, _ = public_key(' '.join(key.split()[:2]))
        host_id = 'ssh-' + hashlib.sha256(f"{host['user']}@{host['hostname']}:{host['port']}".encode()).hexdigest()[:20]
        hosts.append(dict(id=host_id, name=alias[:80], host=host['hostname'], username=host['user'],
                          port=int(host['port']), fingerprint=fingerprint))
    if len({host['id'] for host in hosts}) != len(hosts):
        raise ValueError('SSH aliases resolve to duplicate hosts.')
    profile = dict(id='paired-' + secrets.token_hex(12), name=name, transport='ssh', service=service,
                   url=f'http://127.0.0.1:{port}', allowHttp=False, sshHostId=hosts[-1]['id'],
                   jumpHostIds=[host['id'] for host in hosts[:-1]], autoStartRelay=settings.get('auto_start', False) if service == 'relay' else False, remoteBin=executable)
    # Obtain only the selected service's token; never enumerate credentials.
    prefix = 'PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; export PATH; exec '
    token_command = [executable, service]
    if remote_state_dir is not None:
        if service != 'voice' or not Path(remote_state_dir).is_absolute():
            raise ValueError('Voice state directory must be an absolute remote path.')
        token_command += ['--state-dir', remote_state_dir]
    token_command.append('token')
    token = run(ssh_command(route, len(route)-1, prefix + shlex.join(token_command)))
    if not token or len(token) > 1024 or any(ord(c) < 33 or ord(c) == 127 for c in token):
        raise ValueError(f'Remote {service} returned an invalid token; initialize its identity first.')
    return dict(version=1, profile=profile, hosts=hosts), token, route


def prepare_local_voice(name, address, *, port=None, ssh_port=22, state_dir=None,
                        host_key_path=Path('/etc/ssh/ssh_host_ed25519_key.pub')):
    from codex_tools import voice
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('Use a connection name of up to 64 letters, digits, hyphens or underscores.')
    root = Path(state_dir).expanduser().absolute() if state_dir else voice.default_state_dir()
    settings = voice.load_settings(root)
    job_port = port or settings.get('port', 8790)
    if not 1 <= job_port <= 65535 or not 1 <= ssh_port <= 65535:
        raise ValueError('Invalid voice or SSH port.')
    key = host_key_path.read_text()
    _, fingerprint, _ = public_key(' '.join(key.split()[:2]))
    host = dict(id='ssh-' + hashlib.sha256(f'{getpass.getuser()}@{address}:{ssh_port}'.encode()).hexdigest()[:20],
                name=name, host=address, username=getpass.getuser(), port=ssh_port, fingerprint=fingerprint)
    profile = dict(id='paired-' + secrets.token_hex(12), name=name, transport='ssh', service='voice',
                   url=f'http://127.0.0.1:{job_port}', allowHttp=False, sshHostId=host['id'], jumpHostIds=[],
                   autoStartRelay=False, remoteBin='ct')
    return dict(version=1, profile=profile, hosts=[host]), voice.load_identity(root)['token']


# The script receives the validated public key on stdin, never interpolates it
# into shell text, preserves the file and avoids duplicates by key material.
INSTALL_SCRIPT = """import os,sys
from pathlib import Path
key=sys.stdin.readline().strip()
parts=key.split()
if len(parts)!=3 or parts[0]!='ssh-ed25519': raise SystemExit(2)
directory=Path.home()/'.ssh'
directory.mkdir(mode=0o700,exist_ok=True)
if directory.is_symlink(): raise SystemExit('Refusing symlinked .ssh')
target=directory/'authorized_keys'
flags=os.O_RDWR|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)
fd=os.open(target,flags,0o600)
with os.fdopen(fd,'r+') as file:
 import fcntl
 fcntl.flock(file,fcntl.LOCK_EX)
 text=file.read()
 if not any(parts[1] in line.split() for line in text.splitlines()):
  file.write(('' if not text or text.endswith('\\n') else '\\n')+key+'\\n')
 file.flush()
 os.fsync(file.fileno())
 os.fchmod(file.fileno(),0o600)
"""


def install_key(route, index, key):
    normalized, _, _ = public_key(key)
    command = 'python3 -c ' + shlex.quote(INSTALL_SCRIPT)
    run(ssh_command(route, index, command), input=normalized + ' codex-tools-phone\n')


def install_local_key(key):
    normalized, _, _ = public_key(key)
    run([sys.executable, '-c', INSTALL_SCRIPT], input=normalized + ' codex-tools-phone\n')


class PairingState:
    def __init__(self, bundle, token, *, ttl=300):
        self.bundle, self.relay_token = bundle, token
        self.secret = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + ttl
        self.expires = int(time.time()) + ttl
        self.lock = threading.RLock()
        self.received = threading.Event()
        self.acknowledged = threading.Event()
        self.status = 'waiting'
        self.key = self.fingerprint = ''
        self.installed = []

    def submit(self, key, proof):
        normalized, fingerprint, verifier = public_key(key)
        try:
            verifier.verify(base64.b64decode(proof, validate=True), ('ct-pair-v1:' + self.secret).encode())
        except Exception as exc:
            raise ValueError('Invalid proof of key ownership.') from exc
        with self.lock:
            if self.key and self.key != normalized:
                raise ValueError('This invitation already belongs to another key.')
            if not self.key:
                self.key, self.fingerprint = normalized, fingerprint
                self.status = 'submitted'
                self.received.set()


def handler_for(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No request paths, invitation secrets, keys or tokens in logs.

        def respond(self, status, value):
            body = json.dumps(value, separators=(',', ':')).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            self.connection.settimeout(5)
            raw = b''
            if self.command == 'POST':
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                        return self.respond(413, {'error': 'Invalid request size.'})
                    raw = self.rfile.read(size)
                except (ValueError, TimeoutError):
                    return self.respond(400, {'error': 'Invalid request body.'})
            if time.monotonic() >= state.deadline:
                return self.respond(410, {'error': 'Invitation expired.'})
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + state.secret):
                return self.respond(401, {'error': 'Invalid invitation.'})
            with state.lock:
                if self.command == 'GET' and self.path == '/v1/pair':
                    payload = dict(state.bundle, status=state.status, fingerprint=state.fingerprint,
                                   installed=list(state.installed))
                    if state.status == 'complete':
                        payload['token'] = state.relay_token
                    return self.respond(200, payload)
                if self.command == 'POST' and self.path in ('/v1/key', '/v1/ack'):
                    try:
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise ValueError()
                        if self.path == '/v1/key':
                            state.submit(payload['publicKey'], payload['proof'])
                            return self.respond(200, {'status': state.status})
                        if state.status != 'complete' or payload.get('fingerprint') != state.fingerprint:
                            raise ValueError()
                        state.acknowledged.set()
                        return self.respond(200, {'status': 'saved'})
                    except (ValueError, KeyError, TypeError, TimeoutError):
                        return self.respond(400, {'error': 'Invalid pairing request.'})
                return self.respond(404, {'error': 'Unknown pairing operation.'})
    return Handler


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Codex Tools one-time pairing')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(minutes=10))
            .sign(key, algorithm=hashes.SHA256()))
    cert_path, key_path = Path(directory)/'cert.pem', Path(directory)/'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context, cert.fingerprint(hashes.SHA256()).hex()


class PairingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, context):
        self.context = context
        self.workers = threading.BoundedSemaphore(16)
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.workers.release()

    def finish_request(self, request, client_address):
        # Handshake in the request worker, with a bound; a half-open TLS
        # connection must not block the listener or invitation expiry.
        request.settimeout(5)
        try:
            with self.context.wrap_socket(request, server_side=True) as connection:
                self.RequestHandlerClass(connection, client_address, self)
        except (ssl.SSLError, OSError):
            pass


def invitation(url, fingerprint, state):
    payload = dict(version=1, url=url, certificateSha256=fingerprint, secret=state.secret, expires=state.expires)
    return 'ct-pair:' + base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')


def main(argv=None, *, service='relay'):
    parser = argparse.ArgumentParser(prog='ct voice' if service == 'voice' else 'ct connect', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pair = sub.add_parser('pair-phone', help='Create one QR invitation and authorize the phone on every SSH hop.')
    pair.add_argument('profile', nargs='?' if service == 'voice' else None,
                      default='voice' if service == 'voice' else None,
                      help='Saved ct-connect profile name, SSH alias, or local voice profile name.')
    pair.add_argument('--ssh', help='Override the destination SSH alias.')
    pair.add_argument('--remote-port', type=int)
    pair.add_argument('--remote-bin')
    if service == 'voice':
        pair.add_argument('--remote-state-dir', help='Absolute voice state directory on the SSH destination.')
        pair.add_argument('--local', action='store_true', help='Voice runs on this computer; no SSH to self is needed.')
        pair.add_argument('--ssh-port', type=int, default=22, help='This computer’s SSH port (with --local).')
        pair.add_argument('--state-dir', type=Path, help='Local voice state directory (with --local).')
    pair.add_argument('--address', required=True, help='This computer’s IP or hostname reachable from the phone.')
    pair.add_argument('--bind', default='0.0.0.0', help='Temporary pairing listener (default: all IPv4 interfaces).')
    pair.add_argument('--port', type=int, default=0, help='Pairing port (default: a free port).')
    pair.add_argument('--output', type=Path, help='Also save the QR as an SVG image (sensitive invitation).')
    args = parser.parse_args(argv)
    try:
        import qrcode
        import qrcode.image.svg
        if not 0 <= args.port <= 65535 or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}', args.address):
            raise ValueError('Enter a valid reachable address and port.')
        if service == 'voice' and args.local:
            if args.ssh or args.remote_bin or args.remote_state_dir:
                raise ValueError('--local cannot be combined with remote SSH options.')
            bundle, token = prepare_local_voice(args.profile, args.address, port=args.remote_port,
                                                ssh_port=args.ssh_port, state_dir=args.state_dir)
            route = None
        else:
            if service == 'voice' and args.state_dir:
                raise ValueError('--state-dir requires --local; use --remote-state-dir for an SSH destination.')
            bundle, token, route = prepare(args.profile, ssh=args.ssh, remote_port=args.remote_port, remote_bin=args.remote_bin,
                                           service=service, remote_state_dir=getattr(args, 'remote_state_dir', None))
        state = PairingState(bundle, token)
        with tempfile.TemporaryDirectory(prefix='ct-pair-') as directory:
            context, fingerprint = certificate(directory)
            server = PairingHTTPServer((args.bind, args.port), handler_for(state), context)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                address = f'[{args.address}]' if ':' in args.address else args.address
                code = invitation(f'https://{address}:{server.server_port}', fingerprint, state)
                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
                qr.add_data(code)
                qr.make(fit=True)  # Never truncate an oversized payload.
                location = 'Voice → Pair voice server' if service == 'voice' else 'Connections → Pair phone'
                print(f'Pair {args.profile}: scan in Agent Console → Settings → {location}.')
                print(f'Temporary HTTPS endpoint: https://{address}:{server.server_port} (bind: {args.bind})')
                print('Invitation expires in five minutes. Keep this QR private.')
                qr.print_ascii(invert=True)
                if args.output:
                    with args.output.open('xb') as file:
                        os.fchmod(file.fileno(), 0o600)
                        qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(file)
                    print(f'QR image: {args.output}')
                print('Paste alternative (same invitation):\n' + code, flush=True)
                if not state.received.wait(max(0, state.deadline-time.monotonic())):
                    raise ValueError('Invitation expired without a phone joining.')
                print('\nPhone key: ' + state.fingerprint)
                print('Authorize SSH access on: ' + ' → '.join(h['name'] for h in bundle['hosts']))
                print('Compare the fingerprint on the phone. Authorize? [y/N] ', end='', flush=True)
                readable, _, _ = select.select([sys.stdin], [], [], max(0, state.deadline-time.monotonic()))
                answer = sys.stdin.readline() if readable else ''
                with state.lock:
                    if answer.strip().lower() != 'y' or time.monotonic() >= state.deadline:
                        state.status = 'rejected'
                    else:
                        state.status = 'authorizing'
                if state.status == 'rejected':
                    # Brief grace period lets the phone receive the rejection.
                    state.acknowledged.wait(min(3, max(0, state.deadline-time.monotonic())))
                    return 1
                try:
                    for index, host in enumerate(bundle['hosts']):
                        if time.monotonic() >= state.deadline:
                            raise ValueError('Invitation expired during authorization.')
                        if route is None:
                            install_local_key(state.key)
                        else:
                            install_key(route, index, state.key)
                        with state.lock:
                            state.installed.append(host['name'])
                        print('Authorized: ' + host['name'], flush=True)
                except Exception:
                    with state.lock:
                        state.status = 'failed'
                    print('Authorization incomplete. Already installed on: ' + ', '.join(state.installed))
                    state.acknowledged.wait(min(3, max(0, state.deadline-time.monotonic())))
                    raise
                with state.lock:
                    state.status = 'complete'
                print('Waiting for the phone to save its connection…', flush=True)
                if state.acknowledged.wait(max(0, state.deadline-time.monotonic())):
                    print('Phone paired. Invitation closed.')
                    return 0
                raise ValueError('SSH key authorized, but phone did not confirm saving before expiry. Pair again if needed.')
            finally:
                server.shutdown()
                server.server_close()
    except ImportError:
        print('Pairing QR support needs: pip install "codex-tools[pairing]"')
        return 2
    except (ValueError, OSError, subprocess.SubprocessError, EOFError, KeyboardInterrupt) as exc:
        print('Pairing stopped: ' + str(exc))
        return 1
