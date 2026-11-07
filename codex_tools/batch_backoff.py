"""Adaptive admission pacing; running tasks are never cancelled or replayed."""
from __future__ import annotations

import json
import math
import re
import threading
import time


def rate_limit_reason(event: dict) -> str | None:
    """Recognize provider errors, never numbers in ordinary model output."""
    if not isinstance(event, dict):
        return None
    if event.get("method") == "error":
        params = event.get("params")
        error = params.get("error") if isinstance(params, dict) else None
    elif event.get("method") == "turn/completed":
        params = event.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        error = turn.get("error") if isinstance(turn, dict) else None
    else:
        error = event.get("error")
        if not error and event.get("type") in {"error", "turn.failed"}:
            error = event.get("message") or event.get("error")
    if not error:
        return None
    pending = [error]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"httpStatusCode", "statusCode", "status_code", "http_status"} and str(item) in {"429", "502", "503", "504"}:
                    return "HTTP " + str(item)
                if isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    text = json.dumps(error).lower()
    match = re.search(r"(?:http(?:\s+status)?|status(?:\s+code)?)[\s:=]*(429|502|503|504)\b", text)
    if match:
        return "HTTP " + match.group(1)
    compact = re.sub(r"[^a-z]", "", text)
    for name in ("ratelimit", "toomanyrequests", "serveroverloaded"):
        if name in compact:
            return name
    return None


def adjustment(value: str, *, decrease: bool) -> tuple[str, float]:
    """Accept a factor (0.5x/1.25x) or an integer step (-1/+1)."""
    if not isinstance(value, str):
        raise ValueError("backoff adjustment must be a factor or signed integer step")
    value = value.strip()
    if re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)x", value):
        number = float(value[:-1])
        if math.isfinite(number) and ((0 < number < 1) if decrease else number > 1):
            return "factor", number
    if re.fullmatch(r"-\d+" if decrease else r"\+\d+", value):
        number = int(value)
        if number != 0:
            return "step", number
    examples = "0.5x or -1" if decrease else "+1 or 1.25x"
    raise ValueError(f"invalid backoff {'decrease' if decrease else 'increase'}: use {examples}")


