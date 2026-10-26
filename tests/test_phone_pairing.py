from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from codex_tools import phone_pairing as p

try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False


def phone(state):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    proof = base64.b64encode(key.sign(('ct-pair-v1:' + state.secret).encode())).decode()
    return public, proof


class PairingTests(unittest.TestCase):
    def test_key_proof_one_key_per_invitation_and_no_options(self):
        state = p.PairingState({'version': 1}, 'relay-secret')
        key, proof = phone(state)
        state.submit(key, proof)
        state.submit(key, proof)
        self.assertEqual(state.status, 'submitted')
        self.assertTrue(state.fingerprint.startswith('SHA256:'))
        other, other_proof = phone(state)
        with self.assertRaises(ValueError):
            state.submit(other, other_proof)
        for invalid in ('command="id" ' + key, key + '\n' + key, 'ssh-rsa AAAA'):
            with self.assertRaises(ValueError):
                p.public_key(invalid)
        with self.assertRaises(ValueError):
            p.PairingState({}, '').submit(key, proof)

    def test_https_expiry_token_gating_ack_and_redirect_rejection(self):
        state = p.PairingState({'version': 1, 'profile': {'name': 'Example'}, 'hosts': []}, 'relay-secret')
        with tempfile.TemporaryDirectory() as directory:
            context, fingerprint = p.certificate(directory)
            server = p.PairingHTTPServer(('127.0.0.1', 0), p.handler_for(state), context)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def call(path, body=None, auth=True):
                    connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=ssl._create_unverified_context(), timeout=5)
                    headers = {'Authorization': 'Bearer ' + state.secret} if auth else {}
                    connection.connect()
                    self.assertEqual(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest(), fingerprint)
                    connection.request('GET' if body is None else 'POST', path,
                                       body=None if body is None else json.dumps(body), headers=headers)
                    response = connection.getresponse()
                    result = response.status, json.loads(response.read())
                    connection.close()
                    return result
                with self.assertRaises(ssl.SSLCertVerificationError):
                    verified = http.client.HTTPSConnection('127.0.0.1', server.server_port, timeout=5)
                    try: verified.connect()
                    finally: verified.close()
                self.assertEqual(call('/v1/pair', auth=False)[0], 401)
                self.assertNotIn('token', call('/v1/pair')[1])
                self.assertEqual(call('/v1/pair?url=https://other')[0], 404)
                self.assertEqual(call('/v1/ack', {})[0], 400)
                key, proof = phone(state)
                self.assertEqual(call('/v1/key', {'publicKey': key, 'proof': proof})[0], 200)
                self.assertNotIn('token', call('/v1/pair')[1])
                state.status = 'complete'
                self.assertEqual(call('/v1/pair')[1]['token'], 'relay-secret')
                self.assertEqual(call('/v1/ack', {'fingerprint': state.fingerprint})[0], 200)
                self.assertTrue(state.acknowledged.is_set())
                state.deadline = time.monotonic() - 1
                self.assertEqual(call('/v1/pair')[0], 410)
                self.assertEqual(call('/v1/key', {'publicKey': key, 'proof': proof})[0], 410)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_authorized_keys_preserves_contents_and_is_idempotent(self):
        state = p.PairingState({}, '')
        key, _ = phone(state)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'.ssh'/'authorized_keys'
            target.parent.mkdir()
            target.write_text('# keep existing keys\n')
            env = dict(os.environ, HOME=directory)
            for _ in range(2):
                result = subprocess.run([sys.executable, '-c', p.INSTALL_SCRIPT], input=key+' phone\n', text=True,
                                        capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(), '# keep existing keys\n'+key+' phone\n')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            target.unlink()
            original = Path(directory)/'original'
            original.write_text('untouched')
            target.symlink_to(original)
            result = subprocess.run([sys.executable, '-c', p.INSTALL_SCRIPT], input=key+' phone\n', text=True,
                                    capture_output=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(original.read_text(), 'untouched')

    def test_route_resolution_order_ports_cycles_and_unsupported_proxy(self):
        settings = {
            'destination': dict(hostname='192.0.2.2', user='remote', port='22', proxyjump='jump'),
            'jump': dict(hostname='192.0.2.1', user='local', port='22'),
        }
        with patch.object(p, 'ssh_options', side_effect=lambda name: settings[name]):
            route = p.ssh_route('destination')
            self.assertEqual([h[0] for h in route], ['jump', 'destination'])
            command = p.ssh_command(route, 1, 'true')
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertEqual(command[-4:], ['-J', 'jump', 'destination', 'true'])
            settings['jump']['proxyjump'] = 'destination'
            with self.assertRaises(ValueError): p.ssh_route('destination')
            settings['jump'] = {'proxycommand': 'arbitrary command'}
            with self.assertRaises(ValueError): p.ssh_route('jump')
        self.assertEqual(p.target_arguments('user@jump:2222'), ['-p', '2222', 'user@jump'])
        self.assertEqual(p.target_arguments('user@[::1]:2222'), ['-p', '2222', 'user@[::1]'])
        for name in ('-oProxyCommand=evil', 'a;id', 'a b', 'user@jump:99999'):
            with self.assertRaises(ValueError): p.target_arguments(name)

    def test_prepare_exports_complete_route_without_private_keys(self):
        key, _ = phone(p.PairingState({}, ''))
        route = [('jump', dict(hostname='192.0.2.1', user='one', port='22')),
                 ('destination', dict(hostname='192.0.2.2', user='two', port='2222'))]
        with tempfile.TemporaryDirectory() as directory, patch.object(p.paths, 'user_config_dir', return_value=Path(directory)), \
                patch.object(p, 'ssh_route', return_value=route), patch.object(p, 'run', side_effect=[key, key, 'token']):
            bundle, token, exported_route = p.prepare('destination-via-jump')
        self.assertEqual(token, 'token')
        self.assertEqual(exported_route, route)
        self.assertEqual(bundle['profile']['sshHostId'], bundle['hosts'][1]['id'])
        self.assertEqual(bundle['profile']['jumpHostIds'], [bundle['hosts'][0]['id']])
        self.assertEqual(bundle['hosts'][1]['port'], 2222)
        self.assertNotIn('token', json.dumps(bundle))
        self.assertNotIn('privateKey', json.dumps(bundle))

    def test_voice_pairing_uses_independent_token_and_never_starts_relay(self):
        key, _ = phone(p.PairingState({}, ''))
        route = [('voice-box', dict(hostname='192.0.2.3', user='voice', port='22'))]
        with tempfile.TemporaryDirectory() as directory, patch.object(p.paths, 'user_config_dir', return_value=Path(directory)), \
                patch.object(p, 'ssh_route', return_value=route), patch.object(p, 'run', side_effect=[key, 'voice-token']) as run:
            bundle, token, _ = p.prepare('voice-box', service='voice', remote_state_dir='/srv/voice-data')
        profile = bundle['profile']
        self.assertEqual(token, 'voice-token')
        self.assertEqual(profile['service'], 'voice')
        self.assertEqual(profile['url'], 'http://127.0.0.1:8790')
        self.assertFalse(profile['autoStartRelay'])
        self.assertEqual(run.call_args_list[-1].args[0][-1].split('exec ', 1)[-1],
                         'ct voice --state-dir /srv/voice-data token')
        self.assertNotIn('voice-token', json.dumps(bundle))

    def test_local_voice_pairing_uses_voice_identity_and_local_ssh_host_key(self):
        key, _ = phone(p.PairingState({}, ''))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'identity.json').write_text(json.dumps({'id': 'voice-id', 'token': 'voice-token'}))
            (root / 'config.json').write_text(json.dumps({'port': 8792}))
            host_key = root / 'host.pub'
            host_key.write_text(key)
            bundle, token = p.prepare_local_voice('local-voice', '192.0.2.4', state_dir=root,
                                                   host_key_path=host_key)
        self.assertEqual(token, 'voice-token')
        self.assertEqual(bundle['profile']['url'], 'http://127.0.0.1:8792')
        self.assertEqual(bundle['hosts'][0]['host'], '192.0.2.4')
        self.assertEqual(bundle['hosts'][0]['username'], p.getpass.getuser())
        self.assertEqual(bundle['profile']['service'], 'voice')

    @unittest.skipUnless(HAS_QR, 'optional pairing QR encoder is not installed')
    def test_invitation_fits_medium_qr_and_hides_config_and_relay_token(self):
        import qrcode
        state = p.PairingState({'hosts': ['many hosts'] * 8}, 'never-in-qr')
        code = p.invitation('https://192.0.2.1:12345', 'a'*64, state)
        self.assertLess(len(code.encode()), 2331)
        payload = json.loads(base64.urlsafe_b64decode(code[8:]+'='*(-len(code[8:])%4)))
        self.assertEqual(payload['secret'], state.secret)
        self.assertNotIn('never-in-qr', json.dumps(payload))
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(code)
        qr.make(fit=True)
        self.assertLess(qr.version, 40)

    @unittest.skipUnless(HAS_QR, 'optional pairing QR encoder is not installed')
    def test_cli_single_invitation_confirms_every_host_and_closes_on_phone_save(self):
        bundle = {'version': 1, 'profile': {'name': 'example'}, 'hosts': [{'name': 'jump'}, {'name': 'destination'}]}
        states, installs, threads = [], [], []
        original = p.invitation

        def joined(url, fingerprint, state):
            states.append(state)
            key, proof = phone(state)
            state.submit(key, proof)
            # Simulate the phone's final acknowledgement after authorization.
            def acknowledge():
                for _ in range(100):
                    if state.status == 'complete':
                        state.acknowledged.set()
                        return
                    time.sleep(.01)
            thread = threading.Thread(target=acknowledge)
            thread.start()
            threads.append(thread)
            return original(url, fingerprint, state)

        with patch.object(p, 'prepare', return_value=(bundle, 'fake-token', ['route'])), \
                patch.object(p, 'invitation', side_effect=joined), \
                patch.object(p, 'install_key', side_effect=lambda route, index, key: installs.append(index)), \
                patch.object(p.select, 'select', return_value=([1], [], [])), \
                patch.object(p.sys, 'stdin', io.StringIO('y\n')), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(p.main(['pair-phone', 'example', '--address', '127.0.0.1', '--bind', '127.0.0.1']), 0)
        for thread in threads: thread.join()
        self.assertEqual(installs, [0, 1])
        self.assertEqual(states[0].installed, ['jump', 'destination'])
        self.assertTrue(states[0].acknowledged.is_set())

    @unittest.skipUnless(HAS_QR, 'optional pairing QR encoder is not installed')
    def test_cli_refusal_never_installs_key(self):
        state = p.PairingState({'version': 1, 'hosts': [{'name': 'destination'}]}, 'token')
        key, proof = phone(state)
        state.submit(key, proof)
        # Skip grace wait in this rejection-only fixture.
        state.acknowledged.set()
        with patch.object(p, 'prepare', return_value=(state.bundle, 'token', ['route'])), \
                patch.object(p, 'PairingState', return_value=state), \
                patch.object(p, 'install_key') as install, \
                patch.object(p.select, 'select', return_value=([1], [], [])), \
                patch.object(p.sys, 'stdin', io.StringIO('n\n')), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(p.main(['pair-phone', 'example', '--address', '127.0.0.1', '--bind', '127.0.0.1']), 1)
        install.assert_not_called()
        self.assertEqual(state.status, 'rejected')


if __name__ == '__main__':
    unittest.main()
