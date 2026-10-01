#!/usr/bin/env python3
"""Build a Codex work summary from local session transcripts.

The script is read-only with respect to Codex state. It extracts local session
context for a single local date, then can optionally ask `codex exec` to turn
that context into a concise Markdown daily summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_tools import manager, paths, search, summary_common, summary_prompts


DEFAULT_TIMEZONE = summary_common.detect_local_timezone()
DEFAULT_OUTPUT_DIR = paths.DAILY_SUMMARIES_DIR


@dataclass
class DailySession:
    path: Path
    session_id: str | None = None
    created_at: str | None = None
    local_start: datetime | None = None
    local_end: datetime | None = None
    cwd: str | None = None
    title: str | None = None
    title_source: str | None = None
    records: list[search.TextRecord] = field(default_factory=list)
    today_records: list[search.TextRecord] = field(default_factory=list)


@dataclass(frozen=True)
class DailySummaryResult:
    markdown: str
    output_path: Path | None
    metadata_path: Path | None
    session_count: int


@dataclass(frozen=True)
class DailySummaryInputs:
    sessions: list[DailySession]
    context: str
    prompt: str
    source_last_timestamp: str
    context_sha256: str
    template_sha256: str
    input_fingerprint: str


def parse_args(
    argv: list[str] | None = None,
    prog: str = "codex-tools summary today",
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Create a Markdown daily summary from local Codex sessions."
    )
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date",
        help="Local date to summarize, YYYY-MM-DD. Defaults to today in --timezone.",
    )
    date_group.add_argument(
        "--yesterday",
        action="store_true",
        help="Summarize yesterday in --timezone.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone for date filtering. Default: {DEFAULT_TIMEZONE}",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=search.DEFAULT_SESSIONS_ROOT,
        help=f"Session root to scan. Default: {search.DEFAULT_SESSIONS_ROOT}",
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
        help="Print extracted Markdown context without calling codex exec.",
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
        help="Print the raw daily prompt template without scanning sessions.",
    )
    parser.add_argument(
        "--save-context",
        dest="save_context",
        type=Path,
        help="Write the extracted context Markdown to this path.",
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
        help=(
            "Custom daily prompt template using $weekday, $day, $timezone, "
            "$target_words, $format_instructions, and $context placeholders."
        ),
    )
    parser.add_argument(
        "--words",
        type=summary_common.positive_int,
        default=summary_common.DEFAULT_SUMMARY_WORDS,
        help=(
            "Approximate number of words in the summary. "
            f"Default: {summary_common.DEFAULT_SUMMARY_WORDS}"
        ),
    )
    parser.add_argument(
        "--format",
        dest="summary_format",
        choices=summary_common.SUMMARY_FORMATS,
        default=summary_common.DEFAULT_SUMMARY_FORMAT,
        help="Summary organization. Default: freeform.",
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
            "Directory for the default timestamped summary output. "
            f"Default: {DEFAULT_OUTPUT_DIR}"
        ),
    )
    parser.add_argument(
        "--model",
        default=summary_common.read_default_model(),
        help=(
            "Model for summary generation. Default: "
            f"{summary_common.default_model_label()}"
        ),
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
        help="Maximum characters to include from each transcript message.",
    )
    parser.add_argument(
        "--max-context-chars",
        type=int,
        default=120_000,
        help="Maximum extracted context size sent to codex exec.",
    )
    return parser.parse_args(argv)


def parse_iso_timestamp(value: str | None, timezone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def target_date(args: argparse.Namespace, timezone: ZoneInfo) -> date:
    if args.date:
        return date.fromisoformat(args.date)
    today = datetime.now(timezone).date()
    if args.yesterday:
        return today - timedelta(days=1)
    return today


def weekday_slug(day: date) -> str:
    return day.strftime("%A").lower()


def weekday_label(day: date) -> str:
    return day.strftime("%A")


def short_text(text: str, limit: int) -> str:
    compact = search.compact_space(search.prompt_label_text(text))
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def read_daily_sessions(
    args: argparse.Namespace, day: date, timezone: ZoneInfo
) -> list[DailySession]:
    titles = search.load_session_index_titles(args.session_index)
    sessions: list[DailySession] = []

    for path in search.iter_jsonl_paths(args.sessions_root):
        raw = search.read_session(path)
        search.resolve_session_title(raw, titles)
        daily = DailySession(
            path=path,
            session_id=raw.session_id,
            created_at=raw.created_at,
            local_start=parse_iso_timestamp(raw.created_at, timezone),
            cwd=raw.cwd,
            title=raw.title,
            title_source=raw.title_source,
            records=raw.records,
        )

        today_records: list[search.TextRecord] = []
        record_times: list[datetime] = []
        today_record_times: list[datetime] = []
        for record in raw.records:
            local_time = parse_iso_timestamp(record.timestamp, timezone)
            if local_time is None:
                continue
            record_times.append(local_time)
            if local_time.date() == day:
                today_records.append(record)
                today_record_times.append(local_time)

        if today_record_times:
            daily.local_start = min(today_record_times)
            daily.local_end = max(today_record_times)
        elif record_times:
            daily.local_end = max(record_times)
            if daily.local_start is None:
                daily.local_start = min(record_times)

        starts_today = daily.local_start is not None and daily.local_start.date() == day
        if starts_today or today_records:
            daily.today_records = today_records or raw.records
            sessions.append(daily)

    sessions.sort(key=lambda s: s.local_start or datetime.min.replace(tzinfo=timezone))
    return sessions


def render_context(
    sessions: list[DailySession],
    day: date,
    timezone: ZoneInfo,
    max_record_chars: int,
) -> str:
    lines = [
        f"# Codex Daily Context: {weekday_label(day)}, {day.isoformat()}",
        "",
        f"Timezone: `{timezone.key}`",
        f"Session count: {len(sessions)}",
        "",
        "This is extracted from local Codex JSONL session transcripts. Summarize only work represented here.",
        "",
    ]

    for index, session in enumerate(sessions, start=1):
        start = (
            session.local_start.strftime("%H:%M")
            if session.local_start
            else "unknown"
        )
        end = session.local_end.strftime("%H:%M") if session.local_end else start
        title = session.title or "(no task title found)"
        lines.extend(
            [
                f"## {index}. {title}",
                "",
                f"- Time: {start}-{end}",
                f"- Session id: `{session.session_id or 'unknown'}`",
                f"- CWD: `{session.cwd or 'unknown'}`",
                f"- Source file: `{session.path}`",
                f"- Records included: {len(session.today_records)}",
                "",
                "### Transcript Extract",
                "",
            ]
        )

        for record in session.today_records:
            if record.role not in {"user", "assistant"}:
                continue
            local_time = parse_iso_timestamp(record.timestamp, timezone)
            when = local_time.strftime("%H:%M") if local_time else "unknown"
            text = short_text(record.text, max_record_chars)
            if not text or search.is_internal_prompt_text(text):
                continue
            lines.append(f"- `{when}` **{record.role}:** {text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def summary_prompt(
    context: str,
    day: date,
    timezone: ZoneInfo,
    template_path: Path | None = None,
    *,
    words: int = summary_common.DEFAULT_SUMMARY_WORDS,
    summary_format: str = summary_common.DEFAULT_SUMMARY_FORMAT,
) -> str:
    return summary_common.structured_prompt(
        summary_prompts.render_template(
            "daily",
            {
                "weekday": weekday_label(day),
                "day": day.isoformat(),
                "timezone": timezone.key,
                "target_words": str(words),
                "format_instructions": summary_common.format_instructions(
                    "daily", summary_format
                ),
                "context": context,
            },
            template_path,
        )
    )


def summary_timestamp(
    sessions: list[DailySession], day: date, timezone: ZoneInfo
) -> datetime:
    record_times: list[datetime] = []
    for session in sessions:
        for record in session.today_records:
            local_time = parse_iso_timestamp(record.timestamp, timezone)
            if local_time is not None and local_time.date() == day:
                record_times.append(local_time)
    if record_times:
        return max(record_times)
    return datetime.combine(day, time.min, timezone)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_daily_summary(
    args: argparse.Namespace,
    day: date,
    timezone: ZoneInfo,
) -> DailySummaryInputs:
    sessions = read_daily_sessions(args, day, timezone)
    context = render_context(sessions, day, timezone, args.max_record_chars)
    context = summary_common.truncate_context(context, args.max_context_chars)

    template = summary_prompts.template_text("daily", args.prompt_template)
    words = getattr(args, "words", summary_common.DEFAULT_SUMMARY_WORDS)
    summary_format = getattr(
        args, "summary_format", summary_common.DEFAULT_SUMMARY_FORMAT
    )
    model = getattr(args, "model", None)
    source_last_timestamp = summary_timestamp(sessions, day, timezone).isoformat()
    context_sha256 = sha256_text(context)
    template_sha256 = sha256_text(template)
    fingerprint_source = json.dumps(
        {
            "context_sha256": context_sha256,
            "source_last_timestamp": source_last_timestamp,
            "template_sha256": template_sha256,
            "target_words": words,
            "summary_format": summary_format,
            "model": model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return DailySummaryInputs(
        sessions=sessions,
        context=context,
        prompt=summary_prompt(
            context,
            day,
            timezone,
            args.prompt_template,
            words=words,
            summary_format=summary_format,
        ),
        source_last_timestamp=source_last_timestamp,
        context_sha256=context_sha256,
        template_sha256=template_sha256,
        input_fingerprint=sha256_text(fingerprint_source),
    )


def default_output_path(
    args: argparse.Namespace,
    sessions: list[DailySession],
    day: date,
    timezone: ZoneInfo,
) -> Path:
    timestamp = summary_timestamp(sessions, day, timezone).strftime("%Y%m%dT%H%M%S%z")
    filename = f"codex-daily-summary-{day.isoformat()}-{weekday_slug(day)}-{timestamp}.md"
    return args.output_dir / filename


def metadata_path_for(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".json")


def write_metadata(path: Path, metadata: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    paths.write_private_text(
        temporary,
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    temporary.replace(path)
    path.chmod(0o600)


def generate_daily_summary(
    args: argparse.Namespace,
    day: date,
    timezone: ZoneInfo,
    *,
    require_sessions: bool = False,
    prepared: DailySummaryInputs | None = None,
) -> DailySummaryResult | None:
    """Generate and save one daily summary, or skip an inactive required day."""
    inputs = prepared or prepare_daily_summary(args, day, timezone)
    if require_sessions and not inputs.sessions:
        return None

    if args.save_context:
        args.save_context.parent.mkdir(parents=True, exist_ok=True)
        paths.write_private_text(args.save_context, inputs.context)

    if args.show_context:
        result = inputs.context
    elif args.show_prompt:
        result = inputs.prompt
    else:
        result = summary_common.run_codex_exec(args, inputs.prompt)

    output_path = args.output
    if output_path is None and not args.show_context and not args.show_prompt:
        output_path = default_output_path(args, inputs.sessions, day, timezone)

    metadata_path = None
    if output_path:
        if args.output is None:
            paths.ensure_private_dir(output_path.parent)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        paths.write_private_text(output_path, result.rstrip() + "\n")
        if not args.show_context and not args.show_prompt:
            metadata_path = metadata_path_for(output_path)
            write_metadata(
                metadata_path,
                {
                    "format_version": 1,
                    "kind": "daily",
                    "day": day.isoformat(),
                    "timezone": timezone.key,
                    "created_at": datetime.now(datetime_timezone.utc).isoformat(),
                    "source_last_timestamp": inputs.source_last_timestamp,
                    "context_sha256": inputs.context_sha256,
                    "template_sha256": inputs.template_sha256,
                    "template": (
                        str(args.prompt_template)
                        if args.prompt_template is not None
                        else "built-in:daily_summary.md"
                    ),
                    "input_fingerprint": inputs.input_fingerprint,
                    "model": args.model,
                    "profile": args.profile,
                    "max_record_chars": args.max_record_chars,
                    "max_context_chars": args.max_context_chars,
                    "target_words": getattr(
                        args, "words", summary_common.DEFAULT_SUMMARY_WORDS
                    ),
                    "summary_format": getattr(
                        args,
                        "summary_format",
                        summary_common.DEFAULT_SUMMARY_FORMAT,
                    ),
                },
            )

    return DailySummaryResult(
        markdown=result,
        output_path=output_path,
        metadata_path=metadata_path,
        session_count=len(inputs.sessions),
    )


def main(
    argv: list[str] | None = None,
    prog: str = "codex-tools summary today",
) -> int:
    args = parse_args(argv, prog)
    if args.show_template:
        try:
            template = summary_prompts.template_text("daily", args.prompt_template)
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
        day = target_date(args, timezone)
    except ValueError as exc:
        print(f"error: invalid --date: {exc}", file=sys.stderr)
        return 2

    try:
        generated = generate_daily_summary(args, day, timezone)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    assert generated is not None
    if generated.output_path:
        print(f"Saved Markdown output to {generated.output_path}", file=sys.stderr)
    print(generated.markdown.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
