"""Report local Codex conversation activity within a time range."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from codex_tools import search, summary_common


DEFAULT_TIMEZONE = summary_common.detect_local_timezone()


@dataclass(frozen=True)
class ActivityRecord:
    timestamp: datetime
    role: str


@dataclass
class ConversationActivity:
    thread_id: str
    title: str | None
    title_source: str | None
    start: datetime
    end: datetime
    user_turns: int
    assistant_messages: int
    message_count: int
    cwd: str | None
    recorded_sources: list[str]
    session_ids: list[str]
    rollout_ids: list[str]
    paths: list[str]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools activity",
        description=(
            "List local Codex conversations with user or assistant messages "
            "inside a time range. Times without an offset use --timezone."
        ),
    )
    range_start = parser.add_mutually_exclusive_group()
    range_start.add_argument(
        "--from",
        dest="from_time",
        metavar="TIME",
        help="Start of the activity window. Default: today at 00:00.",
    )
    range_start.add_argument(
        "--last",
        metavar="DURATION",
        help="Show a recent duration such as 10m, 2h, 3d, or 1h30m.",
    )
    parser.add_argument(
        "--to",
        dest="to_time",
        metavar="TIME",
        help="End of the activity window. Default: now.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone for local input and output. Default: {DEFAULT_TIMEZONE}.",
    )
    work_dir_group = parser.add_mutually_exclusive_group()
    work_dir_group.add_argument(
        "--here",
        action="store_true",
        help="Only include conversations started from the current directory.",
    )
    work_dir_group.add_argument(
        "--work-dir",
        type=Path,
        metavar="PATH",
        help="Only include conversations started from this directory.",
    )
    parser.add_argument(
        "--source",
        metavar="SOURCE",
        help=(
            "Only include a recorded session source such as cli, vscode, "
            "app-server, or guardian. This is origin metadata, not a live app."
        ),
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=50,
        metavar="N",
        help="Maximum conversations to show. Default: 50.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=search.DEFAULT_SESSIONS_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        default=search.DEFAULT_SESSION_INDEX,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_duration(value: str) -> timedelta:
    text = value.strip().lower()
    matches = list(re.finditer(r"(\d+)([smhdw])", text))
    if not matches or "".join(match.group(0) for match in matches) != text:
        raise ValueError(
            f"invalid duration {value!r}; use units s, m, h, d, or w (for example 1h30m)"
        )
    seconds_per_unit = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }
    seconds = sum(
        int(match.group(1)) * seconds_per_unit[match.group(2)]
        for match in matches
    )
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return timedelta(seconds=seconds)


def parse_time_value(
    value: str,
    timezone: ZoneInfo,
    *,
    end_of_day: bool = False,
    now: datetime | None = None,
) -> datetime:
    text = value.strip()
    reference = (now or datetime.now(timezone)).astimezone(timezone)
    lowered = text.lower()
    if lowered in {"today", "yesterday"}:
        day = reference.date()
        if lowered == "yesterday":
            day -= timedelta(days=1)
        boundary = time.max if end_of_day else time.min
        return datetime.combine(day, boundary, timezone)

    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            boundary = time.max if end_of_day else time.min
            return datetime.combine(day, boundary, timezone)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"invalid time {value!r}; use ISO date/time, today, or yesterday"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def source_label(value: str | None) -> str:
    if not value:
        return "unknown"
    lowered = value.lower()
    for label, markers in (
        ("app-server", ("app-server", "app_server", "appserver")),
        ("vscode", ("vscode", "visual studio code")),
        ("guardian", ("guardian",)),
        ("cli", ("cli",)),
    ):
        if any(marker in lowered for marker in markers):
            return label
    return search.shorten(value, 24)


def records_in_window(
    session: search.Session,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
) -> list[ActivityRecord]:
    result = []
    for record in session.records:
        if record.role not in {"user", "assistant"}:
            continue
        timestamp = search.parse_record_timestamp(record.timestamp)
        if timestamp is None:
            continue
        local_timestamp = timestamp.astimezone(timezone)
        if start <= local_timestamp <= end:
            result.append(ActivityRecord(local_timestamp, record.role))
    return result


def _append_unique(target: list[str], values: Iterable[str | None]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def collect_activity(
    args: argparse.Namespace,
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    *,
    current_dir: Path | None = None,
) -> list[ConversationActivity]:
    titles = search.load_session_index_titles(args.session_index)
    work_dir = search.selected_work_dir(args, current_dir=current_dir)
    requested_source = args.source.lower() if args.source else None
    grouped: dict[str, ConversationActivity] = {}

    for path in search.iter_jsonl_paths(args.sessions_root):
        session = search.read_session(path)
        if not search.session_matches_work_dir(session, work_dir):
            continue
        normalized_source = source_label(session.source)
        if requested_source and normalized_source.lower() != requested_source:
            continue
        records = records_in_window(session, start, end, timezone)
        if not records:
            continue
        search.resolve_session_title(session, titles)
        group_id = search.task_id(session)
        record_start = min(record.timestamp for record in records)
        record_end = max(record.timestamp for record in records)
        user_turns = sum(record.role == "user" for record in records)
        assistant_messages = sum(record.role == "assistant" for record in records)

        activity = grouped.get(group_id)
        if activity is None:
            grouped[group_id] = ConversationActivity(
                thread_id=group_id,
                title=session.title,
                title_source=session.title_source,
                start=record_start,
                end=record_end,
                user_turns=user_turns,
                assistant_messages=assistant_messages,
                message_count=len(records),
                cwd=session.cwd,
                recorded_sources=[normalized_source],
                session_ids=[session.session_id] if session.session_id else [],
                rollout_ids=[session.rollout_id] if session.rollout_id else [],
                paths=[str(session.path)],
            )
            continue

        activity.start = min(activity.start, record_start)
        activity.end = max(activity.end, record_end)
        activity.user_turns += user_turns
        activity.assistant_messages += assistant_messages
        activity.message_count += len(records)
        if activity.title is None and session.title is not None:
            activity.title = session.title
            activity.title_source = session.title_source
        if activity.cwd is None:
            activity.cwd = session.cwd
        _append_unique(activity.recorded_sources, [normalized_source])
        _append_unique(activity.session_ids, [session.session_id])
        _append_unique(activity.rollout_ids, [session.rollout_id])
        _append_unique(activity.paths, [str(session.path)])

    return sorted(grouped.values(), key=lambda item: item.end, reverse=True)


def workdir_label(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    path = Path(cwd).expanduser()
    home = Path.home()
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative}"


def active_label(activity: ConversationActivity) -> str:
    if activity.start.date() == activity.end.date():
        return (
            f"{activity.start:%Y-%m-%d %H:%M}"
            f"–{activity.end:%H:%M}"
        )
    return f"{activity.start:%Y-%m-%d %H:%M}–{activity.end:%m-%d %H:%M}"


def emit_text(
    activities: list[ConversationActivity],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    limit: int,
) -> None:
    print(f"Codex activity: {start.isoformat()} to {end.isoformat()} ({timezone.key})")
    selected = activities[:limit]
    if not selected:
        print("No conversation activity found.")
        return

    rows = [
        (
            active_label(activity),
            str(activity.user_turns),
            workdir_label(activity.cwd),
            search.shorten(activity.title or activity.thread_id, 64),
        )
        for activity in selected
    ]
    headers = ("ACTIVE", "TURNS", "WORKDIR", "CONVERSATION")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers) - 1)
    ]
    print()
    print(
        f"{headers[0]:<{widths[0]}}  {headers[1]:>{widths[1]}}  "
        f"{headers[2]:<{widths[2]}}  {headers[3]}"
    )
    for row in rows:
        print(
            f"{row[0]:<{widths[0]}}  {row[1]:>{widths[1]}}  "
            f"{row[2]:<{widths[2]}}  {row[3]}"
        )
    if len(activities) > limit:
        print(f"\nShowing {limit} of {len(activities)} active conversations.")


def emit_json(
    activities: list[ConversationActivity],
    start: datetime,
    end: datetime,
    timezone: ZoneInfo,
    limit: int,
) -> None:
    selected = activities[:limit]
    payload = {
        "timezone": timezone.key,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "conversation_count": len(activities),
        "returned_count": len(selected),
        "conversations": [
            {
                "thread_id": activity.thread_id,
                "title": activity.title,
                "title_source": activity.title_source,
                "activity_start": activity.start.isoformat(),
                "activity_end": activity.end.isoformat(),
                "user_turns": activity.user_turns,
                "assistant_messages": activity.assistant_messages,
                "message_count": activity.message_count,
                "workdir": activity.cwd,
                "recorded_sources": activity.recorded_sources,
                "session_count": len(activity.paths),
                "session_ids": activity.session_ids,
                "rollout_ids": activity.rollout_ids,
                "paths": activity.paths,
            }
            for activity in selected
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        timezone = ZoneInfo(args.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        print(f"error: unknown timezone: {args.timezone}", file=sys.stderr)
        return 2

    now = datetime.now(timezone)
    try:
        if args.last:
            if args.to_time:
                print("error: --last cannot be combined with --to", file=sys.stderr)
                return 2
            duration = parse_duration(args.last)
            end = now
            start = (
                now.astimezone(datetime_timezone.utc) - duration
            ).astimezone(timezone)
        else:
            start = parse_time_value(args.from_time or "today", timezone, now=now)
            end = (
                parse_time_value(args.to_time, timezone, end_of_day=True, now=now)
                if args.to_time
                else now
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if start > end:
        print("error: --from must not be later than --to", file=sys.stderr)
        return 2

    activities = collect_activity(args, start, end, timezone)
    if args.json:
        emit_json(activities, start, end, timezone, args.limit)
    else:
        emit_text(activities, start, end, timezone, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
