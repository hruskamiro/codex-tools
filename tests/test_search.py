from __future__ import annotations

import argparse
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_tools import search, summarize_daily


class SearchCommandTests(unittest.TestCase):
    def test_default_interface_uses_jsonl_and_groups_by_task(self) -> None:
        args = search.parse_args(["needle"])

        self.assertEqual(args.source, "jsonl")
        self.assertFalse(args.ungrouped)
        self.assertEqual(args.matches_per_session, 5)
        self.assertEqual(args.context_lines, 2)
        self.assertFalse(args.here)
        self.assertIsNone(args.work_dir)

    def test_here_and_work_dir_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            search.parse_args(["needle", "--here", "--work-dir", "/tmp"])

    def test_selected_work_dir_uses_current_directory_for_here(self) -> None:
        args = search.parse_args(["needle", "--here"])

        selected = search.selected_work_dir(
            args, current_dir=Path("/tmp/project/../project")
        )

        self.assertEqual(selected, Path("/tmp/project").resolve())

    def test_work_dir_matching_canonicalizes_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            alias = root / "project-link"
            project.mkdir()
            alias.symlink_to(project, target_is_directory=True)
            session = search.Session(path=Path("session.jsonl"), cwd=str(alias))

            matches = search.session_matches_work_dir(
                session, search.canonical_work_dir(project)
            )

        self.assertTrue(matches)

    def test_search_filters_by_exact_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            nested = project / "nested"
            project.mkdir()
            nested.mkdir()
            paths = [Path("one.jsonl"), Path("two.jsonl")]
            sessions = [
                search.Session(
                    path=paths[0],
                    session_id="matching-session",
                    cwd=str(project),
                    records=[
                        search.TextRecord("", "user", "needle", 1, "message")
                    ],
                ),
                search.Session(
                    path=paths[1],
                    session_id="nested-session",
                    cwd=str(nested),
                    records=[
                        search.TextRecord("", "user", "needle", 1, "message")
                    ],
                ),
            ]

            output = io.StringIO()
            with (
                patch.object(search, "iter_jsonl_paths", return_value=paths),
                patch.object(search, "read_session", side_effect=sessions),
                patch.object(search, "load_session_index_titles", return_value={}),
                redirect_stdout(output),
            ):
                result = search.main(
                    ["needle", "--work-dir", str(project), "--json"]
                )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual([item["thread_id"] for item in payload], ["matching-session"])

    def test_work_dir_filter_rejects_sqlite_source(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = search.main(["needle", "--source", "sqlite", "--here"])

        self.assertEqual(result, 2)
        self.assertIn("does not contain working-directory metadata", error.getvalue())

    def test_multiline_excerpt_preserves_and_marks_surrounding_lines(self) -> None:
        text = "first\nsecond\n  target value\nfourth\nfifth\nsixth"
        match = re.search("target", text)
        assert match is not None

        lines = search.excerpt_lines(text, match, context_lines=2)

        self.assertEqual([line.number for line in lines], [1, 2, 3, 4, 5])
        self.assertEqual([line.number for line in lines if line.matched], [3])
        self.assertEqual(lines[2].text, "  target value")
        self.assertEqual(
            search.plain_match_excerpt(text, match, context_lines=2),
            "  1 | first\n  2 | second\n> 3 |   target value\n  4 | fourth\n  5 | fifth",
        )

    def test_single_line_excerpt_uses_compact_character_context(self) -> None:
        text = "prefix target suffix"
        match = re.search("target", text)
        assert match is not None

        self.assertEqual(
            search.plain_match_excerpt(text, match, context_lines=2), text
        )

    def test_default_search_does_not_read_sqlite_and_groups_rollouts(self) -> None:
        paths = [Path("one.jsonl"), Path("two.jsonl")]
        sessions = [
            search.Session(
                path=path,
                session_id=f"session-{index}",
                parent_thread_id="shared-task",
                records=[
                    search.TextRecord(
                        "2026-09-27T08:00:00Z",
                        "user",
                        f"needle {index}",
                        index,
                        "response_item/message",
                    )
                ],
            )
            for index, path in enumerate(paths, start=1)
        ]

        output = io.StringIO()
        with (
            patch.object(search, "iter_jsonl_paths", return_value=paths),
            patch.object(search, "read_session", side_effect=sessions),
            patch.object(search, "load_session_index_titles", return_value={}),
            patch.object(search, "read_sqlite_sessions") as read_sqlite,
            redirect_stdout(output),
        ):
            result = search.main(["needle", "--matches", "1", "--json"])

        self.assertEqual(result, 0)
        read_sqlite.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["thread_id"], "shared-task")
        self.assertEqual(payload[0]["session_count"], 2)
        self.assertEqual(
            sum(len(session["matches"]) for session in payload[0]["sessions"]),
            1,
        )

    def test_daily_summary_uses_codex_session_index_title(self) -> None:
        session = search.Session(
            path=Path("session.jsonl"),
            session_id="session-1",
            parent_thread_id="parent-1",
            created_at="2026-09-27T08:00:00Z",
            records=[
                search.TextRecord(
                    "2026-09-27T08:00:00Z",
                    "user",
                    "First prompt",
                    1,
                    "response_item/message",
                )
            ],
        )
        args = argparse.Namespace(
            sessions_root=Path("sessions"),
            session_index=Path("session_index.jsonl"),
        )

        with (
            patch.object(search, "iter_jsonl_paths", return_value=[session.path]),
            patch.object(search, "read_session", return_value=session),
            patch.object(
                search,
                "load_session_index_titles",
                return_value={
                    "parent-1": search.SessionTitle(
                        "Indexed title", search.TITLE_SOURCE_SESSION_INDEX
                    )
                },
            ),
        ):
            sessions = summarize_daily.read_daily_sessions(
                args, date(2026, 9, 27), ZoneInfo("UTC")
            )

        self.assertEqual(sessions[0].title, "Indexed title")
        self.assertEqual(
            sessions[0].title_source, search.TITLE_SOURCE_SESSION_INDEX
        )

    def test_title_resolution_inherits_parent_thread_title(self) -> None:
        session = search.Session(
            path=Path("child.jsonl"),
            session_id="child-session",
            rollout_id="child-rollout",
            parent_thread_id="parent-thread",
            records=[search.TextRecord("", "user", "Fallback prompt", 1, "message")],
        )
        titles = {
            "parent-thread": search.SessionTitle(
                "Parent title", search.TITLE_SOURCE_SESSION_INDEX
            )
        }

        search.resolve_session_title(session, titles)

        self.assertEqual(session.title, "Parent title")
        self.assertEqual(session.title_source, search.TITLE_SOURCE_SESSION_INDEX)

    def test_title_resolution_falls_back_to_first_meaningful_prompt(self) -> None:
        session = search.Session(
            path=Path("session.jsonl"),
            records=[
                search.TextRecord("", "user", "okay", 1, "message"),
                search.TextRecord(
                    "", "user", "Improve the conversation title handling", 2, "message"
                ),
            ],
        )

        search.resolve_session_title(session, {})

        self.assertEqual(session.title, "Improve the conversation title handling")
        self.assertEqual(session.title_source, search.TITLE_SOURCE_FIRST_PROMPT)

    def test_diagnose_has_its_own_small_argument_parser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = search.parse_diagnose_args(
                [
                    "--sessions-root",
                    str(root / "sessions"),
                    "--session-index",
                    str(root / "session_index.jsonl"),
                    "--thread-history",
                    str(root / "history.sqlite"),
                    "--json",
                ]
            )

            output = io.StringIO()
            with redirect_stdout(output):
                result = search.diagnose(args)

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["jsonl"]["files_found"], 0)
        self.assertFalse(payload["session_index_info"]["exists"])
        self.assertFalse(payload["sqlite"]["exists"])
        self.assertEqual(payload["status"], "warning")

    def test_diagnose_reports_invalid_configuration_as_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            config_path.write_text("not valid toml = [", encoding="utf-8")
            args = search.parse_diagnose_args(
                [
                    "--sessions-root",
                    str(root / "sessions"),
                    "--session-index",
                    str(root / "session_index.jsonl"),
                    "--thread-history",
                    str(root / "history.sqlite"),
                ]
            )

            output = io.StringIO()
            with (
                patch.object(search.tool_paths, "CONFIG_FILE", config_path),
                patch.object(search.shutil, "which", return_value="/usr/bin/codex"),
                redirect_stdout(output),
            ):
                result = search.diagnose(args)

        self.assertEqual(result, 1)
        self.assertIn("Codex Tools health: ERROR", output.getvalue())
        self.assertIn("[ERROR] Configuration", output.getvalue())
        self.assertIn("Fix:", output.getvalue())

    def test_diagnose_default_output_hides_database_internals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            sessions.mkdir()
            (sessions / "one.jsonl").write_text(
                '{"timestamp":"2026-09-01T12:00:00Z"}\n', encoding="utf-8"
            )
            args = search.parse_diagnose_args(
                [
                    "--sessions-root",
                    str(sessions),
                    "--session-index",
                    str(root / "session_index.jsonl"),
                    "--thread-history",
                    str(root / "history.sqlite"),
                ]
            )

            output = io.StringIO()
            with (
                patch.object(search.tool_paths, "CONFIG_FILE", root / "config.toml"),
                patch.object(search.shutil, "which", return_value="/usr/bin/codex"),
                redirect_stdout(output),
            ):
                result = search.diagnose(args)

        self.assertEqual(result, 0)
        text = output.getvalue()
        self.assertIn("Codex Tools health: OK", text)
        self.assertIn("[OK] Conversation transcripts", text)
        self.assertIn("No problems found.", text)
        self.assertNotIn("SQLite tables:", text)

    def test_diagnose_verbose_output_includes_technical_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            sessions.mkdir()
            args = search.parse_diagnose_args(
                [
                    "--sessions-root",
                    str(sessions),
                    "--session-index",
                    str(root / "session_index.jsonl"),
                    "--thread-history",
                    str(root / "history.sqlite"),
                    "--verbose",
                ]
            )

            output = io.StringIO()
            with (
                patch.object(search.tool_paths, "CONFIG_FILE", root / "config.toml"),
                patch.object(search.shutil, "which", return_value="/usr/bin/codex"),
                redirect_stdout(output),
            ):
                search.diagnose(args)

        self.assertIn("Technical details", output.getvalue())
        self.assertIn("SQLite tables:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
