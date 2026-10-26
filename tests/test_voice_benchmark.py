"""Benchmark isolation and truthful GPU reporting without requiring a GPU in CI."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from codex_tools import voice, voice_benchmark


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.audio = self.root / 'speech.wav'
        with wave.open(str(self.audio), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0' * 1600)
        self.model = self.root / 'ggml-small.bin'
        self.model.write_bytes(b'lmgg' + b'x' * 100)
        self.settings = {'model': str(self.model), 'cpu': True, 'port': 8790}

    def engine(self, backend='Vulkan0'):
        engine = Mock()
        engine.process.poll.return_value = None
        engine.acceleration.return_value = {'backend': backend, 'device': backend, 'verified': True}
        return engine

    def test_benchmark_does_not_change_configuration_and_always_closes_owned_engine(self):
        settings = dict(self.settings)
        engine = self.engine()
        recognizer = Mock(return_value='Fixture transcript')
        with patch.object(voice, 'find_server', return_value='/fixture/server'), \
             patch.object(voice, 'resolve_model', return_value=str(self.model)), \
             patch.object(voice, 'engine_command', return_value=['fixture']), \
             patch.object(voice, 'ManagedEngine', return_value=engine), \
             patch.object(voice, 'backend_ready', return_value=True), \
             patch.object(voice_benchmark, 'WhisperServer', return_value=recognizer):
            result = voice_benchmark.benchmark(self.audio, self.root, settings, device='gpu', runs=2, warmup=1, language='en')
        self.assertEqual(settings, self.settings)
        self.assertEqual(len(result['cases']), 1)
        self.assertEqual(result['cases'][0]['transcript'], 'Fixture transcript')
        self.assertEqual(result['cases'][0]['backend'], 'Vulkan0')
        self.assertEqual(recognizer.call_count, 3)
        engine.close.assert_called_once()
        self.assertFalse((self.root / 'config.json').exists())

    def test_gpu_request_refuses_silent_cpu_fallback_and_closes_engine(self):
        engine = self.engine('CPU')
        with patch.object(voice, 'find_server', return_value='/fixture/server'), \
             patch.object(voice, 'resolve_model', return_value=str(self.model)), \
             patch.object(voice, 'engine_command', return_value=['fixture']), \
             patch.object(voice, 'ManagedEngine', return_value=engine), \
             patch.object(voice, 'backend_ready', return_value=True), \
             patch.object(voice_benchmark, 'WhisperServer') as recognize:
            with self.assertRaisesRegex(RuntimeError, 'no GPU backend'):
                voice_benchmark.benchmark(self.audio, self.root, self.settings, device='gpu')
            recognize.assert_not_called()
        engine.close.assert_called_once()

    def test_rejects_incomplete_audio_and_unbounded_runs_before_starting_engine(self):
        self.audio.write_bytes(b'not a wav' * 10)
        with patch.object(voice, 'ManagedEngine') as engine:
            with self.assertRaises(ValueError):
                voice_benchmark.benchmark(self.audio, self.root, self.settings)
            with self.assertRaises(ValueError):
                voice_benchmark.benchmark(self.audio, self.root, self.settings, runs=100)
            engine.assert_not_called()


if __name__ == '__main__':
    unittest.main()
