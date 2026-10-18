from __future__ import annotations

import json
import struct
import unittest
from unittest.mock import Mock, patch

from codex_tools import codex_daemon


def server_frame(value: dict[str, object], opcode: int = 1) -> bytes:
    payload = json.dumps(value).encode("utf-8")
    if len(payload) < 126:
        header = bytes([0x80 | opcode, len(payload)])
    else:
        header = bytes([0x80 | opcode, 126]) + struct.pack("!H", len(payload))
    return header + payload


class CodexDaemonTests(unittest.TestCase):
    def test_receives_websocket_json_and_answers_ping(self) -> None:
        class FakeSocket:
            def __init__(self, incoming: bytes) -> None:
                self.incoming = incoming
                self.sent = b""

            def recv(self, size: int) -> bytes:
                result, self.incoming = self.incoming[:size], self.incoming[size:]
                return result

            def sendall(self, value: bytes) -> None:
                self.sent += value

        client_socket = FakeSocket(
            server_frame({"ping": True}, opcode=9)
            + server_frame({"id": 1, "result": {"ok": True}})
        )
        client = codex_daemon.DaemonClient()
        client.socket = client_socket

        message = client._receive_message()

        self.assertEqual(message["result"], {"ok": True})
        pong_header = client_socket.sent[:2]
        self.assertEqual(pong_header[0] & 0x0F, 10)
        self.assertTrue(pong_header[1] & 0x80)

    def test_loaded_thread_ids_follows_pagination(self) -> None:
        client = Mock()
        client.request.side_effect = [
            {"data": ["one", "two"], "nextCursor": "next"},
            {"data": ["three"], "nextCursor": None},
        ]

        result = codex_daemon.loaded_thread_ids(client)

        self.assertEqual(result, ["one", "two", "three"])
        self.assertEqual(client.request.call_count, 2)

    def test_interrupt_thread_targets_the_active_turn(self) -> None:
        client = Mock()
        client.request.side_effect = [
            {
                "thread": {
                    "turns": [
                        {"id": "done", "status": "completed"},
                        {"id": "active", "status": "inProgress"},
                    ]
                }
            },
            {},
        ]
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=None)

        with patch.object(codex_daemon, "DaemonClient", return_value=context):
            result = codex_daemon.interrupt_thread("thread-1")

        self.assertEqual(
            result, {"status": "interrupt_requested", "turn_id": "active"}
        )
        client.request.assert_any_call(
            "turn/interrupt", {"threadId": "thread-1", "turnId": "active"}
        )

    def test_interrupt_thread_is_a_noop_when_idle(self) -> None:
        client = Mock()
        client.request.return_value = {
            "thread": {"turns": [{"id": "done", "status": "completed"}]}
        }
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=None)

        with patch.object(codex_daemon, "DaemonClient", return_value=context):
            result = codex_daemon.interrupt_thread("thread-1")

        self.assertEqual(result, {"status": "idle", "turn_id": ""})
        self.assertEqual(client.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
