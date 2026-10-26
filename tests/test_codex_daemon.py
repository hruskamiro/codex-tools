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
    def test_rpc_rejection_is_distinct_from_transport_failure(self) -> None:
        client = codex_daemon.DaemonClient()
        with patch.object(client, "_send_frame"), patch.object(
            client, "_receive_message", return_value={
                "id": 1, "error": {"code": -32600, "message": "turn is no longer active"},
            },
        ):
            with self.assertRaises(codex_daemon.DaemonRequestError):
                client.request("turn/steer", {})

    def test_steer_targets_the_original_active_turn(self) -> None:
        client = Mock()
        client.request.side_effect = [
            {"thread": {"turns": [{"id": "active", "status": "inProgress"}]}}, {},
        ]
        with patch.object(codex_daemon, "DaemonClient") as context:
            context.return_value.__enter__.return_value = client
            self.assertEqual(codex_daemon.steer_thread("thread-1", "Continue"), "steered")
        client.request.assert_called_with("turn/steer", {
            "threadId": "thread-1", "expectedTurnId": "active",
            "input": [{"type": "text", "text": "Continue"}],
        })
        self.assertEqual(client.request.call_count, 2)

    def test_steer_rejection_for_unchanged_or_unknown_turn_is_not_replayed(self) -> None:
        for latest in (
            {"status": {"type": "active"}, "turns": [{"id": "active", "status": "inProgress"}]},
            {"status": {"type": "active"}},
            {"status": {"type": "systemError"}},
            None,
        ):
            with self.subTest(latest=latest):
                rejection = codex_daemon.DaemonRequestError("input rejected")
                client = Mock()
                client.request.side_effect = [
                    {"thread": {"turns": [{"id": "active", "status": "inProgress"}]}},
                    rejection, {"thread": latest},
                ]
                with patch.object(codex_daemon, "DaemonClient") as context:
                    context.return_value.__enter__.return_value = client
                    with self.assertRaises(codex_daemon.DaemonRequestError) as raised:
                        codex_daemon.steer_thread("thread-1", "Continue")
                self.assertIs(raised.exception, rejection)

    def test_steer_transport_failure_never_triggers_fallback(self) -> None:
        for failure in (ConnectionError("reply lost"), TimeoutError("timed out"), RuntimeError("invalid frame")):
            with self.subTest(failure=failure):
                client = Mock()
                client.request.side_effect = [
                    {"thread": {"turns": [{"id": "active", "status": "inProgress"}]}}, failure,
                ]
                with patch.object(codex_daemon, "DaemonClient") as context:
                    context.return_value.__enter__.return_value = client
                    with self.assertRaises(type(failure)):
                        codex_daemon.steer_thread("thread-1", "Continue")
                self.assertEqual(client.request.call_count, 2)

    def test_start_queue_after_stop_targets_oldest_submission(self) -> None:
        client = Mock()
        client.request.side_effect = [
            {"thread": {"status": {"type": "idle"}}},
            {"data": [{"id": "oldest"}]}, {"turn": {"id": "new-turn"}},
        ]
        with patch.object(codex_daemon, "DaemonClient") as context:
            context.return_value.__enter__.return_value = client
            self.assertEqual(codex_daemon.start_queued_thread_if_idle("thread-1"), "started")
        client.request.assert_any_call("thread/queue/start", {
            "threadId": "thread-1", "queuedSubmissionId": "oldest",
        })

    def test_start_queue_never_interrupts_busy_or_blocked_agent(self) -> None:
        for status in ("active", "notLoaded", "systemError"):
            with self.subTest(status=status):
                client = Mock()
                client.request.return_value = {"thread": {"status": {"type": status}}}
                with patch.object(codex_daemon, "DaemonClient") as context:
                    context.return_value.__enter__.return_value = client
                    self.assertEqual(codex_daemon.start_queued_thread_if_idle("thread-1"), "busy")
                self.assertEqual(client.request.call_count, 1)

    def test_start_queue_is_noop_when_already_consumed(self) -> None:
        client = Mock()
        client.request.side_effect = [{"thread": {"status": {"type": "idle"}}}, {"data": []}]
        with patch.object(codex_daemon, "DaemonClient") as context:
            context.return_value.__enter__.return_value = client
            self.assertEqual(codex_daemon.start_queued_thread_if_idle("thread-1"), "empty")
        self.assertEqual(client.request.call_count, 2)

    def test_start_race_with_another_client_is_not_a_failure(self) -> None:
        client = Mock()
        client.request.side_effect = [
            {"thread": {"status": {"type": "idle"}}}, {"data": [{"id": "one"}]},
            RuntimeError("already running"), {"thread": {"status": {"type": "active"}}},
        ]
        with patch.object(codex_daemon, "DaemonClient") as context:
            context.return_value.__enter__.return_value = client
            self.assertEqual(codex_daemon.start_queued_thread_if_idle("thread-1"), "busy")

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
