from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import agents_ui, relay_cache


def cached_agent() -> relay_cache.CachedAgent:
    return relay_cache.CachedAgent(
        agent_id="agent-1", session_id="session-1", thread_id="thread-1",
        rollout_id="", title="Viewer work", title_source="index",
        cwd="/work", source="cli", created_at="2026-10-07T10:00:00Z",
        updated_at="2026-10-07T11:00:00Z", origin_path="/tmp/session.jsonl",
        records=(relay_cache.CachedRecord("2026-10-07T10:00:00Z", "user", "Hello", 1, "message"),),
    )


class AgentUiTests(unittest.TestCase):
    def test_enter_preserves_start_intent_in_remote_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.replace_host_snapshot("home", "Home", [cached_agent()])
            agents_ui.send_message(cache, {"host": "home", "agent": "agent-1",
                                          "message": "Continue", "start": True}, "codex")
            self.assertEqual(cache.pending("home")[0]["payload"], {"text": "Continue", "start": True})

    def test_enter_wakes_queue_but_tab_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.replace_host_snapshot("local", "Local", [cached_agent()])
            with patch.object(agents_ui.subprocess, "run") as run, patch.object(
                agents_ui.codex_daemon, "start_queued_thread_if_idle"
            ) as start:
                run.return_value.returncode = 0
                for intent in (False, True):
                    agents_ui.send_message(cache, {"host": "local", "agent": "agent-1",
                        "message": "Continue", "start": intent}, "codex", control_socket=Path("/control"))
                self.assertEqual(run.call_count, 2)
                start.assert_called_once_with("thread-1", Path("/control"))

    def test_start_failure_does_not_report_send_failure_or_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.replace_host_snapshot("local", "Local", [cached_agent()])
            with patch.object(agents_ui.subprocess, "run") as run, patch.object(
                agents_ui.codex_daemon, "start_queued_thread_if_idle", side_effect=RuntimeError("unavailable")
            ):
                run.return_value.returncode = 0
                result = agents_ui.send_message(cache, {"host": "local", "agent": "agent-1",
                    "message": "Continue", "start": True}, "codex")
                run.assert_called_once()
            self.assertEqual(result["delivery"], "sent")
            self.assertEqual(result["start_error"], "unavailable")

    def test_console_only_accepts_loopback_hosts(self) -> None:
        self.assertTrue(agents_ui.is_loopback("127.0.0.1"))
        self.assertTrue(agents_ui.is_loopback("localhost"))
        self.assertFalse(agents_ui.is_loopback("0.0.0.0"))

    def test_state_payload_selects_requested_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.replace_host_snapshot("local", "This machine", [cached_agent()])

            payload = agents_ui.state_payload(cache, {"host": ["local"], "agent": ["agent-1"]})

            self.assertEqual(payload["selected"]["title"], "Viewer work")
            self.assertEqual(payload["records"][0]["text"], "Hello")

    def test_remote_message_is_put_in_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.replace_host_snapshot("home", "Home", [cached_agent()])

            result = agents_ui.send_message(cache, {"host": "home", "agent": "agent-1", "message": "Continue"}, "codex")

            self.assertEqual(result["delivery"], "queued")
            self.assertEqual(cache.pending("home")[0]["payload"], {"text": "Continue"})

    def test_local_message_uses_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            cache.replace_host_snapshot("local", "This machine", [cached_agent()])
            with patch.object(agents_ui.subprocess, "run") as run:
                run.return_value.returncode = 0
                result = agents_ui.send_message(cache, {"host": "local", "agent": "agent-1", "message": "Continue"}, "codex-test")

            self.assertEqual(result["delivery"], "sent")
            self.assertEqual(run.call_args.args[0], ["codex-test", "queue", "--thread", "thread-1", "--message", "Continue"])


if __name__ == "__main__":
    unittest.main()
