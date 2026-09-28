#!/usr/bin/env python3
"""Build a Codex weekly summary from saved daily summaries."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_tools import (
    manager,
    paths,
    search,
    summarize_daily,
    summary_common,
    summary_prompts,
)

DEFAULT_TIMEZONE = "Europe/Prague"
DEFAULT_DAILY_SUMMARIES_DIR = paths.DAILY_SUMMARIES_DIR
DEFAULT_OUTPUT_DIR = paths.WEEKLY_SUMMARIES_DIR
DAILY_SUMMARY_RE = re.compile(
    r"^codex-daily-summary-(?P<day>\d{4}-\d{2}-\d{2})-"
    r"(?:(?P<weekday>[a-z]+)-)?"
    r"(?P<stamp>\d{8}T\d{6}[+-]\d{4})\.md$"
)


@dataclass(frozen=True)
class DailySummary:
    path: Path
    day: date
    timestamp: datetime
    markdown: str
    metadata: dict[str, object] | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools summary week",
        description=(
            "Create a Markdown weekly summary, refreshing missing or stale daily "
            "summaries for dates with Codex activity first."
        ),
    )
    week_group = parser.add_mutually_exclusive_group()
    week_group.add_argument(
        "--week-start",
        help="Monday date for the week to summarize, YYYY-MM-DD.",
    )
    week_group.add_argument(
        "--date",
        help="Summarize the ISO week containing this local date, YYYY-MM-DD.",
    )
    week_group.add_argument(
        "--last-week",
        action="store_true",
        help="Summarize the previous ISO week in --timezone.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone for default date selection. Default: {DEFAULT_TIMEZONE}",
    )
    parser.add_argument(
        "--daily-summaries-dir",
        type=Path,
        default=DEFAULT_DAILY_SUMMARIES_DIR,
        help=(
            "Directory containing saved daily Markdown summaries. "
            f"Default: {DEFAULT_DAILY_SUMMARIES_DIR}"
        ),
    )
    parser.add_argument(
        "--refresh-dailies",
        choices=("auto", "missing", "all", "none"),
        default="auto",
        help=(
            "Daily refresh policy. auto rebuilds missing or stale active days; "
            "default: auto."
        ),
    )
    parser.add_argument(
        "--daily-prompt-template",
        type=Path,
        help="Custom daily prompt template used when refreshing daily summaries.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=search.DEFAULT_SESSIONS_ROOT,
        help=f"Session root to scan when refreshing days. Default: {search.DEFAULT_SESSIONS_ROOT}",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        default=search.DEFAULT_SESSION_INDEX,
        help=argparse.SUPPRESS,
    )
    display_group = parser.add_mutually_exclusive_group()
    display_group.add_argument(
        "--show-context",
        dest="show_context",
        action="store_true",
        help="Print context from saved daily summaries without calling codex exec.",
    )
    display_group.add_argument(
        "--context-only",
        dest="show_context",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    display_group.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the complete rendered model prompt without calling codex exec.",
    )
    display_group.add_argument(
        "--show-template",
        action="store_true",
        help="Print the raw weekly prompt template without reading saved summaries.",
    )
    parser.add_argument(
        "--save-context",
        dest="save_context",
        type=Path,
        help="Write the combined weekly context Markdown to this path.",
    )
    parser.add_argument(
        "--keep-context",
        dest="save_context",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        help="Custom weekly prompt template using $start, $end, and $context placeholders.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write the final Markdown summary to this path as well as stdout. "
            "If omitted, normal summary mode writes a timestamped file under "
            f"{DEFAULT_OUTPUT_DIR}/."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory for the default timestamped weekly output. "
            f"Default: {DEFAULT_OUTPUT_DIR}"
        ),
    )
    parser.add_argument(
        "--model",
        help="Optional model to pass through to codex exec.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable to run. Default: codex",
    )
    parser.add_argument(
        "--profile",
        type=manager.valid_name,
        default="default",
        help="codex-manager profile used for model execution. Default: default.",
    )
    parser.add_argument(
        "--manager-root",
        type=Path,
        default=manager.DEFAULT_MANAGER_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--default-home",
        type=Path,
        default=manager.DEFAULT_CODEX_HOME,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-record-chars",
        type=int,
        default=900,
        help="Maximum characters per transcript message in generated daily summaries.",
    )
    parser.add_argument(
        "--max-context-chars",
        type=int,
        default=120_000,
        help="Maximum combined daily-summary context size sent to codex exec.",
    )
    return parser.parse_args(argv)


def week_start_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def weekday_label(day: date) -> str:
    return day.strftime("%A")


def normalize_daily_markdown(markdown: str, day: date) -> str:
    weekday = weekday_label(day)
    iso = day.isoformat()
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("# "):
            continue
        title = line[2:].strip()
        if weekday in title:
            return markdown
        if title == f"Daily Summary: {iso}":
            lines[index] = f"# Daily Summary: {weekday}, {iso}"
            return "\n".join(lines)
        if iso in title:
            lines[index] = "# " + title.replace(iso, f"{weekday}, {iso}", 1)
            return "\n".join(lines)
        lines[index] = f"# {weekday}, {iso}: {title}"
        return "\n".join(lines)
    return f"# Daily Summary: {weekday}, {iso}\n\n{markdown}"


def target_week_start(args: argparse.Namespace, timezone: ZoneInfo) -> date:
    if args.week_start:
        start = date.fromisoformat(args.week_start)
    elif args.date:
        start = week_start_for(date.fromisoformat(args.date))
    else:
        today = datetime.now(timezone).date()
        start = week_start_for(today)
        if args.last_week:
            start -= timedelta(days=7)

    if start.weekday() != 0:
        raise ValueError(f"week start must be a Monday, got {start.isoformat()}")
    return start


def read_daily_summaries(summaries_dir: Path) -> list[DailySummary]:
    summaries: list[DailySummary] = []
    if not summaries_dir.exists():
        return summaries

    for path in summaries_dir.glob("*.md"):
        match = DAILY_SUMMARY_RE.match(path.name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group("day"))
            timestamp = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%z")
        except ValueError:
            continue
        summaries.append(
            DailySummary(
                path=path,
                day=day,
                timestamp=timestamp,
                markdown=normalize_daily_markdown(
                    path.read_text(encoding="utf-8", errors="replace").strip(), day
                ),
                metadata=read_daily_metadata(path),
            )
        )
    return summaries


def read_daily_metadata(summary_path: Path) -> dict[str, object] | None:
    path = summarize_daily.metadata_path_for(summary_path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def daily_summary_is_fresh(
    summary: DailySummary,
    inputs: summarize_daily.DailySummaryInputs,
) -> bool:
    metadata = summary.metadata
    return bool(
        metadata
        and metadata.get("format_version") == 1
        and metadata.get("day") == summary.day.isoformat()
        and metadata.get("input_fingerprint") == inputs.input_fingerprint
    )


def select_latest_by_day(
    summaries: list[DailySummary], start: date, end: date
) -> list[DailySummary]:
    latest: dict[date, DailySummary] = {}
    for summary in summaries:
        if not start <= summary.day <= end:
            continue
        current = latest.get(summary.day)
        if current is None or summary.timestamp > current.timestamp:
            latest[summary.day] = summary
    return [latest[day] for day in sorted(latest)]


def render_context(summaries: list[DailySummary], start: date, end: date) -> str:
    lines = [
        f"# Codex Weekly Context: {start.isoformat()} to {end.isoformat()}",
        "",
        f"Daily summary count: {len(summaries)}",
        "",
        "This is assembled from saved daily Markdown summaries. Summarize only work represented here.",
        "",
    ]
    if not summaries:
        lines.extend(
            [
                "No saved daily summaries were found for this week.",
                "",
            ]
        )
    for summary in summaries:
        lines.extend(
            [
                f"## Source: {weekday_label(summary.day)}, {summary.day.isoformat()}",
                "",
                f"- File: `{summary.path}`",
                f"- Timestamp: `{summary.timestamp.isoformat()}`",
                "",
                summary.markdown,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def summary_prompt(
    context: str,
    start: date,
    end: date,
    template_path: Path | None = None,
) -> str:
    return summary_prompts.render_template(
        "weekly",
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "context": context,
        },
        template_path,
    )


def default_output_path(
    args: argparse.Namespace, summaries: list[DailySummary], start: date, end: date
) -> Path:
    if summaries:
        timestamp = max(summary.timestamp for summary in summaries)
    else:
        timezone = ZoneInfo(args.timezone)
        timestamp = datetime.combine(end, datetime.min.time(), timezone)
    stamp = timestamp.strftime("%Y%m%dT%H%M%S%z")
    filename = f"codex-weekly-summary-{start.isoformat()}_to_{end.isoformat()}-{stamp}.md"
    return args.output_dir / filename


def refresh_daily_summaries(
    args: argparse.Namespace,
    start: date,
    end: date,
    timezone: ZoneInfo,
) -> list[Path]:
    """Refresh daily summaries according to the selected freshness policy."""
    if args.refresh_dailies == "none":
        return []
    existing = select_latest_by_day(
        read_daily_summaries(args.daily_summaries_dir), start, end
    )
    existing_by_day = {summary.day: summary for summary in existing}
    last_day = min(end, datetime.now(timezone).date())
    created: list[Path] = []
    day = start
    while day <= last_day:
        daily_args = argparse.Namespace(
            sessions_root=args.sessions_root,
            session_index=args.session_index,
            max_record_chars=args.max_record_chars,
            max_context_chars=args.max_context_chars,
            save_context=None,
            show_context=False,
            show_prompt=False,
            prompt_template=args.daily_prompt_template,
            output=None,
            output_dir=args.daily_summaries_dir,
            model=args.model,
            codex_bin=args.codex_bin,
            profile=args.profile,
            manager_root=args.manager_root,
            default_home=args.default_home,
        )
        inputs = summarize_daily.prepare_daily_summary(daily_args, day, timezone)
        current = existing_by_day.get(day)
        if not inputs.sessions:
            day += timedelta(days=1)
            continue

        reason = None
        if current is None:
            reason = "missing"
        elif args.refresh_dailies == "all":
            reason = "forced"
        elif args.refresh_dailies == "auto" and not daily_summary_is_fresh(
            current, inputs
        ):
            reason = "stale"

        if reason is not None:
            generated = summarize_daily.generate_daily_summary(
                daily_args,
                day,
                timezone,
                require_sessions=True,
                prepared=inputs,
            )
            if generated is not None and generated.output_path is not None:
                created.append(generated.output_path)
                print(
                    f"Refreshed {reason} daily summary for {day.isoformat()}: "
                    f"{generated.output_path}",
                    file=sys.stderr,
                )
        day += timedelta(days=1)
    return created


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.show_template:
        try:
            template = summary_prompts.template_text("weekly", args.prompt_template)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(template.rstrip())
        return 0

    try:
        timezone = ZoneInfo(args.timezone)
    except Exception as exc:
        print(f"error: invalid timezone {args.timezone!r}: {exc}", file=sys.stderr)
        return 2

    try:
        start = target_week_start(args, timezone)
    except ValueError as exc:
        print(f"error: invalid week selection: {exc}", file=sys.stderr)
        return 2
    end = start + timedelta(days=6)

    preview = args.show_context or args.show_prompt
    if not preview:
        try:
            refresh_daily_summaries(args, start, end, timezone)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"error: could not refresh daily summaries: {exc}", file=sys.stderr)
            return 1

    daily_summaries = read_daily_summaries(args.daily_summaries_dir)
    selected = select_latest_by_day(daily_summaries, start, end)
    if not selected and not preview:
        print(
            f"error: no Codex activity or saved daily summaries found for "
            f"{start.isoformat()} to {end.isoformat()}",
            file=sys.stderr,
        )
        return 1
    context = render_context(selected, start, end)
    context = summary_common.truncate_context(context, args.max_context_chars)

    if args.save_context:
        args.save_context.parent.mkdir(parents=True, exist_ok=True)
        paths.write_private_text(args.save_context, context)

    if args.show_context:
        result = context
    elif args.show_prompt:
        try:
            result = summary_prompt(context, start, end, args.prompt_template)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            result = summary_common.run_codex_exec(
                args, summary_prompt(context, start, end, args.prompt_template)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    output_path = args.output
    if output_path is None and not preview:
        output_path = default_output_path(args, selected, start, end)

    if output_path:
        if args.output is None:
            paths.ensure_private_dir(output_path.parent)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        paths.write_private_text(output_path, result.rstrip() + "\n")
        print(f"Saved Markdown output to {output_path}", file=sys.stderr)
    print(result.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
