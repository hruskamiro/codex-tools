from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_tools import manager


class ManagerCommandTests(unittest.TestCase):
    def test_command_name_must_be_a_plain_filename(self) -> None:
        parser = manager.build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["install", "work", "--command-name", "../outside"]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["uninstall", "work", "--command-name", "/tmp/outside"]
            )

    def test_isolated_profile_data_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_root = root / "manager"
            default_home = root / "default"
            default_home.mkdir()

            result = manager.main(
                [
                    "--manager-root",
                    str(manager_root),
                    "--default-home",
                    str(default_home),
                    "new",
                    "private",
                    "--isolated",
                ]
            )

            self.assertEqual(result, 0)
            profile = manager_root / "profiles" / "private"
            store = manager_root / "stores" / "private-conversations"
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            self.assertEqual((profile / "home").stat().st_mode & 0o777, 0o700)
            self.assertEqual((profile / "profile.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (store / "session_index.jsonl").stat().st_mode & 0o777,
                0o600,
            )


if __name__ == "__main__":
    unittest.main()