class RateLimitBackoff:
    REQUIRED_SUCCESSES = 3

    def __init__(self, jobs, seconds=60.0, *, fixed=False, decrease="0.5x",
                 increase="+1", recovery_period=30.0, initial=None, save=None,
                 clock=time.monotonic, wall_clock=time.time):
        if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
            raise ValueError("--jobs must be a positive integer")
        if not math.isfinite(seconds) or not 0 < seconds <= 600:
            raise ValueError("rate-limit backoff must be between 0 and 600 seconds (exclusive of 0)")
        if not math.isfinite(recovery_period) or recovery_period <= 0:
            raise ValueError("backoff recovery period must be finite and positive")
        self._decrease = adjustment(decrease, decrease=True)
        self._increase = adjustment(increase, decrease=False)
        self.jobs, self.seconds = jobs, seconds
        self.fixed, self.recovery_period = fixed, recovery_period
        self._policy = dict(initial_seconds=seconds, fixed=fixed, decrease=decrease,
                            increase=increase, recovery_period=recovery_period)
        self._clock, self._wall_clock, self._save = clock, wall_clock, save
        self._lock = threading.Lock()
        # Task IDs are invocation-local: another batch can reuse the same names.
        self._seen = set()
        self._reduced = set()
        self._generation = 0
        self._healthy_since = None
        self._successes = 0
        self._current = jobs
        self._incidents = 0
        self._events = []
        self._deadline = clock()
        if initial is not None:
            self._restore(initial)

    def _restore(self, initial):
        if not isinstance(initial, dict) or initial.get("version") not in (1, 2):
            raise ValueError("invalid backoff state")
        if initial.get("version") == 2:
            if initial.get("policy") != self._policy:
                raise ValueError("backoff state does not match the configured policy; use a new --backoff-state file")
            current = initial.get("effective_jobs")
            incidents = initial.get("incidents")
        else:
            if initial.get("initial_seconds") != self.seconds:
                raise ValueError("backoff state does not match the configured cooldown")
            incidents = len(initial.get("limited_tasks", []))
            current = 1 if incidents else self.jobs
        if (not isinstance(current, int) or isinstance(current, bool) or current < 1
                or not isinstance(incidents, int) or isinstance(incidents, bool) or incidents < 0):
            raise ValueError("invalid backoff concurrency or incident count")
        paused_until = initial.get("paused_until")
        if paused_until is not None and (not isinstance(paused_until, (float, int)) or not math.isfinite(paused_until)):
            raise ValueError("invalid backoff cooldown timestamp")
        self._current = self.jobs if self.fixed or incidents == 0 else min(self.jobs, current)
        self._incidents = incidents
        self._events = list(initial.get("events", []))[-100:]
        self._deadline = self._clock() + max(0, (paused_until or 0) - self._wall_clock())
        # Recovery needs fresh successful work; idle time between batches does
        # not prove that the provider can sustain a higher request rate.

    def _snapshot(self):
        remaining = max(0, self._deadline - self._clock())
        observed = 0 if self._healthy_since is None else max(0, self._clock() - self._healthy_since)
        return dict(version=2, policy=dict(self._policy), initial_seconds=self.seconds,
                    maximum_jobs=self.jobs, effective_jobs=self._current, incidents=self._incidents,
                    paused_until=self._wall_clock() + remaining if remaining else None,
                    pause_remaining_seconds=remaining, limited_tasks=sorted(self._seen),
                    recovery=dict(successes=self._successes, required_successes=self.REQUIRED_SUCCESSES,
                                  healthy_seconds=observed, period_seconds=self.recovery_period),
                    events=list(self._events))

    def _persist(self):
        if self._save:
            self._save(self._snapshot())

    def _record(self, **event):
        self._events.append(dict(at=self._wall_clock(), **event))
        self._events = self._events[-100:]

    def _recover(self):
        now = self._clock()
        if (self._incidents == 0 or self._healthy_since is None or now < self._deadline
                or now - self._healthy_since < self.recovery_period
                or self._successes < self.REQUIRED_SUCCESSES):
            return
        previous = self._current
        if not self.fixed and self._current < self.jobs:
            kind, value = self._increase
            target = (
                self.jobs if value >= self.jobs / self._current else math.ceil(self._current * value)
            ) if kind == "factor" else self._current + int(value)
            self._current = min(self.jobs, max(self._current + 1, target))
        self._incidents = 0 if self._current == self.jobs else 1
        self._generation += 1
        self._successes = 0
        self._healthy_since = None
        self._record(kind="recovery", previous_jobs=previous, effective_jobs=self._current)
        self._persist()

    def snapshot(self):
        with self._lock:
            self._recover()
            return self._snapshot()

    def begin(self):
        with self._lock:
            now = self._clock()
            if now >= self._deadline and self._incidents and self._healthy_since is None:
                self._healthy_since = now
            return self._generation, now

    def success(self, task_id, admission):
        with self._lock:
            generation, started = admission
            if (task_id not in self._seen and generation == self._generation
                    and started >= self._deadline and self._healthy_since is not None):
                self._successes += 1
                self._recover()
                self._persist()

    def signal(self, task_id, reason):
        with self._lock:
            fresh = task_id not in self._seen
            self._seen.add(task_id)
            new_incident = fresh or self._incidents == 0
            if new_incident:
                self._incidents += 1
            previous = self._current
            # A single timeout/outage pauses admissions without assuming that
            # reducing concurrency can repair a failed service.
            reduce = reason not in {"request-timeout", "HTTP 502", "HTTP 503", "HTTP 504"}
            if not self.fixed and reduce and task_id not in self._reduced:
                kind, value = self._decrease
                target = math.floor(self._current * value) if kind == "factor" else self._current + int(value)
                self._current = max(1, min(self._current - 1, target))
                self._reduced.add(task_id)
            delay = min(600, self.seconds * 2 ** min(max(0, self._incidents - 1), 20))
            self._deadline = max(self._deadline, self._clock() + delay)
            self._generation += 1
            self._healthy_since = None
            self._successes = 0
            if new_incident or previous != self._current:
                self._record(kind="backoff", task_id=task_id, reason=reason,
                             pause_seconds=delay, previous_jobs=previous, effective_jobs=self._current)
            self._persist()

    def event(self, task_id, event):
        reason = rate_limit_reason(event)
        if reason:
            self.signal(task_id, reason)
