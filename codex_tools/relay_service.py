"""Idempotently start a private relay without tying its lifetime to SSH."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from codex_tools import paths, relay_transport


class AutoStartDisabled(PermissionError):
    """The destination has not authorized unattended relay startup."""


def configure(args: argparse.Namespace) -> int:
    config = relay_transport.load_config(args.config_file)
    if args.allow_auto_start is not None:
        config['allow_auto_start'] = args.allow_auto_start
        relay_transport.save_config(args.config_file, config)
    allowed = config.get('allow_auto_start', False)
    if args.json:
        print(json.dumps({'allow_auto_start': allowed}))
    else:
        print(f"Relay automatic startup: {'allowed' if allowed else 'disabled'}")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config-file", type=Path, default=paths.RELAY_CONFIG_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--cache-file", type=Path, default=paths.RELAY_CACHE_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--control-socket", type=Path, default=None, help=argparse.SUPPRESS)


def healthy(port: int) -> bool:
    """Only a refused TCP connection means absent; all other failures are errors."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return False
        raise RuntimeError(f"cannot check localhost:{port}; no relay was replaced: {exc}") from exc
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=3) as response:
            payload = json.loads(response.read(65536))
        if (not isinstance(payload, dict) or payload.get("service") != "codex-tools-relay"
                or payload.get("protocol") != relay_transport.PROTOCOL_VERSION):
            raise ValueError("not a compatible codex-tools relay")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"localhost:{port} is occupied or unhealthy; leaving it untouched: {exc}") from exc
    return True


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ensure(port: int, config_file: Path, cache_file: Path, *, timeout: float = 20.0,
           control_socket: Path | None = None) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError("relay port must be between 1 and 65535")
    # Never create or rotate identities/pairing keys as a side effect of connect.
    relay_transport.load_config(config_file)
    directory = paths.user_state_dir() / "relay-serve" / str(port)
    paths.ensure_private_dir(directory)
    with (directory / "lock").open("a") as lock:
        (directory / "lock").chmod(0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("another relay startup is still in progress; try again shortly")
                time.sleep(0.1)
        result = {"service": "codex-tools-relay", "url": f"http://127.0.0.1:{port}"}
        if healthy(port):
            return {**result, "status": "running"}
        # Permission may have been revoked while waiting for the startup lock.
        config = relay_transport.load_config(config_file)
        if config.get('allow_auto_start', False) is not True:
            raise AutoStartDisabled(
                'Relay automatic startup is disabled on this destination. '
                'Run ct relay configure --allow-auto-start there to allow it, '
                'or start the relay manually with ct relay serve.'
            )
        process_file = directory / "process.json"
        if process_file.exists():
            previous = json.loads(process_file.read_text())
            pid = previous.get("pid") if isinstance(previous, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise RuntimeError(f"invalid relay startup metadata; inspect {process_file}")
            if process_alive(pid):
                raise RuntimeError(f"relay startup process {pid} exists but is not healthy; inspect {directory / 'serve.log'}")
        log_path = directory / "serve.log"
        if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
            log_path.replace(directory / "serve.previous.log")
        command = [
            sys.executable, "-m", "codex_tools.relay", "serve",
            "--host", "127.0.0.1", "--port", str(port),
            "--config-file", str(config_file.expanduser().resolve()),
            "--cache-file", str(cache_file.expanduser().resolve()),
        ]
        if control_socket is not None:
            command.extend(["--control-socket", str(control_socket.expanduser().resolve())])
        with log_path.open("ab") as log:
            log_path.chmod(0o600)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        temporary = directory / "process.json.tmp"
        paths.write_private_text(temporary, json.dumps({"pid": process.pid}) + "\n")
        temporary.replace(process_file)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"relay startup exited ({process.returncode}); inspect {log_path}")
            if healthy(port):
                return {**result, "status": "started", "pid": process.pid, "log": str(log_path)}
            time.sleep(0.1)
        # Leave a slow startup alone. Its PID prevents a later ensure spawning a
        # duplicate, and a subsequent health check can reuse it once ready.
        raise RuntimeError(f"relay startup is not healthy yet; leaving PID {process.pid} running; inspect {log_path}")


def command(args: argparse.Namespace) -> int:
    try:
        result = ensure(args.port, args.config_file, args.cache_file, control_socket=args.control_socket)
    except AutoStartDisabled as exc:
        if args.json:
            print(json.dumps({'service': 'codex-tools-relay', 'status': 'auto_start_disabled', 'error': str(exc)}))
        else:
            print(f'error: {exc}', file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Relay {result['status']}: {result['url']}")
    return 0
