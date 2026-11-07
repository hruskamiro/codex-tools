import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_tools import app_server, structured
from codex_tools.batch_backoff import RateLimitBackoff, rate_limit_reason

RATE_EVENT = {'method': 'error', 'params': {'willRetry': True, 'error': {
    'message': 'Reconnecting... 1/5',
    'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 429}}}}}


class BackoffTests(unittest.TestCase):
    def test_provider_errors_only_and_persistent_exponential_pause(self):
        self.assertEqual(rate_limit_reason(RATE_EVENT), 'HTTP 429')
        self.assertIsNone(rate_limit_reason({'error': 'Schema validation failed for annotation 429'}))
        self.assertIsNone(rate_limit_reason({'method': 'item/agentMessage/delta',
                                            'params': {'delta': 'The number 429.'}}))
        self.assertEqual(rate_limit_reason({'error': {'httpStatusCode': 503}}), 'HTTP 503')
        now = [1000.0]
        saved = []
        clock = lambda: now[0]
        limiter = RateLimitBackoff(2, 60, clock=clock, wall_clock=clock, save=saved.append)
        self.assertEqual(limiter.snapshot()['effective_jobs'], 2)
        limiter.event('gen-0', RATE_EVENT)
        self.assertEqual(limiter.snapshot()['pause_remaining_seconds'], 60)
        now[0] += 5
        limiter.event('gen-0', RATE_EVENT)
        self.assertEqual(len(limiter.snapshot()['events']), 1)
        self.assertEqual(limiter.snapshot()['pause_remaining_seconds'], 60)
        limiter.event('gen-1', RATE_EVENT)
        self.assertEqual(limiter.snapshot()['pause_remaining_seconds'], 120)
        for n in range(2, 8):
            limiter.event(f'gen-{n}', RATE_EVENT)
        self.assertEqual(limiter.snapshot()['pause_remaining_seconds'], 600)
        restored = RateLimitBackoff(2, 60, initial=saved[-1], clock=clock, wall_clock=clock)
        self.assertEqual(restored.snapshot()['effective_jobs'], 1)
        self.assertEqual(restored.snapshot()['pause_remaining_seconds'], 600)
        now[0] += 700
        self.assertEqual(restored.snapshot()['pause_remaining_seconds'], 0)
        self.assertEqual(restored.snapshot()['effective_jobs'], 1)
        for seconds in [0, -1, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                RateLimitBackoff(2, seconds)

    def test_halves_then_recovers_only_with_fresh_successes_and_healthy_time(self):
        now = [1000.0]
        clock = lambda: now[0]
        limiter = RateLimitBackoff(8, clock=clock, wall_clock=clock)
        old = limiter.begin()
        limiter.signal('limited', 'HTTP 429')
        self.assertEqual(limiter.snapshot()['effective_jobs'], 4)
        now[0] = 1060
        for i in range(3):
            limiter.success(f'old-{i}', old)
        self.assertEqual(limiter.snapshot()['recovery']['successes'], 0)
        for i in range(3):
            limiter.success(f'fresh-{i}', limiter.begin())
        now[0] = 1089
        self.assertEqual(limiter.snapshot()['effective_jobs'], 4)
        now[0] = 1090
        self.assertEqual(limiter.snapshot()['effective_jobs'], 5)
        now[0] += 1000
        self.assertEqual(limiter.snapshot()['effective_jobs'], 5)  # Idle time is insufficient.
        for level in (6, 7, 8):
            admissions = [limiter.begin() for _ in range(3)]
            now[0] += 30
            for i, token in enumerate(admissions):
                limiter.success(f'level-{level}-{i}', token)
            self.assertEqual(limiter.snapshot()['effective_jobs'], level)
        limiter.signal('new-limit', 'HTTP 429')
        state = limiter.snapshot()
        self.assertEqual(state['effective_jobs'], 4)
        self.assertEqual(state['pause_remaining_seconds'], 60)

    def test_repeated_errors_interrupt_recovery_without_repeatedly_cutting_one_task(self):
        now = [0.0]
        clock = lambda: now[0]
        limiter = RateLimitBackoff(8, clock=clock, wall_clock=clock)
        limiter.signal('a', 'HTTP 429')
        now[0] = 60
        token = limiter.begin()
        limiter.success('ok', token)
        limiter.signal('b', 'HTTP 429')
        self.assertEqual(limiter.snapshot()['effective_jobs'], 2)
        self.assertEqual(limiter.snapshot()['recovery']['successes'], 0)
        now[0] += 5
        limiter.signal('b', 'HTTP 429')
        self.assertEqual(limiter.snapshot()['effective_jobs'], 2)
        self.assertEqual(limiter.snapshot()['pause_remaining_seconds'], 120)
        limiter.success('too-late', token)
        self.assertEqual(limiter.snapshot()['recovery']['successes'], 0)

    def test_fixed_preserves_concurrency_and_outages_only_pause(self):
        for fixed in (False, True):
            with self.subTest(fixed=fixed):
                limiter = RateLimitBackoff(8, fixed=fixed)
                limiter.signal('timeout', 'request-timeout')
                limiter.signal('outage', 'HTTP 503')
                self.assertEqual(limiter.snapshot()['effective_jobs'], 8)
                self.assertGreater(limiter.snapshot()['pause_remaining_seconds'], 0)
                limiter.signal('limited', 'HTTP 429')
                self.assertEqual(limiter.snapshot()['effective_jobs'], 8 if fixed else 4)

    def test_single_job_is_a_hard_cap_and_policy_can_use_steps_or_factors(self):
        now = [0.0]
        clock = lambda: now[0]
        limiter = RateLimitBackoff(1, recovery_period=1, clock=clock, wall_clock=clock)
        limiter.signal('limit', 'HTTP 429')
        now[0] = 60
        admissions = [limiter.begin() for _ in range(3)]
        now[0] += 1
        for i, token in enumerate(admissions):
            limiter.success(str(i), token)
        self.assertEqual(limiter.snapshot()['effective_jobs'], 1)
        limiter = RateLimitBackoff(4, decrease='-1', increase='1.5x', recovery_period=1,
                                   clock=clock, wall_clock=clock)
        limiter.signal('limit', 'HTTP 429')
        self.assertEqual(limiter.snapshot()['effective_jobs'], 3)
        now[0] += 60
        admissions = [limiter.begin() for _ in range(3)]
        now[0] += 1
        for i, token in enumerate(admissions):
            limiter.success(str(i), token)
        self.assertEqual(limiter.snapshot()['effective_jobs'], 4)  # Multiplication is capped.
        for _ in range(6):
            limiter.signal(str(_), 'HTTP 429')
        self.assertEqual(limiter.snapshot()['effective_jobs'], 1)

    def test_restore_keeps_cooldown_but_needs_fresh_work_and_ids_are_per_batch(self):
        now = [0.0]
        clock = lambda: now[0]
        limiter = RateLimitBackoff(8, clock=clock, wall_clock=clock)
        limiter.signal('task-0', 'HTTP 429')
        saved = limiter.snapshot()
        now[0] = 10
        restored = RateLimitBackoff(8, initial=json.loads(json.dumps(saved)), clock=clock, wall_clock=clock)
        self.assertEqual(restored.snapshot()['effective_jobs'], 4)
        self.assertEqual(restored.snapshot()['pause_remaining_seconds'], 50)
        now[0] = 10000
        self.assertEqual(restored.snapshot()['effective_jobs'], 4)
        restored.signal('task-0', 'HTTP 429')  # A different batch may reuse task IDs.
        self.assertEqual(restored.snapshot()['effective_jobs'], 2)
        self.assertEqual(restored.snapshot()['pause_remaining_seconds'], 120)
        healthy = RateLimitBackoff(1).snapshot()
        self.assertEqual(RateLimitBackoff(8, initial=healthy).snapshot()['effective_jobs'], 8)
        self.assertEqual(RateLimitBackoff(1, initial=saved).snapshot()['effective_jobs'], 1)
        with self.assertRaisesRegex(ValueError, 'policy'):
            RateLimitBackoff(8, initial=saved, decrease='-1')
        legacy = dict(version=1, initial_seconds=60, limited_tasks=['old'], paused_until=None, events=[])
        self.assertEqual(RateLimitBackoff(8, initial=legacy).snapshot()['effective_jobs'], 1)

    def test_invalid_policy_is_rejected_before_creating_batch_artifacts(self):
        for kwargs in (dict(decrease='2x'), dict(decrease='-0'), dict(increase='+0'),
                       dict(increase='0.5x'), dict(recovery_period=float('nan'))):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RateLimitBackoff(4, **kwargs)
        args = structured.parse_args(['batch', 'tasks.json', '--batch-dir', 'runs'])
        self.assertEqual(args.jobs, 1)
        self.assertEqual(args.rate_limit_backoff, 60)
        self.assertFalse(args.fixed)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'batch'
            with self.assertRaisesRegex(ValueError, '--fixed'):
                structured.run_batch(manifest={}, manifest_path=Path(temporary)/'tasks.json',
                                     batch_dir=path, fixed=True, backoff_decrease='0.5x')
            self.assertFalse(path.exists())

    def test_live_rate_event_blocks_admission_without_cancelling_active_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'schema.json').write_text('{"type":"object"}')
            tasks = []
            for i in range(4):
                (root / f'{i}.txt').write_text(str(i))
                tasks.append(dict(id=f't{i}', prompt=f'{i}.txt', schema='schema.json'))
            manifest = dict(version=1, defaults=dict(model='test'), tasks=tasks)
            barrier = threading.Barrier(2)
            limited = threading.Event()
            lock = threading.Lock()
            starts, active_after_limit = {}, []
            active = 0
            signal_at = [None]

            def fake_task(**kwargs):
                nonlocal active
                i = int(kwargs['prompt'])
                with lock:
                    active += 1
                    starts[i] = time.monotonic()
                    if i >= 2:
                        active_after_limit.append(active)
                if i < 2:
                    barrier.wait(timeout=2)
                    if i == 0:
                        signal_at[0] = time.monotonic()
                        kwargs['on_event'](RATE_EVENT)
                        limited.set()
                    else:
                        self.assertTrue(limited.wait(timeout=2))
                    time.sleep(.01)
                directory = kwargs['run_dir']
                directory.mkdir(parents=True)
                structured.atomic_write_json(directory / 'run.json', {
                    'status': 'success', 'usage': {'input_tokens': 1}, 'duration_s': .01})
                with lock:
                    active -= 1

            with patch.object(structured.app_server, 'access_token', return_value='test'), \
                 patch.object(structured.app_server, 'rate_limits', return_value={}), \
                 patch.object(structured, 'run_task', side_effect=fake_task):
                result = structured.run_batch(manifest=manifest, manifest_path=root/'batch.json',
                    batch_dir=root/'runs', jobs=2, backend='app-server', rate_limit_backoff=.05,
                    backoff_state=root/'shared-backoff.json')
            self.assertEqual(result['counts']['success'], 4)
            self.assertEqual(result['usage']['input_tokens'], 4)
            self.assertEqual(result['backoff']['effective_jobs'], 1)
            self.assertTrue(all(n == 1 for n in active_after_limit))
            self.assertGreaterEqual(starts[2] - signal_at[0], .045)
            self.assertTrue((root/'shared-backoff.json').exists())

    def test_batch_recovers_slots_after_successful_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'schema.json').write_text('{"type":"object"}')
            tasks = []
            for i in range(7):
                (root/f'{i}.txt').write_text(str(i))
                tasks.append(dict(id=f't{i}', prompt=f'{i}.txt', schema='schema.json'))
            manifest = dict(version=1, defaults=dict(model='test'), tasks=tasks)
            now = [1000.0]
            clock = lambda: now[0]
            first = threading.Barrier(2)
            recovered = threading.Barrier(2)
            limited = threading.Event()
            def run_task(**kwargs):
                i = int(kwargs['prompt'])
                if i < 2:
                    first.wait(timeout=2)
                    if i == 0:
                        kwargs['on_event'](RATE_EVENT)
                        limited.set()
                    else:
                        self.assertTrue(limited.wait(2))
                elif i < 5:
                    now[0] += .02
                else:
                    recovered.wait(timeout=2)  # Recovery must reopen the second slot.
                kwargs['run_dir'].mkdir(parents=True)
                structured.atomic_write_json(kwargs['run_dir']/'run.json', dict(status='success'))
            def limiter(*args, **kwargs):
                return RateLimitBackoff(*args, **kwargs, clock=clock, wall_clock=clock)
            with patch.object(structured, 'RateLimitBackoff', side_effect=limiter), \
                 patch.object(structured.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0]+seconds)), \
                 patch.object(structured, 'run_task', side_effect=run_task):
                result = structured.run_batch(manifest=manifest, manifest_path=root/'batch.json',
                                              batch_dir=root/'runs', jobs=2, rate_limit_backoff=.05,
                                              backoff_recovery_period=.05)
            self.assertEqual(result['counts']['success'], 7)
            self.assertEqual(result['backoff']['effective_jobs'], 2)
            self.assertIn('recovery', [event['kind'] for event in result['backoff']['events']])

    def test_validation_error_quoting_http_status_does_not_trigger_provider_pause(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'schema.json').write_text('{"type":"object"}')
            (root/'prompt.txt').write_text('test')
            manifest = dict(version=1, defaults=dict(model='test'), tasks=[
                dict(id='a', prompt='prompt.txt', schema='schema.json')])
            from jsonschema import ValidationError
            with patch.object(structured, 'run_task', side_effect=ValidationError("'HTTP 429' is not valid")):
                result = structured.run_batch(manifest=manifest, manifest_path=root/'batch.json',
                                              batch_dir=root/'runs', jobs=4)
            self.assertEqual(result['counts']['failed'], 1)
            self.assertEqual(result['backoff']['incidents'], 0)
            self.assertEqual(result['backoff']['effective_jobs'], 4)

    def test_fixed_batch_keeps_slots_but_still_waits_after_provider_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'schema.json').write_text('{"type":"object"}')
            tasks = []
            for i in range(4):
                (root/f'{i}.txt').write_text(str(i))
                tasks.append(dict(id=f't{i}', prompt=f'{i}.txt', schema='schema.json'))
            manifest = dict(version=1, defaults=dict(model='test'), tasks=tasks)
            first = threading.Barrier(2)
            second = threading.Barrier(2)
            limited = threading.Event()
            signal_at = [None]
            starts = {}
            def run_task(**kwargs):
                i = int(kwargs['prompt'])
                starts[i] = time.monotonic()
                if i < 2:
                    first.wait(timeout=2)
                    if i == 0:
                        signal_at[0] = time.monotonic()
                        kwargs['on_event']({'type':'error', 'message':'HTTP 429'})
                        limited.set()
                    else:
                        self.assertTrue(limited.wait(2))
                else:
                    second.wait(timeout=2)  # Both fixed slots must remain usable.
                kwargs['run_dir'].mkdir(parents=True)
                structured.atomic_write_json(kwargs['run_dir']/'run.json', dict(status='success'))
            with patch.object(structured, 'run_task', side_effect=run_task):
                result = structured.run_batch(manifest=manifest, manifest_path=root/'batch.json',
                                              batch_dir=root/'runs', jobs=2, fixed=True, rate_limit_backoff=.05)
            self.assertEqual(result['counts']['success'], 4)
            self.assertEqual(result['backoff']['effective_jobs'], 2)
            self.assertTrue(result['backoff']['policy']['fixed'])
            self.assertGreaterEqual(min(starts[2], starts[3]) - signal_at[0], .045)

    def test_exec_backend_observes_provider_errors_during_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'home').mkdir()
            response = root/'run'/'response.txt'
            observed = []
            def run_exec(**kwargs):
                event = {'type':'error', 'message':'HTTP 429'}
                kwargs['event_callback'](event)
                self.assertEqual(observed, ['HTTP 429'])
                response.write_text('{"ok":true}')
                return structured.codex_exec.ExecResult(
                    command=[], returncode=0, events=[event], event_parse_errors=[], stderr='', duration_s=.01)
            with patch.object(structured.codex_exec, 'run_exec', side_effect=run_exec):
                result = structured.run_task(prompt='test', schema={'type':'object'}, run_dir=root/'run',
                                             model='test', default_home=root/'home',
                                             on_event=lambda event: observed.append(rate_limit_reason(event)))
            self.assertEqual(result, {'ok':True})

    def test_app_server_delivers_retry_event_while_turn_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'observed'
            fake = root / 'fake-codex'
            fake.write_text('#!' + sys.executable + '\n' + '''
import json, sys, time
from pathlib import Path
marker = Path(''' + repr(str(marker)) + ''')
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request=json.loads(line)
    method=request.get('method')
    if 'id' not in request:
        continue
    result={'thread': {'id': 'test-thread'}} if method=='thread/start' else {}
    send({'id': request['id'], 'result': result})
    if method=='turn/start':
        send(''' + repr(RATE_EVENT) + ''')
        deadline=time.monotonic()+2
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        if not marker.exists():
            sys.exit(3)
        send({'method':'item/agentMessage/delta','params':{'delta':'{"ok":true}'}})
        send({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
''')
            fake.chmod(0o700)
            seen = []
            def observe(event):
                seen.append(event)
                if rate_limit_reason(event):
                    marker.touch()
            result = app_server.run_structured_task(prompt='test', schema={'type':'object'},
                model='test', reasoning_effort='medium', timeout=5, access_token_value='test',
                codex_bin=str(fake), codex_home=root/'home', on_event=observe)
            self.assertTrue(marker.exists())
            self.assertEqual(result.response_text, '{"ok":true}')
            self.assertTrue(any(rate_limit_reason(e) for e in seen))


if __name__ == '__main__':
    unittest.main()
