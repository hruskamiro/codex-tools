"""Read-only client for the managed local Codex app-server daemon."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

from codex_tools import __version__


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def default_control_socket() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return codex_home / "app-server-control" / "app-server-control.sock"


class DaemonRequestError(RuntimeError):
    """An explicit RPC rejection, rather than an unknown transport outcome."""


class DaemonClient:
    def __init__(self, path: Path | None = None, timeout: float = 3.0) -> None:
        self.path = (path or default_control_socket()).expanduser()
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.request_id = 0

    def __enter__(self) -> "DaemonClient":
        opened = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        opened.settimeout(self.timeout)
        try:
            opened.connect(str(self.path))
            self.socket = opened
            self._handshake()
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "Codex Tools",
                        "title": "Codex Tools",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized", {})
        except Exception:
            opened.close()
            self.socket = None
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def _handshake(self) -> None:
        assert self.socket is not None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /rpc HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(4096)
            if len(response) > 64 * 1024:
                raise RuntimeError("Codex daemon returned an oversized handshake")
        header, _ = response.split(b"\r\n\r\n", 1)
        lines = header.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise RuntimeError(f"Codex daemon rejected WebSocket connection: {lines[0]}")
        headers = {
            name.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for name, value in [line.split(":", 1)]
        }
        expected = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("Codex daemon returned an invalid WebSocket handshake")

    def _exact(self, size: int) -> bytes:
        assert self.socket is not None
        result = b""
        while len(result) < size:
            chunk = self.socket.recv(size - len(result))
            if not chunk:
                raise ConnectionError("Codex daemon closed the connection")
            result += chunk
        return result

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        assert self.socket is not None
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _receive_message(self) -> dict[str, Any]:
        while True:
            first, second = self._exact(2)
            opcode = first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exact(8))[0]
            mask = self._exact(4) if second & 0x80 else b""
            payload = self._exact(size)
            if mask:
                payload = bytes(
                    value ^ mask[index % 4]
                    for index, value in enumerate(payload)
                )
            if opcode == 8:
                raise ConnectionError("Codex daemon closed the WebSocket")
            if opcode == 9:
                self._send_frame(payload, opcode=10)
                continue
            if opcode not in {1, 2}:
                continue
            value = json.loads(payload)
            if isinstance(value, dict):
                return value

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_frame(
            json.dumps(
                {"method": method, "params": params}, separators=(",", ":")
            ).encode("utf-8")
        )

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        self._send_frame(
            json.dumps(
                {"method": method, "id": request_id, "params": params},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        while True:
            message = self._receive_message()
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise DaemonRequestError(f"Codex daemon {method} failed: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}


def loaded_thread_ids(client: DaemonClient) -> list[str]:
    result: list[str] = []
    cursor: str | None = None
    while True:
        response = client.request(
            "thread/loaded/list", {"limit": 100, "cursor": cursor}
        )
        result.extend(str(value) for value in response.get("data", []))
        cursor = response.get("nextCursor")
        if not cursor:
            return result


def live_threads(path: Path | None = None) -> list[dict[str, Any]]:
    with DaemonClient(path) as client:
        threads = []
        for thread_id in loaded_thread_ids(client):
            response = client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = response.get("thread")
            if isinstance(thread, dict):
                threads.append(thread)
    threads.sort(key=lambda item: int(item.get("updatedAt") or 0), reverse=True)
    return threads


def interrupt_thread(thread_id: str, path: Path | None = None) -> dict[str, str]:
    """Interrupt the active turn in a loaded thread, if there is one."""
    with DaemonClient(path) as client:
        response = client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("Codex daemon returned an invalid thread")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            turns = []
        active = next(
            (
                turn
                for turn in reversed(turns)
                if isinstance(turn, dict)
                and turn.get("status") == "inProgress"
                and isinstance(turn.get("id"), str)
            ),
            None,
        )
        if active is None:
            return {"status": "idle", "turn_id": ""}
        turn_id = str(active["id"])
        client.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        return {"status": "interrupt_requested", "turn_id": turn_id}


def steer_thread(thread_id: str, message: str, path: Path | None = None) -> str:
    """Send input to the current turn, guarded by its active turn ID."""
    with DaemonClient(path) as client:
        response = client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("Codex daemon returned an invalid thread")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            turns = []
        active = next(
            (turn for turn in reversed(turns)
             if isinstance(turn, dict) and turn.get("status") == "inProgress"
             and isinstance(turn.get("id"), str)),
            None,
        )
        if active is None:
            return "idle"
        try:
            client.request("turn/steer", {
                "threadId": thread_id,
                "expectedTurnId": active["id"],
                "input": [{"type": "text", "text": message}],
            })
        except DaemonRequestError:
            # Only an explicit rejection proves the input was not accepted.
            # If its target finished, let the caller queue it without steering
            # a different turn. Transport failures must not trigger a replay.
            latest = client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            ).get("thread")
            if isinstance(latest, dict):
                status = latest.get("status")
                status = status.get("type") if isinstance(status, dict) else None
                turns = latest.get("turns")
                active_ids = {
                    turn["id"] for turn in turns
                    if isinstance(turn, dict) and turn.get("status") == "inProgress"
                    and isinstance(turn.get("id"), str)
                } if isinstance(turns, list) else set()
                if status == "idle" or (
                    status == "active" and active_ids and active["id"] not in active_ids
                ):
                    return "idle"
            raise
        return "steered"


def start_queued_thread_if_idle(thread_id: str, path: Path | None = None) -> str:
    """Wake an idle queue without interrupting a turn or bypassing older input."""
    with DaemonClient(path) as client:
        response = client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("Codex daemon returned an invalid thread")
        status = thread.get("status") or {}
        if not isinstance(status, dict) or status.get("type") != "idle":
            return "busy"
        queued = client.request("thread/queue/list", {"threadId": thread_id, "limit": 1})
        submissions = queued.get("data") or []
        if not isinstance(submissions, list):
            raise RuntimeError("Codex daemon returned an invalid queue")
        if not submissions:
            return "empty"
        submission_id = submissions[0].get("id") if isinstance(submissions[0], dict) else None
        if not isinstance(submission_id, str) or not submission_id:
            raise RuntimeError("Codex daemon returned an invalid queued submission")
        try:
            client.request("thread/queue/start", {
                "threadId": thread_id, "queuedSubmissionId": submission_id,
            })
        except RuntimeError:
            # Another client may have started work between the read and start.
            latest = client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            latest_thread = latest.get("thread") or {}
            latest_status = latest_thread.get("status") if isinstance(latest_thread, dict) else None
            if isinstance(latest_status, dict) and latest_status.get("type") == "active":
                return "busy"
            raise
        return "started"
