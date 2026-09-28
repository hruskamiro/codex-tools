"""Shared browser selection and new-window opening support."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import webbrowser
from pathlib import Path


BROWSER_CANDIDATES = (
    "brave-browser",
    "brave",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "firefox",
)


def add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--browser",
        help=(
            "Browser command. Default: CODEX_TOOLS_BROWSER, then "
            "CODEX_VIEWER_BROWSER, Brave/Chrome/Chromium/Firefox, then webbrowser."
        ),
    )
    parser.add_argument(
        "--same-window",
        action="store_true",
        help="Do not pass --new-window to known browsers.",
    )


def browser_command(configured_browser: str | None = None) -> list[str] | None:
    configured = (
        configured_browser
        or os.environ.get("CODEX_TOOLS_BROWSER")
        or os.environ.get("CODEX_VIEWER_BROWSER")
        or os.environ.get("BROWSER")
    )
    if configured:
        return shlex.split(configured)
    for candidate in BROWSER_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return [found]
    return None


def supports_new_window(command: list[str]) -> bool:
    executable = Path(command[0]).name
    return executable in BROWSER_CANDIDATES


def open_browser(url: str, args: argparse.Namespace | None = None) -> None:
    configured = getattr(args, "browser", None) if args is not None else None
    same_window = bool(getattr(args, "same_window", False)) if args is not None else False
    command = browser_command(configured)
    if not command:
        webbrowser.open_new(url)
        return

    if any("%s" in part for part in command):
        command = [part.replace("%s", url) for part in command]
    else:
        if not same_window and supports_new_window(command):
            command.append("--new-window")
        command.append(url)
    try:
        subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError:
        webbrowser.open_new(url)
