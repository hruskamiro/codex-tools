"""Low-level, non-interactive Codex execution support."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


@dataclass
class ExecResult:
    command: list[str]
    returncode: int
    events: list[dict]
    event_parse_errors: list[dict]
    stderr: str
    duration_s: float


def build_command(
    *,
    codex_bin: str,
    model: str | None,
    reasoning_effort: str,
    schema_path: Path,
    response_path: Path,
) -> list[str]:
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"unsupported reasoning effort: {reasoning_effort}")
    command = [
        codex_bin,
        "exec",
    ]
    if model:
        command.extend(["--model", model])
    command.extend(
        [
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(response_path),
            "-",
        ]
    )
    return command


def run_exec(
    *,
    prompt: str,
    command: list[str],
    cwd: Path,
    events_path: Path,
    stderr_path: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    event_callback: Callable[[dict], None] | None = None,
) -> ExecResult:
    """Run Codex, persist its streams, and return parsed JSONL events."""
    started = time.monotonic()
    events: list[dict] = []
    parse_errors: list[dict] = []
    stderr_parts: list[str] = []

    with events_path.open("w", encoding="utf-8") as event_log, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_parts.append(line)
                stderr_log.write(line)
                stderr_log.flush()

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        timed_out = threading.Event()

        def kill_after_timeout() -> None:
            if process.poll() is None:
                timed_out.set()
                process.kill()

        timeout_timer = threading.Timer(timeout, kill_after_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(prompt)
            process.stdin.close()
            for line_number, line in enumerate(process.stdout, start=1):
                event_log.write(line)
                event_log.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(
                        {"line": line_number, "error": str(exc), "text": line.rstrip()}
                    )
                    continue
                events.append(event)
                if event_callback is not None:
                    event_callback(event)
            returncode = process.wait()
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(command, timeout)
        finally:
            timeout_timer.cancel()
            stderr_thread.join(timeout=2)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    return ExecResult(
        command=command,
        returncode=returncode,
        events=events,
        event_parse_errors=parse_errors,
        stderr="".join(stderr_parts),
        duration_s=time.monotonic() - started,
    )


def event_usage(events: list[dict]) -> dict | None:
    usage = None
    for event in events:
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
    return usage


def event_thread_id(events: list[dict]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started":
            return event.get("thread_id")
    return None


def tool_events(events: list[dict]) -> list[dict]:
    """Return model actions other than reasoning and the final message."""
    found = []
    allowed = {"reasoning", "agent_message"}
    for event in events:
        if event.get("type") not in {"item.started", "item.completed"}:
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type and item_type not in allowed:
            found.append({"event": event.get("type"), "item_type": item_type})
    return found
