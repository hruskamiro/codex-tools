from __future__ import annotations

import argparse
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_tools import agents, search


AGENT = {
    "title": "viewer",
    "id": "01agent",
    "threadId": "01thread",
    "sessionId": "01session",
    "rolloutId": "01agent",
    "path": "/tmp/session.jsonl",
    "cwd": "/work/codex-tools",
    "source": "cli",
    "lastAt": "2026-10-07T12:00:00Z",
    "lastLabel": "2026-10-07 14:00",
    "status": "idle",
    "statusLabel": "ready",
}


class AgentCommandTests(unittest.TestCase):
    def test_thread_summary_preserves_live_status(self) -> None:
        summary = agents.thread_summary(
            {
                "id": "thread-1",
                "sessionId": "session-1",
                "name": "Tests",
                "path": "/sessions/one.jsonl",
                "cwd": "/work/project",
                "preview": "Fallback title",
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                },
                "updatedAt": 1_700_000_000,
            }
        )

        self.assertEqual(summary["title"], "Tests")
        self.assertEqual(summary["status"], "active")
        self.assertEqual(summary["statusLabel"], "needs-you")
        self.assertEqual(summary["statusFlags"], ["waitingOnApproval"])

    def test_agent_summaries_returns_only_daemon_threads(self) -> None:
        thread = {
            "id": "thread-live",
            "cwd": "/work/project",
            "preview": "A real conversation",
            "status": {"type": "idle"},
            "updatedAt": 1_700_000_000,
        }
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(agents.codex_daemon, "live_threads", return_value=[thread]),
        ):
            result = agents.agent_summaries(Path("/history"), Path("/index"))

        self.assertEqual([item["id"] for item in result], ["thread-live"])

    def test_agent_summaries_excludes_empty_startup_threads(self) -> None:
        empty = {
            "id": "thread-empty",
            "cwd": "/work/project",
            "preview": "",
            "name": None,
            "status": {"type": "idle"},
            "updatedAt": 1_700_000_000,
        }
        named = {**empty, "id": "thread-named", "name": "Named agent"}
        started = {**empty, "id": "thread-started", "preview": "First prompt"}
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(
                agents.codex_daemon,
                "live_threads",
                return_value=[empty, named, started],
            ),
        ):
            result = agents.agent_summaries(Path("/history"), Path("/index"))

        self.assertEqual(
            [item["id"] for item in result],
            ["thread-named", "thread-started"],
        )

    def test_agent_summaries_is_empty_when_daemon_is_not_running(self) -> None:
        with patch.object(Path, "exists", return_value=False):
            result = agents.agent_summaries(Path("/history"), Path("/index"))

        self.assertEqual(result, [])

    def test_no_subcommand_defaults_to_list(self) -> None:
        args = agents.parse_args([])

        self.assertEqual(args.agents_command, "list")
        self.assertEqual(args.limit, 20)

    def test_ui_uses_terminal_defaults(self) -> None:
        args = agents.parse_args(["ui", "--no-refresh"])

        self.assertEqual(args.agents_command, "ui")
        self.assertTrue(args.no_refresh)
        self.assertFalse(args.no_local)

    def test_ui_can_select_remote_agents_only(self) -> None:
        args = agents.parse_args(["ui", "--no-local"])

        self.assertTrue(args.no_local)

    def test_ui_accepts_sync_mode_override(self) -> None:
        args = agents.parse_args(["ui", "--sync", "economy"])

        self.assertEqual(args.sync, "economy")

    def test_resolve_agent_accepts_exact_name_and_unique_id_prefix(self) -> None:
        other = {**AGENT, "title": "docs", "threadId": "02thread"}

        self.assertEqual(agents.resolve_agent([AGENT, other], "viewer"), AGENT)
        self.assertEqual(agents.resolve_agent([AGENT, other], "01th"), AGENT)

    def test_resolve_agent_rejects_ambiguous_name(self) -> None:
        other = {**AGENT, "id": "02agent", "threadId": "02thread"}

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            agents.resolve_agent([AGENT, other], "viewer")

    def test_recent_exchanges_keeps_all_assistant_messages(self) -> None:
        records = [
            search.TextRecord("1", "user", "First", 1, "message"),
            search.TextRecord("2", "assistant", "One", 2, "message"),
            search.TextRecord("3", "assistant", "Two", 3, "message"),
            search.TextRecord(
                "3.5",
                "user",
                "<environment_context>internal</environment_context>",
                4,
                "message",
            ),
            search.TextRecord("4", "user", "Second", 5, "message"),
            search.TextRecord("5", "assistant", "Three", 6, "message"),
        ]

        exchanges = agents.recent_exchanges(records, 1)

        self.assertEqual(len(exchanges), 1)
        self.assertEqual(exchanges[0].user["text"], "Second")
        self.assertEqual(
            [item["text"] for item in exchanges[0].assistant], ["Three"]
        )

    def test_list_json_emits_normalized_summaries(self) -> None:
        output = io.StringIO()
        args = argparse.Namespace(
            limit=20,
            json=True,
            here=False,
            work_dir=None,
            sessions_root=Path("/sessions"),
            session_index=Path("/index"),
        )
        with (
            patch.object(agents, "agent_summaries", return_value=[AGENT]),
            redirect_stdout(output),
        ):
            result = agents.command_list(args)

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["title"], "viewer")

    def test_send_queues_message_by_canonical_thread_id(self) -> None:
        args = argparse.Namespace(
            agent="viewer",
            message=["Check", "the", "tests"],
            codex_bin="codex-test",
            sessions_root=Path("/sessions"),
            session_index=Path("/index"),
        )
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with (
            patch.object(agents, "agent_summaries", return_value=[AGENT]),
            patch.object(agents.subprocess, "run", return_value=completed) as run,
        ):
            result = agents.command_send(args)

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            [
                "codex-test",
                "queue",
                "--thread",
                "01thread",
                "--message",
                "Check the tests",
            ],
            check=False,
        )

    def test_send_rejects_empty_message(self) -> None:
        error = io.StringIO()
        args = argparse.Namespace(message=[], agent="viewer")

        with redirect_stderr(error):
            result = agents.command_send(args)

        self.assertEqual(result, 2)
        self.assertIn("non-empty message", error.getvalue())


if __name__ == "__main__":
    unittest.main()
