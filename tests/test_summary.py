from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_tools import (
    browser,
    cli,
    config as user_config,
    paths,
    summary,
    summarize_daily,
    summarize_weekly,
    summary_clean,
    summary_common,
    summary_prompts,
    summary_site,
)


class SummaryCommandTests(unittest.TestCase):
    def test_local_timezone_detection_uses_first_valid_candidate(self) -> None:
        with patch.object(
            summary_common,
            "_system_timezone_candidates",
            return_value=["Not/A-Timezone", "Europe/Bratislava", "UTC"],
        ):
            detected = summary_common.detect_local_timezone()

        self.assertEqual(detected, "Europe/Bratislava")

    def test_local_timezone_detection_falls_back_to_utc(self) -> None:
        with patch.object(
            summary_common,
            "_system_timezone_candidates",
            return_value=["Not/A-Timezone"],
        ):
            detected = summary_common.detect_local_timezone()

        self.assertEqual(detected, "UTC")

    def test_daily_and_weekly_defaults_share_detected_timezone(self) -> None:
        self.assertEqual(
            summarize_daily.DEFAULT_TIMEZONE,
            summary_common.detect_local_timezone(),
        )
        self.assertEqual(
            summarize_weekly.DEFAULT_TIMEZONE,
            summarize_daily.DEFAULT_TIMEZONE,
        )
        self.assertIsInstance(ZoneInfo(summarize_daily.DEFAULT_TIMEZONE), ZoneInfo)

    def test_summary_site_refuses_unmarked_nonempty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            site.mkdir()
            important = site / "important.txt"
            important.write_text("keep me", encoding="utf-8")

            result = summary_site.main(["--site-dir", str(site)])

            self.assertEqual(result, 2)
            self.assertEqual(important.read_text(encoding="utf-8"), "keep me")

    def test_summary_site_can_replace_its_own_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"

            self.assertEqual(summary_site.main(["--site-dir", str(site)]), 0)
            self.assertTrue((site / summary_site.SITE_MARKER).is_file())
            self.assertEqual(site.stat().st_mode & 0o777, 0o700)
            self.assertEqual((site / "index.html").stat().st_mode & 0o777, 0o600)
            self.assertEqual(summary_site.main(["--site-dir", str(site)]), 0)

    def test_private_summary_writer_restricts_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "summary.md"

            paths.write_private_text(target, "private\n")

            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_browser_open_uses_new_window_by_default(self) -> None:
        args = argparse.Namespace(browser=None, same_window=False)
        with (
            patch.object(browser, "browser_command", return_value=["google-chrome"]),
            patch.object(browser.subprocess, "Popen") as popen,
        ):
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired(
                "google-chrome", 0.5
            )
            opened = browser.open_browser("file:///tmp/summary.html", args)

        self.assertTrue(opened)
        self.assertEqual(
            popen.call_args.args[0],
            ["google-chrome", "--new-window", "file:///tmp/summary.html"],
        )

    def test_only_summary_is_a_public_top_level_command(self) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(cli.main(["summarize"]), 2)
        self.assertIn("unknown command: summarize", output.call_args_list[0].args[0])

    def test_context_option_names_are_consistent(self) -> None:
        daily = summarize_daily.parse_args(["--show-context", "--save-context", "d.md"])
        weekly = summarize_weekly.parse_args(
            ["--show-context", "--save-context", "w.md"]
        )
        self.assertTrue(daily.show_context)
        self.assertEqual(daily.save_context, Path("d.md"))
        self.assertTrue(weekly.show_context)
        self.assertEqual(weekly.save_context, Path("w.md"))

    def test_summary_defaults_to_freeform_at_roughly_200_words(self) -> None:
        daily = summarize_daily.parse_args([])
        weekly = summarize_weekly.parse_args([])

        self.assertEqual(daily.words, 200)
        self.assertEqual(daily.summary_format, "freeform")
        self.assertEqual(weekly.words, 200)
        self.assertEqual(weekly.summary_format, "freeform")

    def test_summary_default_model_is_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config" / "summary.json"

            self.assertIsNone(summary_common.read_default_model(config))
            summary_common.write_default_model("gpt-6.1-sol", config)
            self.assertEqual(
                summary_common.read_default_model(config), "gpt-6.1-sol"
            )
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)

            summary_common.write_default_model(None, config)
            self.assertIsNone(summary_common.read_default_model(config))

    def test_summary_model_command_sets_shows_and_resets_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            with (
                patch.object(user_config.paths, "CONFIG_FILE", config),
                patch.object(
                    user_config.paths,
                    "SUMMARY_CONFIG_FILE",
                    Path(temporary) / "legacy-summary.json",
                ),
                patch.object(
                    user_config.paths,
                    "VIEWER_CONFIG_FILE",
                    Path(temporary) / "legacy-viewer.json",
                ),
                patch("builtins.print") as output,
            ):
                self.assertEqual(summary.main(["model", "gpt-6.1-sol"]), 0)
                self.assertEqual(
                    [call.args[0] for call in output.call_args_list],
                    [
                        "Daily summary model: gpt-6.1-sol (user config)",
                        "Weekly summary model: gpt-6.1-sol (user config)",
                    ],
                )

                output.reset_mock()
                self.assertEqual(summary.main(["model"]), 0)
                self.assertEqual(
                    [call.args[0] for call in output.call_args_list],
                    [
                        "Daily summary model: gpt-6.1-sol (user config)",
                        "Weekly summary model: gpt-6.1-sol (user config)",
                    ],
                )

                output.reset_mock()
                self.assertEqual(summary.main(["model", "--reset"]), 0)
                self.assertEqual(
                    [call.args[0] for call in output.call_args_list],
                    [
                        "Daily summary model: Codex profile default (built-in)",
                        "Weekly summary model: Codex profile default (built-in)",
                    ],
                )

    def test_saved_summary_model_is_default_but_cli_can_override_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            user_config.set_value("summary.daily.model", "saved-model", config)
            with patch.object(user_config.paths, "CONFIG_FILE", config):
                daily = summarize_daily.parse_args([])
                weekly = summarize_weekly.parse_args(["--model", "one-run-model"])

        self.assertEqual(daily.model, "saved-model")
        self.assertEqual(weekly.model, "one-run-model")

    def test_packaged_daily_prompt_renders_context_verbatim(self) -> None:
        prompt = summarize_daily.summary_prompt(
            "A formula costs $5 and uses $x$.",
            date(2026, 9, 28),
            ZoneInfo("Europe/Bratislava"),
        )

        self.assertIn("Monday, 2026-09-28", prompt)
        self.assertIn("timezone Europe/Bratislava", prompt)
        self.assertIn("approximately 200 words", prompt)
        self.assertIn("do not force the content into fixed sections", prompt)
        self.assertIn("A formula costs $5 and uses $x$.", prompt)

    def test_worklog_format_retains_fixed_daily_sections(self) -> None:
        prompt = summarize_daily.summary_prompt(
            "Context",
            date(2026, 9, 28),
            ZoneInfo("UTC"),
            words=350,
            summary_format="worklog",
        )

        self.assertIn("approximately 350 words", prompt)
        self.assertIn("Main Work, Smaller Items, and Open Threads", prompt)

    def test_custom_summary_prompt_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "daily.md"
            template.write_text("Summary for $day\n\n$context", encoding="utf-8")

            prompt = summarize_daily.summary_prompt(
                "Context body",
                date(2026, 9, 28),
                ZoneInfo("UTC"),
                template,
            )

        self.assertTrue(prompt.startswith("Summary for 2026-09-28\n\nContext body\n\n"))
        self.assertIn("Return a JSON object matching the supplied schema", prompt)

    def test_show_prompt_does_not_invoke_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(summary_common, "run_codex_exec") as run,
                patch("builtins.print") as output,
            ):
                result = summarize_weekly.main(
                    [
                        "--week-start",
                        "2026-09-21",
                        "--show-prompt",
                        "--daily-summaries-dir",
                        str(root / "daily"),
                    ]
                )

            self.assertEqual(result, 0)
            run.assert_not_called()
            rendered = output.call_args.args[0]
            self.assertIn("Weekly Summary: 2026-09-21 to 2026-09-27", rendered)

    def test_show_template_does_not_scan_sessions(self) -> None:
        with (
            patch.object(summarize_daily, "read_daily_sessions") as read_sessions,
            patch("builtins.print") as output,
        ):
            result = summarize_daily.main(["--show-template"])

        self.assertEqual(result, 0)
        read_sessions.assert_not_called()
        template = output.call_args.args[0]
        self.assertIn("$weekday", template)
        self.assertIn("$context", template)

    def test_show_template_can_print_custom_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "weekly.md"
            template.write_text("Custom $start through $end\n$context\n", encoding="utf-8")
            with patch("builtins.print") as output:
                result = summarize_weekly.main(
                    ["--show-template", "--prompt-template", str(template)]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            output.call_args.args[0], "Custom $start through $end\n$context"
        )

    def test_invalid_prompt_placeholder_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "invalid.md"
            template.write_text("$unknown", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown"):
                summary_prompts.render_template(
                    "daily", {"context": ""}, template
                )

    def test_weekly_generation_fills_only_missing_days(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / (
                "codex-daily-summary-2026-09-21-monday-"
                "20260921T120000+0200.md"
            )
            existing.write_text("# Daily Summary: Monday, 2026-09-21\n", encoding="utf-8")
            args = argparse.Namespace(
                daily_summaries_dir=root,
                refresh_dailies="missing",
                daily_prompt_template=None,
                sessions_root=root / "sessions",
                session_index=root / "session_index.jsonl",
                max_record_chars=900,
                max_context_chars=120_000,
                model=None,
                codex_bin="codex",
                profile="default",
                manager_root=root / "manager",
                default_home=root / "home",
            )
            generated_days: list[date] = []

            def prepare(_args, day, _timezone):
                return SimpleNamespace(sessions=[object()], input_fingerprint=str(day))

            def generate(
                _args,
                day,
                _timezone,
                *,
                require_sessions=False,
                prepared=None,
            ):
                generated_days.append(day)
                self.assertTrue(require_sessions)
                self.assertIsNotNone(prepared)
                return None

            with (
                patch.object(
                    summarize_daily, "prepare_daily_summary", side_effect=prepare
                ),
                patch.object(
                    summarize_daily, "generate_daily_summary", side_effect=generate
                ),
            ):
                created = summarize_weekly.refresh_daily_summaries(
                    args,
                    date(2026, 9, 21),
                    date(2026, 9, 27),
                    ZoneInfo("Europe/Bratislava"),
                )

            self.assertEqual(created, [])
            self.assertEqual(
                generated_days,
                [date(2026, 9, day) for day in range(22, 28)],
            )

    def test_weekly_refresh_uses_daily_model_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            user_config.set_value("summary.daily.model", "daily-model", config_path)
            user_config.set_value("summary.daily.words", 175, config_path)
            args = argparse.Namespace(
                daily_summaries_dir=root / "daily",
                refresh_dailies="missing",
                daily_prompt_template=None,
                sessions_root=root / "sessions",
                session_index=root / "session_index.jsonl",
                max_record_chars=900,
                max_context_chars=120_000,
                model="weekly-model",
                reasoning_effort="high",
                timeout=999,
                codex_bin="codex",
                profile="default",
                manager_root=root / "manager",
                default_home=root / "home",
            )
            captured = []

            def prepare(daily_args, _day, _timezone):
                captured.append(daily_args)
                return SimpleNamespace(sessions=[], input_fingerprint="empty")

            with (
                patch.object(user_config.paths, "CONFIG_FILE", config_path),
                patch.object(
                    summarize_daily, "prepare_daily_summary", side_effect=prepare
                ),
            ):
                summarize_weekly.refresh_daily_summaries(
                    args,
                    date(2026, 9, 28),
                    date(2026, 9, 28),
                    ZoneInfo("Europe/Bratislava"),
                )

            self.assertEqual(captured[0].model, "daily-model")
            self.assertEqual(captured[0].words, 175)
            self.assertEqual(captured[0].reasoning_effort, "low")
            self.assertEqual(captured[0].timeout, 300)

    def test_daily_freshness_uses_input_fingerprint(self) -> None:
        summary = summarize_weekly.DailySummary(
            path=Path("summary.md"),
            day=date(2026, 9, 28),
            timestamp=summarize_weekly.datetime.fromisoformat(
                "2026-09-28T12:00:00+02:00"
            ),
            markdown="# Daily",
            metadata={
                "format_version": 1,
                "day": "2026-09-28",
                "input_fingerprint": "same",
            },
        )

        self.assertTrue(
            summarize_weekly.daily_summary_is_fresh(
                summary, SimpleNamespace(input_fingerprint="same")
            )
        )
        self.assertFalse(
            summarize_weekly.daily_summary_is_fresh(
                summary, SimpleNamespace(input_fingerprint="changed")
            )
        )

    def test_daily_generation_writes_freshness_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "daily.md"
            args = argparse.Namespace(
                save_context=None,
                show_context=False,
                show_prompt=False,
                prompt_template=None,
                output=output,
                output_dir=root,
                model="test-model",
                profile="default",
                max_record_chars=900,
                max_context_chars=120_000,
            )
            inputs = summarize_daily.DailySummaryInputs(
                sessions=[],
                context="context",
                prompt="prompt",
                source_last_timestamp="2026-09-28T12:00:00+02:00",
                context_sha256="context-hash",
                template_sha256="template-hash",
                input_fingerprint="input-hash",
            )
            with patch.object(summary_common, "run_codex_exec", return_value="summary"):
                generated = summarize_daily.generate_daily_summary(
                    args,
                    date(2026, 9, 28),
                    ZoneInfo("Europe/Bratislava"),
                    prepared=inputs,
                )

            self.assertIsNotNone(generated)
            metadata_path = output.with_suffix(".md.json")
            metadata = summarize_weekly.json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["input_fingerprint"], "input-hash")
            self.assertEqual(metadata["model"], "test-model")

    def test_auto_refresh_reuses_fresh_daily_and_rebuilds_stale_daily(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_path = root / (
                "codex-daily-summary-2026-09-28-monday-"
                "20260928T120000+0200.md"
            )
            summary_path.write_text("# Daily", encoding="utf-8")
            metadata_path = summarize_daily.metadata_path_for(summary_path)
            metadata_path.write_text(
                '{"format_version": 1, "day": "2026-09-28", '
                '"input_fingerprint": "fresh"}',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                daily_summaries_dir=root,
                refresh_dailies="auto",
                daily_prompt_template=None,
                sessions_root=root / "sessions",
                session_index=root / "session_index.jsonl",
                max_record_chars=900,
                max_context_chars=120_000,
                model=None,
                codex_bin="codex",
                profile="default",
                manager_root=root / "manager",
                default_home=root / "home",
            )

            fresh = SimpleNamespace(sessions=[object()], input_fingerprint="fresh")
            with (
                patch.object(summarize_daily, "prepare_daily_summary", return_value=fresh),
                patch.object(summarize_daily, "generate_daily_summary") as generate,
            ):
                created = summarize_weekly.refresh_daily_summaries(
                    args,
                    date(2026, 9, 28),
                    date(2026, 9, 28),
                    ZoneInfo("Europe/Bratislava"),
                )
            self.assertEqual(created, [])
            generate.assert_not_called()

            stale = SimpleNamespace(sessions=[object()], input_fingerprint="changed")
            generated = summarize_daily.DailySummaryResult(
                markdown="# Daily",
                output_path=summary_path,
                metadata_path=metadata_path,
                session_count=1,
            )
            with (
                patch.object(summarize_daily, "prepare_daily_summary", return_value=stale),
                patch.object(
                    summarize_daily, "generate_daily_summary", return_value=generated
                ) as generate,
            ):
                created = summarize_weekly.refresh_daily_summaries(
                    args,
                    date(2026, 9, 28),
                    date(2026, 9, 28),
                    ZoneInfo("Europe/Bratislava"),
                )
            self.assertEqual(created, [summary_path])
            generate.assert_called_once()

    def test_no_daily_refresh_avoids_scanning_transcripts(self) -> None:
        args = argparse.Namespace(refresh_dailies="none")
        with patch.object(summarize_daily, "prepare_daily_summary") as prepare:
            created = summarize_weekly.refresh_daily_summaries(
                args,
                date(2026, 9, 28),
                date(2026, 9, 28),
                ZoneInfo("Europe/Bratislava"),
            )
        self.assertEqual(created, [])
        prepare.assert_not_called()

    def test_context_truncation_keeps_latest_material(self) -> None:
        context = "beginning\n" + ("middle" * 50) + "\nlatest work"
        truncated = summary_common.truncate_context(context, 100)

        self.assertLessEqual(len(truncated), 100)
        self.assertTrue(truncated.startswith("beginning"))
        self.assertTrue(truncated.endswith("latest work"))
        self.assertIn("Context truncated", truncated)

    def test_summary_clean_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daily = root / "daily"
            daily.mkdir()
            summary = daily / "codex-daily-summary-2026-09-27-sunday-test.md"
            summary.write_text("summary", encoding="utf-8")

            with patch("builtins.input", return_value="n"):
                result = summary_clean.main(
                    [
                        "--daily",
                        "--daily-summaries-dir",
                        str(daily),
                        "--site-dir",
                        str(root / "site"),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertTrue(summary.exists())

    def test_summary_clean_removes_only_generated_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daily = root / "daily"
            weekly = root / "weekly"
            site = root / "site"
            daily.mkdir()
            weekly.mkdir()
            site.mkdir()
            daily_summary = daily / "codex-daily-summary-2026-09-27-sunday-test.md"
            weekly_summary = weekly / (
                "codex-weekly-summary-2026-09-21_to_2026-09-27-test.md"
            )
            unrelated = daily / "notes.md"
            daily_summary.write_text("daily", encoding="utf-8")
            daily_metadata = daily_summary.with_suffix(".md.json")
            daily_metadata.write_text("{}", encoding="utf-8")
            weekly_summary.write_text("weekly", encoding="utf-8")
            unrelated.write_text("keep", encoding="utf-8")
            (site / "index.html").write_text("site", encoding="utf-8")

            result = summary_clean.main(
                [
                    "--yes",
                    "--daily-summaries-dir",
                    str(daily),
                    "--weekly-summaries-dir",
                    str(weekly),
                    "--site-dir",
                    str(site),
                ]
            )

            self.assertEqual(result, 0)
            self.assertFalse(daily_summary.exists())
            self.assertFalse(daily_metadata.exists())
            self.assertFalse(weekly_summary.exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse(site.exists())

    def test_summary_site_open_uses_shared_browser_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            with patch.object(browser, "open_browser") as open_browser:
                result = summary_site.main(
                    [
                        "--daily-summaries-dir",
                        str(root / "daily"),
                        "--weekly-summaries-dir",
                        str(root / "weekly"),
                        "--site-dir",
                        str(site),
                        "--open",
                    ]
                )

            self.assertEqual(result, 0)
            open_browser.assert_called_once()
            self.assertEqual(open_browser.call_args.args[0], (site / "index.html").as_uri())


if __name__ == "__main__":
    unittest.main()
