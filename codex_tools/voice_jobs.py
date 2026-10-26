#!/usr/bin/env python3
"""Durable localhost voice jobs backed by an existing whisper-server.

Python standard library only. No Codex, Whisper Python packages, or SSH sessions
are needed by this process. Run whisper-server separately on localhost.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener
import uuid
import wave

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 180
JOB_ID = re.compile(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}')
RETENTION_SECONDS = 7 * 86400
PUBLIC_JOB_COLUMNS = 'id,status,text,error,audio_seconds,transcription_seconds,queue_seconds'


class VoiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validate_backend(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.port or parsed.path not in ('', '/')
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError('Whisper URL must be http://127.0.0.1:PORT.')
    return url.rstrip('/')


class WhisperServer:
    def __init__(self, url, timeout=600):
        self.url = validate_backend(url)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def __call__(self, audio, language):
        boundary = 'voice-' + uuid.uuid4().hex
        fields = {'response_format': 'json', 'language': language or 'auto'}
        parts = []
        for name, value in fields.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        parts += [(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode(),
                  audio, f'\r\n--{boundary}--\r\n'.encode()]
        request = Request(self.url + '/inference', data=b''.join(parts),
                          headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with self.opener.open(request, timeout=self.timeout) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise VoiceError('Whisper returned an oversized transcript.', 502)
        text = json.loads(data).get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise VoiceError('No usable speech was recognized. Record again.', 422)
        return text.strip()


class VoiceService:
    def __init__(self, root, *, transcribe, host_id, start_worker=True, engine_info=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._lease = (self.root / 'worker.lock').open('a')
        self._lease_path = self.root / 'worker.lock'
        self._lease_path.chmod(0o600)
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease.close()
            raise RuntimeError('Another voice service already owns this state directory.') from None
        self.path = self.root / 'jobs.sqlite3'
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.transcribe = transcribe
        self.host_id = host_id
        self.engine_info = engine_info
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        with self.connect() as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, language TEXT,
                audio BLOB, status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                finished REAL, retry_at REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name in ('audio_seconds', 'transcription_seconds', 'queue_seconds'):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} REAL')
            db.execute("UPDATE jobs SET status='queued', retry_at=0 WHERE status='transcribing'")
            self._prune(db)
        if start_worker:
            self._thread = threading.Thread(target=self._worker, name='voice-jobs', daemon=True)
            self._thread.start()

    def connect(self):
        return closing(sqlite3.connect(self.path, timeout=10))

    @staticmethod
    def validate_id(job_id):
        if not JOB_ID.fullmatch(job_id):
            raise VoiceError('Invalid voice job ID.')

    @staticmethod
    def _prune(db):
        db.execute("DELETE FROM jobs WHERE status IN ('completed','failed') AND finished < ?",
                   (time.time() - RETENTION_SECONDS,))

    @staticmethod
    def _public(row):
        names = ('id', 'status', 'text', 'error', 'audio_seconds', 'transcription_seconds', 'queue_seconds')
        return {key: value for key, value in zip(names, row) if value is not None}

    def capabilities(self):
        # Availability describes the durable queue, not a promise that the
        # recognizer is currently reachable. Jobs wait safely if it is down.
        info = self.engine_info() if callable(self.engine_info) else self.engine_info or {}
        return {'protocol': 1, 'service': 'voice-jobs', 'enabled': True,
                'available': True, 'host_id': self.host_id, 'engine': 'whisper.cpp',
                'model': info.get('model', 'Configured on whisper-server'),
                'device': info.get('device', 'Server configured'),
                'backend': info.get('backend'), 'device_verified': info.get('verified', False),
                'durable_jobs': True, 'retention_seconds': RETENTION_SECONDS,
                'max_audio_bytes': MAX_AUDIO_BYTES, 'max_seconds': MAX_SECONDS}

    def get(self, job_id):
        self.validate_id(job_id)
        with self.connect() as db:
            row = db.execute(f'SELECT {PUBLIC_JOB_COLUMNS} FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise VoiceError('Voice job not found.', 404)
        return self._public(row)

    def submit(self, job_id, audio, language=None):
        self.validate_id(job_id)
        if not 44 <= len(audio) <= MAX_AUDIO_BYTES:
            raise VoiceError('Recording exceeds size limits.', 413)
        if language and not re.fullmatch('[a-z]{2,3}', language):
            raise VoiceError('Invalid language code.')
        try:
            with wave.open(io.BytesIO(audio)) as wav:
                if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                        or wav.getframerate() != 16000 or wav.getcomptype() != 'NONE'
                        or not 0 < wav.getnframes() <= MAX_SECONDS * 16000
                        or len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2):
                    raise ValueError()
                audio_seconds = wav.getnframes() / wav.getframerate()
        except (wave.Error, EOFError, ValueError):
            raise VoiceError('Expected a complete mono 16 kHz, 16-bit PCM WAV.') from None
        digest = hashlib.sha256(audio).hexdigest()
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT digest,language FROM jobs WHERE id=?', (job_id,)).fetchone()
            if previous is not None:
                if previous != (digest, language):
                    raise VoiceError('Job ID already belongs to different audio or language.', 409)
                row = db.execute(f'SELECT {PUBLIC_JOB_COLUMNS} FROM jobs WHERE id=?', (job_id,)).fetchone()
                return self._public(row)
            self._prune(db)
            if db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= 1000:
                raise VoiceError('Voice result storage is full.', 503)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','transcribing')").fetchone()[0] >= 10:
                raise VoiceError('Voice queue is full. Recording remains on your device.', 429)
            db.execute('INSERT INTO jobs (id,digest,language,audio,status,created,audio_seconds) VALUES (?,?,?,?,?,?,?)',
                       (job_id, digest, language, audio, 'queued', time.time(), audio_seconds))
        # The transaction commits before acknowledging the upload.
        self._wake.set()
        return {'id': job_id, 'status': 'queued', 'text': '', 'error': '', 'audio_seconds': audio_seconds}

    def _worker(self):
        while not self._stop.is_set():
            try:
                self._process_one()
            except Exception:
                logging.exception('Voice worker could not update its durable queue.')
            self._wake.wait(0.25)
            self._wake.clear()

    def _process_one(self):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,audio,language,attempts,created FROM jobs WHERE status='queued' AND retry_at<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                return
            job_id, audio, language, attempts, created = row
            queue_seconds = max(0.0, time.time() - created)
            db.execute("UPDATE jobs SET status='transcribing',error='' WHERE id=?", (job_id,))
        started = time.perf_counter()
        try:
            text = self.transcribe(audio, language)
            if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
                raise VoiceError('No usable speech was recognized. Record again.', 422)
        except Exception as exc:
            retry = not isinstance(exc, VoiceError) or exc.status >= 500
            if isinstance(exc, HTTPError):
                retry = exc.code >= 500 or exc.code in (408, 429)
            with self.connect() as db, db:
                if retry:
                    db.execute("UPDATE jobs SET status='queued',attempts=attempts+1,retry_at=?,error=? WHERE id=?",
                               (time.time() + min(60, 2 ** min(attempts, 6)), 'Waiting for the transcription engine; recording is saved on the server.', job_id))
                else:
                    db.execute("UPDATE jobs SET status='failed',audio=NULL,finished=?,error=?,transcription_seconds=?,queue_seconds=? WHERE id=?",
                               (time.time(), 'Transcription failed. Check the engine or record again.', time.perf_counter() - started, queue_seconds, job_id))
            return
        with self.connect() as db, db:
            db.execute("UPDATE jobs SET status='completed',text=?,error='',audio=NULL,finished=?,transcription_seconds=?,queue_seconds=? WHERE id=?",
                       (text.strip(), time.time(), time.perf_counter() - started, queue_seconds, job_id))

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            # Inference is independent of HTTP connections; finish storing its
            # outcome on graceful shutdown. SIGKILL recovery reruns unfinished jobs.
            self._thread.join()
        self._lease.close()


def make_handler(service, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Losing an acknowledgement does not undo an accepted job.

        def do_GET(self):
            self.handle_voice(False)

        def do_POST(self):
            self.handle_voice(True)

        def handle_voice(self, upload):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
                self.reply({'error': 'Unauthorized.'}, 401)
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path == '/v1/voice' and not upload and not parsed.query:
                    self.reply(service.capabilities())
                    return
                prefix = '/v1/voice/jobs/'
                if not parsed.path.startswith(prefix):
                    raise VoiceError('Not found.', 404)
                job_id = parsed.path[len(prefix):]
                service.validate_id(job_id)
                if not upload:
                    if parsed.query:
                        raise VoiceError('Invalid query.')
                    self.reply(service.get(job_id))
                    return
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                if set(query) - {'language'} or any(len(v) != 1 for v in query.values()):
                    raise VoiceError('Invalid voice options.')
                length = int(self.headers.get('Content-Length', '0'))
                if (self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'audio/wav'
                        or not 44 <= length <= MAX_AUDIO_BYTES):
                    raise VoiceError('Invalid voice upload.', 413)
                self.connection.settimeout(30)
                audio = self.rfile.read(length)
                if len(audio) != length:
                    raise VoiceError('Incomplete upload.')
                self.reply(service.submit(job_id, audio, query.get('language', [None])[0]))
            except VoiceError as exc:
                self.reply({'error': str(exc)}, exc.status)
            except (ValueError, OSError):
                self.reply({'error': 'Voice request failed.'}, 400)
    return Handler
