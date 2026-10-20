"""Keep a private SSH relay connection alive while the local UI works offline."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from codex_tools import paths, relay_cache, relay_transport


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("peer", help="Already-paired localhost relay peer.")
    parser.add_argument("--ssh", metavar="HOST", help="SSH host/alias; defaults to the peer name.")
    parser.add_argument("--remote-port", type=int, default=8780, help="Remote localhost relay port (8780).")
    parser.add_argument("--auto-start", action="store_true", help="Ensure the remote localhost relay is running through SSH.")
    parser.add_argument("--remote-bin", default="ct", metavar="PATH", help="Remote codex-tools executable (prefer an absolute path).")
    parser.add_argument("--ui", choices=("console", "viewer", "none"), default="console")
    parser.add_argument("--config-file", type=Path, default=paths.RELAY_CONFIG_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--cache-file", type=Path, default=relay_cache.DEFAULT_CACHE_PATH, help=argparse.SUPPRESS)


def ssh_command(host: str, local_port: int, remote_port: int) -> list[str]:
    if not host or host.startswith("-") or any(character.isspace() for character in host):
        raise ValueError("use an SSH host alias or user@host, without options or whitespace")
    if not all(1 <= port <= 65535 for port in (local_port, remote_port)):
        raise ValueError("relay ports must be between 1 and 65535")
    return [
        "ssh", "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", host,
    ]


def retry_delay(failures: int) -> float:
    """Bound retry traffic, including after laptop sleep or route flapping."""
    base = min(30.0, 2.0 ** min(max(0, failures), 5))
    return min(30.0, base * random.uniform(0.8, 1.2))


def bootstrap_command(host: str, remote_port: int, executable: str) -> list[str]:
    if (not isinstance(executable, str) or not executable.strip() or executable.startswith("-")
            or any(ord(character) < 32 for character in executable)):
        raise ValueError("remote executable must be a command name or absolute path, without control characters")
    # Reuse validated SSH authentication/keepalive options, without forwarding
    # or -N: this short-lived connection runs exactly one quoted remote command.
    tunnel = ssh_command(host, 1, remote_port)
    return [*tunnel[:1], *tunnel[2:tunnel.index("-L")], host,
            shlex.join([executable, "relay", "ensure", "--port", str(remote_port), "--json"])]


def require_free_port(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(
            f"localhost port {port} is already in use; stop its existing tunnel "
            "or pair this peer with another localhost port"
        ) from exc


def interrupted(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


class TunnelSupervisor:
    def __init__(self, command: list[str], peer: dict, cache: relay_cache.RelayCache, directory: Path,
                 *, interval: float = 10.0, announce: bool = False,
                 bootstrap: list[str] | None = None) -> None:
        self.command = command
        self.peer = peer
        self.cache = cache
        self.directory = directory
        self.interval = interval
        self.announce = announce
        self.bootstrap = bootstrap
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._message = ""
        self.thread = threading.Thread(target=self.run, name="relay-ssh", daemon=True)

    def status(self, message: str) -> None:
        if message == self._message:
            return
        self._message = message
        paths.write_private_text(self.directory / "status.json", json.dumps({
            "pid": os.getpid(), "message": message, "updated_at": time.time(),
        }) + "\n")
        if self.announce:
            print(message, file=sys.stderr, flush=True)

    def close(self) -> None:
        self.stop.set()
        self.connected.clear()
        self._stop_process()
        if self.thread.ident is not None:
            self.thread.join(timeout=0.5)
        try:
            self.status("Connection stopped; cached content remains available.")
        except OSError:
            pass

    def _stop_process(self) -> None:
        with self._process_lock:
            process = self.process
            self.process = None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def synchronize(self) -> str:
        payload = relay_transport.fetch_snapshot(self.peer, timeout=20)
        relay_transport.apply_snapshot(self.cache, payload, base_url=self.peer["url"])
        host_id = str(payload["host"]["id"])
        relay_transport.deliver_pending(self.cache, self.peer, host_id)
        return host_id

    def ensure_remote(self) -> None:
        self.status("Checking remote relay through SSH; cached content remains available.")
        with self._process_lock:
            if self.stop.is_set():
                return
            self.process = subprocess.Popen(
                self.bootstrap, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            process = self.process
        try:
            output, errors = process.communicate(timeout=45)
        except subprocess.TimeoutExpired as exc:
            self._stop_process()
            raise RuntimeError("remote relay check timed out; it will retry without replacing remote processes") from exc
        if self.stop.is_set():
            return
        if process.returncode:
            detail = errors.decode("utf-8", errors="replace").strip()[-2000:]
            raise RuntimeError(f"remote relay startup check failed: {detail or process.returncode}")
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("remote relay ensure returned an invalid response; check the remote executable/version") from exc
        if (not isinstance(result, dict) or result.get("service") != "codex-tools-relay"
                or result.get("status") not in {"running", "started"}):
            raise RuntimeError("remote relay ensure did not confirm a healthy relay")
        self.status(f"Remote relay {result['status']}; opening the SSH tunnel.")

    def monitor(self, process: subprocess.Popen) -> None:
        host_id = None
        failures = 0
        sync_failures = 0
        next_sync = 0.0
        ever_healthy = False
        startup_deadline = time.monotonic() + 15
        while not self.stop.is_set() and process.poll() is None:
            try:
                health = relay_transport.fetch_json(
                    self.peer, self.peer["url"].rstrip("/") + "/v1/health", 5,
                )
                if health.get("service") != "codex-tools-relay":
                    raise ValueError("tunnel endpoint is not a codex-tools relay")
                failures = 0
            except (OSError, ValueError, RuntimeError) as exc:
                if not ever_healthy and time.monotonic() < startup_deadline:
                    self.stop.wait(min(0.5, self.interval))
                    continue
                failures += 1
                self.connected.clear()
                self.status(f"Connection interrupted; cached content remains available. {exc}")
                if failures >= 3:
                    return
            else:
                ever_healthy = True
                try:
                    if host_id is None and time.monotonic() >= next_sync:
                        host_id = self.synchronize()
                    if host_id is not None:
                        if self.stop.is_set():
                            return
                        if self.cache.pending(host_id):
                            relay_transport.deliver_pending(self.cache, self.peer, host_id)
                        self.connected.set()
                        self.status("Connected and synchronized; the UI handles incremental conversation updates.")
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    if self.stop.is_set():
                        return
                    self.connected.clear()
                    next_sync = time.monotonic() + retry_delay(sync_failures)
                    sync_failures += 1
                    self.status(f"SSH connected; synchronization will retry. {exc}")
            self.stop.wait(self.interval)

    def run(self) -> None:
        failures = 0
        while not self.stop.is_set():
            started = time.monotonic()
            last_error = ""
            try:
                if self.bootstrap is not None:
                    self.ensure_remote()
                self.status("Connecting through SSH; cached content remains available.")
                log_path = self.directory / "ssh.log"
                if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
                    log_path.replace(self.directory / "ssh.previous.log")
                with log_path.open("ab") as log:
                    log_path.chmod(0o600)
                    with self._process_lock:
                        if self.stop.is_set():
                            return
                        self.process = subprocess.Popen(
                            self.command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True,
                        )
                        process = self.process
                    self.monitor(process)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                last_error = str(exc)
                if not self.stop.is_set():
                    self.status(f"Connection unavailable: {exc}")
            finally:
                was_connected = self.connected.is_set()
                self.connected.clear()
                self._stop_process()
            if not self.stop.is_set():
                if was_connected and time.monotonic() - started >= 30:
                    failures = 0
                delay = retry_delay(failures)
                failures += 1
                detail = f" {last_error}" if last_error else ""
                self.status(f"SSH disconnected; retrying in {delay:.0f}s.{detail} Details: {self.directory / 'ssh.log'}")
                self.stop.wait(delay)


def open_cached_viewer(cache: relay_cache.RelayCache, peer: dict) -> bool:
    from codex_tools import agents_tui
    for host in cache.hosts():
        if str(host.get("base_url") or "").rstrip("/") == peer["url"].rstrip("/"):
            agents = cache.agents(str(host["host_id"]))
            if agents:
                agents_tui.open_agent_viewer(agents_tui.AgentItem(
                    str(host["host_id"]), str(host["name"]), agents[0], peer["url"],
                ))
                return True
    return False


def command_connect(args: argparse.Namespace) -> int:
    config = relay_transport.load_config(args.config_file)
    peer = config["peers"].get(args.peer)
    if not isinstance(peer, dict):
        raise ValueError(f"pair the peer first: ct relay pair {args.peer} http://127.0.0.1:18780")
    url = urlparse(peer["url"])
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"}
            or url.path not in {"", "/"} or url.query or url.fragment
            or url.username is not None or url.password is not None):
        raise ValueError("SSH connection requires a peer URL such as http://127.0.0.1:18780")
    port = url.port or 80
    command = ssh_command(args.ssh or args.peer, port, args.remote_port)
    bootstrap = (bootstrap_command(args.ssh or args.peer, args.remote_port, args.remote_bin)
                 if getattr(args, "auto_start", False) else None)
    identity = hashlib.sha256(f"{args.peer}:{port}".encode()).hexdigest()[:20]
    directory = paths.user_state_dir() / "relay-ssh" / identity
    paths.ensure_private_dir(directory)
    with (directory / "lock").open("a") as lock:
        (directory / "lock").chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("this peer already has a running connection supervisor") from exc
        require_free_port(port)
        cache = relay_cache.RelayCache(args.cache_file)
        supervisor = TunnelSupervisor(command, peer, cache, directory, announce=args.ui != "console", bootstrap=bootstrap)
        print(f"SSH relay: {args.ssh or args.peer} → localhost:{port}. State/logs: {directory}", file=sys.stderr)
        print("Cached content works offline. SSH host keys and key authentication must be configured beforehand.", file=sys.stderr)
        ui_process = None
        old_handlers = {}
        try:
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGHUP):
                    old_handlers[signum] = signal.signal(signum, interrupted)
            supervisor.thread.start()
            if args.ui == "console":
                ui_process = subprocess.Popen([
                    sys.executable, "-m", "codex_tools.cli", "agents", "ui", "--no-local",
                    "--cache-file", str(args.cache_file), "--relay-config", str(args.config_file),
                ])
                return ui_process.wait()
            opened = False
            while not supervisor.stop.wait(0.5):
                if not supervisor.thread.is_alive():
                    raise RuntimeError(f"connection supervisor stopped; inspect {directory}")
                if args.ui == "viewer" and not opened:
                    try:
                        opened = open_cached_viewer(cache, peer)
                    except (OSError, RuntimeError, ValueError) as exc:
                        print(f"Viewer: {exc}", file=sys.stderr)
                        supervisor.stop.wait(5)
        except KeyboardInterrupt:
            return 130
        finally:
            try:
                if ui_process is not None and ui_process.poll() is None:
                    ui_process.terminate()
                    try:
                        ui_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        ui_process.kill()
                        ui_process.wait(timeout=2)
            finally:
                supervisor.close()
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    try:
        return command_connect(parser.parse_args())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
