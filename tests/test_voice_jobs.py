"""Standalone voice queue recovery, API and whisper-server integration tests."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
import wave

from codex_tools.voice_jobs import VoiceService, VoiceError, WhisperServer, make_handler
from codex_tools.voice import main


def audio():
    value = io.BytesIO()
    with wave.open(value, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b'\x00\x00' * 1600)
    return value.getvalue()


class VoiceBackendTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'voice'
        self.services = []
        self.addCleanup(lambda: [s.close() for s in self.services])

    def service(self, recognizer=lambda *_: 'Rozpoznaný text.', worker=True):
        service = VoiceService(self.root, transcribe=recognizer, host_id='fixture-host', start_worker=worker)
        self.services.append(service)
        return service

    def close(self, service):
        service.close()
        self.services.remove(service)

    def finished(self, service, job_id):
        for _ in range(500):
            result = service.get(job_id)
            if result['status'] in ('completed', 'failed'):
                return result
            time.sleep(.01)
        self.fail('Voice worker did not finish')

    def server(self, service):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(service, 'fixture-key'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, method='GET', body=None, token='fixture-key'):
        c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            c.request(method, path, body, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'audio/wav'})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()

    def test_completed_result_survives_restart_without_running_recognition_again(self):
        calls = []
        service = self.service(lambda *args: calls.append(args) or 'Saved transcript')
        job_id = str(uuid.uuid4())
        service.submit(job_id, audio(), 'sk')
        result = self.finished(service, job_id)
        self.close(service)
        restored = self.service(lambda *_: self.fail('Completed recognition was repeated'))
        self.assertEqual(restored.get(job_id), result)
        self.assertEqual(restored.submit(job_id, audio(), 'sk'), result)
        self.assertEqual(len(calls), 1)
        with restored.connect() as db:
            self.assertIsNone(db.execute('SELECT audio FROM jobs').fetchone()[0])
        self.assertEqual(restored.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_queued_and_interrupted_jobs_recover_on_restart(self):
        service = self.service(worker=False)
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for job_id in ids:
            service.submit(job_id, audio())
        with service.connect() as db, db:
            db.execute("UPDATE jobs SET status='transcribing' WHERE id=?", (ids[0],))
        self.close(service)
        restored = self.service()
        for job_id in ids:
            self.assertEqual(self.finished(restored, job_id)['status'], 'completed')

    def test_lost_upload_ack_and_disconnected_client_do_not_lose_or_repeat_job(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def recognize(*args):
            calls.append(args)
            started.set()
            if not release.wait(3):
                raise AssertionError('Test did not release recognizer')
            return 'Returned after reconnect'
        service = self.service(recognize)
        server = self.server(service)
        job_id, data = str(uuid.uuid4()), audio()
        path = '/v1/voice/jobs/' + job_id
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as client:
            client.sendall((f'POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-key\r\nContent-Type: audio/wav\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n').encode() + data)
            self.assertTrue(started.wait(2))
            # Drop the connection without reading the acceptance response.
        self.assertEqual(service.get(job_id)['status'], 'transcribing')
        release.set()
        self.finished(service, job_id)
        status, result = self.request(server, path)
        self.assertEqual(status, 200)
        self.assertEqual(result['text'], 'Returned after reconnect')
        self.assertEqual(self.request(server, path, 'POST', data)[1], result)
        self.assertEqual(len(calls), 1)

    def test_partial_upload_is_not_accepted_and_same_id_can_be_retried(self):
        service = self.service()
        server = self.server(service)
        job_id, data = str(uuid.uuid4()), audio()
        path = '/v1/voice/jobs/' + job_id
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as client:
            client.sendall((f'POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-key\r\nContent-Type: audio/wav\r\nContent-Length: {len(data)}\r\n\r\n').encode() + data[:44])
            client.shutdown(socket.SHUT_WR)
            client.recv(4096)
        self.assertEqual(self.request(server, path)[0], 404)
        self.assertEqual(self.request(server, path, 'POST', data)[0], 200)
        self.assertEqual(self.finished(service, job_id)['status'], 'completed')

    def test_engine_outage_keeps_audio_and_retries_without_client_reupload(self):
        attempts = []
        def recognize(*_):
            attempts.append(True)
            if len(attempts) == 1:
                raise ConnectionRefusedError('Whisper is offline')
            return 'Recovered backend'
        service = self.service(recognize, worker=False)
        job_id = str(uuid.uuid4())
        service.submit(job_id, audio())
        service._process_one()
        self.assertEqual(service.get(job_id)['status'], 'queued')
        with service.connect() as db, db:
            self.assertIsNotNone(db.execute('SELECT audio FROM jobs').fetchone()[0])
            db.execute('UPDATE jobs SET retry_at=0')
        service._process_one()
        self.assertEqual(service.get(job_id)['text'], 'Recovered backend')

    def test_conflicts_limits_and_rejected_inputs_preserve_existing_jobs(self):
        service = self.service(worker=False)
        job_id = str(uuid.uuid4())
        service.submit(job_id, audio(), 'sk')
        with self.assertRaises(VoiceError) as caught:
            service.submit(job_id, audio(), 'en')
        self.assertEqual(caught.exception.status, 409)
        for ident, data, language in [('bad', audio(), None), (str(uuid.uuid4()), b'not wav' * 10, None), (str(uuid.uuid4()), audio(), 'bad_language')]:
            with self.assertRaises(VoiceError):
                service.submit(ident, data, language)
        for _ in range(9):
            service.submit(str(uuid.uuid4()), audio())
        with self.assertRaises(VoiceError) as caught:
            service.submit(str(uuid.uuid4()), audio())
        self.assertEqual(caught.exception.status, 429)
        with self.assertRaises(RuntimeError):
            VoiceService(self.root, transcribe=lambda *_: '', host_id='other')

    def test_authentication_and_invalid_options(self):
        service = self.service()
        server = self.server(service)
        self.assertEqual(self.request(server, '/v1/voice', token='wrong')[0], 401)
        caps = self.request(server, '/v1/voice')[1]
        self.assertTrue(caps['durable_jobs'])
        path = '/v1/voice/jobs/' + str(uuid.uuid4())
        self.assertEqual(self.request(server, path + '?path=/etc/passwd', 'POST', audio())[0], 400)
        self.assertEqual(self.request(server, path + '?language=sk&language=en', 'POST', audio())[0], 400)

    def test_whisper_adapter_posts_multipart_and_returns_text(self):
        received = []
        class Backend(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                data = self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path, self.headers['Content-Type'], data))
                body = json.dumps({'text': 'Real adapter result'}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        service = self.service(WhisperServer(f'http://127.0.0.1:{backend.server_port}'))
        job_id = str(uuid.uuid4())
        service.submit(job_id, audio(), 'sk')
        self.assertEqual(self.finished(service, job_id)['text'], 'Real adapter result')
        self.assertEqual(received[0][0], '/inference')
        self.assertTrue(received[0][1].startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="language"\r\n\r\nsk', received[0][2])
        self.assertIn(audio(), received[0][2])

    def test_completed_job_persists_audio_processing_and_queue_timing(self):
        service = self.service(worker=False)
        job_id = str(uuid.uuid4())
        accepted = service.submit(job_id, audio())
        self.assertAlmostEqual(accepted['audio_seconds'], .1)
        with service.connect() as db, db:
            db.execute('UPDATE jobs SET created=created-2 WHERE id=?', (job_id,))
        service._process_one()
        result = service.get(job_id)
        self.assertGreaterEqual(result['queue_seconds'], 2)
        self.assertGreaterEqual(result['transcription_seconds'], 0)
        self.assertAlmostEqual(result['audio_seconds'], .1)
        self.close(service)
        restored = self.service(worker=False)
        self.assertEqual(restored.get(job_id), result)
        self.assertEqual(restored.submit(job_id, audio()), result)

    def test_legacy_database_adds_timing_columns_without_losing_results(self):
        self.root.mkdir()
        job_id = str(uuid.uuid4())
        with sqlite3.connect(self.root / 'jobs.sqlite3') as db:
            db.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY,digest TEXT,language TEXT,audio BLOB,
                status TEXT,text TEXT,error TEXT,created REAL,finished REAL,retry_at REAL DEFAULT 0,
                attempts INTEGER DEFAULT 0)""")
            db.execute('INSERT INTO jobs (id,digest,status,text,error,created,finished) VALUES (?,?,?,?,?,?,?)',
                       (job_id, 'original', 'completed', 'Preserved transcript', '', time.time(), time.time()))
        service = self.service(worker=False)
        result = service.get(job_id)
        self.assertEqual(result['text'], 'Preserved transcript')
        self.assertNotIn('transcription_seconds', result)
        with service.connect() as db:
            self.assertIn('audio_seconds', {r[1] for r in db.execute('PRAGMA table_info(jobs)')})

    def test_identity_init_never_rotates_an_existing_key(self):
        self.assertEqual(main(['--state-dir', str(self.root), 'init']), 0)
        identity = (self.root / 'identity.json').read_bytes()
        self.assertEqual(main(['--state-dir', str(self.root), 'init']), 1)
        self.assertEqual((self.root / 'identity.json').read_bytes(), identity)


if __name__ == '__main__':
    unittest.main()
