#!/usr/bin/env python3
"""Search local Codex task/session transcripts."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

from codex_tools import config as user_config
from codex_tools import paths as tool_paths


HELP_EPILOG = """\
Examples:
  codex-tools search "pdf lookup worker"
  codex-tools search "stored sessions" --context-turns 2 --matches 3
  codex-tools search "JSONDecodeError" --include-tools --role tool
  codex-tools search "sidescribe price" --since 2026-09-01 --role user
  codex-tools search --list --limit 20
  codex-tools diagnose

Robustness notes:
  The tool is read-only. It searches JSONL session files by default and groups
  rollouts by task. Use --source sqlite only as a compatibility fallback.
  Missing optional state and malformed records are skipped where possible.
"""

DEFAULT_SESSIONS_ROOT = Path("~/.codex/sessions").expanduser()
DEFAULT_ARCHIVE_ROOT = Path("~/.codex/archived_sessions").expanduser()
DEFAULT_SESSION_INDEX = Path("~/.codex/session_index.jsonl").expanduser()
DEFAULT_THREAD_HISTORY = Path("~/.codex/thread_history_1.sqlite").expanduser()
SINGLE_LINE_CONTEXT_CHARS = 90
CONTEXT_RECORD_CHARS = 180
TITLE_SOURCE_SESSION_INDEX = "codex session index"
TITLE_SOURCE_FIRST_PROMPT = "first user prompt"
TITLE_SOURCE_MATCHING_PROMPT = "matching user prompt"
TITLE_SOURCE_MATCHING_SNIPPET = "matching snippet"

SKIPPED_PAYLOAD_KEYS = {
    "base_instructions",
    "internal_chat_message_metadata_passthrough",
    "encrypted_content",
}


@dataclass
class TextRecord:
    timestamp: str
    role: str
    text: str
    line_no: int
    record_type: str


@dataclass
class Session:
    path: Path
    session_id: str | None = None
    rollout_id: str | None = None
    parent_thread_id: str | None = None
    created_at: str | None = None
    cwd: str | None = None
    source: str | None = None
    title: str | None = None
    title_source: str | None = None
    records: list[TextRecord] = field(default_factory=list)


@dataclass(frozen=True)
class SessionTitle:
    text: str
    source: str


@dataclass(frozen=True)
class ExcerptLine:
    number: int
    text: str
    matched: bool
    match_start: int
    match_end: int


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def color(self, text: str, code: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self.color(text, "1")

    def dim(self, text: str) -> str:
        return self.color(text, "2")

    def title(self, text: str) -> str:
        return self.color(text, "1;36")

    def label(self, text: str) -> str:
        return self.color(text, "36")

    def value(self, text: str) -> str:
        return self.color(text, "37")

    def role(self, text: str) -> str:
        if text == "user":
            return self.color(text, "1;32")
        if text == "assistant":
            return self.color(text, "1;34")
        return self.color(text, "1;35")

    def match(self, text: str) -> str:
        return self.color(text, "1;30;43")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools search",
        description=(
            "Read-only search for local Codex conversations, rollouts, and "
            "thread history."
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="*", help="Text or regex to search for.")
    parser.add_argument(
        "--source",
        choices=("jsonl", "sqlite"),
        default="jsonl",
        help="Transcript source. Use sqlite only as a compatibility fallback.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=DEFAULT_SESSIONS_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--include-archive",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        default=DEFAULT_SESSION_INDEX,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--thread-history",
        type=Path,
        default=DEFAULT_THREAD_HISTORY,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--case-sensitive", action="store_true", help="Use case-sensitive matching."
    )
    parser.add_argument("--regex", action="store_true", help="Treat query as regex.")
    parser.add_argument(
        "--all-terms",
        action="store_true",
        help="Require every whitespace-separated query term to match in a session.",
    )
    parser.add_argument(
        "--titles-only",
        action="store_true",
        help="Search only Codex task/thread titles.",
    )
    parser.add_argument(
        "--include-tools",
        action="store_true",
        help="Include tool calls, command output, and other non-message text.",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--role",
        choices=("any", "user", "assistant", "tool", "task"),
        default="any",
        help="Only search records with this role. Default: any.",
    )
    parser.add_argument(
        "--since",
        help="Only include sessions with records at or after this date/time.",
    )
    parser.add_argument(
        "--until",
        help="Only include sessions with records at or before this date/time.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List sessions without requiring a query.",
    )
    parser.add_argument(
        "--ungrouped",
        action="store_true",
        help="Show individual rollout/session files instead of grouping by task.",
    )
    parser.add_argument(
        "--limit",
        metavar="N",
        type=int,
        default=30,
        help="Maximum matching tasks to print. Default: 30.",
    )
    parser.add_argument(
        "--matches",
        dest="matches_per_session",
        metavar="N",
        type=int,
        default=5,
        help="Maximum matching snippets to print per task. Default: 5.",
    )
    parser.add_argument(
        "--context-lines",
        metavar="N",
        type=int,
        default=2,
        help="Physical lines before and after a multiline match. Default: 2.",
    )
    parser.add_argument(
        "--context-turns",
        metavar="N",
        type=int,
        default=0,
        help="Show this many nearby transcript records before and after each match.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text output.",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def iter_jsonl_paths(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_filter_datetime(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = datetime.fromisoformat(text).date()
            boundary = time.max if end_of_day else time.min
            return datetime.combine(day, boundary, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        print(f"warning: ignoring invalid date/time filter: {value}", file=sys.stderr)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_record_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_from_ms(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def compact_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, limit: int = 120) -> str:
    compact = compact_space(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def prompt_label_text(text: str) -> str:
    marker = "## My request for Codex:"
    if marker in text:
        return text.split(marker, 1)[1]
    return text


def is_internal_prompt_text(text: str) -> bool:
    stripped = prompt_label_text(text).lstrip()
    if not stripped:
        return True
    internal_prefixes = (
        "# AGENTS.md instructions",
        "The following is the Codex agent history",
        "<recommended_plugins>",
        "<environment_context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<skills_instructions>",
        "<apps_instructions>",
        "<plugins_instructions>",
        "<developer_context",
    )
    return stripped.startswith(internal_prefixes)


def is_low_information_prompt(text: str) -> bool:
    compact = compact_space(text).lower()
    if len(compact) >= 35:
        return False
    low_information = {
        "please do",
        "okay, do it",
        "ok, do it",
        "do it",
        "okay, can you continue then",
        "ok, can you continue then",
        "continue",
        "please continue",
        "go on",
        "yes",
        "yeah",
        "ok",
        "okay",
    }
    return compact in low_information


def choose_prompt_label(prompts: Iterable[str]) -> str | None:
    fallback: str | None = None
    for prompt in prompts:
        if not isinstance(prompt, str) or is_internal_prompt_text(prompt):
            continue
        label_text = prompt_label_text(prompt)
        if fallback is None:
            fallback = label_text
        if not is_low_information_prompt(label_text):
            return shorten(label_text)
    if fallback is not None:
        return shorten(fallback)
    return None


def inferred_session_title(session: Session) -> str | None:
    return choose_prompt_label(
        record.text for record in session.records if record.role == "user"
    )


def infer_title_from_matches(
    matches: list[tuple[TextRecord, re.Match[str]]],
) -> str | None:
    return choose_prompt_label(
        record.text for record, _ in matches if record.role == "user"
    )


def infer_title_from_matching_snippet(
    matches: list[tuple[TextRecord, re.Match[str]]],
    context: int,
) -> str | None:
    if not matches:
        return None
    record, found = matches[0]
    return shorten(snippet(record.text, found, context).lstrip(". "))


def first_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [first_text(v) for v in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if key in SKIPPED_PAYLOAD_KEYS:
                continue
            if key in {
                "text",
                "message",
                "output",
                "formatted_output",
                "arguments",
                "cmd",
                "command",
                "diff",
                "path",
            }:
                found = first_text(item)
                if found:
                    parts.append(found)
            elif key in {"content", "payload"}:
                found = first_text(item)
                if found:
                    parts.append(found)
        return "\n".join(parts)
    return None


def text_records_from_event(
    event: dict[str, Any],
    line_no: int,
    include_tools: bool,
    include_system: bool,
) -> list[TextRecord]:
    timestamp = str(event.get("timestamp") or "")
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return []

    if event_type == "session_meta":
        return []

    role = str(payload.get("role") or payload.get("type") or event_type)
    payload_type = str(payload.get("type") or "")
    is_message = event_type == "response_item" and payload_type == "message"
    is_user_event = event_type == "event_msg" and payload_type == "user_message"
    is_agent_event = event_type == "event_msg" and payload_type == "agent_message"

    if is_message:
        if role in {"developer", "system"} and not include_system:
            return []
        text = first_text(payload.get("content"))
    elif (is_user_event or is_agent_event) and include_tools:
        # These duplicate response_item messages in normal sessions. Keep them
        # behind --include-tools for older/partial logs or forensic searches.
        text = first_text(payload.get("message"))
    elif include_tools:
        text = first_text(payload)
    else:
        return []

    if not text:
        return []

    return [
        TextRecord(
            timestamp=timestamp,
            role=role,
            text=text,
            line_no=line_no,
            record_type=event_type if not payload_type else f"{event_type}/{payload_type}",
        )
    ]


def read_session(
    path: Path, include_tools: bool = False, include_system: bool = False
) -> Session:
    session = Session(path=path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                if event.get("type") == "session_meta":
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        session.session_id = str(
                            payload.get("session_id") or payload.get("id") or ""
                        ) or session.session_id
                        session.rollout_id = str(payload.get("id") or "") or session.rollout_id
                        session.parent_thread_id = str(
                            payload.get("parent_thread_id") or ""
                        ) or session.parent_thread_id
                        session.created_at = str(
                            payload.get("timestamp") or event.get("timestamp") or ""
                        ) or session.created_at
                        session.cwd = str(payload.get("cwd") or "") or session.cwd
                        session.source = str(payload.get("source") or "") or session.source
                    continue

                session.records.extend(
                    text_records_from_event(event, line_no, include_tools, include_system)
                )
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
    return session


def sqlite_text_from_item(item: dict[str, Any], item_type: str) -> str | None:
    if item_type == "agentMessage":
        return first_text(item.get("text"))
    if item_type == "userMessage":
        return first_text(item.get("content"))
    if item_type == "commandExecution":
        parts = []
        command = first_text(item.get("cmd") or item.get("command"))
        output = first_text(item.get("output") or item.get("formatted_output"))
        if command:
            parts.append(command)
        if output:
            parts.append(output)
        return "\n".join(parts) or first_text(item)
    if item_type == "fileChange":
        return first_text(item.get("changes"))
    return first_text(item)


def sqlite_role(item_type: str) -> str:
    if item_type == "userMessage":
        return "user"
    if item_type == "agentMessage":
        return "assistant"
    return "tool"


def read_sqlite_sessions(
    db_path: Path, include_tools: bool = False, include_system: bool = False
) -> list[Session]:
    if not db_path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"warning: could not open SQLite thread history {db_path}: {exc}", file=sys.stderr)
        return []

    sessions: dict[str, Session] = {}
    bad_json_rows = 0
    try:
        rows = connection.execute(
            """
            select thread_id, turn_id, item_id, rollout_ordinal, created_at_ms,
                   item_type, item_json
            from thread_items
            order by thread_id, rollout_ordinal
            """
        )
        for (
            thread_id,
            turn_id,
            item_id,
            rollout_ordinal,
            created_at_ms,
            item_type,
            item_json,
        ) in rows:
            item_type = str(item_type or "")
            if item_type in {"reasoning"} and not include_system:
                continue
            role = sqlite_role(item_type)
            if role == "tool" and not include_tools:
                continue

            try:
                item = json.loads(item_json)
            except (TypeError, json.JSONDecodeError):
                bad_json_rows += 1
                continue
            if not isinstance(item, dict):
                continue

            text = sqlite_text_from_item(item, item_type)
            if not text:
                continue

            thread_id = str(thread_id or "")
            if not thread_id:
                continue
            session = sessions.get(thread_id)
            if session is None:
                session = Session(
                    path=Path(f"sqlite:{db_path.name}:{thread_id}"),
                    session_id=thread_id,
                    parent_thread_id=thread_id,
                    created_at=iso_from_ms(created_at_ms),
                    source=f"sqlite:{db_path}",
                )
                sessions[thread_id] = session
            session.records.append(
                TextRecord(
                    timestamp=iso_from_ms(created_at_ms),
                    role=role,
                    text=text,
                    line_no=int(rollout_ordinal or 0),
                    record_type=f"sqlite/{item_type or 'unknown'}",
                )
            )
            if not session.rollout_id and turn_id:
                session.rollout_id = str(turn_id)
    except sqlite3.Error as exc:
        print(f"warning: could not read SQLite thread history {db_path}: {exc}", file=sys.stderr)
        return []
    finally:
        connection.close()

    if bad_json_rows:
        print(
            f"warning: skipped {bad_json_rows} malformed SQLite item_json rows",
            file=sys.stderr,
        )
    return list(sessions.values())


def load_session_index_titles(index_path: Path) -> dict[str, SessionTitle]:
    titles: dict[str, SessionTitle] = {}
    if not index_path.exists():
        return titles
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                thread_id = str(item.get("id") or "")
                thread_name = str(item.get("thread_name") or "")
                if thread_id and thread_name:
                    titles[thread_id] = SessionTitle(
                        text=shorten(thread_name),
                        source=TITLE_SOURCE_SESSION_INDEX,
                    )
    except OSError:
        return {}
    return titles


def make_matchers(args: argparse.Namespace) -> list[re.Pattern[str]]:
    query = " ".join(args.query).strip()
    if not query:
        return []
    flags = 0 if args.case_sensitive else re.IGNORECASE
    if args.regex:
        return [re.compile(query, flags)]
    terms = query.split() if args.all_terms else [query]
    return [re.compile(re.escape(term), flags) for term in terms if term]


def task_id(session: Session) -> str:
    return session.parent_thread_id or session.session_id or str(session.path)


def title_lookup_ids(session: Session) -> list[str]:
    ids = []
    for value in (session.parent_thread_id, session.session_id, session.rollout_id):
        if value and value not in ids:
            ids.append(value)
            local_value = f"local:{value}"
            if local_value not in ids:
                ids.append(local_value)
    return ids


def resolve_session_title(
    session: Session, titles: dict[str, SessionTitle]
) -> None:
    for lookup_id in title_lookup_ids(session):
        indexed = titles.get(lookup_id)
        if indexed is not None:
            session.title = indexed.text
            session.title_source = indexed.source
            return
    session.title = inferred_session_title(session)
    if session.title is not None:
        session.title_source = TITLE_SOURCE_FIRST_PROMPT


def session_search_text(session: Session) -> str:
    parts = []
    if session.title:
        parts.append(session.title)
    parts.extend(record.text for record in session.records)
    return "\n".join(parts)


def matching_records(
    session: Session,
    matchers: list[re.Pattern[str]],
    require_all_terms: bool,
    titles_only: bool = False,
    role: str = "any",
) -> list[tuple[TextRecord, re.Match[str]]]:
    records: list[tuple[TextRecord, re.Match[str]]] = []
    searchable_records = [
        record for record in session.records if role == "any" or record.role == role
    ]
    session_text = (
        (session.title or "")
        if titles_only
        else "\n".join(
            [session.title or ""] + [record.text for record in searchable_records]
        )
    )
    if require_all_terms and not all(m.search(session_text) for m in matchers):
        return []
    if session.title and role in {"any", "task"}:
        for matcher in matchers:
            found = matcher.search(session.title)
            if found:
                records.append(
                    (
                        TextRecord(
                            timestamp=session.created_at or "",
                            role="task",
                            text=session.title,
                            line_no=0,
                            record_type="task_title",
                        ),
                        found,
                    )
                )
                break
    if titles_only:
        return records
    for record in searchable_records:
        for matcher in matchers:
            found = matcher.search(record.text)
            if found:
                records.append((record, found))
                break
    return records


def session_in_date_range(
    session: Session, since: datetime | None, until: datetime | None
) -> bool:
    if since is None and until is None:
        return True
    values = [parse_record_timestamp(record.timestamp) for record in session.records]
    values.append(parse_record_timestamp(session.created_at))
    timestamps = [value for value in values if value is not None]
    if not timestamps:
        return True
    return any(
        (since is None or timestamp >= since)
        and (until is None or timestamp <= until)
        for timestamp in timestamps
    )


def context_records(
    session: Session, record: TextRecord, context_turns: int
) -> list[TextRecord]:
    if context_turns <= 0:
        return []
    try:
        index = next(
            idx
            for idx, candidate in enumerate(session.records)
            if candidate is record
        )
    except StopIteration:
        return []
    start = max(0, index - context_turns)
    end = min(len(session.records), index + context_turns + 1)
    return session.records[start:end]


def snippet(text: str, match: re.Match[str], context: int) -> str:
    start = max(0, match.start() - context)
    end = min(len(text), match.end() + context)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + compact_space(text[start:end]) + suffix


def highlighted_snippet(
    text: str, match: re.Match[str], context: int, style: Style
) -> str:
    start = max(0, match.start() - context)
    end = min(len(text), match.end() + context)
    inner_start = match.start() - start
    inner_end = match.end() - start
    part = text[start:end]
    marked = (
        part[:inner_start]
        + style.match(part[inner_start:inner_end])
        + part[inner_end:]
    )
    prefix = style.dim("...") if start > 0 else ""
    suffix = style.dim("...") if end < len(text) else ""
    return prefix + compact_space(marked) + suffix


def excerpt_lines(
    text: str, match: re.Match[str], context_lines: int
) -> list[ExcerptLine]:
    lines = text.split("\n")
    start_line = text.count("\n", 0, match.start())
    final_match_offset = match.end() - 1 if match.end() > match.start() else match.start()
    end_line = text.count("\n", 0, final_match_offset)
    first_line = max(0, start_line - max(0, context_lines))
    last_line = min(len(lines) - 1, end_line + max(0, context_lines))

    result: list[ExcerptLine] = []
    offset = sum(len(line) + 1 for line in lines[:first_line])
    for index in range(first_line, last_line + 1):
        raw_line = lines[index]
        display_line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        matched = start_line <= index <= end_line
        local_start = max(0, min(len(display_line), match.start() - offset))
        local_end = max(0, min(len(display_line), match.end() - offset))
        result.append(
            ExcerptLine(
                number=index + 1,
                text=display_line,
                matched=matched,
                match_start=local_start if matched else 0,
                match_end=local_end if matched else 0,
            )
        )
        offset += len(raw_line) + 1
    return result


def plain_match_excerpt(
    text: str, match: re.Match[str], context_lines: int
) -> str:
    if "\n" not in text:
        return snippet(text, match, SINGLE_LINE_CONTEXT_CHARS)
    lines = excerpt_lines(text, match, context_lines)
    width = len(str(lines[-1].number))
    return "\n".join(
        f"{'>' if line.matched else ' '} {line.number:>{width}} | {line.text}"
        for line in lines
    )


def print_match_excerpt(
    text: str,
    match: re.Match[str],
    context_lines: int,
    style: Style,
    indent: str,
) -> None:
    if "\n" not in text:
        print(
            f"{indent}{highlighted_snippet(text, match, SINGLE_LINE_CONTEXT_CHARS, style)}"
        )
        return

    lines = excerpt_lines(text, match, context_lines)
    width = len(str(lines[-1].number))
    for line in lines:
        rendered = line.text
        if line.matched and line.match_end > line.match_start:
            rendered = (
                line.text[: line.match_start]
                + style.match(line.text[line.match_start : line.match_end])
                + line.text[line.match_end :]
            )
        marker = style.match(">") if line.matched else " "
        number = style.dim(f"{line.number:>{width}}")
        print(f"{indent}{marker} {number} {style.dim('|')} {rendered}")


def print_context_records(
    session: Session,
    match_record: TextRecord,
    context_turns: int,
    max_chars: int,
    style: Style,
    indent: str = "       ",
) -> None:
    records = context_records(session, match_record, context_turns)
    if not records:
        return
    print(f"{indent}{style.label('context:')}")
    for record in records:
        if record is not match_record and is_internal_prompt_text(record.text):
            continue
        marker = "*" if record is match_record else " "
        text = shorten(record.text, max_chars)
        print(
            f"{indent} {marker} {style.dim(record.timestamp)} "
            f"{style.role(record.role)} {text}"
        )


def json_context_records(
    session: Session, match_record: TextRecord, context_turns: int, max_chars: int
) -> list[dict[str, Any]]:
    return [
        {
            "matched": record is match_record,
            "timestamp": record.timestamp,
            "role": record.role,
            "line": record.line_no,
            "record_type": record.record_type,
            "text": shorten(record.text, max_chars),
        }
        for record in context_records(session, match_record, context_turns)
        if record is match_record or not is_internal_prompt_text(record.text)
    ]


def session_updated_at(session: Session) -> str:
    timestamps = [
        parsed
        for parsed in (parse_record_timestamp(record.timestamp) for record in session.records)
        if parsed is not None
    ]
    if timestamps:
        return max(timestamps).isoformat()
    return session.created_at or ""


def session_sort_key(session: Session) -> str:
    return session_updated_at(session) or session.created_at or session.path.name


def use_color(args: argparse.Namespace) -> bool:
    if args.color == "always":
        return True
    if args.color == "never":
        return False
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def emit_text(
    sessions: list[Session],
    matches_by_session: dict[Path, list[tuple[TextRecord, re.Match[str]]]],
    args: argparse.Namespace,
) -> None:
    style = Style(use_color(args))
    for session in sessions[: args.limit]:
        matches = matches_by_session.get(session.path, [])
        print()
        print(style.dim("-" * 96))
        title = session.title or "(no task title found)"
        print(style.title(title))
        if session.created_at:
            print(f"{style.label('created:')} {style.value(session.created_at)}")
        updated_at = session_updated_at(session)
        if updated_at and updated_at != session.created_at:
            print(f"{style.label('updated:')} {style.value(updated_at)}")
        if session.session_id:
            print(f"{style.label('thread: ')} {style.value(session.session_id)}")
        if session.rollout_id and session.rollout_id != session.session_id:
            print(f"{style.label('rollout:')} {style.value(session.rollout_id)}")
        if session.cwd:
            print(f"{style.label('cwd:    ')} {style.value(session.cwd)}")
        print(f"{style.label('file:   ')} {style.value(str(session.path))}")
        if args.list:
            print(f"{style.label('records:')} {style.bold(str(len(session.records)))}")
            continue
        print(f"{style.label('matches:')} {style.bold(str(len(matches)))}")
        print()
        for idx, (record, found) in enumerate(
            matches[: args.matches_per_session], start=1
        ):
            where = (
                f"{style.dim(record.timestamp)} "
                f"{style.role(record.role)} "
                f"{style.dim('record')} {style.bold(str(record.line_no))}"
            )
            print(f"  {style.bold(str(idx) + '.')} {where}")
            print_match_excerpt(
                record.text,
                found,
                args.context_lines,
                style,
                indent="     ",
            )
            print_context_records(
                session,
                record,
                args.context_turns,
                CONTEXT_RECORD_CHARS,
                style,
                indent="     ",
            )
            print()


def emit_json(
    sessions: list[Session],
    matches_by_session: dict[Path, list[tuple[TextRecord, re.Match[str]]]],
    args: argparse.Namespace,
) -> None:
    payload = []
    for session in sessions[: args.limit]:
        matches = matches_by_session.get(session.path, [])
        payload.append(
            {
                "title": session.title,
                "title_source": session.title_source,
                "created_at": session.created_at,
                "updated_at": session_updated_at(session),
                "session_id": session.session_id,
                "thread_id": task_id(session),
                "rollout_id": session.rollout_id,
                "parent_thread_id": session.parent_thread_id,
                "cwd": session.cwd,
                "source": session.source,
                "path": str(session.path),
                "record_count": len(session.records),
                "match_count": len(matches),
                "matches": [
                    {
                        "timestamp": record.timestamp,
                        "role": record.role,
                        "line": record.line_no,
                        "record_type": record.record_type,
                        "snippet": plain_match_excerpt(
                            record.text, found, args.context_lines
                        ),
                        "context": json_context_records(
                            session,
                            record,
                            args.context_turns,
                            CONTEXT_RECORD_CHARS,
                        ),
                    }
                    for record, found in matches[: args.matches_per_session]
                ],
            }
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def emit_grouped_text(
    sessions: list[Session],
    matches_by_session: dict[Path, list[tuple[TextRecord, re.Match[str]]]],
    args: argparse.Namespace,
) -> None:
    style = Style(use_color(args))
    groups: dict[str, list[Session]] = {}
    for session in sessions:
        groups.setdefault(task_id(session), []).append(session)

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: max(session_sort_key(session) for session in item[1]),
        reverse=True,
    )

    for group_id, group_sessions in ordered_groups[: args.limit]:
        group_sessions.sort(key=session_sort_key, reverse=True)
        primary = next(
            (session for session in group_sessions if session.title),
            group_sessions[0],
        )
        title = primary.title or "(no task title found)"
        total_matches = sum(
            len(matches_by_session.get(session.path, [])) for session in group_sessions
        )

        print()
        print(style.dim("-" * 96))
        print(style.title(title))
        print(f"{style.label('thread: ')} {style.value(group_id)}")
        group_updated_at = max(session_updated_at(session) for session in group_sessions)
        if group_updated_at:
            print(f"{style.label('updated:')} {style.value(group_updated_at)}")
        print(f"{style.label('sessions:')} {style.bold(str(len(group_sessions)))}")
        print(f"{style.label('matches:')} {style.bold(str(total_matches))}")
        if primary.cwd:
            print(f"{style.label('cwd:    ')} {style.value(primary.cwd)}")

        printed = 0
        for session in group_sessions:
            if printed >= args.matches_per_session:
                break
            matches = matches_by_session.get(session.path, [])
            if not matches:
                continue
            print()
            rollout = session.rollout_id or session.path.stem
            print(
                f"  {style.label('rollout:')} {style.value(rollout)} "
                f"{style.dim(session.created_at or '')}"
            )
            updated_at = session_updated_at(session)
            if updated_at and updated_at != session.created_at:
                print(f"  {style.label('updated:')} {style.value(updated_at)}")
            print(f"  {style.label('file:   ')} {style.value(str(session.path))}")
            for record, found in matches:
                if printed >= args.matches_per_session:
                    break
                where = (
                    f"{style.dim(record.timestamp)} "
                    f"{style.role(record.role)} "
                    f"{style.dim('record')} {style.bold(str(record.line_no))}"
                )
                print(f"    {style.bold(str(printed + 1) + '.')} {where}")
                print_match_excerpt(
                    record.text,
                    found,
                    args.context_lines,
                    style,
                    indent="       ",
                )
                print_context_records(
                    session,
                    record,
                    args.context_turns,
                    CONTEXT_RECORD_CHARS,
                    style,
                    indent="       ",
                )
                printed += 1

    if len(ordered_groups) > args.limit:
        print(f"\nShowing {args.limit} of {len(ordered_groups)} matching tasks.")


def emit_grouped_json(
    sessions: list[Session],
    matches_by_session: dict[Path, list[tuple[TextRecord, re.Match[str]]]],
    args: argparse.Namespace,
) -> None:
    groups: dict[str, list[Session]] = {}
    for session in sessions:
        groups.setdefault(task_id(session), []).append(session)

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: max(session_sort_key(session) for session in item[1]),
        reverse=True,
    )

    payload = []
    for group_id, group_sessions in ordered_groups[: args.limit]:
        group_sessions.sort(key=session_sort_key, reverse=True)
        primary = next(
            (session for session in group_sessions if session.title),
            group_sessions[0],
        )
        remaining_matches = args.matches_per_session
        session_payload = []
        for session in group_sessions:
            all_matches = matches_by_session.get(session.path, [])
            selected_matches = all_matches[:remaining_matches]
            remaining_matches -= len(selected_matches)
            session_payload.append(
                {
                    "created_at": session.created_at,
                    "updated_at": session_updated_at(session),
                    "session_id": session.session_id,
                    "rollout_id": session.rollout_id,
                    "parent_thread_id": session.parent_thread_id,
                    "cwd": session.cwd,
                    "source": session.source,
                    "path": str(session.path),
                    "record_count": len(session.records),
                    "match_count": len(all_matches),
                    "matches": [
                        {
                            "timestamp": record.timestamp,
                            "role": record.role,
                            "line": record.line_no,
                            "record_type": record.record_type,
                            "snippet": plain_match_excerpt(
                                record.text, found, args.context_lines
                            ),
                            "context": json_context_records(
                                session,
                                record,
                                args.context_turns,
                                CONTEXT_RECORD_CHARS,
                            ),
                        }
                        for record, found in selected_matches
                    ],
                }
            )
        payload.append(
            {
                "thread_id": group_id,
                "title": primary.title,
                "title_source": primary.title_source,
                "updated_at": max(session_updated_at(session) for session in group_sessions),
                "session_count": len(group_sessions),
                "match_count": sum(
                    len(matches_by_session.get(session.path, []))
                    for session in group_sessions
                ),
                "sessions": session_payload,
            }
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def sqlite_table_names(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type = 'table' order by name"
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def parse_diagnose_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools diagnose",
        description="Check Codex Tools configuration, dependencies, and local data.",
    )
    parser.add_argument(
        "--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT
    )
    parser.add_argument(
        "--include-archive",
        action="store_true",
        help="Include archived JSONL sessions in the counts.",
    )
    parser.add_argument(
        "--session-index", type=Path, default=DEFAULT_SESSION_INDEX
    )
    parser.add_argument(
        "--thread-history", type=Path, default=DEFAULT_THREAD_HISTORY
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show storage paths, exact timestamps, and database internals.",
    )
    return parser.parse_args(argv)


def _diagnostic_status(checks: list[dict[str, str]]) -> str:
    statuses = {check["status"] for check in checks}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


def _friendly_range(oldest: str | None, newest: str | None) -> str:
    def friendly(value: str | None) -> str:
        parsed = parse_record_timestamp(value)
        if parsed is None:
            return "unknown"
        local = parsed.astimezone()
        return f"{local.day} {local.strftime('%b %Y')}"

    return f"{friendly(oldest)} – {friendly(newest)}"


def _count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else (plural or singular + "s")
    return f"{count:,} {noun}"


def _inspect_session_index(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "exists": path.exists(),
        "readable": False,
        "titles": 0,
        "malformed_lines": 0,
    }
    if not info["exists"]:
        return info
    titles: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    info["malformed_lines"] += 1
                    continue
                if not isinstance(item, dict):
                    info["malformed_lines"] += 1
                    continue
                thread_id = str(item.get("id") or "")
                thread_name = str(item.get("thread_name") or "")
                if thread_id and thread_name:
                    titles.add(thread_id)
        info["readable"] = True
    except OSError as exc:
        info["error"] = str(exc)
    info["titles"] = len(titles)
    return info


def _print_diagnostic_check(check: dict[str, str]) -> None:
    print(f"[{check['status'].upper()}] {check['name']}")
    print(
        textwrap.fill(
            check["message"],
            width=88,
            initial_indent="     ",
            subsequent_indent="     ",
        )
    )
    if check.get("fix"):
        print(
            textwrap.fill(
                check["fix"],
                width=88,
                initial_indent="     Fix: ",
                subsequent_indent="          ",
            )
        )


def diagnose(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "sessions_root": str(args.sessions_root),
        "thread_history": str(args.thread_history),
        "session_index": str(args.session_index),
    }

    scan_error: str | None = None
    try:
        jsonl_paths = list(iter_jsonl_paths(args.sessions_root))
        if args.include_archive:
            jsonl_paths.extend(iter_jsonl_paths(DEFAULT_ARCHIVE_ROOT))
    except OSError as exc:
        jsonl_paths = []
        scan_error = str(exc)
    readable = 0
    malformed_lines = 0
    unreadable_paths: list[str] = []
    oldest_jsonl: str | None = None
    newest_jsonl: str | None = None
    for path in jsonl_paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue
                    if isinstance(event, dict):
                        timestamp = str(event.get("timestamp") or "")
                        if timestamp:
                            oldest_jsonl = (
                                min(oldest_jsonl, timestamp)
                                if oldest_jsonl
                                else timestamp
                            )
                            newest_jsonl = (
                                max(newest_jsonl, timestamp)
                                if newest_jsonl
                                else timestamp
                            )
            readable += 1
        except OSError:
            unreadable_paths.append(str(path))
            continue
    payload["jsonl"] = {
        "root_exists": args.sessions_root.exists(),
        "files_found": len(jsonl_paths),
        "files_readable": readable,
        "files_unreadable": len(jsonl_paths) - readable,
        "unreadable_paths": unreadable_paths,
        "malformed_lines": malformed_lines,
        "oldest_timestamp": oldest_jsonl,
        "newest_timestamp": newest_jsonl,
    }
    if scan_error:
        payload["jsonl"]["error"] = scan_error

    session_index_info = _inspect_session_index(args.session_index)
    payload["session_index_info"] = session_index_info

    sqlite_info: dict[str, Any] = {
        "exists": args.thread_history.exists(),
        "tables": sqlite_table_names(args.thread_history),
    }
    if args.thread_history.exists():
        try:
            connection = sqlite3.connect(f"file:{args.thread_history}?mode=ro", uri=True)
            try:
                sqlite_info["thread_items"] = connection.execute(
                    "select count(*) from thread_items"
                ).fetchone()[0]
                sqlite_info["thread_turns"] = connection.execute(
                    "select count(*) from thread_turns"
                ).fetchone()[0]
                sqlite_info["item_types"] = dict(
                    connection.execute(
                        """
                        select item_type, count(*)
                        from thread_items
                        group by item_type
                        order by count(*) desc
                        """
                    ).fetchall()
                )
                row = connection.execute(
                    "select min(created_at_ms), max(created_at_ms) from thread_items"
                ).fetchone()
                sqlite_info["oldest_timestamp"] = iso_from_ms(row[0]) if row else None
                sqlite_info["newest_timestamp"] = iso_from_ms(row[1]) if row else None
                sqlite_info["thread_count"] = connection.execute(
                    "select count(distinct thread_id) from thread_items"
                ).fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error as exc:
            sqlite_info["error"] = str(exc)
    payload["sqlite"] = sqlite_info

    config_info: dict[str, Any] = {
        "path": str(tool_paths.CONFIG_FILE),
        "exists": tool_paths.CONFIG_FILE.exists(),
    }
    try:
        user_config.read_config()
        config_info["valid"] = True
    except ValueError as exc:
        config_info["valid"] = False
        config_info["error"] = str(exc)
    payload["config"] = config_info

    codex_path = shutil.which("codex")
    payload["codex_cli"] = {"found": codex_path is not None, "path": codex_path}

    checks: list[dict[str, str]] = []
    if config_info["valid"]:
        config_message = (
            "User configuration is valid."
            if config_info["exists"]
            else "No user configuration file; built-in defaults are valid."
        )
        checks.append({"name": "Configuration", "status": "ok", "message": config_message})
    else:
        checks.append(
            {
                "name": "Configuration",
                "status": "error",
                "message": str(config_info["error"]),
                "fix": (
                    "Run `codex-tools config validate`, then correct or remove the "
                    "invalid value."
                ),
            }
        )

    if codex_path:
        checks.append(
            {
                "name": "Codex CLI",
                "status": "ok",
                "message": f"Available at {codex_path}.",
            }
        )
    else:
        checks.append(
            {
                "name": "Codex CLI",
                "status": "warning",
                "message": (
                    "Not found on PATH. Search and the viewer still work, but "
                    "generated summaries do not."
                ),
                "fix": "Install Codex CLI or make the `codex` executable available on PATH.",
            }
        )

    jsonl = payload["jsonl"]
    if jsonl.get("error"):
        checks.append(
            {
                "name": "Conversation transcripts",
                "status": "error",
                "message": f"Could not scan {args.sessions_root}: {jsonl['error']}",
                "fix": "Check the directory path and its permissions.",
            }
        )
    elif not jsonl["root_exists"]:
        checks.append(
            {
                "name": "Conversation transcripts",
                "status": "warning",
                "message": (
                    "No Codex sessions directory was found; there are no "
                    "conversations to search yet."
                ),
                "fix": "Run Codex once, or pass the correct directory with `--sessions-root`.",
            }
        )
    elif jsonl["files_found"] == 0:
        checks.append(
            {
                "name": "Conversation transcripts",
                "status": "warning",
                "message": "The sessions directory exists but contains no transcript files.",
                "fix": "Run Codex to create a conversation, or check `--sessions-root`.",
            }
        )
    elif jsonl["files_readable"] == 0:
        checks.append(
            {
                "name": "Conversation transcripts",
                "status": "error",
                "message": f"None of the {jsonl['files_found']} transcript files could be read.",
                "fix": "Check ownership and read permissions for the sessions directory.",
            }
        )
    elif jsonl["files_unreadable"] or jsonl["malformed_lines"]:
        problems = []
        if jsonl["files_unreadable"]:
            problems.append(f"{jsonl['files_unreadable']} unreadable file(s)")
        if jsonl["malformed_lines"]:
            problems.append(f"{jsonl['malformed_lines']} malformed record(s)")
        checks.append(
            {
                "name": "Conversation transcripts",
                "status": "warning",
                "message": (
                    f"Read {jsonl['files_readable']} of {jsonl['files_found']} files; "
                    + " and ".join(problems)
                    + "."
                ),
                "fix": "Run with `--verbose` to inspect the affected paths and exact counts.",
            }
        )
    else:
        checks.append(
            {
                "name": "Conversation transcripts",
                "status": "ok",
                "message": (
                    f"All {_count_phrase(jsonl['files_found'], 'file')} are readable "
                    "with no malformed records. "
                    "Coverage: "
                    f"{_friendly_range(jsonl['oldest_timestamp'], jsonl['newest_timestamp'])}."
                ),
            }
        )

    if not session_index_info["exists"]:
        title_message = "No title index found; titles will be derived from conversation prompts."
        title_status = "ok"
        title_fix = ""
    elif not session_index_info["readable"]:
        title_message = (
            "The optional title index could not be read: "
            f"{session_index_info.get('error', 'unknown error')}"
        )
        title_status = "warning"
        title_fix = "Check the file's ownership and read permissions."
    elif session_index_info["malformed_lines"]:
        title_message = (
            f"Loaded {session_index_info['titles']} indexed titles, but skipped "
            f"{session_index_info['malformed_lines']} malformed record(s)."
        )
        title_status = "warning"
        title_fix = "Codex Tools will derive titles for entries it cannot read."
    else:
        title_message = (
            f"{_count_phrase(session_index_info['titles'], 'indexed title')} available; "
            "other titles will be derived from conversation prompts."
        )
        title_status = "ok"
        title_fix = ""
    title_check = {
        "name": "Conversation titles",
        "status": title_status,
        "message": title_message,
    }
    if title_fix:
        title_check["fix"] = title_fix
    checks.append(title_check)

    if not sqlite_info["exists"]:
        sqlite_check = {
            "name": "Thread-history database (optional)",
            "status": "ok",
            "message": "Not present; JSONL transcripts remain the primary data source.",
        }
    elif sqlite_info.get("error"):
        sqlite_check = {
            "name": "Thread-history database (optional)",
            "status": "warning",
            "message": f"Could not read the optional database: {sqlite_info['error']}",
            "fix": "Use the default JSONL source, or check the database file and schema.",
        }
    else:
        sqlite_check = {
            "name": "Thread-history database (optional)",
            "status": "ok",
            "message": (
                "Readable: "
                f"{_count_phrase(sqlite_info.get('thread_count', 0), 'thread')}, "
                f"{_count_phrase(sqlite_info.get('thread_turns', 0), 'turn')}, and "
                f"{_count_phrase(sqlite_info.get('thread_items', 0), 'item')}."
            ),
        }
    checks.append(sqlite_check)

    payload["checks"] = checks
    payload["status"] = _diagnostic_status(checks)

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Codex Tools health: {payload['status'].upper()}")
        print()
        for index, check in enumerate(checks):
            if index:
                print()
            _print_diagnostic_check(check)
        print()
        if payload["status"] == "ok":
            print("No problems found.")
        elif payload["status"] == "warning":
            print("Codex Tools is usable, but the warnings above may limit some features.")
        else:
            print("One or more errors need attention before all features will work.")

        if args.verbose:
            print("\nTechnical details")
            print(f"  Configuration: {config_info['path']}")
            print(f"  Sessions root: {payload['sessions_root']}")
            print(
                "  Transcript range: "
                f"{jsonl['oldest_timestamp'] or 'unknown'} -> "
                f"{jsonl['newest_timestamp'] or 'unknown'}"
            )
            for path in jsonl["unreadable_paths"]:
                print(f"  Unreadable transcript: {path}")
            print(f"  Session index: {payload['session_index']}")
            print(f"  Thread history: {payload['thread_history']}")
            print(f"  SQLite tables: {', '.join(sqlite_info['tables']) or '(none)'}")
            if "thread_items" in sqlite_info:
                print(
                    "  SQLite range: "
                    f"{sqlite_info['oldest_timestamp'] or 'unknown'} -> "
                    f"{sqlite_info['newest_timestamp'] or 'unknown'}"
                )
                print("  SQLite item types:")
                for item_type, count in sqlite_info["item_types"].items():
                    print(f"    {item_type}: {count}")
    return 1 if payload["status"] == "error" else 0


def diagnose_main(argv: list[str] | None = None) -> int:
    return diagnose(parse_diagnose_args(argv))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.query and not args.list:
        print("error: provide a query, or use --list", file=sys.stderr)
        return 2

    use_jsonl = args.source == "jsonl"
    use_sqlite = args.source == "sqlite"

    paths = list(iter_jsonl_paths(args.sessions_root)) if use_jsonl else []
    if use_jsonl and args.include_archive:
        paths.extend(iter_jsonl_paths(DEFAULT_ARCHIVE_ROOT))
    titles = load_session_index_titles(args.session_index)
    matchers = make_matchers(args)
    since = parse_filter_datetime(args.since)
    until = parse_filter_datetime(args.until, end_of_day=True)

    matched_sessions: list[Session] = []
    matches_by_session: dict[Path, list[tuple[TextRecord, re.Match[str]]]] = {}

    all_sessions: list[Session] = []
    for path in paths:
        session = read_session(path, args.include_tools, args.include_system)
        all_sessions.append(session)
    if use_sqlite:
        all_sessions.extend(
            read_sqlite_sessions(
                args.thread_history, args.include_tools, args.include_system
            )
        )

    for session in all_sessions:
        if not session_in_date_range(session, since, until):
            continue
        resolve_session_title(session, titles)
        if args.list:
            matched_sessions.append(session)
            continue
        matches = matching_records(
            session,
            matchers,
            args.all_terms,
            titles_only=args.titles_only,
            role=args.role,
        )
        if matches:
            if session.title is None:
                session.title = infer_title_from_matches(matches)
                if session.title is not None:
                    session.title_source = TITLE_SOURCE_MATCHING_PROMPT
            if session.title is None:
                session.title = infer_title_from_matching_snippet(
                    matches, SINGLE_LINE_CONTEXT_CHARS
                )
                if session.title is not None:
                    session.title_source = TITLE_SOURCE_MATCHING_SNIPPET
            matched_sessions.append(session)
            matches_by_session[session.path] = matches

    matched_sessions.sort(key=session_sort_key, reverse=True)
    if args.json:
        if args.ungrouped:
            emit_json(matched_sessions, matches_by_session, args)
        else:
            emit_grouped_json(matched_sessions, matches_by_session, args)
    else:
        if args.ungrouped:
            emit_text(matched_sessions, matches_by_session, args)
        else:
            emit_grouped_text(matched_sessions, matches_by_session, args)
        if args.ungrouped and len(matched_sessions) > args.limit:
            print(f"\nShowing {args.limit} of {len(matched_sessions)} matching sessions.")
        elif not matched_sessions:
            print("No matching sessions found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
