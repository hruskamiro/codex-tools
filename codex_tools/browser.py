"""Shared browser selection and new-window opening support."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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
SYSTEM_OPENERS = (
    ("xdg-open",),
    ("gio", "open"),
)


def add_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--browser",
        help=(
            "Browser command. Default: CODEX_TOOLS_BROWSER, then "
            "CODEX_VIEWER_BROWSER, BROWSER, then the desktop default browser."
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
    for opener in SYSTEM_OPENERS:
        found = shutil.which(opener[0])
        if found:
            return [found, *opener[1:]]
    return None


def supports_new_window(command: list[str]) -> bool:
    executable = Path(command[0]).name
    return executable in BROWSER_CANDIDATES


def open_browser(url: str, args: argparse.Namespace | None = None) -> bool:
    configured = getattr(args, "browser", None) if args is not None else None
    same_window = bool(getattr(args, "same_window", False)) if args is not None else False
    command = browser_command(configured)
    if not command:
        try:
            opened = webbrowser.open_new(url)
        except (OSError, webbrowser.Error) as exc:
            print(f"error: could not start a browser: {exc}", file=sys.stderr)
            return False
        if not opened:
            print(
                "error: could not find or start a browser; use --browser COMMAND",
                file=sys.stderr,
            )
        return bool(opened)

    if any("%s" in part for part in command):
        command = [part.replace("%s", url) for part in command]
    else:
        if not same_window and supports_new_window(command):
            command.append("--new-window")
        command.append(url)
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error_log:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=error_log,
            )
            try:
                return_code = process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                return True
            if return_code == 0:
                return True
            error_log.seek(0)
            detail = error_log.read().strip()
    except OSError as exc:
        detail = str(exc)
    rendered = shlex.join(command)
    suffix = f": {detail}" if detail else ""
    print(f"error: browser command failed ({rendered}){suffix}", file=sys.stderr)
    return False
