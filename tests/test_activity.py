from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_tools import activity, search


class ActivityCommandTests(unittest.TestCase):
    def test_default_range_starts_today(self) -> None:
        args = activity.parse_args([])

        self.assertIsNone(args.from_time)
        self.assertIsNone(args.last)
        self.assertIsNone(args.to_time)
        self.assertFalse(args.here)
        self.assertIsNone(args.work_dir)

    def test_parse_local_time_and_date_boundaries(self) -> None:
        timezone = ZoneInfo("Europe/Bratislava")
        now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone)

        self.assertEqual(
            activity.parse_time_value("today", timezone, now=now),
            datetime(2026, 10, 4, 0, 0, tzinfo=timezone),
        )
        self.assertEqual(
            activity.parse_time_value(
                "2026-10-03", timezone, end_of_day=True, now=now
            ).date().isoformat(),
            "2026-10-03",
        )
        self.assertEqual(
            activity.parse_time_value("2026-10-04 08:30", timezone, now=now).hour,
            8,
        )

    def test_duration_accepts_compound_values(self) -> None:
        self.assertEqual(activity.parse_duration("10m").total_seconds(), 600)
        self.assertEqual(activity.parse_duration("2h").total_seconds(), 7200)
        self.assertEqual(activity.parse_duration("1h30m").total_seconds(), 5400)

    def test_duration_rejects_unknown_or_zero_values(self) -> None:
        for value in ("", "10", "2 hours", "1mo", "0m"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                activity.parse_duration(value)

    def test_last_and_from_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            activity.parse_args(["--last", "2h", "--from", "today"])

    def test_collects_only_messages_inside_window_and_groups_rollouts(self) -> None:
        timezone = ZoneInfo("UTC")
        paths = [Path("one.jsonl"), Path("two.jsonl")]
        sessions = [
            search.Session(
                path=paths[0],
                session_id="session-1",
                parent_thread_id="thread-1",
                cwd="/work/project",
                source="cli",
                records=[
                    search.TextRecord(
                        "2026-10-04T17:59:00Z", "user", "before", 1, "message"
                    ),
                    search.TextRecord(
                        "2026-10-04T18:05:00Z", "user", "work", 2, "message"
                    ),
                    search.TextRecord(
                        "2026-10-04T18:06:00Z",
                        "assistant",
                        "done",
                        3,
                        "message",
                    ),
                ],
            ),
            search.Session(
                path=paths[1],
                session_id="session-2",
                parent_thread_id="thread-1",
                cwd="/work/project",
                source="cli",
                records=[
                    search.TextRecord(
                        "2026-10-04T18:30:00Z", "user", "continue", 1, "message"
                    )
                ],
            ),
        ]
        args = argparse.Namespace(
            sessions_root=Path("sessions"),
            session_index=Path("session_index.jsonl"),
            here=False,
            work_dir=None,
            source=None,
        )

        with (
            patch.object(search, "iter_jsonl_paths", return_value=paths),
            patch.object(search, "read_session", side_effect=sessions),
            patch.object(search, "load_session_index_titles", return_value={}),
        ):
            result = activity.collect_activity(
                args,
                datetime(2026, 10, 4, 18, 0, tzinfo=timezone),
                datetime(2026, 10, 4, 19, 0, tzinfo=timezone),
                timezone,
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].thread_id, "thread-1")
        self.assertEqual(result[0].user_turns, 2)
        self.assertEqual(result[0].assistant_messages, 1)
        self.assertEqual(result[0].message_count, 3)
        self.assertEqual(len(result[0].paths), 2)
        self.assertEqual(result[0].start.minute, 5)
        self.assertEqual(result[0].end.minute, 30)

    def test_json_output_contains_metadata_but_not_transcript_content(self) -> None:
        timezone = ZoneInfo("UTC")
        path = Path("one.jsonl")
        session = search.Session(
            path=path,
            session_id="session-1",
            cwd="/work/project",
            source="vscode",
            records=[
                search.TextRecord(
                    "2026-10-04T18:05:00Z", "user", "Conversation label", 1, "message"
                ),
                search.TextRecord(
                    "2026-10-04T18:06:00Z",
                    "assistant",
                    "private response content",
                    2,
                    "message",
                ),
            ],
        )

        output = io.StringIO()
        with (
            patch.object(search, "iter_jsonl_paths", return_value=[path]),
            patch.object(search, "read_session", return_value=session),
            patch.object(search, "load_session_index_titles", return_value={}),
            redirect_stdout(output),
        ):
            result = activity.main(
                [
                    "--from",
                    "2026-10-04 18:00",
                    "--to",
                    "2026-10-04 19:00",
                    "--timezone",
                    "UTC",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["conversation_count"], 1)
        self.assertEqual(
            payload["conversations"][0]["recorded_sources"], ["vscode"]
        )
        self.assertEqual(
            payload["conversations"][0]["workdir"], "/work/project"
        )
        self.assertNotIn("project", payload["conversations"][0])
        self.assertEqual(payload["conversations"][0]["title"], "Conversation label")
        self.assertNotIn("private response content", output.getvalue())

    def test_here_and_source_filters_are_applied(self) -> None:
        timezone = ZoneInfo("UTC")
        project = Path("/work/project")
        paths = [Path("one.jsonl"), Path("two.jsonl")]
        sessions = [
            search.Session(
                path=paths[0],
                session_id="wanted",
                cwd=str(project),
                source="vscode",
                records=[search.TextRecord("2026-10-04T18:05:00Z", "user", "x", 1, "message")],
            ),
            search.Session(
                path=paths[1],
                session_id="wrong-source",
                cwd=str(project),
                source="cli",
                records=[search.TextRecord("2026-10-04T18:05:00Z", "user", "x", 1, "message")],
            ),
        ]
        args = argparse.Namespace(
            sessions_root=Path("sessions"),
            session_index=Path("session_index.jsonl"),
            here=True,
            work_dir=None,
            source="vscode",
        )

        with (
            patch.object(search, "iter_jsonl_paths", return_value=paths),
            patch.object(search, "read_session", side_effect=sessions),
            patch.object(search, "load_session_index_titles", return_value={}),
        ):
            result = activity.collect_activity(
                args,
                datetime(2026, 10, 4, 18, 0, tzinfo=timezone),
                datetime(2026, 10, 4, 19, 0, tzinfo=timezone),
                timezone,
                current_dir=project,
            )

        self.assertEqual([item.thread_id for item in result], ["wanted"])

    def test_text_uses_workdir_and_omits_recorded_source(self) -> None:
        timezone = ZoneInfo("UTC")
        item = activity.ConversationActivity(
            thread_id="thread-1",
            title="Example conversation",
            title_source="test",
            start=datetime(2026, 10, 4, 18, 0, tzinfo=timezone),
            end=datetime(2026, 10, 4, 18, 5, tzinfo=timezone),
            user_turns=2,
            assistant_messages=2,
            message_count=4,
            cwd=str(Path.home() / "projects" / "codex-tools"),
            recorded_sources=["vscode"],
            session_ids=["session-1"],
            rollout_ids=["rollout-1"],
            paths=["one.jsonl"],
        )

        output = io.StringIO()
        with redirect_stdout(output):
            activity.emit_text(
                [item], item.start, item.end, timezone, limit=50
            )

        text = output.getvalue()
        self.assertIn("WORKDIR", text)
        self.assertIn("~/projects/codex-tools", text)
        self.assertNotIn("PROJECT", text)
        self.assertNotIn("SOURCE", text)
        self.assertNotIn("vscode", text)

    def test_invalid_range_returns_usage_error(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = activity.main(
                [
                    "--from",
                    "2026-10-04 19:00",
                    "--to",
                    "2026-10-04 18:00",
                    "--timezone",
                    "UTC",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("--from must not be later", error.getvalue())

    def test_last_cannot_be_combined_with_to(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = activity.main(["--last", "2h", "--to", "today"])

        self.assertEqual(result, 2)
        self.assertIn("--last cannot be combined with --to", error.getvalue())

    def test_invalid_last_duration_returns_usage_error(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = activity.main(["--last", "soon"])

        self.assertEqual(result, 2)
        self.assertIn("invalid duration", error.getvalue())


if __name__ == "__main__":
    unittest.main()
