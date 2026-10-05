from __future__ import annotations

import argparse
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

from codex_tools import browser


class BrowserTests(unittest.TestCase):
    def test_browser_command_uses_desktop_opener_by_default(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                browser.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
            ),
        ):
            command = browser.browser_command()

        self.assertEqual(command, ["/usr/bin/xdg-open"])

    def test_default_browser_resolves_desktop_entry_for_new_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            applications = Path(temporary) / "applications"
            applications.mkdir()
            (applications / "brave-browser.desktop").write_text(
                "[Desktop Entry]\n"
                "Name=Brave\n"
                "Exec=/usr/bin/brave-browser-stable %U\n"
                "[Desktop Action new-window]\n"
                "Exec=/usr/bin/brave-browser-stable --new-window\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="brave-browser.desktop\n"
            )
            with (
                patch.dict(
                    os.environ,
                    {"XDG_DATA_HOME": temporary, "XDG_DATA_DIRS": ""},
                    clear=True,
                ),
                patch.object(
                    browser.shutil,
                    "which",
                    side_effect=lambda name: (
                        "/usr/bin/xdg-settings" if name == "xdg-settings" else None
                    ),
                ),
                patch.object(browser.subprocess, "run", return_value=completed),
            ):
                command = browser.browser_command()

        self.assertEqual(command, ["/usr/bin/brave-browser-stable"])

    def test_resolved_default_browser_opens_a_new_window(self) -> None:
        args = argparse.Namespace(browser=None, same_window=False)
        with (
            patch.object(
                browser,
                "browser_command",
                return_value=["/usr/bin/brave-browser-stable"],
            ),
            patch.object(browser.subprocess, "Popen") as popen,
        ):
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired(
                "brave-browser-stable", 0.5
            )
            opened = browser.open_browser("http://127.0.0.1:8765/", args)

        self.assertTrue(opened)
        self.assertEqual(
            popen.call_args.args[0],
            [
                "/usr/bin/brave-browser-stable",
                "--new-window",
                "http://127.0.0.1:8765/",
            ],
        )

    def test_explicit_browser_takes_precedence(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            command = browser.browser_command("firefox --private-window")

        self.assertEqual(command, ["firefox", "--private-window"])

    def test_open_browser_reports_immediate_failure(self) -> None:
        args = argparse.Namespace(browser=None, same_window=False)
        error = io.StringIO()
        with (
            patch.object(browser, "browser_command", return_value=["xdg-open"]),
            patch.object(browser.subprocess, "Popen") as popen,
            redirect_stderr(error),
        ):
            popen.return_value.wait.return_value = 3
            opened = browser.open_browser("http://127.0.0.1:8765/", args)

        self.assertFalse(opened)
        self.assertIn("browser command failed", error.getvalue())

    def test_open_browser_accepts_long_running_browser(self) -> None:
        args = argparse.Namespace(browser="firefox", same_window=False)
        with patch.object(browser.subprocess, "Popen") as popen:
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired(
                "firefox", 0.5
            )
            opened = browser.open_browser("http://127.0.0.1:8765/", args)

        self.assertTrue(opened)
        self.assertEqual(
            popen.call_args.args[0],
            ["firefox", "--new-window", "http://127.0.0.1:8765/"],
        )


if __name__ == "__main__":
    unittest.main()
