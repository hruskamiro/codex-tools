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
    "brave-browser-stable",
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
DESKTOP_FIELD_CODES = frozenset("fFuUdDnNickvm")


def add_browser_args(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
    default_label: str | None = None,
) -> None:
    default_help = (
        f"Effective default: {default_label}."
        if default_label
        else (
            "Default: CODEX_TOOLS_BROWSER, then CODEX_VIEWER_BROWSER, BROWSER, "
            "then the desktop default browser."
        )
    )
    parser.add_argument(
        "--browser",
        default=default,
        help=f"Browser command. {default_help}",
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
    desktop_command = default_desktop_browser_command()
    if desktop_command and supports_new_window(desktop_command):
        return desktop_command
    for opener in SYSTEM_OPENERS:
        found = shutil.which(opener[0])
        if found:
            return [found, *opener[1:]]
    return None


def desktop_entry_paths(desktop_id: str) -> list[Path]:
    if not desktop_id.endswith(".desktop") or Path(desktop_id).name != desktop_id:
        return []
    data_home = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    roots = [data_home, *(Path(value) for value in data_dirs.split(":") if value)]
    return [root / "applications" / desktop_id for root in roots]


def desktop_entry_command(path: Path) -> list[str] | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_desktop_entry = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_desktop_entry = stripped == "[Desktop Entry]"
            continue
        if not in_desktop_entry or not stripped.startswith("Exec="):
            continue
        try:
            parts = shlex.split(stripped.removeprefix("Exec="))
        except ValueError:
            return None
        command = []
        for part in parts:
            if len(part) == 2 and part[0] == "%" and part[1] in DESKTOP_FIELD_CODES:
                continue
            command.append(part.replace("%%", "%"))
        return command or None
    return None


def default_desktop_browser_command() -> list[str] | None:
    xdg_settings = shutil.which("xdg-settings")
    if not xdg_settings:
        return None
    try:
        result = subprocess.run(
            [xdg_settings, "get", "default-web-browser"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    desktop_id = result.stdout.strip()
    for path in desktop_entry_paths(desktop_id):
        command = desktop_entry_command(path)
        if command:
            return command
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
