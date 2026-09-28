"""Shared support for model-backed summary commands."""

from __future__ import annotations

import argparse
import subprocess
import tempfile

from codex_tools import manager


TRUNCATION_MARKER = "\n\n_Context truncated by --max-context-chars._\n\n"


def truncate_context(value: str, limit: int) -> str:
    """Limit context while retaining both initial framing and newest material."""
    if len(value) <= limit:
        return value
    available = max(0, limit - len(TRUNCATION_MARKER))
    head_size = available // 2
    tail_size = available - head_size
    head = value[:head_size].rstrip()
    tail = value[-tail_size:].lstrip() if tail_size else ""
    return head + TRUNCATION_MARKER + tail


def run_codex_exec(args: argparse.Namespace, prompt: str) -> str:
    """Run a read-only, ephemeral Codex turn and return its final message."""
    with tempfile.NamedTemporaryFile("r", encoding="utf-8", delete=True) as output:
        command = [
            args.codex_bin,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            output.name,
            "-",
        ]
        if args.model:
            command[2:2] = ["--model", args.model]

        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=manager.profile_environment(
                args.profile, args.manager_root, args.default_home
            ),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "codex exec failed with exit code "
                f"{completed.returncode}\n\nSTDERR:\n{completed.stderr}\n\nSTDOUT:\n{completed.stdout}"
            )
        output.seek(0)
        summary = output.read().strip()
        return summary or completed.stdout.strip()
