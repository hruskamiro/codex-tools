from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import aliases


class AliasTests(unittest.TestCase):
    def test_install_refuses_existing_files_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            completion_dir = root / "completion"
            bin_dir.mkdir()
            completion_dir.mkdir()
            alias = bin_dir / "ct"
            ct_completion = completion_dir / "ct"
            command_completion = completion_dir / "codex-tools"
            alias.write_text("keep alias", encoding="utf-8")
            ct_completion.write_text("keep ct completion", encoding="utf-8")
            command_completion.write_text("keep command completion", encoding="utf-8")

            with patch.object(aliases, "target_path", return_value=Path("/bin/true")):
                result = aliases.main(
                    [
                        "--bin-dir",
                        str(bin_dir),
                        "--completion-dir",
                        str(completion_dir),
                        "install",
                    ]
                )

            self.assertEqual(result, 2)
            self.assertEqual(alias.read_text(encoding="utf-8"), "keep alias")
            self.assertEqual(
                ct_completion.read_text(encoding="utf-8"), "keep ct completion"
            )
            self.assertEqual(
                command_completion.read_text(encoding="utf-8"),
                "keep command completion",
            )

    def test_force_replaces_broken_alias_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            completion_dir = root / "completion"
            bin_dir.mkdir()
            completion_dir.mkdir()
            alias = bin_dir / "ct"
            ct_completion = completion_dir / "ct"
            command_completion = completion_dir / "codex-tools"
            alias.symlink_to(root / "uninstalled-codex-tools")
            ct_completion.write_text("stale completion", encoding="utf-8")
            command_completion.symlink_to(root / "missing-completion")
            target = root / "new-codex-tools"
            target.touch()

            with patch.object(aliases, "target_path", return_value=target):
                result = aliases.main(
                    [
                        "--bin-dir",
                        str(bin_dir),
                        "--completion-dir",
                        str(completion_dir),
                        "install",
                        "--force",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.readlink(), target)
            self.assertTrue(ct_completion.is_symlink())
            self.assertEqual(ct_completion.readlink(), Path("codex-tools"))
            self.assertEqual(
                command_completion.read_text(encoding="utf-8"),
                aliases.COMPLETION_SCRIPT,
            )

    def test_completion_covers_current_command_groups(self) -> None:
        expected = {
            "search summary viewer manager app-server alias structured config diagnose",
            "today yesterday day week model site clean",
            "serve start restart stop status open pick doctor",
            "new rename export import list run install uninstall remove rm delete path doctor repair",
            "login status usage list logout test",
            "install remove list",
            "run batch check",
            "show path edit validate set unset",
        }

        for commands in expected:
            self.assertIn(f'="{commands}"', aliases.COMPLETION_SCRIPT)
        self.assertIn(
            "complete -F _codex_tools_completion ct codex-tools",
            aliases.COMPLETION_SCRIPT,
        )

    def test_force_does_not_replace_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            completion_dir = root / "completion"
            (bin_dir / "ct").mkdir(parents=True)

            with patch.object(aliases, "target_path", return_value=Path("/bin/true")):
                result = aliases.main(
                    [
                        "--bin-dir",
                        str(bin_dir),
                        "--completion-dir",
                        str(completion_dir),
                        "install",
                        "--force",
                    ]
                )

            self.assertEqual(result, 2)
            self.assertTrue((bin_dir / "ct").is_dir())

    def test_remove_cleans_up_alias_and_both_completion_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            completion_dir = root / "completion"
            bin_dir.mkdir()
            completion_dir.mkdir()
            (bin_dir / "ct").touch()
            for name in aliases.COMPLETION_NAMES:
                (completion_dir / name).touch()

            result = aliases.main(
                [
                    "--bin-dir",
                    str(bin_dir),
                    "--completion-dir",
                    str(completion_dir),
                    "remove",
                ]
            )

            self.assertEqual(result, 0)
            self.assertFalse((bin_dir / "ct").exists())
            for name in aliases.COMPLETION_NAMES:
                self.assertFalse((completion_dir / name).exists())


if __name__ == "__main__":
    unittest.main()
