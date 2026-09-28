"""Common filesystem locations for Codex Tools."""

from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "codex-tools"
    return Path("~/.local/share/codex-tools").expanduser()


def user_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "codex-tools"
    return Path("~/.config/codex-tools").expanduser()


def user_state_dir() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / "codex-tools"
    return Path("~/.local/state/codex-tools").expanduser()


def ensure_private_dir(path: Path) -> None:
    """Create a directory and keep it private to the current user."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private_text(path: Path, value: str) -> None:
    """Write sensitive text with owner-only permissions."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    path.chmod(0o600)


SUMMARY_DATA_DIR = user_data_dir() / "summaries"
DAILY_SUMMARIES_DIR = SUMMARY_DATA_DIR / "daily"
WEEKLY_SUMMARIES_DIR = SUMMARY_DATA_DIR / "weekly"
SUMMARY_SITE_DIR = SUMMARY_DATA_DIR / "site"
VIEWER_CONFIG_FILE = user_config_dir() / "viewer.json"
VIEWER_STATE_DIR = user_state_dir() / "viewer"
