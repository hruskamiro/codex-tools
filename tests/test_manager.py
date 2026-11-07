from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_tools import manager


class ManagerCommandTests(unittest.TestCase):
    def test_new_profiles_share_live_sqlite_stores_in_wal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default, managed = root / "default", root / "manager"
            default.mkdir()
            common = ["--manager-root", str(managed), "--default-home", str(default)]
            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(manager.main([*common, "new", "work"]), 0)
                self.assertEqual(manager.main([*common, "new", "client", "--share", "work"]), 0)
                self.assertEqual(manager.main([*common, "new", "private", "--isolated"]), 0)
            work = managed / "profiles/work/home"
            client = managed / "profiles/client/home"
            private = managed / "profiles/private/home"
            for name in ("state_5.sqlite", "thread_history_1.sqlite"):
                with self.subTest(database=name):
                    self.assertEqual((work / name).resolve(), default / name)
                    self.assertEqual((client / name).resolve(), default / name)
                    self.assertEqual((private / name).resolve(), managed / "stores/private-conversations" / name)
                    self.assertEqual((default / name).stat().st_size, 0)
                    # Multiple live connections through different home paths must
                    # see commits and share WAL locks, including the first open.
                    first = sqlite3.connect(work / name, timeout=0)
                    second = sqlite3.connect(client / name, timeout=0)
                    base = sqlite3.connect(default / name, timeout=0)
                    try:
                        self.assertEqual(first.execute("pragma journal_mode=wal").fetchone()[0], "wal")
                        first.execute("create table messages (id integer)")
                        first.execute("insert into messages values (1)")
                        first.commit()
                        self.assertEqual(second.execute("select * from messages").fetchall(), [(1,)])
                        second.execute("insert into messages values (2)")
                        with self.assertRaises(sqlite3.OperationalError):
                            first.execute("begin immediate")
                        second.commit()
                        self.assertEqual(base.execute("select count(*) from messages").fetchone()[0], 2)
                        self.assertTrue((default / (name + "-wal")).exists())
                        self.assertTrue((default / (name + "-shm")).exists())
                        for profile_home in (work, client):
                            self.assertFalse((profile_home / (name + "-wal")).exists())
                            self.assertFalse((profile_home / (name + "-shm")).exists())
                        isolated = sqlite3.connect(private / name)
                        try:
                            self.assertEqual(isolated.execute("select name from sqlite_master where name='messages'").fetchall(), [])
                        finally:
                            isolated.close()
                        with patch("sys.stdout", new_callable=StringIO):
                            self.assertEqual(manager.main([*common, "repair", "work", "--force"]), 0)
                        self.assertTrue((work / name).is_symlink())
                        self.assertEqual(base.execute("select count(*) from messages").fetchone()[0], 2)
                    finally:
                        first.close()
                        second.close()
                        base.close()

    def test_legacy_prefixed_wrapper_remains_manageable(self) -> None:
        for action in ('rename', 'uninstall'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                default = root / 'default'
                default.mkdir()
                managed, bin_dir = root / 'manager', root / 'bin'
                bin_dir.mkdir()
                common = ['--manager-root', str(managed), '--default-home', str(default)]
                with patch('sys.stdout', new_callable=StringIO):
                    self.assertEqual(manager.main([*common, 'new', 'work']), 0)
                    legacy = bin_dir / 'codex-work'
                    legacy.write_text(manager.wrapper_script('work', 'codex', managed, default))
                    legacy.chmod(0o755)
                    arguments = ['rename', 'work', 'company'] if action == 'rename' else ['uninstall', 'work']
                    self.assertEqual(manager.main([*common, *arguments, '--bin-dir', str(bin_dir)]), 0)
                self.assertFalse(legacy.exists())
                self.assertEqual((bin_dir / 'company').exists(), action == 'rename')
                self.assertFalse((bin_dir / 'codex-company').exists())

    def test_plain_wrapper_names_preserve_unrelated_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = root / 'default'
            default.mkdir()
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            command = bin_dir / 'work'
            command.write_text('unrelated command')
            common = ['--manager-root', str(root / 'manager'), '--default-home', str(default)]
            with patch('sys.stdout', new_callable=StringIO), patch('sys.stderr', new_callable=StringIO):
                self.assertEqual(manager.main([*common, 'new', 'work']), 0)
                for action in ('install', 'uninstall'):
                    self.assertEqual(manager.main([*common, action, 'work', '--bin-dir', str(bin_dir)]), 2)
                    self.assertEqual(command.read_text(), 'unrelated command')

    def test_wrapper_cannot_shadow_its_codex_binary(self) -> None:
        for action in ('install', 'rename'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                default = root / 'default'
                default.mkdir()
                managed, bin_dir = root / 'manager', root / 'bin'
                name = 'codex' if action == 'install' else 'work'
                common = ['--manager-root', str(managed), '--default-home', str(default)]
                with patch('sys.stdout', new_callable=StringIO), patch('sys.stderr', new_callable=StringIO):
                    self.assertEqual(manager.main([*common, 'new', name]), 0)
                    if action == 'rename':
                        self.assertEqual(manager.main([*common, 'install', name, '--bin-dir', str(bin_dir)]), 0)
                    arguments = ['install', name] if action == 'install' else ['rename', name, 'codex']
                    self.assertEqual(manager.main([*common, *arguments, '--bin-dir', str(bin_dir)]), 2)
                self.assertFalse((bin_dir / 'codex').exists())
                self.assertTrue((managed / 'profiles' / name).is_dir())

    def test_unified_manager_help_uses_the_unified_command_name(self) -> None:
        with patch("sys.stdout", new_callable=StringIO) as stdout:
            parser = manager.build_parser("codex-tools manager")
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("usage: codex-tools manager", stdout.getvalue())

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

    def test_export_import_renames_profile_and_shares_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_manager = root / "source-manager"
            source_default = root / "source-default"
            source_default.mkdir()
            (source_default / "config.toml").write_text(
                'cli_auth_credentials_store = "auto"\nmodel = "test-model"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                manager.main(
                    [
                        "--manager-root",
                        str(source_manager),
                        "--default-home",
                        str(source_default),
                        "new",
                        "work",
                    ]
                ),
                0,
            )
            source_home = source_manager / "profiles" / "work" / "home"
            (source_home / "auth.json").write_text(
                '{"secret":"test-only"}\n', encoding="utf-8"
            )
            bundle = root / "work.codex-profile"
            with patch(
                "codex_tools.manager.getpass.getpass",
                side_effect=["correct horse", "correct horse"],
            ):
                result = manager.main(
                    [
                        "--manager-root",
                        str(source_manager),
                        "--default-home",
                        str(source_default),
                        "export",
                        "work",
                        "--output",
                        str(bundle),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b"test-only", bundle.read_bytes())

            target_manager = root / "target-manager"
            target_default = root / "target-default"
            bin_dir = root / "bin"
            with patch(
                "codex_tools.manager.getpass.getpass", return_value="correct horse"
            ):
                result = manager.main(
                    [
                        "--manager-root",
                        str(target_manager),
                        "--default-home",
                        str(target_default),
                        "import",
                        str(bundle),
                        "--name",
                        "laptop-work",
                        "--install",
                        "--bin-dir",
                        str(bin_dir),
                    ]
                )

            self.assertEqual(result, 0)
            imported = target_manager / "profiles" / "laptop-work"
            imported_home = imported / "home"
            self.assertEqual(
                (imported_home / "auth.json").read_text(encoding="utf-8"),
                '{"secret":"test-only"}\n',
            )
            self.assertEqual(
                (imported_home / "config.toml").read_text(encoding="utf-8"),
                'cli_auth_credentials_store = "file"\nmodel = "test-model"\n',
            )
            self.assertEqual((imported_home / "auth.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (imported_home / "sessions").resolve(),
                (target_default / "sessions").resolve(),
            )
            self.assertTrue((bin_dir / "laptop-work").is_file())
            for name in ("state_5.sqlite", "thread_history_1.sqlite"):
                self.assertTrue((imported_home / name).is_symlink())
                self.assertEqual((imported_home / name).resolve(), target_default / name)
                self.assertEqual((target_default / name).stat().st_mode & 0o777, 0o600)
            metadata = json.loads(
                (imported / "profile.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["wrapper"]["command"], "laptop-work")

    def test_rename_updates_default_wrapper_and_sharing_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_root = root / "manager"
            default_home = root / "default"
            bin_dir = root / "bin"
            default_home.mkdir()
            common = [
                "--manager-root",
                str(manager_root),
                "--default-home",
                str(default_home),
            ]
            self.assertEqual(manager.main([*common, "new", "work", "--isolated"]), 0)
            self.assertEqual(manager.main([*common, "new", "client", "--share", "work"]), 0)
            self.assertEqual(
                manager.main([*common, "install", "work", "--bin-dir", str(bin_dir)]),
                0,
            )

            old_wrapper = bin_dir / "work"
            self.assertTrue(old_wrapper.is_file())
            self.assertEqual(manager.main([*common, "rename", "work", "company"]), 0)

            new_wrapper = bin_dir / "company"
            self.assertFalse(old_wrapper.exists())
            self.assertTrue(new_wrapper.is_file())
            self.assertIn("company", new_wrapper.read_text(encoding="utf-8"))
            self.assertFalse((manager_root / "profiles" / "work").exists())
            renamed = json.loads(
                (manager_root / "profiles" / "company" / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(renamed["wrapper"]["command"], "company")
            self.assertEqual(renamed["share"], "isolated")
            dependent = json.loads(
                (manager_root / "profiles" / "client" / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(dependent["share"], "company")
            self.assertTrue((manager_root / "stores" / "work-conversations").is_dir())

            self.assertEqual(
                manager.main(
                    [*common, "uninstall", "company", "--bin-dir", str(bin_dir)]
                ),
                0,
            )
            self.assertFalse(new_wrapper.exists())
            renamed = json.loads(
                (manager_root / "profiles" / "company" / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("wrapper", renamed)

    def test_rename_preserves_custom_wrapper_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_root = root / "manager"
            default_home = root / "default"
            bin_dir = root / "bin"
            default_home.mkdir()
            common = [
                "--manager-root",
                str(manager_root),
                "--default-home",
                str(default_home),
            ]
            self.assertEqual(manager.main([*common, "new", "work"]), 0)
            self.assertEqual(
                manager.main(
                    [
                        *common,
                        "install",
                        "work",
                        "--bin-dir",
                        str(bin_dir),
                        "--wrapper",
                        "cw",
                    ]
                ),
                0,
            )
            self.assertEqual(manager.main([*common, "rename", "work", "company"]), 0)

            custom_wrapper = bin_dir / "cw"
            self.assertTrue(custom_wrapper.is_file())
            self.assertIn("company", custom_wrapper.read_text(encoding="utf-8"))
            metadata = json.loads(
                (manager_root / "profiles" / "company" / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["wrapper"]["command"], "cw")

    def test_rename_adopts_legacy_untracked_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_root = root / "manager"
            default_home = root / "default"
            bin_dir = root / "bin"
            default_home.mkdir()
            bin_dir.mkdir()
            common = [
                "--manager-root",
                str(manager_root),
                "--default-home",
                str(default_home),
            ]
            self.assertEqual(manager.main([*common, "new", "codex-mom"]), 0)
            legacy_wrapper = bin_dir / "codex-mom"
            legacy_wrapper.write_text(
                manager.wrapper_script(
                    "codex-mom", "codex", manager_root.resolve(), default_home.resolve()
                ),
                encoding="utf-8",
            )
            legacy_wrapper.chmod(0o755)

            self.assertEqual(
                manager.main(
                    [
                        *common,
                        "rename",
                        "codex-mom",
                        "mom",
                        "--bin-dir",
                        str(bin_dir),
                    ]
                ),
                0,
            )

            self.assertFalse(legacy_wrapper.exists())
            renamed_wrapper = bin_dir / "mom"
            self.assertTrue(renamed_wrapper.is_file())
            self.assertIn("mom", renamed_wrapper.read_text(encoding="utf-8"))
            metadata = json.loads(
                (manager_root / "profiles" / "mom" / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["wrapper"]["command"], "mom")

    def test_exported_conversations_restore_to_private_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_manager = root / "source-manager"
            source_default = root / "source-default"
            source_default.mkdir()
            self.assertEqual(
                manager.main(
                    [
                        "--manager-root",
                        str(source_manager),
                        "--default-home",
                        str(source_default),
                        "new",
                        "work",
                    ]
                ),
                0,
            )
            source_home = source_manager / "profiles" / "work" / "home"
            (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
            session = source_default / "sessions" / "2026" / "example.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text('{"message":"portable"}\n', encoding="utf-8")
            shell_snapshot = source_default / "shell_snapshots" / "machine.sh"
            shell_snapshot.write_text("machine-only\n", encoding="utf-8")

            bundle = root / "work.codex-profile"
            with patch(
                "codex_tools.manager.getpass.getpass",
                side_effect=["correct horse", "correct horse"],
            ):
                self.assertEqual(
                    manager.main(
                        [
                            "--manager-root",
                            str(source_manager),
                            "--default-home",
                            str(source_default),
                            "export",
                            "work",
                            "--include-conversations",
                            "--output",
                            str(bundle),
                        ]
                    ),
                    0,
                )

            target_manager = root / "target-manager"
            target_default = root / "target-default"
            with patch(
                "codex_tools.manager.getpass.getpass", return_value="correct horse"
            ):
                result = manager.main(
                    [
                        "--manager-root",
                        str(target_manager),
                        "--default-home",
                        str(target_default),
                        "import",
                        str(bundle),
                    ]
                )

            self.assertEqual(result, 0)
            store = target_manager / "stores" / "work-conversations"
            self.assertEqual(
                (store / "sessions" / "2026" / "example.jsonl").read_text(
                    encoding="utf-8"
                ),
                '{"message":"portable"}\n',
            )
            self.assertFalse((store / "shell_snapshots" / "machine.sh").exists())

    def test_import_existing_profile_requires_force_and_preserves_store(self) -> None:
        cases = (
            "without-force", "auth-only", "with-config", "private-store",
            "wrong-password", "isolation-conflict", "conversation-conflict",
            "wrapper-conflict", "write-failure",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_root = root / "source"
                target_root = root / "target"
                default = root / "default"
                default.mkdir()
                common = ["--manager-root", str(target_root), "--default-home", str(default)]
                source_args = ["--manager-root", str(source_root), "--default-home", str(default)]
                with patch("sys.stdout", new_callable=StringIO):
                    self.assertEqual(manager.main(source_args + ["new", "work"]), 0)
                    isolation = ["--isolated"] if case == "private-store" else []
                    self.assertEqual(manager.main(common + ["new", "work"] + isolation), 0)
                source_home = source_root / "profiles/work/home"
                (source_home / "auth.json").write_text('{"token":"new"}\n')
                if case == "with-config":
                    (source_home / "config.toml").write_text('model = "imported"\n')
                else:
                    (source_home / "config.toml").unlink(missing_ok=True)
                plain = root / "plain.zip"
                bundle = root / "work.codex-profile"
                profile = manager.load_profile(source_root, "work", default)
                manager.create_profile_archive(plain, profile, case == "conversation-conflict")
                manager.profile_bundle.encrypt_file(plain, bundle, "test-password")

                home = target_root / "profiles/work/home"
                (home / "auth.json").write_text('{"token":"old"}\n')
                (home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\nmodel = "local"\n')
                before_auth = (home / "auth.json").read_bytes()
                before_config = (home / "config.toml").read_bytes()
                metadata = target_root / "profiles/work/profile.json"
                before_metadata = metadata.read_bytes()
                # Simulate a legacy profile with private database copies. A
                # forced login refresh must not convert or replace its stores.
                database_contents = {}
                for name in ("state_5.sqlite", "thread_history_1.sqlite"):
                    database = home / name
                    database.unlink()
                    with sqlite3.connect(database) as connection:
                        connection.execute("create table preserved (value integer)")
                        connection.execute("insert into preserved values (42)")
                    database_contents[name] = database.read_bytes()
                sessions = (home / "sessions").resolve()
                conversation = sessions / "keep.jsonl"
                conversation.write_text("existing conversation\n")
                args = common + ["import", str(bundle)]
                if case != "without-force":
                    args += ["--force"]
                if case == "isolation-conflict":
                    args += ["--isolated"]
                if case == "wrapper-conflict":
                    bin_dir = root / "bin"
                    bin_dir.mkdir()
                    (bin_dir / "work").write_text("existing wrapper")
                    args += ["--install", "--bin-dir", str(bin_dir)]
                password = "wrong-password" if case == "wrong-password" else "test-password"
                real_replace = os.replace

                def replace(source, target):
                    if case == "write-failure" and Path(source).name == "auth.json":
                        raise FileExistsError("simulated credential replacement failure")
                    return real_replace(source, target)

                with patch("codex_tools.manager.prompt_import_passphrase", return_value=password), patch(
                    "sys.stderr", new_callable=StringIO
                ) as stderr, patch("sys.stdout", new_callable=StringIO), patch(
                    "codex_tools.manager.os.replace", side_effect=replace
                ):
                    result = manager.main(args)
                success = case in {"auth-only", "with-config", "private-store"}
                self.assertEqual(result, 0 if success else 2)
                self.assertEqual(metadata.read_bytes(), before_metadata)
                for name, contents in database_contents.items():
                    self.assertFalse((home / name).is_symlink())
                    self.assertEqual((home / name).read_bytes(), contents)
                self.assertEqual((home / "sessions").resolve(), sessions)
                self.assertEqual(conversation.read_text(), "existing conversation\n")
                if success:
                    self.assertEqual((home / "auth.json").read_text(), '{"token":"new"}\n')
                    model = "imported" if case == "with-config" else "local"
                    self.assertIn(f'model = "{model}"', (home / "config.toml").read_text())
                    self.assertIn('cli_auth_credentials_store = "file"', (home / "config.toml").read_text())
                    for name in ("auth.json", "config.toml"):
                        self.assertEqual((home / name).stat().st_mode & 0o777, 0o600)
                else:
                    self.assertEqual((home / "auth.json").read_bytes(), before_auth)
                    self.assertEqual((home / "config.toml").read_bytes(), before_config)
                    self.assertNotIn("Traceback", stderr.getvalue())
                if case == "without-force":
                    self.assertIn("--force", stderr.getvalue())
                self.assertFalse(list(home.glob(".profile-import-*")))

    def test_wrong_import_passphrase_leaves_no_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_manager = root / "source-manager"
            source_default = root / "source-default"
            source_default.mkdir()
            self.assertEqual(
                manager.main(
                    [
                        "--manager-root",
                        str(source_manager),
                        "--default-home",
                        str(source_default),
                        "new",
                        "work",
                    ]
                ),
                0,
            )
            source_home = source_manager / "profiles" / "work" / "home"
            (source_home / "auth.json").write_text("{}\n", encoding="utf-8")
            bundle = root / "work.codex-profile"
            with patch(
                "codex_tools.manager.getpass.getpass",
                side_effect=["correct horse", "correct horse"],
            ):
                self.assertEqual(
                    manager.main(
                        [
                            "--manager-root",
                            str(source_manager),
                            "--default-home",
                            str(source_default),
                            "export",
                            "work",
                            "--output",
                            str(bundle),
                        ]
                    ),
                    0,
                )

            target_manager = root / "target-manager"
            with patch(
                "codex_tools.manager.getpass.getpass", return_value="wrong password"
            ), patch("sys.stderr", new_callable=StringIO) as stderr:
                result = manager.main(
                    [
                        "--manager-root",
                        str(target_manager),
                        "--default-home",
                        str(root / "target-default"),
                        "import",
                        str(bundle),
                    ]
                )

            self.assertEqual(result, 2)
            self.assertIn("incorrect passphrase", stderr.getvalue())
            self.assertFalse((target_manager / "profiles" / "work").exists())


if __name__ == "__main__":
    unittest.main()
