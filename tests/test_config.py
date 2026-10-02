from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import cli, config


class ConfigTests(unittest.TestCase):
    def test_unified_cli_routes_to_config_commands(self) -> None:
        with (
            patch.object(config.paths, "CONFIG_FILE", Path("/tmp/example-config.toml")),
            patch("builtins.print") as output,
        ):
            self.assertEqual(cli.main(["config", "path"]), 0)

        output.assert_called_once_with(Path("/tmp/example-config.toml"))

    def test_typed_values_round_trip_privately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "config.toml"

            config.set_value("summary.daily.words", "275", path)
            config.set_value("summary.weekly.model", "test-model", path)

            self.assertEqual(config.value("summary.daily.words", path), 275)
            self.assertEqual(config.value("summary.weekly.model", path), "test-model")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

            config.unset_value("summary.daily.words", path)
            self.assertEqual(config.resolve("summary.daily.words", path), (200, "built-in"))

    def test_unknown_and_invalid_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            with self.assertRaisesRegex(ValueError, "unknown configuration key"):
                config.set_value("summary.daily.typo", "1", path)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                config.set_value("summary.daily.words", "0", path)
            with self.assertRaisesRegex(ValueError, "must be one of"):
                config.set_value("summary.daily.format", "verbose", path)

    def test_command_specific_summary_defaults_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            config.set_value("summary.daily.model", "daily-model", path)
            config.set_value("summary.weekly.model", "weekly-model", path)
            config.set_value("summary.daily.words", 150, path)
            config.set_value("summary.weekly.words", 450, path)

            self.assertEqual(config.section("summary.daily", path)["model"], "daily-model")
            self.assertEqual(config.section("summary.weekly", path)["model"], "weekly-model")
            self.assertEqual(config.section("summary.daily", path)["words"], 150)
            self.assertEqual(config.section("summary.weekly", path)["words"], 450)

    def test_show_reports_effective_value_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            config.set_value("summary.daily.words", 325, path)
            with (
                patch.object(config.paths, "CONFIG_FILE", path),
                patch("builtins.print") as output,
            ):
                config.show_config()

            rendered = "\n".join(
                str(call.args[0]) if call.args else "" for call in output.call_args_list
            )
            self.assertIn(f"Configuration: {path}", rendered)
            self.assertRegex(rendered, r"summary\.daily\.words\s+325\s+user config")
            self.assertRegex(rendered, r"summary\.weekly\.words\s+200\s+built-in")

    def test_first_write_carries_forward_legacy_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unified = root / "config.toml"
            summary = root / "summary.json"
            viewer = root / "viewer.json"
            summary.write_text(json.dumps({"default_model": "legacy-model"}))
            viewer.write_text(json.dumps({"default_view": "latex"}))
            with (
                patch.object(config.paths, "CONFIG_FILE", unified),
                patch.object(config.paths, "SUMMARY_CONFIG_FILE", summary),
                patch.object(config.paths, "VIEWER_CONFIG_FILE", viewer),
            ):
                self.assertEqual(
                    config.resolve("summary.daily.model"),
                    ("legacy-model", "legacy config"),
                )
                config.set_value("summary.daily.words", 225)

                self.assertEqual(config.value("summary.daily.model"), "legacy-model")
                self.assertEqual(config.value("summary.weekly.model"), "legacy-model")
                self.assertEqual(config.value("viewer.default_view"), "latex")
