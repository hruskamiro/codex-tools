"""Local whisper-server timing with temporary engines and no configuration changes."""
from __future__ import annotations

import io
from pathlib import Path
import socket
import statistics
import tempfile
import time
import wave

from codex_tools.voice_jobs import MAX_AUDIO_BYTES, MAX_SECONDS, WhisperServer


def benchmark(audio_file, root, settings, *, model=None, device='auto', runs=3, warmup=1,
              language='', progress=None):
    # Import lazily: ordinary voice setup never starts benchmark engines.
    from codex_tools.voice import ManagedEngine, backend_ready, engine_command, find_server, resolve_model

    if device not in ('auto', 'cpu', 'gpu', 'both') or not 1 <= runs <= 20 or not 0 <= warmup <= 5:
        raise ValueError('Choose auto/cpu/gpu/both, 1-20 runs and 0-5 warmup runs.')
    audio_file = Path(audio_file).expanduser()
    if not 44 <= audio_file.stat().st_size <= MAX_AUDIO_BYTES:
        raise ValueError('Benchmark audio must be a WAV recording up to 10 MiB.')
    audio = audio_file.read_bytes()
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            duration = wav.getnframes() / wav.getframerate()
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000
                    or wav.getcomptype() != 'NONE' or not 0 < duration <= MAX_SECONDS
                    or len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2):
                raise ValueError()
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        raise ValueError('Benchmark audio must be complete mono 16 kHz, 16-bit PCM WAV, at most three minutes.') from None
    server = find_server(settings.get('whisper_bin'))
    model_file = resolve_model(model or settings.get('model'), root, server)
    if not model_file:
        raise ValueError('Select an installed model with --model, or configure voice first.')
    if language and (not language.isalpha() or not language.isascii() or not 2 <= len(language) <= 3):
        raise ValueError('Use a language code such as en or leave it empty for automatic detection.')
    cases = []
    for requested in (('cpu', 'gpu') if device == 'both' else (device,)):
        if progress:
            progress(f'{Path(model_file).name}: {requested}, {runs} measured runs after {warmup} warmups')
        with tempfile.TemporaryDirectory(prefix='ct-voice-benchmark-') as folder:
            directory = Path(folder)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            config = {**settings, 'whisper_bin': server, 'model': model_file,
                      'whisper_url': url, 'cpu': requested == 'cpu'}
            engine = ManagedEngine(engine_command(config, root), directory)
            started = time.perf_counter()
            try:
                engine.start()
                deadline = time.monotonic() + 120
                while not backend_ready(url):
                    if engine.process.poll() is not None:
                        raise RuntimeError('Benchmark engine exited. Check whisper-server/model compatibility with ct voice doctor.')
                    if time.monotonic() > deadline:
                        raise RuntimeError('Benchmark model did not become ready within two minutes.')
                    time.sleep(.05)
                loading = time.perf_counter() - started
                acceleration = engine.acceleration()
                if requested == 'gpu' and (not acceleration['verified'] or acceleration.get('backend') in (None, 'CPU')):
                    raise RuntimeError('GPU was requested but no GPU backend was verified. Check driver access and the whisper-server build.')
                recognize = WhisperServer(url)
                warmups = []
                for _ in range(warmup):
                    started = time.perf_counter()
                    recognize(audio, language or None)
                    warmups.append(time.perf_counter() - started)
                timings = []
                transcript = ''
                for _ in range(runs):
                    started = time.perf_counter()
                    transcript = recognize(audio, language or None)
                    timings.append(time.perf_counter() - started)
                mean = statistics.mean(timings)
                cases.append({'model': Path(model_file).stem.removeprefix('ggml-'),
                              'requested_device': requested, **acceleration,
                              'audio_seconds': duration, 'startup_seconds': loading,
                              'warmup_seconds': warmups, 'runs': runs, 'threads': config.get('threads', 4),
                              'transcription_seconds': timings, 'mean_seconds': mean,
                              'realtime_factor': mean / duration, 'speed_vs_realtime': duration / max(mean, 1e-9),
                              'transcript': transcript})
            finally:
                engine.close()
    return {'audio_seconds': duration, 'cases': cases,
            'note': 'Measured locally with temporary model processes. Loading is separate; one recording does not establish general accuracy.'}
