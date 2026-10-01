"""Command routing for Codex summary utilities."""

from __future__ import annotations

import argparse
import sys

from codex_tools import (
    summarize_daily,
    summarize_weekly,
    summary_clean,
    summary_common,
    summary_site,
)


def help_text() -> str:
    return """\
usage: codex-tools summary <command> [options]

Commands:
  today       Summarize today's local Codex work.
  yesterday   Summarize yesterday's local Codex work.
  day DATE    Summarize a specific local date.
  week        Summarize a week, refreshing missing or stale daily summaries.
  model       Show or set the default model used for summaries.
  site        Build a static archive site from saved summaries.
  clean       Remove saved summaries and the generated site.

Examples:
  codex-tools summary today --show-context
  codex-tools summary today --model gpt-6-astra
  codex-tools summary model gpt-6-astra
  codex-tools summary day 2026-09-11
  codex-tools summary week --last-week
  codex-tools summary site --open
  codex-tools summary clean
"""


def model_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-tools summary model",
        description="Show or set the default model used for summaries.",
    )
    parser.add_argument(
        "model",
        nargs="?",
        help="Model to use by default, for example gpt-6.1-sol.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Use the selected Codex profile's default model.",
    )
    args = parser.parse_args(argv)
    if args.reset and args.model:
        parser.error("MODEL and --reset cannot be used together")
    if args.reset:
        summary_common.write_default_model(None)
    elif args.model:
        summary_common.write_default_model(args.model)

    selected = summary_common.read_default_model()
    if selected:
        print(f"Default summary model: {selected}")
    else:
        print("Default summary model: Codex profile default")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = [] if argv is None else list(argv)
    if not args or args[0] in {"-h", "--help"}:
        print(help_text())
        return 0

    command = args[0]
    rest = args[1:]
    if command == "today":
        return summarize_daily.main(rest)
    if command == "yesterday":
        return summarize_daily.main(
            ["--yesterday", *rest], prog="codex-tools summary yesterday"
        )
    if command == "day":
        if rest and rest[0] in {"-h", "--help"}:
            return summarize_daily.main(["--help"], prog="codex-tools summary day")
        if not rest:
            print("usage: codex-tools summary day DATE [options]")
            return 2
        return summarize_daily.main(
            ["--date", rest[0], *rest[1:]], prog="codex-tools summary day"
        )
    if command == "week":
        return summarize_weekly.main(rest)
    if command == "model":
        return model_main(rest)
    if command == "site":
        return summary_site.main(rest)
    if command == "clean":
        return summary_clean.main(rest)

    print(f"error: unknown summary command: {command}", file=sys.stderr)
    print("Run `codex-tools summary --help` for usage.", file=sys.stderr)
    return 2
