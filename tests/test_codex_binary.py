from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import codex_binary


class CodexBinaryTests(unittest.TestCase):
    def executable(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def test_ssh_path_missing_codex_finds_common_user_installations(self):
        for directory in (".local/bin", ".volta/bin", "bin", ".cargo/bin", ".npm-global/bin"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                executable = self.executable(root / directory / "codex")
                with patch.object(codex_binary.shutil, "which", return_value=None), \
                     patch.object(codex_binary.Path, "home", return_value=root):
                    self.assertEqual(codex_binary.resolve(), str(executable))

    def test_path_takes_precedence_over_fallback_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = self.executable(root / "chosen/codex")
            self.executable(root / ".volta/bin/codex")
            with patch.object(codex_binary.shutil, "which", return_value=str(executable)), \
                 patch.object(codex_binary.Path, "home", return_value=root):
                self.assertEqual(codex_binary.resolve(), str(executable))

    def test_preserves_version_manager_symlink_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.executable(root / ".volta/bin/volta-shim")
            executable = shim.with_name("codex")
            executable.symlink_to(shim)
            with patch.object(codex_binary.shutil, "which", return_value=None), \
                 patch.object(codex_binary.Path, "home", return_value=root):
                self.assertEqual(codex_binary.resolve(), str(executable))

    def test_missing_or_nonexecutable_binary_does_not_prevent_readonly_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = self.executable(root / ".local/bin/codex")
            executable.chmod(0o600)
            with patch.object(codex_binary.shutil, "which", return_value=None), \
                 patch.object(codex_binary.Path, "home", return_value=root):
                self.assertEqual(codex_binary.resolve(), "codex")

    def test_explicit_command_is_not_replaced_with_another_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.executable(root / ".volta/bin/codex")
            with patch.object(codex_binary.shutil, "which", return_value=None), \
                 patch.object(codex_binary.Path, "home", return_value=root):
                self.assertEqual(codex_binary.resolve("custom-codex"), "custom-codex")
                self.assertEqual(codex_binary.resolve("/explicit/codex"), "/explicit/codex")

    def test_managed_native_cli_works_without_nvm_or_node_on_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = self.executable(root / ".codex/packages/app-server-daemon/current/bin/codex")
            with patch.object(codex_binary.shutil, "which", return_value=None), \
                 patch.object(codex_binary.Path, "home", return_value=root):
                self.assertEqual(codex_binary.resolve(), str(executable))


if __name__ == "__main__":
    unittest.main()
