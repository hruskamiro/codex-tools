#!/usr/bin/env python3
"""Serve a local read-only Codex conversation viewer."""

from __future__ import annotations

import argparse
import curses
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import urlopen

from codex_tools import config, paths, typeset
from codex_tools.browser import add_browser_args, browser_command, open_browser
from codex_tools.search import (
    DEFAULT_ARCHIVE_ROOT,
    DEFAULT_SESSIONS_ROOT,
    DEFAULT_SESSION_INDEX,
    Session,
    SessionTitle,
    iter_jsonl_paths,
    load_session_index_titles,
    read_session,
    resolve_session_title,
    text_records_from_event,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_VIEW_MODE = "markdown"
VIEW_MODES = {"markdown", "latex"}
WEB_ASSET_MODES = {"bundled", "cdn"}
LATEX_FILES = (
    "standalone.cls",
    "fontspec.sty",
    "xcolor.sty",
    "hyperref.sty",
    "fvextra.sty",
    "enumitem.sty",
    "tabularx.sty",
    "colortbl.sty",
    "soul.sty",
)
LATEX_FONTS = ("TeX Gyre Pagella", "TeX Gyre Heros", "PT Mono")
VENDOR_DIR = Path(__file__).with_name("vendor")
SUMMARY_HEAD_LINES = 80
SUMMARY_TAIL_LINES = 80
TAIL_READ_BLOCK_SIZE = 64 * 1024
VIEWER_PID_FILE = paths.VIEWER_STATE_DIR / "viewer.json"
VIEWER_LOG_FILE = paths.VIEWER_STATE_DIR / "viewer.log"
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
EXTERNAL_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
FENCED_MARKDOWN_CODE_RE = re.compile(
    r"^[ \t]*(?:```|~~~)[^\n]*\n.*?^[ \t]*(?:```|~~~)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
INLINE_MARKDOWN_CODE_RE = re.compile(r"(?P<ticks>`+)[^\n]*?(?P=ticks)")
ATTACHMENT_TYPES = {
    ".pdf": ("application/pdf", "PDF"),
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
    ".webp": ("image/webp", "WEBP"),
    ".gif": ("image/gif", "GIF"),
    ".txt": ("text/plain; charset=utf-8", "TEXT"),
    ".log": ("text/plain; charset=utf-8", "LOG"),
    ".csv": ("text/csv; charset=utf-8", "CSV"),
    ".tsv": ("text/tab-separated-values; charset=utf-8", "TSV"),
    ".json": ("application/json; charset=utf-8", "JSON"),
    ".yaml": ("text/yaml; charset=utf-8", "YAML"),
    ".yml": ("text/yaml; charset=utf-8", "YAML"),
    ".toml": ("text/plain; charset=utf-8", "TOML"),
}
TEXT_ATTACHMENT_SUFFIXES = {
    ".txt",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
}


def read_default_view(config_path: Path | None = None) -> str:
    if config_path is None:
        return str(config.value("viewer.default_view"))
    path = config_path or paths.VIEWER_CONFIG_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("default_view")
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return DEFAULT_VIEW_MODE
    return value if value in VIEW_MODES else DEFAULT_VIEW_MODE


def write_default_view(mode: str, config_path: Path | None = None) -> None:
    if mode not in VIEW_MODES:
        raise ValueError(f"unsupported viewer mode: {mode}")
    if config_path is None:
        config.set_value("viewer.default_view", mode)
        return
    path = config_path or paths.VIEWER_CONFIG_FILE
    paths.ensure_private_dir(path.parent)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    payload["default_view"] = mode
    paths.write_private_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def command_set_default_view(args: argparse.Namespace) -> int:
    write_default_view(args.default_view)
    print(f"Default viewer mode: {args.default_view}")
    return 0


def configured_typeset_code_mode() -> tuple[str, str]:
    selected, source = config.resolve("viewer.typeset_code_mode")
    if source == "built-in" and typeset.DEFAULT_CODE_MODE != "auto":
        return typeset.DEFAULT_CODE_MODE, "environment"
    return str(selected), source


def add_server_args(parser: argparse.ArgumentParser) -> None:
    configured_code_mode, code_mode_source = configured_typeset_code_mode()
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=DEFAULT_SESSIONS_ROOT,
        help=f"Session root to scan. Default: {DEFAULT_SESSIONS_ROOT}",
    )
    parser.add_argument(
        "--include-archive",
        action="store_true",
        help=f"Also allow sessions under {DEFAULT_ARCHIVE_ROOT}.",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=DEFAULT_ARCHIVE_ROOT,
        help=f"Archived session root. Default: {DEFAULT_ARCHIVE_ROOT}",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        default=DEFAULT_SESSION_INDEX,
        help=f"Codex session index. Default: {DEFAULT_SESSION_INDEX}",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host to bind. Default: {DEFAULT_HOST}",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding to a non-loopback host without authentication.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind. Default: {DEFAULT_PORT}",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the viewer in the default browser after starting.",
    )
    add_browser_args(parser)
    parser.add_argument(
        "--no-self-reload",
        action="store_true",
        help="Disable automatic server restart when this viewer script changes.",
    )
    parser.add_argument(
        "--typeset-debug",
        action="store_true",
        help="Enable isolated, cache-bypassing typeset bubble previews.",
    )
    parser.add_argument(
        "--typeset-code-mode",
        choices=sorted(typeset.CODE_MODES),
        default=configured_code_mode,
        help=(
            "Code-block renderer: auto, pygments, or verbatim. Effective default: "
            f"{configured_code_mode} ({code_mode_source})."
        ),
    )
    parser.add_argument(
        "--web-assets",
        choices=sorted(WEB_ASSET_MODES),
        default="bundled",
        help="Browser dependency source. Default: bundled.",
    )


def parse_args(
    argv: list[str] | None = None, *, prog: str = "codex-viewer"
) -> argparse.Namespace:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        return argparse.Namespace(command="default", func=command_default)
    commands = {"serve", "start", "restart", "stop", "status", "open", "pick", "doctor"}
    preference_flags = {"--set-default-latex", "--set-default-markdown"}
    if (
        raw_args[0] not in commands
        and raw_args[0] not in {"-h", "--help"}
        and raw_args[0] not in preference_flags
    ):
        raw_args = ["serve", *raw_args]

    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run and navigate the local read-only Codex conversation viewer.",
    )
    preference = parser.add_mutually_exclusive_group()
    preference.add_argument(
        "--set-default-latex",
        action="store_const",
        const="latex",
        dest="default_view",
        help="Open selected conversations in the LaTeX view by default.",
    )
    preference.add_argument(
        "--set-default-markdown",
        action="store_const",
        const="markdown",
        dest="default_view",
        help="Open selected conversations in the Markdown view by default.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the viewer server in the foreground.")
    add_server_args(serve)
    serve.set_defaults(func=command_serve)

    start = sub.add_parser("start", help="Start the viewer server in the background.")
    add_server_args(start)
    start.set_defaults(func=command_start)

    restart = sub.add_parser("restart", help="Restart the background viewer server.")
    add_server_args(restart)
    restart.set_defaults(func=command_restart)

    stop = sub.add_parser("stop", help="Stop the background viewer server.")
    stop.set_defaults(func=command_stop)

    status = sub.add_parser("status", help="Show background viewer server status.")
    status.set_defaults(func=command_status)

    open_cmd = sub.add_parser("open", help="Open the viewer in a browser.")
    add_browser_args(open_cmd)
    open_cmd.set_defaults(func=command_open)

    pick = sub.add_parser("pick", help="Choose a conversation and open it in a browser.")
    add_server_args(pick)
    pick.add_argument("--limit", type=int, default=80, help="Number of conversations to list.")
    pick.add_argument("--tail", type=int, default=24, help="Tail size to open. Default: 24.")
    pick.add_argument("--tools", action="store_true", help="Include tool records in the view.")
    pick.add_argument("--query", "-q", default="", help="Initial search query.")
    pick.set_defaults(func=command_pick)

    doctor = sub.add_parser(
        "doctor", help="Check viewer startup and optional LaTeX requirements."
    )
    doctor.set_defaults(func=command_doctor)
    args = parser.parse_args(raw_args)
    if args.default_view:
        if args.command:
            parser.error("default-view options cannot be combined with a command")
        args.func = command_set_default_view
    elif not hasattr(args, "func"):
        parser.error("a command or default-view option is required")
    return args


def compact_space(text: str) -> str:
    return " ".join(text.split())


def parse_timestamp(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def format_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def newest_record_time(session: Any) -> str:
    for record in reversed(session.records):
        if record.timestamp:
            return record.timestamp
    return session.created_at or ""


def safe_int(value: str | None, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(low, min(high, parsed))


def file_signature(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    return {
        "path": str(path),
        "mtimeNs": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def viewer_signature() -> dict[str, int | str]:
    return file_signature(Path(__file__).resolve())


def read_tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0:
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= limit:
            read_size = min(TAIL_READ_BLOCK_SIZE, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-limit:]]


def read_head_lines(path: Path, limit: int) -> list[str]:
    lines = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(limit):
            line = handle.readline()
            if not line:
                break
            lines.append(line.rstrip("\r\n"))
    return lines


def sampled_session(path: Path) -> Session:
    # The chooser only needs enough context for title, metadata, and recency.
    # Full transcript parsing stays on /api/session after a conversation opens.
    session = Session(path=path)
    lines = read_head_lines(path, SUMMARY_HEAD_LINES)
    tail_lines = read_tail_lines(path, SUMMARY_TAIL_LINES)
    if tail_lines != lines[-len(tail_lines):]:
        lines.extend(tail_lines)

    for line_no, line in enumerate(lines, start=1):
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
            text_records_from_event(
                event, line_no, include_tools=False, include_system=False
            )
        )
    return session


class ViewerState:
    def __init__(
        self,
        sessions_root: Path,
        archive_root: Path,
        include_archive: bool,
        session_index: Path,
        typeset_debug: bool = False,
        typeset_code_mode: str = typeset.DEFAULT_CODE_MODE,
        web_assets: str = "bundled",
    ) -> None:
        self.sessions_root = sessions_root.expanduser().resolve()
        self.archive_root = archive_root.expanduser().resolve()
        self.include_archive = include_archive
        self.typeset_debug = typeset_debug
        self.typeset_code_mode = typeset_code_mode
        self.web_assets = web_assets
        self.titles = load_session_index_titles(session_index)

    def allowed_roots(self) -> list[Path]:
        roots = [self.sessions_root]
        if self.include_archive:
            roots.append(self.archive_root)
        return roots

    def is_allowed_path(self, path: Path) -> bool:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return False
        if resolved.suffix != ".jsonl":
            return False
        return any(resolved == root or resolved.is_relative_to(root) for root in self.allowed_roots())

    def session_paths(self) -> list[Path]:
        paths = list(iter_jsonl_paths(self.sessions_root))
        if self.include_archive:
            paths.extend(iter_jsonl_paths(self.archive_root))
        return [path for path in paths if self.is_allowed_path(path)]


def session_summary(path: Path, titles: dict[str, SessionTitle]) -> dict[str, Any]:
    session = sampled_session(path)
    resolve_session_title(session, titles)
    last_time = newest_record_time(session)
    mtime = path.stat().st_mtime
    title = session.title or path.stem
    payload = {
        "path": str(path),
        "title": title,
        "titleSource": session.title_source or "",
        "createdAt": session.created_at or "",
        "createdLabel": format_time(session.created_at),
        "lastAt": last_time,
        "lastLabel": format_time(last_time) or datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
        "cwd": session.cwd or "",
        "source": session.source or "",
        "sessionId": session.session_id or "",
        "threadId": session.parent_thread_id or session.session_id or "",
        "rolloutId": session.rollout_id or "",
        # This is a sampled count used by the chooser to distinguish actual
        # conversations from metadata-only internal rollouts.  Full parsing
        # remains deferred until the conversation is opened.
        "recordCount": len(session.records),
        "mtime": mtime,
    }
    payload["id"] = preferred_session_id(payload)
    return payload


def session_group_key(item: dict[str, Any]) -> str:
    return str(item.get("threadId") or item.get("sessionId") or item.get("path") or "")


def session_sort_key(item: dict[str, Any]) -> tuple[str, float]:
    return (str(item.get("lastAt") or ""), float(item.get("mtime") or 0))


def session_selection_key(item: dict[str, Any]) -> tuple[int, int, str, float]:
    """Prefer the Codex CLI thread rollout over its internal child rollouts."""
    rollout_id = str(item.get("rolloutId") or "")
    session_id = str(item.get("sessionId") or "")
    is_canonical = bool(rollout_id and session_id and rollout_id == session_id)
    has_records = int(item.get("recordCount") or 0) > 0
    last_at, mtime = session_sort_key(item)
    return (int(is_canonical), int(has_records), last_at, mtime)


def dedupe_session_summaries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(session_group_key(item), []).append(item)

    deduped = []
    for group in grouped.values():
        group.sort(key=session_selection_key, reverse=True)
        selected = dict(group[0])
        selected["rolloutCount"] = len(group)
        selected["rolloutPaths"] = [str(item.get("path") or "") for item in group]
        deduped.append(selected)
    deduped.sort(key=session_sort_key, reverse=True)
    return deduped


def preferred_session_id(item: dict[str, Any]) -> str:
    return str(item.get("rolloutId") or item.get("sessionId") or item.get("threadId") or "")


def resolve_session_path(state: ViewerState, query: dict[str, list[str]]) -> Path:
    raw_path = query.get("path", [""])[0]
    if raw_path:
        path = Path(raw_path).expanduser().resolve()
        if not state.is_allowed_path(path):
            raise PermissionError("session path is outside the configured Codex roots")
        return path

    raw_id = query.get("id", [""])[0].strip()
    if not raw_id:
        raise ValueError("session path or id is required")

    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in state.session_paths():
        try:
            item = session_summary(path, state.titles)
        except OSError:
            continue
        candidates.append((path, item))

    for key in ("rolloutId", "sessionId", "threadId"):
        matches = [(path, item) for path, item in candidates if raw_id == str(item.get(key) or "")]
        if matches:
            matches.sort(key=lambda pair: session_sort_key(pair[1]), reverse=True)
            return matches[0][0].expanduser().resolve()

    raise FileNotFoundError(f"no session found for id {raw_id}")


def handle_sessions(state: ViewerState, query: dict[str, list[str]]) -> dict[str, Any]:
    limit = safe_int(query.get("limit", [None])[0], 80, 1, 500)
    search = compact_space(query.get("q", [""])[0]).lower()
    sessions = []
    for path in state.session_paths():
        try:
            item = session_summary(path, state.titles)
        except OSError:
            continue
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("title", "cwd", "createdAt", "lastAt", "sessionId", "threadId")
        ).lower()
        if search and search not in haystack:
            continue
        sessions.append(item)
    deduped = dedupe_session_summaries(sessions)
    return {"sessions": deduped[:limit], "total": len(deduped), "fileTotal": len(sessions)}


def records_for_view(
    records: list[Any], tail: int, all_records: bool, anchor_line: int = 0
) -> list[Any]:
    if all_records:
        return records
    start = max(0, len(records) - tail)
    if anchor_line:
        anchor_index = next(
            (
                index
                for index, record in enumerate(records)
                if record.line_no == anchor_line
            ),
            start,
        )
        start = min(start, anchor_index)
    return records[start:]


def handle_session(state: ViewerState, query: dict[str, list[str]]) -> dict[str, Any]:
    path = resolve_session_path(state, query)
    include_tools = query.get("tools", ["0"])[0] == "1"
    all_records = query.get("all", ["0"])[0] == "1"
    tail = safe_int(query.get("tail", [None])[0], 24, 1, 500)
    anchor_line = safe_int(query.get("anchor", [None])[0], 0, 0, 2**31 - 1)
    session = read_session(path, include_tools=include_tools, include_system=False)
    resolve_session_title(session, state.titles)
    records = records_for_view(session.records, tail, all_records, anchor_line)
    return {
        "session": session_summary(path, state.titles),
        "tail": tail,
        "all": all_records,
        "includeTools": include_tools,
        "typesetDebug": state.typeset_debug,
        "shownCount": len(records),
        "totalCount": len(session.records),
        "records": [
            {
                **asdict(record),
                "timeLabel": format_time(record.timestamp),
            }
            for record in records
        ],
    }


def handle_typeset(
    state: ViewerState,
    query: dict[str, list[str]],
    *,
    isolated: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    path = resolve_session_path(state, query)
    include_tools = query.get("tools", ["0"])[0] == "1"
    all_records = query.get("all", ["0"])[0] == "1"
    tail = safe_int(query.get("tail", [None])[0], 8, 1, 100)
    anchor_line = safe_int(query.get("anchor", [None])[0], 0, 0, 2**31 - 1)
    workers = safe_int(query.get("workers", [None])[0], 8, 1, 16)
    header_mode = query.get("header", [typeset.DEFAULT_HEADER_MODE])[0]
    if header_mode not in {"embedded", "external"}:
        raise ValueError(f"unknown typeset header mode: {header_mode}")
    default_code_mode = getattr(state, "typeset_code_mode", typeset.DEFAULT_CODE_MODE)
    code_mode = query.get("code", [default_code_mode])[0]
    effective_code_mode = typeset.resolved_code_mode(code_mode)
    session = read_session(path, include_tools=include_tools, include_system=False)
    resolve_session_title(session, state.titles)
    isolated_line_raw = query.get("line", [""])[0].strip()
    isolated_line: int | None = None
    if isolated_line_raw:
        if not isolated:
            raise FileNotFoundError("isolated typeset route not found")
        if force and not state.typeset_debug:
            raise PermissionError("isolated typeset rendering requires --typeset-debug")
        if not isolated_line_raw.isdigit():
            raise ValueError("typeset line must be a positive integer")
        isolated_line = int(isolated_line_raw)
        matching = [
            record
            for record in session.records
            if record.role == "assistant" and record.line_no == isolated_line
        ]
        if not matching:
            raise FileNotFoundError(f"assistant bubble not found at line {isolated_line}")
        records = [matching[-1]]
    else:
        records = records_for_view(session.records, tail, all_records, anchor_line)
    payload_records = []
    assistant_jobs: list[tuple[int, str, str]] = []
    for index, record in enumerate(records):
        item = {
            **asdict(record),
            "timeLabel": format_time(record.timestamp),
        }
        if record.role == "assistant":
            time_label = format_time(record.timestamp)
            title = f"Assistant answer · {time_label}" if time_label else "Assistant answer"
            assistant_jobs.append((index, record.text, title))
            item["attachments"] = attachment_link_payload(path, record)
            item["externalLinks"] = external_link_payload(record.text)
        payload_records.append(item)

    def attach_result(index: int, result: typeset.TypesetResult) -> None:
        pdf_url = f"/typeset/pdf/{result.key}.pdf" if result.ok else ""
        if pdf_url and force:
            pdf_url += f"?fresh={time.time_ns()}"
        payload_records[index]["typeset"] = {
            "ok": result.ok,
            "key": result.key,
            "cached": result.cached,
            "pdfUrl": pdf_url,
            "codeBlocks": typeset.fenced_code_blocks(payload_records[index]["text"]),
            "error": result.error or "",
        }

    if assistant_jobs:
        with ThreadPoolExecutor(max_workers=min(workers, len(assistant_jobs))) as pool:
            futures = {
                pool.submit(
                    typeset.render_pdf,
                    text,
                    title=title,
                    force=force,
                    header_mode=header_mode,
                    code_mode=code_mode,
                ): index
                for index, text, title in assistant_jobs
            }
            for future in as_completed(futures):
                attach_result(futures[future], future.result())

    return {
        "session": session_summary(path, state.titles),
        "tail": tail,
        "all": all_records,
        "includeTools": include_tools,
        "typesetWorkers": workers,
        "typesetDebug": state.typeset_debug,
        "typesetHeaderMode": header_mode,
        "typesetCodeMode": effective_code_mode,
        "isolatedLine": isolated_line,
        "debugLine": isolated_line if force else None,
        "typesetFresh": force,
        "shownCount": len(records),
        "totalCount": len(session.records),
        "records": payload_records,
    }


def local_attachment_links(markdown: str) -> list[tuple[str, Path]]:
    links: list[tuple[str, Path]] = []
    for match in MARKDOWN_LINK_RE.finditer(markdown):
        label = match.group(1).strip()
        target = match.group(2).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        candidate = Path(target).expanduser()
        if (
            not candidate.is_absolute()
            or candidate.suffix.lower() not in ATTACHMENT_TYPES
        ):
            continue
        links.append((label or candidate.name, candidate))
    return links


def _without_markdown_code(markdown: str) -> str:
    without_fences = FENCED_MARKDOWN_CODE_RE.sub("", markdown)
    return INLINE_MARKDOWN_CODE_RE.sub("", without_fences)


def _external_url(target: str) -> str | None:
    candidate = target.strip()
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()
    else:
        candidate = candidate.split(maxsplit=1)[0]
    candidate = candidate.rstrip(".,;:!?")
    while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
        candidate = candidate[:-1]
    candidate = candidate.rstrip("]}")
    if candidate.count("(") > candidate.count(")"):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate


def external_link_payload(markdown: str) -> list[dict[str, str]]:
    text = _without_markdown_code(markdown)
    candidates: list[tuple[int, int, str, str]] = []
    for match in MARKDOWN_LINK_RE.finditer(text):
        url = _external_url(match.group(2))
        if url:
            candidates.append((match.start(), 0, match.group(1).strip() or url, url))
    for match in EXTERNAL_URL_RE.finditer(text):
        url = _external_url(match.group(0))
        if url:
            candidates.append((match.start(), 1, url, url))

    payload: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, _, label, url in sorted(candidates):
        if url in seen:
            continue
        seen.add(url)
        payload.append(
            {
                "label": label,
                "url": url,
                "host": urlparse(url).hostname or "",
            }
        )
    return payload


def validated_attachment(path: Path) -> tuple[Path, str, str]:
    candidate = path.resolve()
    suffix = candidate.suffix.lower()
    attachment_type = ATTACHMENT_TYPES.get(suffix)
    if attachment_type is None or not candidate.is_file():
        raise FileNotFoundError("attachment not found")
    try:
        with candidate.open("rb") as handle:
            sample = handle.read(8192)
    except OSError as exc:
        raise FileNotFoundError("attachment not found") from exc

    valid = False
    if suffix == ".pdf":
        valid = sample.startswith(b"%PDF-")
    elif suffix == ".png":
        valid = sample.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = sample.startswith(b"\xff\xd8\xff")
    elif suffix == ".gif":
        valid = sample.startswith((b"GIF87a", b"GIF89a"))
    elif suffix == ".webp":
        valid = len(sample) >= 12 and sample[:4] == b"RIFF" and sample[8:12] == b"WEBP"
    elif suffix in TEXT_ATTACHMENT_SUFFIXES and b"\x00" not in sample:
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            valid = False
        else:
            valid = True
    if not valid:
        raise FileNotFoundError("attachment not found")
    return candidate, attachment_type[0], attachment_type[1]


def attachment_link_payload(session_path: Path, record: Any) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for index, (label, candidate) in enumerate(local_attachment_links(record.text)):
        try:
            _, _, kind = validated_attachment(candidate)
        except (FileNotFoundError, OSError):
            continue
        payload.append(
            {
                "label": label,
                "kind": kind,
                "url": "/open?"
                + urlencode(
                    {
                        "path": str(session_path),
                        "line": record.line_no,
                        "link": index,
                    }
                ),
            }
        )
    return payload


def resolve_linked_attachment(
    state: ViewerState, query: dict[str, list[str]]
) -> tuple[Path, str]:
    session_path = resolve_session_path(state, query)
    line_raw = query.get("line", [""])[0]
    link_raw = query.get("link", [""])[0]
    if not line_raw.isdigit() or int(line_raw) <= 0:
        raise FileNotFoundError("attachment not found")
    if not link_raw.isdigit():
        raise FileNotFoundError("attachment not found")
    line = int(line_raw)
    link_index = int(link_raw)
    session = read_session(session_path, include_tools=False, include_system=False)
    matching = [
        record
        for record in session.records
        if record.role == "assistant" and record.line_no == line
    ]
    if not matching:
        raise FileNotFoundError("attachment not found")
    links = local_attachment_links(matching[-1].text)
    if link_index >= len(links):
        raise FileNotFoundError("attachment not found")
    candidate, content_type, _ = validated_attachment(links[link_index][1])
    return candidate, content_type


def handle_version(state: ViewerState, query: dict[str, list[str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {"viewer": viewer_signature()}
    if query.get("path", [""])[0] or query.get("id", [""])[0]:
        path = resolve_session_path(state, query)
        payload["sessionFile"] = file_signature(path)
    return payload


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(
    handler: BaseHTTPRequestHandler, body: str, content_type: str, status: int = 200
) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def bytes_response(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    content_type: str,
    status: int = 200,
    cache: str = "no-store",
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", cache)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def attachment_file_response(
    handler: BaseHTTPRequestHandler, path: Path, content_type: str
) -> None:
    size = path.stat().st_size
    encoded_name = quote(path.name, safe="")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Disposition", f"inline; filename*=UTF-8''{encoded_name}")
    handler.send_header("Cache-Control", "private, no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(size))
    handler.end_headers()
    with path.open("rb") as source:
        shutil.copyfileobj(source, handler.wfile)


def handle_typeset_pdf(handler: BaseHTTPRequestHandler, key_with_suffix: str) -> None:
    if not key_with_suffix.endswith(".pdf"):
        raise FileNotFoundError("typeset PDF not found")
    key = key_with_suffix[:-4]
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise FileNotFoundError("typeset PDF not found")
    pdf_path = typeset.typeset_cache_dir() / key[:2] / key / "bubble.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError("typeset PDF not found")
    bytes_response(
        handler,
        pdf_path.read_bytes(),
        "application/pdf",
        cache="private, max-age=86400",
    )


def handle_vendor_asset(handler: BaseHTTPRequestHandler, raw_name: str) -> None:
    relative = unquote(raw_name).lstrip("/")
    if not relative or "\x00" in relative:
        raise FileNotFoundError("vendor asset not found")
    candidate = (VENDOR_DIR / relative).resolve()
    if not candidate.is_relative_to(VENDOR_DIR.resolve()) or not candidate.is_file():
        raise FileNotFoundError("vendor asset not found")
    content_types = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".ttf": "font/ttf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }
    content_type = content_types.get(candidate.suffix.lower())
    if content_type is None:
        raise FileNotFoundError("vendor asset not found")
    bytes_response(
        handler,
        candidate.read_bytes(),
        content_type,
        cache="public, max-age=31536000, immutable",
    )


def viewer_document(template: str, web_assets: str) -> str:
    rendered = template.replace('data-web-assets="bundled"', f'data-web-assets="{web_assets}"')
    if web_assets == "bundled":
        return rendered
    for bundled, cdn in WEB_ASSET_CDN_URLS.items():
        rendered = rendered.replace(bundled, cdn)
    return rendered.replace(
        "</head>",
        '  <link rel="preconnect" href="https://cdn.jsdelivr.net">\n</head>',
        1,
    )


def chooser_document(default_view: str) -> str:
    mode = default_view if default_view in VIEW_MODES else DEFAULT_VIEW_MODE
    return INDEX_HTML.replace(
        'data-default-view="markdown"', f'data-default-view="{mode}"', 1
    )


def make_handler(state: ViewerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    text_response(
                        self,
                        chooser_document(read_default_view()),
                        "text/html; charset=utf-8",
                    )
                elif parsed.path == "/view" or parsed.path.startswith("/v/"):
                    text_response(
                        self,
                        viewer_document(VIEW_HTML, state.web_assets),
                        "text/html; charset=utf-8",
                    )
                elif (
                    parsed.path == "/typeset"
                    or parsed.path.startswith("/t/")
                    or parsed.path == "/debug/typeset"
                    or parsed.path.startswith("/debug/typeset/")
                ):
                    text_response(
                        self,
                        viewer_document(TYPESET_HTML, state.web_assets),
                        "text/html; charset=utf-8",
                    )
                elif parsed.path == "/app.css":
                    text_response(self, APP_CSS, "text/css; charset=utf-8")
                elif parsed.path == "/app.js":
                    text_response(self, APP_JS, "application/javascript; charset=utf-8")
                elif parsed.path == "/view.js":
                    text_response(self, VIEW_JS, "application/javascript; charset=utf-8")
                elif parsed.path == "/typeset.js":
                    text_response(self, TYPESET_JS, "application/javascript; charset=utf-8")
                elif parsed.path.startswith("/vendor/"):
                    handle_vendor_asset(self, parsed.path.removeprefix("/vendor/"))
                elif parsed.path == "/api/sessions":
                    json_response(self, handle_sessions(state, query))
                elif parsed.path == "/api/session":
                    json_response(self, handle_session(state, query))
                elif parsed.path == "/api/typeset":
                    json_response(self, handle_typeset(state, query))
                elif parsed.path == "/api/typeset/answer":
                    json_response(self, handle_typeset(state, query, isolated=True))
                elif parsed.path == "/api/debug/typeset":
                    json_response(
                        self,
                        handle_typeset(state, query, isolated=True, force=True),
                    )
                elif parsed.path == "/api/version":
                    json_response(self, handle_version(state, query))
                elif parsed.path == "/open":
                    attachment_path, content_type = resolve_linked_attachment(
                        state, query
                    )
                    attachment_file_response(self, attachment_path, content_type)
                elif parsed.path.startswith("/typeset/pdf/"):
                    handle_typeset_pdf(self, parsed.path.removeprefix("/typeset/pdf/"))
                else:
                    json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except FileNotFoundError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # pragma: no cover - defensive for local UI
                json_response(
                    self,
                    {"error": f"{exc.__class__.__name__}: {exc}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return Handler


def start_self_reloader(interval: float = 0.8) -> None:
    script = Path(__file__).resolve()
    try:
        original = file_signature(script)
    except OSError:
        return

    def watch() -> None:
        while True:
            time.sleep(interval)
            try:
                current = file_signature(script)
            except OSError:
                continue
            if current != original:
                print("Viewer source changed; restarting server.", file=sys.stderr, flush=True)
                os.execv(sys.executable, [sys.executable, *sys.argv])

    threading.Thread(target=watch, daemon=True).start()


def viewer_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def host_is_loopback(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_bind_host(args: argparse.Namespace) -> bool:
    if host_is_loopback(args.host) or args.allow_remote:
        return True
    print(
        "error: refusing to expose private transcripts on a non-loopback host; "
        "pass --allow-remote to acknowledge the risk",
        file=sys.stderr,
    )
    return False


def view_url(
    base_url: str,
    path: str,
    tail: int,
    tools: bool,
    session_id: str = "",
    mode: str = DEFAULT_VIEW_MODE,
) -> str:
    params = {"tail": str(tail)}
    if tools:
        params["tools"] = "1"
    typeset = mode == "latex"
    if session_id:
        route = "t" if typeset else "v"
        return f"{base_url.rstrip('/')}/{route}/{session_id}?{urlencode(params)}"
    params["path"] = path
    route = "typeset" if typeset else "view"
    return f"{base_url.rstrip('/')}/{route}?{urlencode(params)}"


def server_state_from_args(args: argparse.Namespace) -> ViewerState:
    return ViewerState(
        args.sessions_root,
        args.archive_root,
        args.include_archive,
        args.session_index,
        args.typeset_debug,
        args.typeset_code_mode,
        args.web_assets,
    )


def run_server(args: argparse.Namespace) -> int:
    state = server_state_from_args(args)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    url = viewer_url(args.host, args.port)
    print(f"Codex conversation viewer: {url}")
    if not args.no_self_reload:
        start_self_reloader()
        print("Self-reload is on; use --no-self-reload to disable it.")
    print("Press Ctrl-C to stop.")
    if args.open:
        if not open_browser(url, args):
            print(
                "warning: viewer is running, but the browser could not be opened",
                file=sys.stderr,
            )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def command_serve(args: argparse.Namespace) -> int:
    if not validate_bind_host(args):
        return 2
    return run_server(args)


def read_daemon_state() -> dict[str, Any] | None:
    if not VIEWER_PID_FILE.exists():
        return None
    try:
        return json.loads(VIEWER_PID_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_daemon_state(payload: dict[str, Any]) -> None:
    paths.ensure_private_dir(VIEWER_PID_FILE.parent)
    paths.write_private_text(VIEWER_PID_FILE, json.dumps(payload, indent=2) + "\n")


def remove_daemon_state() -> None:
    try:
        VIEWER_PID_FILE.unlink()
    except (FileNotFoundError, OSError):
        pass


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_daemon_state() -> dict[str, Any] | None:
    state = read_daemon_state()
    if not state:
        return None
    pid = int(state.get("pid") or 0)
    if pid and pid_is_running(pid):
        return state
    remove_daemon_state()
    return None


def viewer_health(url: str, timeout: float = 0.6) -> tuple[bool, str]:
    endpoint = f"{url.rstrip('/')}/api/version"
    try:
        with urlopen(endpoint, timeout=timeout) as response:
            if response.status != HTTPStatus.OK:
                return False, f"HTTP {response.status} from {endpoint}"
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc)
    viewer = payload.get("viewer") if isinstance(payload, dict) else None
    if not isinstance(viewer, dict) or not isinstance(viewer.get("path"), str):
        return False, f"unexpected response from {endpoint}"
    return True, "responding"


def wait_for_url(url: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        healthy, _ = viewer_health(url, timeout=0.4)
        if healthy:
            return True
        time.sleep(0.1)
    return False


def stop_spawned_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def daemon_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "codex_tools.viewer",
        "serve",
        "--sessions-root",
        str(args.sessions_root),
        "--archive-root",
        str(args.archive_root),
        "--session-index",
        str(args.session_index),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--no-self-reload",
    ]
    if args.include_archive:
        command.append("--include-archive")
    if args.allow_remote:
        command.append("--allow-remote")
    if args.typeset_debug:
        command.append("--typeset-debug")
    command.extend(["--typeset-code-mode", args.typeset_code_mode])
    command.extend(["--web-assets", args.web_assets])
    return command


def command_start(args: argparse.Namespace) -> int:
    if not validate_bind_host(args):
        return 2
    current = running_daemon_state()
    if current:
        url = str(current.get("url") or "")
        healthy, detail = viewer_health(url)
        if not healthy:
            print(
                f"error: viewer process {current.get('pid')} is running but "
                f"not responding: {detail}",
                file=sys.stderr,
            )
            print(f"Log: {current.get('log')}", file=sys.stderr)
            print("Run `codex-tools viewer restart` to recover.", file=sys.stderr)
            return 1
        print(f"Viewer already running: {url}")
        if args.open:
            if not open_browser(url, args):
                return 1
        return 0

    paths.ensure_private_dir(paths.VIEWER_STATE_DIR)
    log_handle = VIEWER_LOG_FILE.open("a", encoding="utf-8")
    VIEWER_LOG_FILE.chmod(0o600)
    try:
        process = subprocess.Popen(
            daemon_command(args),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"error: could not start viewer: {exc}", file=sys.stderr)
        return 1
    finally:
        log_handle.close()
    url = viewer_url(args.host, args.port)
    write_daemon_state(
        {
            "pid": process.pid,
            "host": args.host,
            "port": args.port,
            "url": url,
            "log": str(VIEWER_LOG_FILE),
            "startedAt": datetime.now().isoformat(),
            "typesetDebug": args.typeset_debug,
            "webAssets": args.web_assets,
        }
    )
    ready = wait_for_url(url)
    return_code = process.poll()
    if not ready or return_code is not None:
        stop_spawned_process(process)
        remove_daemon_state()
        reason = (
            f"process exited with status {return_code}"
            if return_code is not None
            else "server did not become ready within 5 seconds"
        )
        print(f"error: viewer failed to start: {reason}", file=sys.stderr)
        print(f"Log: {VIEWER_LOG_FILE}", file=sys.stderr)
        return 1
    print(f"Started viewer: {url}")
    if args.open:
        if not open_browser(url, args):
            print("Viewer remains available at the URL above.", file=sys.stderr)
            return 1
    return 0


def command_restart(args: argparse.Namespace) -> int:
    command_stop(args)
    return command_start(args)


def command_stop(args: argparse.Namespace) -> int:
    state = running_daemon_state()
    if not state:
        print("Viewer is not running.")
        return 0
    pid = int(state["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not pid_is_running(pid):
            break
        time.sleep(0.1)
    if pid_is_running(pid):
        os.kill(pid, signal.SIGKILL)
    remove_daemon_state()
    print("Stopped viewer.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    state = running_daemon_state()
    if not state:
        print("Viewer is not running.")
        return 1
    url = str(state.get("url") or "")
    healthy, detail = viewer_health(url)
    if not healthy:
        print(f"Viewer process is running but unhealthy: {detail}")
        print(f"PID: {state.get('pid')}")
        print(f"Log: {state.get('log')}")
        return 1
    print(f"Viewer running: {url}")
    print(f"PID: {state.get('pid')}")
    print(f"Log: {state.get('log')}")
    return 0


def probe_command(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = completed.stdout.strip()
    return completed.returncode == 0 and bool(output), output


def latex_requirement_checks() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    xelatex = shutil.which("xelatex")
    checks.append(("xelatex", bool(xelatex), xelatex or "not found on PATH"))

    kpsewhich = shutil.which("kpsewhich")
    checks.append(("kpsewhich", bool(kpsewhich), kpsewhich or "not found on PATH"))
    for filename in LATEX_FILES:
        if not kpsewhich:
            checks.append((filename, False, "cannot check without kpsewhich"))
            continue
        ok, output = probe_command([kpsewhich, filename])
        checks.append((filename, ok, output or "not found"))

    fc_match = shutil.which("fc-match")
    checks.append(("fc-match", bool(fc_match), fc_match or "not found on PATH"))
    for font in LATEX_FONTS:
        if not fc_match:
            checks.append((font, False, "cannot check without fc-match"))
            continue
        ok, output = probe_command([fc_match, "--format=%{family}", font])
        matched = ok and font.casefold() in output.casefold()
        checks.append((font, matched, output or "not found"))
    return checks


def viewer_startup_checks() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    command = browser_command()
    checks.append(
        (
            "browser launcher",
            command is not None,
            " ".join(command) if command else "not found; use --browser COMMAND",
        )
    )

    display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
    checks.append(
        (
            "graphical session",
            bool(display),
            display or "DISPLAY and WAYLAND_DISPLAY are unset",
        )
    )

    state = running_daemon_state()
    if state:
        url = str(state.get("url") or "")
        healthy, detail = viewer_health(url)
        checks.append(("viewer server", healthy, f"{url} ({detail})"))
    else:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((DEFAULT_HOST, DEFAULT_PORT))
        except OSError as exc:
            checks.append(
                (
                    f"port {DEFAULT_PORT}",
                    False,
                    f"unavailable while viewer is stopped: {exc}",
                )
            )
        else:
            checks.append(
                (f"port {DEFAULT_PORT}", True, "available for viewer startup")
            )
    return checks


def command_doctor(args: argparse.Namespace) -> int:
    startup_checks = viewer_startup_checks()
    print("Viewer startup:")
    for name, ok, detail in startup_checks:
        print(f"  [{'ok' if ok else 'problem'}] {name}: {detail}")
    startup_ok = all(ok for _, ok, _ in startup_checks)
    print("Markdown viewer is ready." if startup_ok else "Viewer startup has problems.")

    latex_checks = latex_requirement_checks()
    print()
    print("Optional LaTeX view:")
    for name, ok, detail in latex_checks:
        print(f"  [{'ok' if ok else 'missing'}] {name}: {detail}")
    default_view = read_default_view()
    print(f"Default viewer mode: {default_view}")
    latex_ok = all(ok for _, ok, _ in latex_checks)
    if latex_ok:
        print("LaTeX viewer is ready.")
        print("Set it as the default with: codex-tools viewer --set-default-latex")
    else:
        print("LaTeX is unavailable; the Markdown viewer still works.")
        print()
        print("Ubuntu/Debian install hint:")
        print(
            "  sudo apt install texlive-xetex texlive-latex-extra "
            "fonts-texgyre fonts-paratype"
        )
    return 0 if startup_ok and (default_view != "latex" or latex_ok) else 1


def default_server_args() -> argparse.Namespace:
    code_mode, _ = configured_typeset_code_mode()
    return argparse.Namespace(
        sessions_root=DEFAULT_SESSIONS_ROOT,
        include_archive=False,
        archive_root=DEFAULT_ARCHIVE_ROOT,
        session_index=DEFAULT_SESSION_INDEX,
        host=DEFAULT_HOST,
        allow_remote=False,
        port=DEFAULT_PORT,
        open=False,
        browser=None,
        same_window=False,
        no_self_reload=True,
        typeset_debug=False,
        typeset_code_mode=code_mode,
        web_assets="bundled",
    )


def default_pick_args() -> argparse.Namespace:
    args = default_server_args()
    args.limit = 80
    args.tail = 24
    args.tools = False
    args.query = ""
    return args


def command_default(args: argparse.Namespace) -> int:
    return command_pick(default_pick_args())


def command_open(args: argparse.Namespace) -> int:
    state = running_daemon_state()
    if not state:
        start_args = default_server_args()
        result = command_start(start_args)
        if result:
            return result
        state = running_daemon_state()
    if not state:
        print("error: viewer is not running", file=sys.stderr)
        return 1
    url = str(state["url"])
    healthy, detail = viewer_health(url)
    if not healthy:
        print(f"error: viewer is not responding: {detail}", file=sys.stderr)
        print("Run `codex-tools viewer restart` to recover.", file=sys.stderr)
        return 1
    if not open_browser(url, args):
        return 1
    print(f"Opened {state['url']}")
    return 0


def session_label(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "Untitled")
    time_label = str(item.get("lastLabel") or "")
    rollout_count = int(item.get("rolloutCount") or 0)
    rollout_label = f"{rollout_count} rollouts" if rollout_count > 1 else ""
    cwd = str(item.get("cwd") or item.get("path") or "")
    bits = [bit for bit in (time_label, title, rollout_label, cwd) if bit]
    return "  ".join(bits)


def session_label_parts(item: dict[str, Any]) -> tuple[str, str, str, str]:
    rollout_count = int(item.get("rolloutCount") or 0)
    return (
        str(item.get("lastLabel") or ""),
        str(item.get("title") or "Untitled"),
        f"{rollout_count} rollouts" if rollout_count > 1 else "",
        str(item.get("cwd") or item.get("path") or ""),
    )


def choose_session_numbered(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for index, item in enumerate(sessions, start=1):
        print(f"{index:>3}. {session_label(item)}")
    try:
        raw = input("Open conversation number: ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    try:
        selected = int(raw)
    except ValueError:
        return None
    if 1 <= selected <= len(sessions):
        return sessions[selected - 1]
    return None


def choose_session_curses(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    def setup_colors() -> dict[str, int]:
        if not curses.has_colors():
            return {}
        curses.start_color()
        curses.use_default_colors()
        palette = {
            "header": (curses.COLOR_CYAN, -1),
            "muted": (curses.COLOR_BLUE, -1),
            "title": (curses.COLOR_WHITE, -1),
            "accent": (curses.COLOR_MAGENTA, -1),
            "selected": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "selected_muted": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "footer": (curses.COLOR_GREEN, -1),
        }
        pairs = {}
        for index, (name, colors) in enumerate(palette.items(), start=1):
            try:
                curses.init_pair(index, colors[0], colors[1])
            except curses.error:
                continue
            pairs[name] = curses.color_pair(index)
        return pairs

    def draw_text(stdscr: Any, y: int, x: int, text: str, width: int, attr: int) -> int:
        if width <= 0:
            return x
        clipped = text[:width]
        try:
            stdscr.addnstr(y, x, clipped, width, attr)
        except curses.error:
            pass
        return x + len(clipped)

    def draw_row(
        stdscr: Any,
        y: int,
        item: dict[str, Any],
        width: int,
        selected: bool,
        colors: dict[str, int],
    ) -> None:
        time_label, title, rollout_label, cwd = session_label_parts(item)
        if selected:
            base = colors.get("selected", curses.A_REVERSE)
            muted = colors.get("selected_muted", base)
            accent = base
            title_attr = base | curses.A_BOLD
        else:
            base = curses.A_NORMAL
            muted = colors.get("muted", curses.A_DIM)
            accent = colors.get("accent", curses.A_BOLD)
            title_attr = colors.get("title", curses.A_NORMAL) | curses.A_BOLD

        try:
            stdscr.addnstr(y, 0, " " * max(0, width - 1), width - 1, base)
        except curses.error:
            pass

        x = 1
        x = draw_text(stdscr, y, x, time_label.ljust(18), min(19, width - x - 1), muted)
        if rollout_label:
            title_width = max(12, width - x - len(rollout_label) - 28)
        else:
            title_width = max(12, width - x - 24)
        x = draw_text(stdscr, y, x, title.ljust(title_width), title_width, title_attr)
        if rollout_label:
            x = draw_text(stdscr, y, x, f" {rollout_label} ", min(len(rollout_label) + 2, width - x - 1), accent)
        remaining = width - x - 1
        if remaining > 4:
            draw_text(stdscr, y, x, cwd, remaining, muted)

    def run(stdscr: Any) -> int | None:
        curses.curs_set(0)
        colors = setup_colors()
        index = 0
        offset = 0
        while True:
            height, width = stdscr.getmaxyx()
            page_size = max(1, height - 4)
            if index < offset:
                offset = index
            elif index >= offset + page_size:
                offset = index - page_size + 1
            stdscr.erase()
            header = "Codex conversations"
            hint = "arrows/j/k move  Enter opens  q quits"
            count = f"{len(sessions)} shown"
            header_attr = colors.get("header", curses.A_BOLD) | curses.A_BOLD
            muted_attr = colors.get("muted", curses.A_DIM)
            footer_attr = colors.get("footer", curses.A_NORMAL)
            stdscr.addnstr(0, 1, header, max(0, width - 2), header_attr)
            stdscr.addnstr(0, max(1, width - len(count) - 2), count, len(count), muted_attr)
            stdscr.addnstr(1, 1, hint, max(0, width - 2), muted_attr)
            for row, item in enumerate(sessions[offset : offset + page_size], start=2):
                absolute = offset + row - 2
                draw_row(stdscr, row, item, width, absolute == index, colors)
            footer = "Newest rollout is opened when a conversation has multiple rollouts."
            stdscr.addnstr(height - 1, 1, footer, max(0, width - 2), footer_attr)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), 27):
                return None
            if key in (curses.KEY_UP, ord("k")):
                index = max(0, index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = min(len(sessions) - 1, index + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                return index

    selected = curses.wrapper(run)
    if selected is None:
        return None
    return sessions[selected]


def load_sessions_for_pick(args: argparse.Namespace) -> dict[str, Any]:
    state = server_state_from_args(args)
    return handle_sessions(
        state,
        {"limit": [str(args.limit)], "q": [args.query]},
    )


def choose_session_curses_async(args: argparse.Namespace) -> dict[str, Any] | None:
    loaded = threading.Event()
    result: dict[str, Any] = {"sessions": [], "error": None}

    def worker() -> None:
        try:
            result.update(load_sessions_for_pick(args))
        except Exception as exc:
            result["error"] = exc
        finally:
            loaded.set()

    threading.Thread(target=worker, daemon=True).start()

    def setup_colors() -> dict[str, int]:
        if not curses.has_colors():
            return {}
        curses.start_color()
        curses.use_default_colors()
        palette = {
            "header": (curses.COLOR_CYAN, -1),
            "muted": (curses.COLOR_BLUE, -1),
            "title": (curses.COLOR_WHITE, -1),
            "accent": (curses.COLOR_MAGENTA, -1),
            "selected": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "selected_muted": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "footer": (curses.COLOR_GREEN, -1),
        }
        pairs = {}
        for index, (name, colors) in enumerate(palette.items(), start=1):
            try:
                curses.init_pair(index, colors[0], colors[1])
            except curses.error:
                continue
            pairs[name] = curses.color_pair(index)
        return pairs

    def draw_text(stdscr: Any, y: int, x: int, text: str, width: int, attr: int) -> int:
        if width <= 0:
            return x
        clipped = text[:width]
        try:
            stdscr.addnstr(y, x, clipped, width, attr)
        except curses.error:
            pass
        return x + len(clipped)

    def draw_row(
        stdscr: Any,
        y: int,
        item: dict[str, Any],
        width: int,
        selected: bool,
        colors: dict[str, int],
    ) -> None:
        time_label, title, rollout_label, cwd = session_label_parts(item)
        if selected:
            base = colors.get("selected", curses.A_REVERSE)
            muted = colors.get("selected_muted", base)
            accent = base
            title_attr = base | curses.A_BOLD
        else:
            base = curses.A_NORMAL
            muted = colors.get("muted", curses.A_DIM)
            accent = colors.get("accent", curses.A_BOLD)
            title_attr = colors.get("title", curses.A_NORMAL) | curses.A_BOLD

        try:
            stdscr.addnstr(y, 0, " " * max(0, width - 1), width - 1, base)
        except curses.error:
            pass

        x = 1
        x = draw_text(stdscr, y, x, time_label.ljust(18), min(19, width - x - 1), muted)
        title_width = max(12, width - x - len(rollout_label) - 28) if rollout_label else max(12, width - x - 24)
        x = draw_text(stdscr, y, x, title.ljust(title_width), title_width, title_attr)
        if rollout_label:
            x = draw_text(stdscr, y, x, f" {rollout_label} ", min(len(rollout_label) + 2, width - x - 1), accent)
        remaining = width - x - 1
        if remaining > 4:
            draw_text(stdscr, y, x, cwd, remaining, muted)

    def run(stdscr: Any) -> int | None:
        curses.curs_set(0)
        stdscr.timeout(120)
        colors = setup_colors()
        index = 0
        offset = 0
        spinner = "|/-\\"
        tick = 0
        while True:
            sessions = list(result.get("sessions") or [])
            error = result.get("error")
            height, width = stdscr.getmaxyx()
            page_size = max(1, height - 4)
            if sessions:
                index = min(index, len(sessions) - 1)
            if index < offset:
                offset = index
            elif index >= offset + page_size:
                offset = index - page_size + 1
            stdscr.erase()
            header_attr = colors.get("header", curses.A_BOLD) | curses.A_BOLD
            muted_attr = colors.get("muted", curses.A_DIM)
            footer_attr = colors.get("footer", curses.A_NORMAL)
            stdscr.addnstr(0, 1, "Codex conversations", max(0, width - 2), header_attr)

            if error is not None:
                message = f"Could not load conversations: {error}"
                stdscr.addnstr(2, 1, message, max(0, width - 2), curses.A_BOLD)
                stdscr.addnstr(height - 1, 1, "q quits", max(0, width - 2), footer_attr)
            elif not loaded.is_set():
                mark = spinner[tick % len(spinner)]
                query = f" matching {args.query!r}" if args.query else ""
                message = f"{mark} Loading recent conversations{query}..."
                stdscr.addnstr(2, 1, message, max(0, width - 2), muted_attr | curses.A_BOLD)
                stdscr.addnstr(height - 1, 1, "q quits", max(0, width - 2), footer_attr)
            elif not sessions:
                stdscr.addnstr(2, 1, "No conversations found.", max(0, width - 2), muted_attr | curses.A_BOLD)
                stdscr.addnstr(height - 1, 1, "q quits", max(0, width - 2), footer_attr)
            else:
                hint = "arrows/j/k move  Enter opens  q quits"
                count = f"{len(sessions)} shown"
                stdscr.addnstr(0, max(1, width - len(count) - 2), count, len(count), muted_attr)
                stdscr.addnstr(1, 1, hint, max(0, width - 2), muted_attr)
                for row, item in enumerate(sessions[offset : offset + page_size], start=2):
                    absolute = offset + row - 2
                    draw_row(stdscr, row, item, width, absolute == index, colors)
                footer = "Newest rollout is opened when a conversation has multiple rollouts."
                stdscr.addnstr(height - 1, 1, footer, max(0, width - 2), footer_attr)
            stdscr.refresh()

            key = stdscr.getch()
            tick += 1
            if key in (ord("q"), 27):
                return None
            if error is not None or not sessions:
                continue
            if key in (curses.KEY_UP, ord("k")):
                index = max(0, index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                index = min(len(sessions) - 1, index + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                return index

    selected = curses.wrapper(run)
    sessions = list(result.get("sessions") or [])
    if selected is None or not sessions:
        return None
    return sessions[selected]


def choose_session(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not sessions:
        print("No conversations found.")
        return None
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            return choose_session_curses(sessions)
        except curses.error:
            pass
    return choose_session_numbered(sessions)


def command_pick(args: argparse.Namespace) -> int:
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            item = choose_session_curses_async(args)
        except curses.error:
            payload = load_sessions_for_pick(args)
            item = choose_session_numbered(payload["sessions"])
    else:
        payload = load_sessions_for_pick(args)
        item = choose_session(payload["sessions"])
    if item is None:
        return 1
    daemon = running_daemon_state()
    if not daemon:
        args.open = False
        result = command_start(args)
        if result:
            return result
        daemon = running_daemon_state()
    if not daemon:
        print("error: viewer is not running", file=sys.stderr)
        return 1
    url = view_url(
        str(daemon["url"]),
        str(item["path"]),
        args.tail,
        args.tools,
        preferred_session_id(item),
        read_default_view(),
    )
    if not open_browser(url, args):
        print(f"Viewer remains available at {url}", file=sys.stderr)
        return 1
    print(f"Opened {url}")
    return 0


WEB_ASSET_CDN_URLS = {
    "/vendor/purify.min.js": "https://cdn.jsdelivr.net/npm/dompurify@3.2.7/dist/purify.min.js",
    "/vendor/marked.min.js": "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js",
    "/vendor/highlight.min.js": "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.11.1/highlight.min.js",
    "/vendor/highlight-github.min.css": "https://cdn.jsdelivr.net/npm/highlight.js@11.11.1/styles/github.min.css",
    "/vendor/katex/katex.min.css": "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.css",
    "/vendor/katex/katex.min.js": "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.js",
    "/vendor/katex/auto-render.min.js": "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/contrib/auto-render.min.js",
    "/vendor/pdf.min.mjs": "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs",
    "/vendor/pdf.worker.min.mjs": "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs",
}


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Conversation Viewer</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body class="theme-mist" data-default-view="markdown">
  <main class="app-shell chooser-shell">
    <section class="session-pane chooser-pane">
      <header class="brand">
        <div>
          <p class="eyebrow">Local Codex</p>
          <h1>Choose Conversation</h1>
        </div>
        <button id="refreshSessions" class="icon-button" type="button" title="Refresh sessions" aria-label="Refresh sessions">↻</button>
      </header>
      <div class="chooser-controls">
        <label class="select-label">
          <span>Tail</span>
          <select id="tailCount">
            <option value="1">1</option>
            <option value="4">4</option>
            <option value="8">8</option>
            <option value="16">16</option>
            <option value="24" selected>24</option>
            <option value="48">48</option>
            <option value="96">96</option>
          </select>
        </label>
        <label class="toggle">
          <input id="includeTools" type="checkbox">
          <span>Tools</span>
        </label>
        <a id="openLatest" class="primary-button open-latest disabled" href="#" aria-disabled="true">Open latest</a>
      </div>
      <label class="search">
        <span>Search</span>
        <input id="sessionSearch" type="search" autocomplete="off" placeholder="Title, path, project">
      </label>
      <div id="sessionCount" class="pane-note"></div>
      <nav id="sessionList" class="session-list" aria-label="Codex sessions"></nav>
    </section>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""


VIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Conversation</title>
  <link rel="stylesheet" href="/vendor/katex/katex.min.css">
  <link rel="stylesheet" href="/vendor/highlight-github.min.css">
  <link rel="stylesheet" href="/app.css">
</head>
<body class="view-body theme-mist" data-web-assets="bundled">
  <main class="view-shell">
    <header class="view-header">
      <div>
        <p id="sessionMeta" class="eyebrow">Codex conversation</p>
        <h1 id="sessionTitle">Loading conversation</h1>
      </div>
      <div class="view-actions">
        <button id="refreshConversation" class="icon-button" type="button" title="Refresh conversation" aria-label="Refresh conversation"><span class="refresh-icon" aria-hidden="true">↻</span></button>
        <a class="back-link" href="/">Choose another</a>
      </div>
    </header>
    <div class="conversation-info view-info"><span id="conversationInfo"></span><span id="refreshStatus" class="refresh-status" role="status" aria-live="polite"></span></div>
    <article id="conversation" class="conversation view-conversation">
      <p class="loading">Loading conversation...</p>
    </article>
    <nav id="viewStatusbar" class="view-statusbar" aria-label="Conversation navigation">
      <button id="statusbarToggle" class="statusbar-toggle" type="button" title="Show navigation" aria-label="Show navigation" aria-expanded="false" aria-controls="statusbarActions">/</button>
      <div id="statusbarActions" class="statusbar-actions">
        <button id="jumpPrevious" class="status-button icon-status-button" type="button" title="Previous block or page (Left Arrow)" aria-label="Previous block or page, Left Arrow shortcut">←</button>
        <button id="jumpNext" class="status-button icon-status-button" type="button" title="Next block or page (Right Arrow)" aria-label="Next block or page, Right Arrow shortcut">→</button>
        <button id="jumpTop" class="status-button" type="button">Up <kbd>U</kbd></button>
        <button id="jumpLatest" class="status-button" type="button">Latest <kbd>L</kbd></button>
        <button id="loadEarlier" class="status-button" type="button">Earlier <kbd>E</kbd></button>
        <button id="loadAll" class="status-button" type="button">All <kbd>A</kbd></button>
        <button id="statusRefresh" class="status-button icon-status-button" type="button" title="Refresh conversation (R); refresh and jump to latest (Shift+R)" aria-label="Refresh conversation with R; refresh and jump to latest with Shift+R"><span class="refresh-icon" aria-hidden="true">↻</span><kbd>R</kbd></button>
        <a id="typesetViewLink" class="status-button" href="/typeset">LaTeX <kbd>T</kbd></a>
        <a class="status-button" href="/">Choose <kbd>C</kbd></a>
      </div>
    </nav>
  </main>
  <script src="/vendor/purify.min.js"></script>
  <script src="/vendor/marked.min.js"></script>
  <script src="/vendor/highlight.min.js"></script>
  <script src="/vendor/katex/katex.min.js"></script>
  <script src="/vendor/katex/auto-render.min.js"></script>
  <script src="/view.js"></script>
</body>
</html>
"""


TYPESET_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex Typeset Conversation</title>
  <link rel="stylesheet" href="/vendor/katex/katex.min.css">
  <link rel="stylesheet" href="/vendor/highlight-github.min.css">
  <link rel="stylesheet" href="/app.css">
</head>
<body class="view-body theme-mist typeset-body" data-web-assets="bundled">
  <main class="view-shell typeset-shell">
    <header class="view-header">
      <div>
        <p id="sessionMeta" class="eyebrow">Codex typeset conversation</p>
        <h1 id="sessionTitle">Loading typeset view</h1>
      </div>
      <div class="view-actions">
        <button id="refreshConversation" class="icon-button" type="button" title="Refresh typeset view" aria-label="Refresh typeset view"><span class="refresh-icon" aria-hidden="true">↻</span></button>
        <a class="back-link" href="/">Choose another</a>
      </div>
    </header>
    <div class="conversation-info view-info"><span id="conversationInfo"></span><span id="refreshStatus" class="refresh-status" role="status" aria-live="polite"></span></div>
    <article id="conversation" class="conversation view-conversation typeset-conversation">
      <p class="loading">Typesetting conversation...</p>
    </article>
    <nav id="viewStatusbar" class="view-statusbar" aria-label="Typeset conversation navigation">
      <button id="statusbarToggle" class="statusbar-toggle" type="button" title="Show navigation" aria-label="Show navigation" aria-expanded="false" aria-controls="statusbarActions">/</button>
      <div id="statusbarActions" class="statusbar-actions">
        <button id="jumpPrevious" class="status-button icon-status-button" type="button" title="Previous block or page (Left Arrow)" aria-label="Previous block or page, Left Arrow shortcut">←</button>
        <button id="jumpNext" class="status-button icon-status-button" type="button" title="Next block or page (Right Arrow)" aria-label="Next block or page, Right Arrow shortcut">→</button>
        <button id="jumpTop" class="status-button" type="button">Up <kbd>U</kbd></button>
        <button id="jumpLatest" class="status-button" type="button">Latest <kbd>L</kbd></button>
        <button id="loadEarlier" class="status-button" type="button">Earlier <kbd>E</kbd></button>
        <button id="loadAll" class="status-button" type="button">All <kbd>A</kbd></button>
        <button id="statusRefresh" class="status-button icon-status-button" type="button" title="Refresh typeset view (R); refresh and jump to latest (Shift+R)" aria-label="Refresh typeset view with R; refresh and jump to latest with Shift+R"><span class="refresh-icon" aria-hidden="true">↻</span><kbd>R</kbd></button>
        <a id="normalViewLink" class="status-button" href="/view">Markdown <kbd>T</kbd></a>
        <a class="status-button" href="/">Choose <kbd>C</kbd></a>
      </div>
    </nav>
  </main>
  <script src="/vendor/purify.min.js"></script>
  <script src="/vendor/marked.min.js"></script>
  <script src="/vendor/highlight.min.js"></script>
  <script src="/vendor/katex/katex.min.js"></script>
  <script src="/vendor/katex/auto-render.min.js"></script>
  <script src="/typeset.js"></script>
</body>
</html>
"""


APP_CSS = r"""
/* Theme variables. Add another body.theme-* block to create a full viewer theme. */
:root {
  color-scheme: light;
  --syntax-number: #267f8d;
  --syntax-string: #ad4a76;
  --syntax-path-a: #32759d;
  --syntax-path-b: #7562a9;
  --syntax-path-separator: #9b8f82;
  --font-ui: Inter, Aptos, "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  --font-prose: "Iowan Old Style", Charter, "Source Serif 4", "Sitka Text", Georgia, Cambria, "Times New Roman", serif;
  --font-mono: "JetBrains Mono", "Berkeley Mono", "Cascadia Code", "Roboto Mono", "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
}

body.theme-warm {
  --bg: #f4efe7;
  --paper: #fffdf8;
  --paper-warm: #fbf6ed;
  --surface: rgba(255, 253, 248, 0.88);
  --surface-muted: rgba(255, 253, 248, 0.78);
  --toolbar-bg: rgba(255, 253, 248, 0.72);
  --header-bg: rgba(255, 253, 248, 0.82);
  --info-bg: rgba(255, 253, 248, 0.62);
  --status-bg: rgba(255, 253, 248, 0.88);
  --ink: #2b2926;
  --muted: #746d63;
  --faint: #a79d90;
  --line: #ded4c5;
  --line-strong: #cbbda9;
  --panel-line: rgba(203, 189, 169, 0.84);
  --accent: #8c4b35;
  --accent-dark: #683627;
  --accent-soft: #f0ded4;
  --assistant: #fffaf0;
  --user: #2f2a25;
  --user-line: #4d443b;
  --user-text: #fff7ec;
  --user-muted: #d8c8b5;
  --tool: #f2f4ef;
  --code: #f6f1e8;
  --shadow: 0 18px 50px rgba(78, 56, 32, 0.12);
  --status-shadow: 0 12px 34px rgba(78, 56, 32, 0.13);
  --body-glow: radial-gradient(circle at 18% 0%, rgba(255, 253, 248, 0.92), rgba(255, 253, 248, 0) 28%);
  --body-gradient: linear-gradient(135deg, #f7f1e8 0%, #eee5d8 52%, #f6f0e7 100%);
}

body.theme-mist,
body:not([class*="theme-"]) {
  --bg: #f1f0ea;
  --paper: #fffdf7;
  --paper-warm: #fbf7ee;
  --surface: rgba(255, 253, 247, 0.9);
  --surface-muted: rgba(255, 253, 247, 0.78);
  --toolbar-bg: rgba(255, 253, 247, 0.74);
  --header-bg: rgba(255, 253, 247, 0.86);
  --info-bg: rgba(251, 247, 238, 0.68);
  --status-bg: rgba(255, 253, 247, 0.9);
  --ink: #282b27;
  --muted: #697067;
  --faint: #98a096;
  --line: #d7d7ca;
  --line-strong: #c4c7b7;
  --panel-line: rgba(196, 199, 183, 0.86);
  --accent: #6d7046;
  --accent-dark: #4c5531;
  --accent-soft: #e8ead7;
  --assistant: #fffaf0;
  --user: #293026;
  --user-line: #4c563d;
  --user-text: #fbf8ee;
  --user-muted: #d4d9ca;
  --tool: #eef2ee;
  --code: #f5f2e9;
  --shadow: 0 18px 50px rgba(54, 64, 45, 0.12);
  --status-shadow: 0 12px 34px rgba(54, 64, 45, 0.13);
  --body-glow: radial-gradient(circle at 18% 0%, rgba(255, 253, 247, 0.94), rgba(255, 253, 247, 0) 28%);
  --body-gradient: linear-gradient(135deg, #f8f3eb 0%, #e7eadf 54%, #f6f2ea 100%);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background:
    var(--body-glow),
    var(--body-gradient);
  color: var(--ink);
  font-family: var(--font-ui);
  line-height: 1.55;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

button,
input,
select {
  font: inherit;
}

button {
  cursor: pointer;
}

.app-shell {
  display: grid;
  grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
  gap: 22px;
  height: 100vh;
  padding: 18px;
}

.chooser-shell {
  display: block;
  height: auto;
  min-height: 100vh;
  margin: 0 auto;
  max-width: 880px;
}

.session-pane,
.reader-pane {
  min-height: 0;
  background: var(--surface);
  border: 1px solid var(--panel-line);
  box-shadow: var(--shadow);
  backdrop-filter: blur(18px);
}

.session-pane {
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  overflow: hidden;
}

.chooser-pane {
  min-height: calc(100vh - 36px);
}

.reader-pane {
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  overflow: hidden;
}

.brand,
.reader-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  border-bottom: 1px solid var(--line);
}

.brand {
  padding: 20px 18px 16px;
}

.reader-toolbar {
  padding: 18px 22px;
  background: var(--toolbar-bg);
}

.eyebrow {
  margin: 0 0 4px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 760;
  letter-spacing: 0;
  text-transform: uppercase;
}

h1,
h2 {
  margin: 0;
  letter-spacing: 0;
}

h1 {
  font-size: 21px;
  font-weight: 760;
}

h2 {
  max-width: 760px;
  font-family: var(--font-prose);
  font-size: 28px;
  font-weight: 680;
  line-height: 1.18;
}

.icon-button,
.primary-button,
.session-item {
  border: 1px solid var(--line-strong);
  border-radius: 8px;
}

.icon-button {
  display: inline-grid;
  width: 36px;
  height: 36px;
  place-items: center;
  flex: 0 0 auto;
  background: var(--paper);
  color: var(--accent-dark);
  font-size: 19px;
}

.primary-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 0 14px;
  border-radius: 8px;
  background: var(--accent);
  color: #fffaf5;
  font-weight: 700;
  text-decoration: none;
}

.primary-button.disabled {
  pointer-events: none;
  opacity: 0.48;
}

.search {
  display: grid;
  gap: 7px;
  padding: 15px 18px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}

.chooser-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
}

.search input,
.select-label select {
  min-height: 36px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--paper);
  color: var(--ink);
}

.search input {
  width: 100%;
  padding: 0 11px;
}

.pane-note {
  min-height: 30px;
  padding: 8px 18px 0;
  color: var(--muted);
  font-size: 12px;
}

.session-list {
  display: grid;
  gap: 8px;
  min-height: 0;
  overflow: auto;
  padding: 12px;
}

.session-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px;
  width: 100%;
  padding: 12px;
  align-items: center;
  text-align: left;
  text-decoration: none;
  background: var(--surface-muted);
  color: var(--ink);
}

.session-item:hover,
.session-item.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.session-title {
  overflow: hidden;
  font-size: 14px;
  font-weight: 720;
  line-height: 1.3;
  text-overflow: ellipsis;
}

.session-subtitle {
  overflow: hidden;
  color: var(--muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-copy {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.open-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  color: var(--accent-dark);
  font-size: 12px;
  font-weight: 760;
  background: var(--toolbar-bg);
}

.toolbar-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
}

.select-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 13px;
}

.select-label select {
  padding: 0 8px;
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 36px;
  padding: 0 10px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--paper);
  color: var(--muted);
  font-size: 13px;
}

.conversation-info {
  min-height: 38px;
  padding: 9px 28px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}

.refresh-status:not(:empty)::before {
  content: " · ";
}

.refresh-status {
  color: var(--accent-dark);
  font-weight: 700;
}

.is-refreshing .refresh-icon {
  display: inline-block;
  animation: refresh-spin 800ms linear infinite;
}

@keyframes refresh-spin {
  to {
    transform: rotate(360deg);
  }
}

.view-body {
  background:
    var(--body-glow),
    var(--body-gradient);
}

.view-shell {
  min-height: 100vh;
}

.view-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 24px clamp(20px, 4vw, 56px) 18px;
  border-bottom: 1px solid var(--line);
  background: var(--header-bg);
}

.view-actions {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 12px;
}

.view-header h1 {
  max-width: 940px;
  font-family: var(--font-prose);
  font-size: 32px;
  font-weight: 650;
  line-height: 1.14;
}

.back-link {
  flex: 0 0 auto;
  margin-top: 3px;
  color: var(--accent-dark);
  font-size: 13px;
  font-weight: 700;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}

.view-info {
  padding-inline: clamp(20px, 4vw, 56px);
  background: var(--info-bg);
}

.view-conversation {
  min-height: calc(100vh - 130px);
}

.conversation {
  flex: 1;
  min-height: 0;
  overflow: auto;
  overflow-anchor: none;
  padding: 30px clamp(20px, 4vw, 56px) 24px;
  scroll-behavior: smooth;
}

.conversation.statusbar-expanded {
  padding-bottom: 72px;
}

.view-statusbar {
  position: fixed;
  right: clamp(14px, 3vw, 34px);
  bottom: 14px;
  z-index: 10;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--panel-line);
  border-radius: 8px;
  background: var(--status-bg);
  box-shadow: var(--status-shadow);
  padding: 5px;
  backdrop-filter: blur(12px);
}

.statusbar-actions {
  display: none;
  align-items: center;
  gap: 5px;
}

.view-statusbar.is-expanded .statusbar-actions {
  display: inline-flex;
}

.statusbar-toggle {
  display: inline-grid;
  width: 30px;
  height: 30px;
  place-items: center;
  flex: 0 0 auto;
  border: 0;
  border-radius: 6px;
  color: var(--accent-dark);
  background: transparent;
  font-family: var(--font-mono);
  font-size: 16px;
  font-weight: 760;
  line-height: 1;
}

.statusbar-toggle:hover,
.statusbar-toggle:focus-visible,
.view-statusbar.is-expanded .statusbar-toggle {
  background: var(--accent-soft);
  outline: none;
}

.statusbar-toggle.is-refreshing,
.statusbar-toggle.is-refreshing:disabled {
  color: var(--accent-dark);
  background: var(--accent-soft);
  opacity: 1;
  cursor: wait;
}

.status-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 40px;
  height: 30px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--accent-dark);
  font-size: 12px;
  font-weight: 760;
  line-height: 1;
  text-decoration: none;
}

.status-button kbd {
  min-width: 15px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  padding: 1px 3px;
  color: var(--muted);
  background: var(--surface);
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 700;
  line-height: 1.1;
}

.status-button:hover,
.status-button:focus-visible {
  background: var(--accent-soft);
  outline: none;
}

.status-button:disabled {
  cursor: default;
  opacity: 0.42;
}

.status-button:disabled:hover {
  background: transparent;
}

.icon-status-button {
  min-width: 30px;
  font-size: 15px;
}

.conversation.empty {
  display: grid;
  place-items: center;
  color: var(--muted);
  font-family: var(--font-prose);
  font-size: 22px;
}

.message {
  position: relative;
  max-width: 880px;
  margin: 0 auto 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--assistant);
  overflow: hidden;
  scroll-margin-top: 18px;
}

.message.is-navigation-current::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  z-index: 4;
  width: 3px;
  border-radius: 7px 0 0 7px;
  background: var(--faint);
  pointer-events: none;
}

.message.user {
  max-width: 760px;
  margin-bottom: 16px;
  border-color: var(--user-line);
  background: var(--user);
  color: var(--user-text);
  box-shadow: 0 12px 32px rgba(47, 42, 37, 0.12);
}

.message.tool,
.message.function_call,
.message.function_call_output,
.message.local_shell_call {
  max-width: 760px;
  background: var(--tool);
}

.typeset-conversation {
  padding-top: 24px;
}

.typeset-message {
  max-width: min(900px, 100%);
  background: transparent;
  border: 0;
  overflow: visible;
}

.typeset-message.is-navigation-current::before {
  content: none;
}

.typeset-message.is-navigation-current .typeset-pdf-page,
.typeset-message.is-navigation-current .typeset-fallback {
  border-left-color: var(--faint);
}

.typeset-message.is-navigation-current .typeset-pdf-page::before,
.typeset-message.is-navigation-current .typeset-fallback::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  z-index: 4;
  width: 2px;
  border-radius: 0 0 0 7px;
  background: var(--faint);
  pointer-events: none;
}

.typeset-message .message-header {
  margin: 0 auto 8px;
  max-width: 760px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--toolbar-bg);
}

.typeset-external-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  width: min(760px, 100%);
  margin: 0 auto;
  padding: 7px 10px;
  border: 1px solid var(--panel-line);
  border-bottom: 0;
  border-left: 3px solid var(--accent);
  border-radius: 8px 8px 0 0;
  color: var(--muted);
  background: var(--toolbar-bg);
  font-family: var(--font-ui);
  font-size: 11px;
}

.typeset-external-header + .typeset-pdf-page {
  border-radius: 0 0 8px 8px;
}

.typeset-pdf-page.has-resources {
  border-bottom: 0;
  border-radius: 8px 8px 0 0;
}

.typeset-fallback.has-resources {
  border-bottom: 0;
  border-radius: 8px 8px 0 0;
}

.typeset-external-header + .typeset-pdf-page.has-resources {
  border-radius: 0;
}

.typeset-external-header + .typeset-fallback.has-resources {
  border-radius: 0;
}

.typeset-attachments {
  display: grid;
  gap: 6px;
  width: min(760px, 100%);
  margin: 0 auto;
  padding: 8px 11px;
  border: 1px solid var(--panel-line);
  border-top: 0;
  border-radius: 0 0 8px 8px;
  background: var(--toolbar-bg);
  color: var(--muted);
  font-family: var(--font-ui);
  font-size: 11px;
}

.typeset-resource-group {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.typeset-resource-group strong {
  flex: 0 0 auto;
  min-width: 68px;
  color: var(--accent-dark);
}

.typeset-attachment-list {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.typeset-attachments a {
  position: relative;
  min-width: 0;
  overflow-wrap: anywhere;
  color: #8b570b;
  font-weight: 750;
  text-underline-offset: 2px;
}

.typeset-attachments a[data-link-hint] {
  border-radius: 3px;
  background: var(--accent-soft);
  box-shadow: 0 0 0 2px var(--accent-soft);
}

.typeset-attachments a[data-link-hint]::before {
  content: attr(data-link-hint);
  position: absolute;
  top: 50%;
  left: -9px;
  z-index: 2;
  display: inline-grid;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  transform: translate(-100%, -50%);
  border: 1px solid #a66d16;
  border-bottom-width: 2px;
  border-radius: 4px;
  background: #fff8e8;
  box-shadow: 0 2px 3px rgb(61 38 6 / 22%), inset 0 1px rgb(255 255 255 / 85%);
  color: var(--accent-dark);
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 800;
  line-height: 1;
  pointer-events: none;
  text-decoration: none;
}

.typeset-attachment-kind {
  margin-left: 4px;
  color: var(--muted);
  font-size: 9px;
  font-weight: 650;
  letter-spacing: 0.04em;
}

.typeset-message.assistant-continuing {
  margin-bottom: 0;
}

.typeset-message.assistant-continued .typeset-external-header {
  border-top: 0;
  border-left: 1px solid var(--panel-line);
  border-radius: 0;
}

.typeset-message.assistant-continuing .typeset-pdf-page,
.typeset-message.assistant-continuing .typeset-fallback {
  border-bottom: 0;
  border-radius: 0;
  box-shadow: none;
}

.typeset-external-header + .typeset-fallback {
  margin-top: 0;
  border-radius: 0 0 8px 8px;
}

.typeset-external-header strong {
  color: var(--accent-dark);
  font-weight: 780;
}

.typeset-external-header-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}

.typeset-external-header a {
  margin-left: 4px;
  color: #8b570b;
  font-weight: 750;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
}

.typeset-copy-markdown {
  display: inline-grid;
  width: 22px;
  height: 22px;
  padding: 0;
  place-items: center;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  font-family: var(--font-ui);
  font-size: 15px;
  line-height: 1;
}

.typeset-copy-markdown:hover,
.typeset-copy-markdown:focus-visible {
  border-color: var(--line-strong);
  background: var(--paper);
  color: var(--accent-dark);
}

.typeset-copy-markdown.is-copied {
  color: var(--accent-dark);
}

.typeset-copy-markdown.is-error {
  color: #9b352d;
}

.typeset-debug-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: min(760px, 100%);
  min-height: 30px;
  margin: 0 auto 6px;
  padding: 4px 9px;
  border-left: 3px solid #a66d16;
  color: #714708;
  background: #fff4d8;
  font-family: var(--font-ui);
  font-size: 11px;
  font-weight: 750;
}

.typeset-debug-bar a {
  color: #714708;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
}

.typeset-answer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: min(760px, 100%);
  min-height: 30px;
  margin: 0 auto 6px;
  padding: 4px 9px;
  border-left: 3px solid var(--accent);
  color: var(--muted);
  background: var(--toolbar-bg);
  font-family: var(--font-ui);
  font-size: 11px;
  font-weight: 750;
}

.typeset-answer-bar a {
  color: var(--accent-dark);
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
}

.typeset-debug-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  min-height: 180px;
  color: var(--muted);
  font-family: var(--font-ui);
  font-size: 13px;
}

.typeset-debug-loading strong,
.typeset-debug-loading span {
  display: block;
}

.typeset-debug-loading strong {
  margin-bottom: 3px;
  color: var(--ink);
}

.typeset-debug-spinner {
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  border: 2px solid var(--line-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: typeset-debug-spin 0.75s linear infinite;
}

@keyframes typeset-debug-spin {
  to { transform: rotate(360deg); }
}

.typeset-pdf-page {
  position: relative;
  display: block;
  width: min(760px, 100%);
  height: auto;
  margin: 0 auto;
  border: 1px solid var(--panel-line);
  border-radius: 8px;
  background: var(--paper);
  box-shadow: var(--shadow);
  overflow: hidden;
}

.typeset-pdf-page canvas {
  display: block;
  width: 100%;
  height: auto;
  background: var(--paper);
}

.typeset-pdf-page .textLayer {
  position: absolute;
  inset: 0;
  overflow: clip;
  line-height: 1;
  text-align: initial;
  transform-origin: 0 0;
  -webkit-text-size-adjust: none;
  text-size-adjust: none;
  forced-color-adjust: none;
  z-index: 1;
}

.typeset-pdf-page .code-copy-layer {
  position: absolute;
  inset: 0;
  z-index: 2;
  pointer-events: none;
}

.typeset-code-copy {
  position: absolute;
  display: grid;
  min-width: 22px;
  min-height: 22px;
  padding: 0;
  place-items: center;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: color-mix(in srgb, var(--paper) 94%, transparent);
  box-shadow: 0 1px 3px rgb(30 32 28 / 16%);
  color: var(--muted);
  font-family: var(--font-ui);
  font-size: 15px;
  line-height: 1;
  pointer-events: auto;
  cursor: pointer;
}

.typeset-code-copy:hover,
.typeset-code-copy:focus-visible,
.typeset-code-copy.is-copied {
  border-color: var(--accent);
  color: var(--accent-dark);
}

.typeset-code-copy.is-error {
  border-color: #9b352d;
  color: #9b352d;
}

.typeset-pdf-page .textLayer :is(span, br) {
  position: absolute;
  color: transparent;
  white-space: pre;
  cursor: text;
  transform-origin: 0 0;
}

.typeset-pdf-page .textLayer > :not(.markedContent),
.typeset-pdf-page .textLayer .markedContent span:not(.markedContent) {
  z-index: 1;
}

.typeset-pdf-page .textLayer span.markedContent {
  top: 0;
  height: 0;
}

.typeset-pdf-page .textLayer span[role="img"] {
  user-select: none;
  cursor: default;
}

.typeset-pdf-page .textLayer ::selection {
  background: color-mix(in srgb, var(--accent) 30%, transparent);
}

.typeset-pdf-page .textLayer br::selection {
  background: transparent;
}

.typeset-pdf-page .textLayer .endOfContent {
  display: block;
  position: absolute;
  inset: 100% 0 0;
  cursor: default;
  user-select: none;
  z-index: 0;
}

.typeset-pdf-page .textLayer.selecting .endOfContent {
  top: 0;
}

.typeset-pdf-page.is-loading,
.typeset-pdf-page.is-error {
  display: grid;
  min-height: 120px;
  place-items: center;
  color: var(--muted);
  font-family: var(--font-ui);
  font-size: 13px;
  font-weight: 700;
}

.typeset-pdf-page.is-error a {
  color: var(--accent-dark);
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}

.typeset-fallback {
  position: relative;
  max-width: 760px;
  margin: 0 auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--assistant);
  overflow: hidden;
}

.typeset-error {
  margin: 0;
  padding: 10px 18px;
  border-bottom: 1px solid var(--line);
  color: var(--accent-dark);
  background: var(--accent-soft);
  font-size: 12px;
  font-weight: 720;
}

.message.collapsed-user {
  overflow: visible;
}

.message.collapsed-user .message-header {
  display: none;
}

.user-details {
  border-radius: 8px;
}

.user-details summary {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 14px;
  min-height: 48px;
  padding: 13px 16px;
  list-style: none;
  cursor: pointer;
  color: var(--user-muted);
}

.user-details summary::-webkit-details-marker {
  display: none;
}

.user-details summary::before {
  content: "+";
  display: inline-grid;
  width: 22px;
  height: 22px;
  place-items: center;
  flex: 0 0 auto;
  border: 1px solid rgba(255, 247, 236, 0.34);
  border-radius: 999px;
  color: var(--user-text);
  font-size: 15px;
  line-height: 1;
}

.user-details[open] summary {
  border-bottom: 1px solid var(--user-line);
}

.user-details[open] summary::before {
  content: "-";
}

.user-summary-main {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  color: var(--user-text);
  font-size: 13px;
  font-weight: 760;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-summary-meta {
  flex: 0 0 auto;
  color: var(--user-muted);
  font-size: 12px;
}

.message.user .message-body {
  --syntax-number: #7fd9d4;
  --syntax-string: #ff9dbb;
  --syntax-path-a: #92cfff;
  --syntax-path-b: #d0b2ff;
  --syntax-path-separator: #d8c8b5;
  color: var(--user-text);
  font-family: var(--font-ui);
  font-size: 14px;
  line-height: 1.62;
}

.message.user .message-body code {
  background: rgba(255, 247, 236, 0.13);
  color: var(--user-text);
}

.message.user .message-body pre {
  border-color: var(--user-line);
  background: rgba(0, 0, 0, 0.18);
}

.message.user .message-body a {
  color: #ffe0c5;
}

.message-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 16px;
  border-bottom: 1px solid rgba(222, 212, 197, 0.72);
  color: var(--muted);
  font-size: 11px;
}

.message-header-actions {
  display: inline-flex;
  align-items: baseline;
  gap: 10px;
  flex: 0 0 auto;
}

.typeset-answer-link {
  color: var(--accent-dark);
  font-weight: 750;
  text-decoration-thickness: 1px;
  text-underline-offset: 2px;
}

.role {
  color: var(--accent-dark);
  font-weight: 780;
  text-transform: capitalize;
}

.message-body {
  padding: 20px 22px 23px;
  font-family: var(--font-prose);
  font-size: 18.5px;
  line-height: 1.74;
}

.message-body > :first-child {
  margin-top: 0;
}

.message-body > :last-child {
  margin-bottom: 0;
}

.message-body h1,
.message-body h2,
.message-body h3 {
  margin: 1.2em 0 0.45em;
  font-family: var(--font-ui);
  line-height: 1.24;
}

.message-body h1 {
  font-size: 28px;
}

.message-body h2 {
  font-size: 23px;
}

.message-body h3 {
  font-size: 19px;
}

.message-body p,
.message-body ul,
.message-body ol,
.message-body blockquote {
  margin: 0.95em 0;
}

.message-body li {
  margin: 0.28em 0;
}

.message-body a {
  color: var(--accent-dark);
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}

.message-body blockquote {
  margin-left: 0;
  padding: 0.08em 0 0.08em 1em;
  border-left: 3px solid var(--line-strong);
  color: #5e574f;
}

.message-body code {
  border-radius: 5px;
  background: var(--code);
  padding: 0.12em 0.28em;
  font-family: var(--font-mono);
  font-size: 0.82em;
}

.message-body pre {
  overflow: auto;
  margin: 1.05em 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fbf8f1;
  padding: 15px 16px;
  font-family: var(--font-mono);
  font-size: 13.5px;
  line-height: 1.5;
}

.message-body pre code {
  background: transparent;
  padding: 0;
  font-size: inherit;
}

.message-body pre,
.message-body pre code,
.message-body code {
  --syntax-number: #267f8d;
  --syntax-string: #a94170;
  --syntax-path-a: #286f98;
  --syntax-path-b: #705ca5;
  --syntax-path-separator: #8f8478;
}

.semantic-number,
.semantic-string,
.semantic-path-part {
  font-weight: 650;
}

.semantic-number {
  color: var(--syntax-number);
}

.semantic-string {
  color: var(--syntax-string);
}

.semantic-path-part.alt-a {
  color: var(--syntax-path-a);
}

.semantic-path-part.alt-b {
  color: var(--syntax-path-b);
}

.semantic-path-separator {
  color: var(--syntax-path-separator);
  font-weight: 500;
}

.message-body table {
  display: block;
  overflow-x: auto;
  width: 100%;
  border-collapse: collapse;
  font-size: 15px;
}

.message-body th,
.message-body td {
  border: 1px solid var(--line);
  padding: 7px 9px;
}

.loading,
.error {
  max-width: 760px;
  margin: 40px auto;
  color: var(--muted);
  font-family: var(--font-prose);
  font-size: 21px;
}

.error {
  color: #8d2f25;
}

@media (max-width: 860px) {
  .app-shell {
    grid-template-columns: 1fr;
    height: auto;
    min-height: 100vh;
  }

  .session-pane {
    max-height: 42vh;
  }

  .chooser-pane {
    max-height: none;
  }

  .reader-pane {
    min-height: 58vh;
  }

  .reader-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .toolbar-controls {
    justify-content: flex-start;
  }

  h2 {
    font-size: 23px;
  }

  .message-body {
    font-size: 17px;
  }
}

@media (max-width: 600px) {
  .view-header {
    align-items: stretch;
    flex-direction: column;
    gap: 12px;
  }

  .view-actions {
    justify-content: space-between;
  }

  .view-header h1 {
    font-size: 28px;
  }

  .view-statusbar.is-expanded {
    left: 14px;
  }

  .view-statusbar.is-expanded .statusbar-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
    max-width: calc(100vw - 76px);
  }

  .conversation.statusbar-expanded {
    padding-bottom: 110px;
  }
}
"""


APP_JS = r"""
const state = {
  sessions: [],
  defaultView: document.body.dataset.defaultView === "latex" ? "latex" : "markdown",
};

const els = {
  sessionList: document.getElementById("sessionList"),
  sessionCount: document.getElementById("sessionCount"),
  search: document.getElementById("sessionSearch"),
  refreshSessions: document.getElementById("refreshSessions"),
  openLatest: document.getElementById("openLatest"),
  tailCount: document.getElementById("tailCount"),
  includeTools: document.getElementById("includeTools"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function sessionMeta(item) {
  const bits = [];
  if (item.lastLabel) bits.push(item.lastLabel);
  if (item.rolloutCount > 1) bits.push(`${item.rolloutCount} rollouts`);
  if (item.recordCount) bits.push(`${item.recordCount} messages`);
  return bits.join(" · ");
}

function conversationId(item) {
  return item.id || item.rolloutId || item.sessionId || item.threadId || "";
}

function viewHref(item) {
  const id = conversationId(item);
  const params = new URLSearchParams({
    tail: els.tailCount.value,
  });
  if (els.includeTools.checked) params.set("tools", "1");
  if (id) {
    const route = state.defaultView === "latex" ? "t" : "v";
    return `/${route}/${encodeURIComponent(id)}?${params}`;
  }
  params.set("path", item.path);
  const route = state.defaultView === "latex" ? "typeset" : "view";
  return `/${route}?${params}`;
}

function renderSessions() {
  els.sessionList.innerHTML = "";
  if (state.sessions.length) {
    els.openLatest.href = viewHref(state.sessions[0]);
    els.openLatest.classList.remove("disabled");
    els.openLatest.setAttribute("aria-disabled", "false");
  } else {
    els.openLatest.href = "#";
    els.openLatest.classList.add("disabled");
    els.openLatest.setAttribute("aria-disabled", "true");
  }
  for (const item of state.sessions) {
    const link = document.createElement("a");
    link.className = "session-item";
    link.href = viewHref(item);
    link.innerHTML = `
      <span class="session-copy">
        <span class="session-title">${escapeHtml(item.title)}</span>
        <span class="session-subtitle">${escapeHtml(sessionMeta(item))}</span>
        <span class="session-subtitle">${escapeHtml(item.cwd || item.path)}</span>
      </span>
      <span class="open-pill">Open view</span>
    `;
    els.sessionList.appendChild(link);
  }
}

async function loadSessions() {
  const params = new URLSearchParams({
    limit: "160",
    q: els.search.value.trim(),
  });
  els.sessionCount.textContent = "Loading sessions...";
  const response = await fetch(`/api/sessions?${params}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not load sessions");
  state.sessions = data.sessions;
  els.sessionCount.textContent = `${data.total} conversation${data.total === 1 ? "" : "s"}`;
  renderSessions();
}

let searchTimer = 0;
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadSessions().catch((error) => {
    els.sessionCount.textContent = error.message;
  }), 180);
});

els.refreshSessions.addEventListener("click", () => loadSessions().catch((error) => {
  els.sessionCount.textContent = error.message;
}));
els.tailCount.addEventListener("change", renderSessions);
els.includeTools.addEventListener("change", renderSessions);

loadSessions().catch((error) => {
  els.sessionCount.textContent = error.message;
});
"""


VIEW_JS = r"""
const semanticHighlightConfig = {
  enabled: true,
  selectors: ".message-body, #sessionMeta",
  skipSelector: [
    "code",
    "kbd",
    "samp",
    "script",
    "style",
    ".katex",
    ".katex-display",
    ".semantic-number",
    ".semantic-string",
    ".semantic-path-part",
    ".semantic-path-separator",
  ].join(", "),
  tokenPattern: /"(?:\\.|[^"\\])*"|(?<![A-Za-z0-9_])'(?:\\.|[^'\\])*'(?![A-Za-z0-9_])|“[^”]*”|‘[^’]*’|\/[A-Za-z0-9._~@:+-]+(?:[\/_-]+[A-Za-z0-9._~@:+-]+)*|[A-Za-z0-9._~@+-]*[A-Za-z][A-Za-z0-9._~@+-]*\.[A-Za-z][A-Za-z0-9]{0,9}|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?/g,
  numberPattern: /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?$/,
  extensionPattern: /(?:^|[^.])\.[A-Za-z][A-Za-z0-9]{0,9}$/,
};

const params = new URLSearchParams(location.search);
let loadingConversation = false;
let currentTail = Number.parseInt(params.get("tail") || "24", 10) || 24;
let showAllRecords = params.get("all") === "1";
let lastConversationData = null;
let messageNavigationIndex = -1;
let refreshStatusTimer = 0;
const els = {
  conversation: document.getElementById("conversation"),
  conversationInfo: document.getElementById("conversationInfo"),
  jumpLatest: document.getElementById("jumpLatest"),
  jumpNext: document.getElementById("jumpNext"),
  jumpPrevious: document.getElementById("jumpPrevious"),
  jumpTop: document.getElementById("jumpTop"),
  loadAll: document.getElementById("loadAll"),
  loadEarlier: document.getElementById("loadEarlier"),
  refreshConversation: document.getElementById("refreshConversation"),
  refreshStatus: document.getElementById("refreshStatus"),
  statusRefresh: document.getElementById("statusRefresh"),
  statusbar: document.getElementById("viewStatusbar"),
  statusbarToggle: document.getElementById("statusbarToggle"),
  sessionTitle: document.getElementById("sessionTitle"),
  sessionMeta: document.getElementById("sessionMeta"),
  typesetViewLink: document.getElementById("typesetViewLink"),
};

function setStatusbarRefreshState(refreshing) {
  if (!els.statusbarToggle) return;
  els.statusbarToggle.disabled = refreshing;
  els.statusbarToggle.classList.toggle("is-refreshing", refreshing);
  els.statusbarToggle.toggleAttribute("aria-busy", refreshing);
  if (refreshing) {
    els.statusbarToggle.innerHTML = '<span class="refresh-icon" aria-hidden="true">↻</span>';
    els.statusbarToggle.title = "Refreshing conversation...";
    els.statusbarToggle.setAttribute("aria-label", "Refreshing conversation");
    return;
  }
  els.statusbarToggle.textContent = "/";
  const expanded = els.statusbar?.classList.contains("is-expanded") || false;
  const label = expanded ? "Hide navigation" : "Show navigation";
  els.statusbarToggle.title = label;
  els.statusbarToggle.setAttribute("aria-label", label);
}

function setRefreshState(refreshing) {
  for (const button of [els.refreshConversation, els.statusRefresh]) {
    if (!button) continue;
    if (!button.dataset.idleTitle) button.dataset.idleTitle = button.title;
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.getAttribute("aria-label") || button.title;
    button.disabled = refreshing;
    button.classList.toggle("is-refreshing", refreshing);
    button.title = refreshing ? "Refreshing conversation..." : button.dataset.idleTitle;
    button.setAttribute(
      "aria-label",
      refreshing ? "Refreshing conversation" : button.dataset.idleLabel,
    );
  }
  els.conversation?.toggleAttribute("aria-busy", refreshing);
  setStatusbarRefreshState(refreshing);
  if (refreshing && els.refreshStatus) {
    window.clearTimeout(refreshStatusTimer);
    els.refreshStatus.textContent = "Refreshing...";
  }
}

function showRefreshResult(previousTotal, failed = false) {
  if (!els.refreshStatus) return;
  const added = Math.max(0, Number(lastConversationData?.totalCount || 0) - previousTotal);
  els.refreshStatus.textContent = failed
    ? "Refresh failed"
    : added
      ? `${added} new message${added === 1 ? "" : "s"}`
      : "Up to date";
  window.clearTimeout(refreshStatusTimer);
  refreshStatusTimer = window.setTimeout(() => {
    els.refreshStatus.textContent = "";
  }, 1800);
}

function sameConversationRecord(record, candidate) {
  return record.line_no === candidate?.line_no
    && record.role === candidate?.role
    && record.timestamp === candidate?.timestamp
    && record.text === candidate?.text;
}

function sameConversationContent(previous, next) {
  if (!previous || previous.totalCount !== next.totalCount) return false;
  if (previous.records.length !== next.records.length) return false;
  return previous.records.every((record, index) =>
    sameConversationRecord(record, next.records[index]));
}

function extendsConversationContent(previous, next) {
  if (!previous || previous.records.length > next.records.length) return false;
  return previous.records.every((record, index) =>
    sameConversationRecord(record, next.records[index]));
}

function setStatusbarExpanded(expanded) {
  if (!els.statusbar || !els.statusbarToggle) return;
  els.statusbar.classList.toggle("is-expanded", expanded);
  els.conversation?.classList.toggle("statusbar-expanded", expanded);
  els.statusbarToggle.setAttribute("aria-expanded", String(expanded));
  const label = expanded ? "Hide navigation" : "Show navigation";
  els.statusbarToggle.title = label;
  els.statusbarToggle.setAttribute("aria-label", label);
}

function toggleStatusbar() {
  if (els.statusbarToggle?.disabled) return;
  setStatusbarExpanded(!els.statusbar?.classList.contains("is-expanded"));
}

function routeId(prefix) {
  const marker = `/${prefix}/`;
  if (!location.pathname.startsWith(marker)) return "";
  return decodeURIComponent(location.pathname.slice(marker.length));
}

function identityParams(prefix = "v") {
  const identity = new URLSearchParams();
  const id = routeId(prefix) || params.get("id") || "";
  const path = params.get("path") || "";
  if (id) identity.set("id", id);
  else if (path) identity.set("path", path);
  return identity;
}

function focusedTypesetUrl(record) {
  const identity = identityParams("v");
  if (identity.has("id")) {
    return `/t/${encodeURIComponent(identity.get("id"))}/${encodeURIComponent(record.line_no)}`;
  }
  identity.set("line", String(record.line_no));
  return `/typeset?${identity}`;
}

function normalizeLanguage(lang) {
  const key = String(lang || "").trim().toLowerCase().replace(/^language-/, "");
  const aliases = {
    cplusplus: "cpp",
    dockerfile: "dockerfile",
    js: "javascript",
    jsx: "javascript",
    py: "python",
    rb: "ruby",
    sh: "bash",
    shell: "bash",
    ts: "typescript",
    tsx: "typescript",
    yml: "yaml",
  };
  return aliases[key] || key;
}

function looksLikeJson(code) {
  const text = code.trim();
  if (!/^[\[{]/.test(text)) return false;
  try {
    JSON.parse(text);
    return true;
  } catch {
    return false;
  }
}

function detectCodeLanguage(code) {
  const text = String(code || "").trim();
  if (!text) return "";
  if (/^(diff --git|@@\s+-\d|---\s+|\+\+\+\s+)/m.test(text)) return "diff";
  if (looksLikeJson(text)) return "json";
  if (/^Traceback \(most recent call last\):/m.test(text)) return "python";
  if (/^\s*(from\s+\S+\s+import\s+|import\s+\S+|def\s+\w+\(|class\s+\w+.*:)/m.test(text)) return "python";
  if (/\b(Console|System)\.(WriteLine|out\.println)\b|^\s*(public|private|protected)\s+(class|static)\b/m.test(text)) return "java";
  if (/^\s*#include\s+<|^\s*(int|void|char|float|double)\s+\w+\s*\(/m.test(text)) return "cpp";
  if (/^\s*(const|let|var)\s+\w+\s*=|=>|function\s+\w*\s*\(|console\.log\(/m.test(text)) return "javascript";
  if (/\b(interface|type)\s+\w+\s*[={]|:\s*(string|number|boolean)\b/.test(text)) return "typescript";
  if (/^\s*(SELECT|WITH|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b/im.test(text)) return "sql";
  if (/^\s*<([A-Za-z][\w:-]*)(\s|>|\/>)/.test(text)) return "xml";
  if (/^\s*([A-Za-z_][\w.-]*:\s*($|[\[{>|'"]|\S)|-\s+[A-Za-z_][\w.-]*:)/m.test(text)) return "yaml";
  if (/^\s*(\$ )?(sudo\s+|cd\s+|ls\s+|rg\s+|grep\s+|find\s+|cat\s+|python\d?\s+|npm\s+|git\s+|curl\s+)/m.test(text)) return "bash";
  return "";
}

function highlightCode(code, lang) {
  if (!window.hljs) return escapeHtml(code);
  const explicit = normalizeLanguage(lang);
  if (explicit && hljs.getLanguage(explicit)) {
    return hljs.highlight(code, { language: explicit }).value;
  }
  const detected = detectCodeLanguage(code);
  if (detected && hljs.getLanguage(detected)) {
    return hljs.highlight(code, { language: detected }).value;
  }
  try {
    return hljs.highlightAuto(code).value;
  } catch {
    return escapeHtml(code);
  }
}

function mathExtension(name, level, pattern, display) {
  return {
    name,
    level,
    start(source) {
      const marker = display ? source.indexOf("\\[") : source.indexOf("\\(");
      return marker < 0 ? undefined : marker;
    },
    tokenizer(source) {
      const match = pattern.exec(source);
      if (!match) return undefined;
      return { type: name, raw: match[0], text: match[1] };
    },
    renderer(token) {
      const left = display ? "\\[" : "\\(";
      const right = display ? "\\]" : "\\)";
      const tag = display ? "div" : "span";
      return `<${tag} class="math-source">${left}${escapeHtml(token.text)}${right}</${tag}>`;
    },
  };
}

if (window.marked) {
  // Marked treats backslashes before brackets and parentheses as Markdown
  // escapes. Capture LaTeX delimiters first so KaTeX auto-render can still
  // see them after Markdown rendering. Fenced and inline code take precedence
  // over these extensions and remain literal.
  marked.use({
    extensions: [
      mathExtension("displayMath", "block", /^\\\[([\s\S]+?)\\\](?:\n|$)/, true),
      mathExtension("inlineMath", "inline", /^\\\(([\s\S]+?)\\\)/, false),
    ],
  });
  marked.setOptions({
    gfm: true,
    breaks: false,
    highlight(code, lang) {
      return highlightCode(code, lang);
    },
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function isStringToken(token) {
  return token.length >= 2 && (
    (token.startsWith('"') && token.endsWith('"')) ||
    (token.startsWith("'") && token.endsWith("'")) ||
    (token.startsWith("“") && token.endsWith("”")) ||
    (token.startsWith("‘") && token.endsWith("’"))
  );
}

function isAbsolutePathStart(text, index) {
  if (index === 0) return true;
  return /[\s=:(\[{<,]/.test(text[index - 1]);
}

function isPathToken(token, text, index) {
  if (token.startsWith("/")) return isAbsolutePathStart(text, index);
  if (/[\/\\]/.test(token)) return false;
  return semanticHighlightConfig.extensionPattern.test(token);
}

function appendText(fragment, text) {
  if (text) fragment.appendChild(document.createTextNode(text));
}

function spanFor(className, text) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text;
  return span;
}

function renderPathToken(token) {
  const fragment = document.createDocumentFragment();
  const parts = token.startsWith("/")
    ? token.split(/([\/_-]+)/)
    : token.split(/([-_.]+)/);
  let segmentIndex = 0;
  for (const part of parts) {
    if (!part) continue;
    if (/^[\/\\_.-]+$/.test(part)) {
      fragment.appendChild(spanFor("semantic-path-separator", part));
      continue;
    }
    const className = segmentIndex % 2 === 0
      ? "semantic-path-part alt-a"
      : "semantic-path-part alt-b";
    fragment.appendChild(spanFor(className, part));
    segmentIndex += 1;
  }
  return fragment;
}

function semanticFragmentForText(text) {
  const fragment = document.createDocumentFragment();
  const pattern = new RegExp(
    semanticHighlightConfig.tokenPattern.source,
    semanticHighlightConfig.tokenPattern.flags,
  );
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const token = match[0];
    const index = match.index || 0;
    appendText(fragment, text.slice(cursor, index));
    if (isStringToken(token)) {
      fragment.appendChild(spanFor("semantic-string", token));
    } else if (semanticHighlightConfig.numberPattern.test(token)) {
      fragment.appendChild(spanFor("semantic-number", token));
    } else if (isPathToken(token, text, index)) {
      fragment.appendChild(renderPathToken(token));
    } else {
      appendText(fragment, token);
    }
    cursor = index + token.length;
  }
  appendText(fragment, text.slice(cursor));
  return fragment;
}

function shouldHighlightTextNode(node) {
  if (!node.nodeValue || !node.nodeValue.trim()) return false;
  const parent = node.parentElement;
  if (!parent) return false;
  return !parent.closest(semanticHighlightConfig.skipSelector);
}

function applySemanticHighlights(root) {
  if (!semanticHighlightConfig.enabled) return;
  const containers = root.matches?.(semanticHighlightConfig.selectors)
    ? [root]
    : Array.from(root.querySelectorAll(semanticHighlightConfig.selectors));
  for (const container of containers) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      if (shouldHighlightTextNode(walker.currentNode)) nodes.push(walker.currentNode);
    }
    for (const node of nodes) {
      const fragment = semanticFragmentForText(node.nodeValue);
      node.parentNode.replaceChild(fragment, node);
    }
  }
}

function renderMarkdown(text) {
  if (!window.marked || !window.DOMPurify) {
    return `<pre><code>${escapeHtml(text)}</code></pre>`;
  }
  return DOMPurify.sanitize(marked.parse(text || ""), { ADD_ATTR: ["target"] });
}

function applyMathAndCode(root) {
  if (window.hljs) {
    root.querySelectorAll("pre code").forEach((block) => {
      if (block.classList.contains("hljs")) return;
      const code = block.textContent || "";
      const languageClass = Array.from(block.classList)
        .find((className) => className.startsWith("language-"));
      block.innerHTML = highlightCode(code, languageClass || "");
      block.classList.add("hljs");
    });
  }
  if (window.renderMathInElement) {
    renderMathInElement(root, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
  }
}

function setStatus(message, className = "loading") {
  els.conversation.className = `conversation view-conversation ${className}`;
  els.conversation.innerHTML = `<p>${escapeHtml(message)}</p>`;
}

function recordClass(role) {
  return String(role || "message").toLowerCase().replace(/[^a-z0-9_-]+/g, "_");
}

function compactText(value, limit = 140) {
  const compact = String(value || "").replace(/\s+/g, " ").trim();
  if (compact.length <= limit) return compact || "User message";
  return `${compact.slice(0, limit - 1).trim()}...`;
}

function renderRecord(record) {
  const role = record.role || "message";
  const roleClass = recordClass(role);
  const answerLink = roleClass === "assistant"
    ? `<a class="typeset-answer-link" href="${escapeHtml(focusedTypesetUrl(record))}" target="_blank" rel="noopener">Open answer ↗</a>`
    : "";
  const header = `
    <header class="message-header">
      <span><span class="role">${escapeHtml(role)}</span> · line ${escapeHtml(record.line_no)}</span>
      <span class="message-header-actions">
        <time>${escapeHtml(record.timeLabel || record.timestamp || "")}</time>
        ${answerLink}
      </span>
    </header>
  `;
  const body = `<div class="message-body">${renderMarkdown(record.text || "")}</div>`;
  if (roleClass === "user") {
    return `
      <section class="message user collapsed-user" data-line-no="${escapeHtml(record.line_no)}">
        <details class="user-details">
          <summary>
            <span class="user-summary-main">${escapeHtml(compactText(record.text))}</span>
            <span class="user-summary-meta">${escapeHtml(record.timeLabel || `line ${record.line_no}`)}</span>
          </summary>
          ${body}
        </details>
      </section>
    `;
  }
  return `
    <section class="message ${roleClass}" data-line-no="${escapeHtml(record.line_no)}">
      ${header}
      ${body}
    </section>
  `;
}

function setNavigationCurrent(message) {
  els.conversation.querySelectorAll(".message.is-navigation-current").forEach((current) => {
    current.classList.remove("is-navigation-current");
    current.removeAttribute("aria-current");
  });
  if (!message) return;
  message.classList.add("is-navigation-current");
  message.setAttribute("aria-current", "true");
}

function scrollToLatestAssistant() {
  const assistants = els.conversation.querySelectorAll(".message.assistant");
  const messages = els.conversation.querySelectorAll(".message");
  const target = assistants[assistants.length - 1] || messages[messages.length - 1];
  if (!target) return;
  messageNavigationIndex = Array.from(messages).indexOf(target);
  setNavigationCurrent(target);
  target.scrollIntoView({ block: "start" });
}

function scrollToConversationTop() {
  const target = els.conversation.querySelector(".message");
  messageNavigationIndex = 0;
  setNavigationCurrent(target);
  els.conversation.scrollIntoView({ block: "start" });
}

function messageLineId(message) {
  if (message?.dataset?.lineNo) return message.dataset.lineNo;
  const text = message?.querySelector(".message-header span")?.textContent || "";
  const match = text.match(/line\s+(\d+)/);
  return match ? match[1] : "";
}

function firstVisibleMessage() {
  const viewportTop = 0;
  const messages = Array.from(els.conversation.querySelectorAll(".message"));
  return messages.find((message) => message.getBoundingClientRect().bottom > viewportTop + 8) || null;
}

function captureScrollAnchor() {
  const message = firstVisibleMessage();
  if (!message) return { lineNo: "", top: 0, pageY: window.scrollY };
  return {
    lineNo: messageLineId(message),
    top: message.getBoundingClientRect().top,
    pageY: window.scrollY,
  };
}

function restoreScrollAnchor(anchor) {
  if (!anchor?.lineNo) {
    window.scrollTo(0, anchor?.pageY || 0);
    return;
  }
  const target = Array.from(els.conversation.querySelectorAll(".message"))
    .find((message) => messageLineId(message) === String(anchor.lineNo));
  if (!target) {
    window.scrollTo(0, anchor.pageY || 0);
    return;
  }
  window.scrollBy(0, target.getBoundingClientRect().top - anchor.top);
}

function scrollToMessageLine(lineNo) {
  if (!lineNo) return false;
  const target = Array.from(els.conversation.querySelectorAll(".message"))
    .find((message) => messageLineId(message) === String(lineNo));
  if (!target) return false;
  target.scrollIntoView({ block: "start" });
  return true;
}

function messageScrollMargin(message) {
  const margin = Number.parseFloat(window.getComputedStyle(message).scrollMarginTop);
  return Number.isFinite(margin) ? margin : 0;
}

function directionalMessageIndex(messages, direction, anchorTop) {
  const tolerance = 2;
  if (direction > 0) {
    return messages.findIndex(
      (message) => message.getBoundingClientRect().top > anchorTop + tolerance,
    );
  }
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].getBoundingClientRect().top < anchorTop - tolerance) return index;
  }
  return -1;
}

function jumpMessage(delta) {
  const messages = Array.from(els.conversation.querySelectorAll(".message"));
  if (!messages.length) return;
  const direction = delta < 0 ? -1 : 1;
  const anchorTop = messageScrollMargin(messages[0]);
  const targetIndex = directionalMessageIndex(messages, direction, anchorTop);
  const pageDistance = Math.max(1, window.innerHeight);
  if (targetIndex < 0) {
    messageNavigationIndex = -1;
    setNavigationCurrent(null);
    window.scrollBy({ top: direction * pageDistance, left: 0, behavior: "auto" });
    return;
  }

  const target = messages[targetIndex];
  const targetDistance = target.getBoundingClientRect().top - messageScrollMargin(target);
  if (Math.abs(targetDistance) <= pageDistance) {
    messageNavigationIndex = targetIndex;
    setNavigationCurrent(target);
    target.scrollIntoView({ block: "start" });
    return;
  }

  messageNavigationIndex = -1;
  setNavigationCurrent(null);
  const movement = direction * Math.min(Math.abs(targetDistance), pageDistance);
  window.scrollBy({ top: movement, left: 0, behavior: "auto" });
}

function updateLoadButtons(data) {
  const allShown = Boolean(data.all) || data.shownCount >= data.totalCount;
  if (els.loadEarlier) els.loadEarlier.disabled = allShown || loadingConversation;
  if (els.loadAll) els.loadAll.disabled = allShown || loadingConversation;
}

function renderConversation(data, options = {}) {
  const previousData = lastConversationData;
  lastConversationData = data;
  messageNavigationIndex = -1;
  const anchorLine = options.anchorLine || "";
  const session = data.session;
  document.title = session.title || "Codex Conversation";
  els.sessionTitle.textContent = session.title || "Untitled conversation";
  els.sessionMeta.textContent = session.cwd || session.path;
  const typesetParams = identityParams("v");
  typesetParams.set("tail", String(Math.min(currentTail, 8)));
  if (showAllRecords) typesetParams.set("all", "1");
  if (typesetParams.has("id")) {
    const id = typesetParams.get("id");
    typesetParams.delete("id");
    els.typesetViewLink.href = `/t/${encodeURIComponent(id)}?${typesetParams}`;
  } else {
    els.typesetViewLink.href = `/typeset?${typesetParams}`;
  }
  els.conversationInfo.textContent = [
    session.lastLabel ? `Last message ${session.lastLabel}` : "",
    `${data.shownCount || data.records.length} of ${data.totalCount || data.records.length} shown`,
    data.all ? "all records" : `tail ${data.tail}`,
    data.includeTools ? "tool records included" : "",
  ].filter(Boolean).join(" · ");
  updateLoadButtons(data);

  if (!data.records.length) {
    setStatus("This session has no renderable messages.", "empty");
    return;
  }

  els.conversation.className = "conversation view-conversation";
  if (options.preserveScroll && extendsConversationContent(previousData, data)) {
    const addedRecords = data.records.slice(previousData.records.length);
    if (addedRecords.length) {
      const firstAddedIndex = els.conversation.querySelectorAll(".message").length;
      els.conversation.insertAdjacentHTML(
        "beforeend",
        addedRecords.map(renderRecord).join(""),
      );
      const addedMessages = Array.from(
        els.conversation.querySelectorAll(".message"),
      ).slice(firstAddedIndex);
      for (const message of addedMessages) {
        applyMathAndCode(message);
        applySemanticHighlights(message);
      }
    }
  } else {
    els.conversation.innerHTML = data.records.map(renderRecord).join("");
    applyMathAndCode(els.conversation);
    applySemanticHighlights(document);
  }
  if (anchorLine) {
    requestAnimationFrame(() => {
      if (!scrollToMessageLine(anchorLine)) restoreScrollAnchor(options.scrollAnchor);
    });
  } else if (options.preserveScroll) {
    requestAnimationFrame(() => {
      restoreScrollAnchor(options.scrollAnchor);
    });
  } else {
    requestAnimationFrame(scrollToLatestAssistant);
  }
}

async function loadConversation(options = {}) {
  if (loadingConversation) return;
  const identity = identityParams("v");
  if (![...identity.keys()].length) {
    setStatus("No conversation path or id was provided.", "error");
    return;
  }
  const scrollAnchor = options.preserveScroll || options.anchorLine
    ? captureScrollAnchor()
    : null;
  const renderOptions = { ...options, scrollAnchor };
  loadingConversation = true;
  updateLoadButtons({ shownCount: 0, totalCount: 1, all: false });
  const apiParams = new URLSearchParams(identity);
  apiParams.set("tail", String(currentTail));
  if (renderOptions.scrollAnchor?.lineNo) {
    apiParams.set("anchor", renderOptions.scrollAnchor.lineNo);
  }
  if (options.preserveScroll) {
    const firstRendered = els.conversation.querySelector(".message");
    const firstRenderedLine = messageLineId(firstRendered);
    if (firstRenderedLine) apiParams.set("anchor", firstRenderedLine);
  }
  try {
    if (showAllRecords) apiParams.set("all", "1");
    if (params.get("tools") === "1") apiParams.set("tools", "1");
    const response = await fetch(`/api/session?${apiParams}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load conversation");
    if (renderOptions.avoidUnchangedRender && sameConversationContent(lastConversationData, data)) {
      lastConversationData = data;
      return;
    }
    renderConversation(data, renderOptions);
  } finally {
    loadingConversation = false;
    if (lastConversationData) updateLoadButtons(lastConversationData);
  }
}

async function refreshConversation(options = {}) {
  if (loadingConversation) return;
  const jumpToLatest = Boolean(options.jumpToLatest);
  const previousTotal = Number(lastConversationData?.totalCount || 0);
  setRefreshState(true);
  try {
    await loadConversation({
      preserveScroll: !jumpToLatest,
      avoidUnchangedRender: true,
    });
    if (jumpToLatest) scrollToLatestAssistant();
    showRefreshResult(previousTotal);
  } catch (error) {
    showRefreshResult(previousTotal, true);
    throw error;
  } finally {
    setRefreshState(false);
  }
}

function loadEarlierConversation() {
  const anchor = firstVisibleMessage();
  const anchorLine = messageLineId(anchor);
  currentTail = Math.min(currentTail * 2, 500);
  return loadConversation({ anchorLine });
}

function loadAllConversation() {
  const anchor = firstVisibleMessage();
  const anchorLine = messageLineId(anchor);
  showAllRecords = true;
  return loadConversation({ anchorLine });
}

els.refreshConversation?.addEventListener("click", () => {
  refreshConversation().catch((error) => {
    setStatus(error.message, "error");
  });
});

els.statusRefresh?.addEventListener("click", () => {
  refreshConversation().catch((error) => {
    setStatus(error.message, "error");
  });
});

els.jumpTop?.addEventListener("click", scrollToConversationTop);
els.statusbarToggle?.addEventListener("click", toggleStatusbar);
els.jumpLatest?.addEventListener("click", scrollToLatestAssistant);
els.jumpPrevious?.addEventListener("click", () => jumpMessage(-1));
els.jumpNext?.addEventListener("click", () => jumpMessage(1));
els.loadEarlier?.addEventListener("click", () => {
  loadEarlierConversation().catch((error) => {
    setStatus(error.message, "error");
  });
});
els.loadAll?.addEventListener("click", () => {
  loadAllConversation().catch((error) => {
    setStatus(error.message, "error");
  });
});

window.addEventListener("keydown", (event) => {
  const target = event.target;
  const isEditable = target?.closest?.("input, textarea, select, [contenteditable='true']");
  const key = event.key?.toLowerCase();
  const isRefreshKey = event.code === "KeyR" || event.key?.toLowerCase() === "r";
  const isPreviousKey = event.code === "ArrowLeft";
  const isNextKey = event.code === "ArrowRight";
  const isStatusbarKey = event.key === "/";
  const isActionKey = ["u", "l", "e", "a", "t", "c"].includes(key);
  if ((!isRefreshKey && !isPreviousKey && !isNextKey && !isStatusbarKey && !isActionKey) || event.metaKey || event.ctrlKey) return;
  if (isEditable && !event.altKey) return;
  event.preventDefault();
  if (isStatusbarKey) {
    toggleStatusbar();
  } else if (isRefreshKey) {
    refreshConversation({ jumpToLatest: event.shiftKey }).catch((error) => {
      setStatus(error.message, "error");
    });
  } else if (isPreviousKey) {
    jumpMessage(-1);
  } else if (isNextKey) {
    jumpMessage(1);
  } else if (key === "u") {
    scrollToConversationTop();
  } else if (key === "l") {
    scrollToLatestAssistant();
  } else if (key === "e" && !els.loadEarlier?.disabled) {
    loadEarlierConversation().catch((error) => setStatus(error.message, "error"));
  } else if (key === "a" && !els.loadAll?.disabled) {
    loadAllConversation().catch((error) => setStatus(error.message, "error"));
  } else if (key === "t") {
    location.assign(els.typesetViewLink.href);
  } else if (key === "c") {
    location.assign("/");
  }
}, true);

loadConversation().catch((error) => {
  setStatus(error.message, "error");
});
"""


TYPESET_JS = r"""
const params = new URLSearchParams(location.search);
const TYPESET_PDF_RENDER_SCALE = 1.6;
const LINK_HINT_DELAY_MS = 200;
const CODE_COPY_ORIGIN = "https://codex-tools.invalid";
const WEB_ASSETS = document.body.dataset.webAssets || "bundled";
const PDFJS_MODULE_URL = WEB_ASSETS === "cdn"
  ? "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs"
  : "/vendor/pdf.min.mjs";
const PDFJS_WORKER_URL = WEB_ASSETS === "cdn"
  ? "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs"
  : "/vendor/pdf.worker.min.mjs";
let loadingConversation = false;
let currentTail = Number.parseInt(params.get("tail") || "8", 10) || 8;
let showAllRecords = params.get("all") === "1";
let lastConversationData = null;
let typesetDebugEnabled = false;
let typesetHeaderMode = "external";
let messageNavigationIndex = -1;
let linkHintTimer = null;
let linkHintFrame = null;
let linkHintsActive = false;
let hintedResourceLinks = [];
let refreshStatusTimer = 0;
const els = {
  conversation: document.getElementById("conversation"),
  conversationInfo: document.getElementById("conversationInfo"),
  jumpLatest: document.getElementById("jumpLatest"),
  jumpNext: document.getElementById("jumpNext"),
  jumpPrevious: document.getElementById("jumpPrevious"),
  jumpTop: document.getElementById("jumpTop"),
  loadAll: document.getElementById("loadAll"),
  loadEarlier: document.getElementById("loadEarlier"),
  normalViewLink: document.getElementById("normalViewLink"),
  refreshConversation: document.getElementById("refreshConversation"),
  refreshStatus: document.getElementById("refreshStatus"),
  statusRefresh: document.getElementById("statusRefresh"),
  statusbar: document.getElementById("viewStatusbar"),
  statusbarToggle: document.getElementById("statusbarToggle"),
  sessionTitle: document.getElementById("sessionTitle"),
  sessionMeta: document.getElementById("sessionMeta"),
};

function setStatusbarRefreshState(refreshing) {
  if (!els.statusbarToggle) return;
  els.statusbarToggle.disabled = refreshing;
  els.statusbarToggle.classList.toggle("is-refreshing", refreshing);
  els.statusbarToggle.toggleAttribute("aria-busy", refreshing);
  if (refreshing) {
    els.statusbarToggle.innerHTML = '<span class="refresh-icon" aria-hidden="true">↻</span>';
    els.statusbarToggle.title = "Refreshing typeset view...";
    els.statusbarToggle.setAttribute("aria-label", "Refreshing typeset view");
    return;
  }
  els.statusbarToggle.textContent = "/";
  const expanded = els.statusbar?.classList.contains("is-expanded") || false;
  const label = expanded ? "Hide navigation" : "Show navigation";
  els.statusbarToggle.title = label;
  els.statusbarToggle.setAttribute("aria-label", label);
}

function setRefreshState(refreshing) {
  for (const button of [els.refreshConversation, els.statusRefresh]) {
    if (!button) continue;
    if (!button.dataset.idleTitle) button.dataset.idleTitle = button.title;
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.getAttribute("aria-label") || button.title;
    button.disabled = refreshing;
    button.classList.toggle("is-refreshing", refreshing);
    button.title = refreshing ? "Refreshing typeset view..." : button.dataset.idleTitle;
    button.setAttribute(
      "aria-label",
      refreshing ? "Refreshing typeset view" : button.dataset.idleLabel,
    );
  }
  els.conversation?.toggleAttribute("aria-busy", refreshing);
  setStatusbarRefreshState(refreshing);
  if (refreshing && els.refreshStatus) {
    window.clearTimeout(refreshStatusTimer);
    els.refreshStatus.textContent = "Refreshing...";
  }
}

function showRefreshResult(previousTotal, failed = false) {
  if (!els.refreshStatus) return;
  const added = Math.max(0, Number(lastConversationData?.totalCount || 0) - previousTotal);
  els.refreshStatus.textContent = failed
    ? "Refresh failed"
    : added
      ? `${added} new message${added === 1 ? "" : "s"}`
      : "Up to date";
  window.clearTimeout(refreshStatusTimer);
  refreshStatusTimer = window.setTimeout(() => {
    els.refreshStatus.textContent = "";
  }, 1800);
}

function sameConversationRecord(record, candidate) {
  return record.line_no === candidate?.line_no
    && record.role === candidate?.role
    && record.timestamp === candidate?.timestamp
    && record.text === candidate?.text;
}

function sameConversationContent(previous, next) {
  if (!previous || previous.totalCount !== next.totalCount) return false;
  if (previous.records.length !== next.records.length) return false;
  return previous.records.every((record, index) =>
    sameConversationRecord(record, next.records[index]));
}

function extendsConversationContent(previous, next) {
  if (!previous || previous.records.length > next.records.length) return false;
  return previous.records.every((record, index) =>
    sameConversationRecord(record, next.records[index]));
}

function setStatusbarExpanded(expanded) {
  if (!els.statusbar || !els.statusbarToggle) return;
  els.statusbar.classList.toggle("is-expanded", expanded);
  els.conversation?.classList.toggle("statusbar-expanded", expanded);
  els.statusbarToggle.setAttribute("aria-expanded", String(expanded));
  const label = expanded ? "Hide navigation" : "Show navigation";
  els.statusbarToggle.title = label;
  els.statusbarToggle.setAttribute("aria-label", label);
}

function toggleStatusbar() {
  if (els.statusbarToggle?.disabled) return;
  setStatusbarExpanded(!els.statusbar?.classList.contains("is-expanded"));
}

function routeId(prefix) {
  const marker = `/${prefix}/`;
  if (!location.pathname.startsWith(marker)) return "";
  return decodeURIComponent(location.pathname.slice(marker.length));
}

function focusedRoute() {
  const debugMarker = "/debug/typeset/";
  if (location.pathname === "/debug/typeset") {
    return { active: true, debug: true, id: "", line: params.get("line") || "" };
  }
  if (location.pathname.startsWith(debugMarker)) {
    const parts = location.pathname.slice(debugMarker.length).split("/");
    const line = decodeURIComponent(parts.pop() || "");
    const id = decodeURIComponent(parts.join("/"));
    return { active: true, debug: true, id, line };
  }
  if (location.pathname === "/typeset" && params.get("line")) {
    return { active: true, debug: false, id: "", line: params.get("line") || "" };
  }
  const marker = "/t/";
  if (location.pathname.startsWith(marker)) {
    const parts = location.pathname.slice(marker.length).split("/");
    if (parts.length >= 2) {
      const line = decodeURIComponent(parts.pop() || "");
      const id = decodeURIComponent(parts.join("/"));
      return { active: true, debug: false, id, line };
    }
  }
  return { active: false, debug: false, id: "", line: "" };
}

function identityParams(prefix = "t") {
  const identity = new URLSearchParams();
  const route = focusedRoute();
  const id = route.id || routeId(prefix) || params.get("id") || "";
  const path = params.get("path") || "";
  if (id) identity.set("id", id);
  else if (path) identity.set("path", path);
  return identity;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMarkdown(text) {
  if (!window.marked || !window.DOMPurify) {
    return `<pre><code>${escapeHtml(text)}</code></pre>`;
  }
  return DOMPurify.sanitize(marked.parse(text || ""), { ADD_ATTR: ["target"] });
}

function applyMathAndCode(root) {
  if (window.hljs) {
    root.querySelectorAll("pre code").forEach((block) => {
      if (block.classList.contains("hljs")) return;
      try {
        hljs.highlightElement(block);
      } catch {
        // Best-effort fallback rendering.
      }
    });
  }
  if (window.renderMathInElement) {
    renderMathInElement(root, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
  }
}

function setStatus(message, className = "loading") {
  els.conversation.className = `conversation view-conversation typeset-conversation ${className}`;
  els.conversation.innerHTML = `<p>${escapeHtml(message)}</p>`;
}

function showFocusedAnswerLoader(line, fresh = false) {
  els.conversation.className = "conversation view-conversation typeset-conversation loading";
  els.conversation.innerHTML = `
    <div class="typeset-debug-loading" role="status" aria-live="polite">
      <span class="typeset-debug-spinner" aria-hidden="true"></span>
      <div>
        <strong>${fresh ? "Recompiling" : "Opening"} assistant line ${escapeHtml(line)}</strong>
        <span>${fresh ? "Running XeLaTeX and preparing" : "Loading"} the PDF preview...</span>
      </div>
    </div>
  `;
}

function recordClass(role) {
  return String(role || "message").toLowerCase().replace(/[^a-z0-9_-]+/g, "_");
}

function compactText(value, limit = 140) {
  const compact = String(value || "").replace(/\s+/g, " ").trim();
  if (compact.length <= limit) return compact || "User message";
  return `${compact.slice(0, limit - 1).trim()}...`;
}

function recordHeader(record) {
  const role = record.role || "message";
  return `
    <header class="message-header">
      <span><span class="role">${escapeHtml(role)}</span> · line ${escapeHtml(record.line_no)}</span>
      <time>${escapeHtml(record.timeLabel || record.timestamp || "")}</time>
    </header>
  `;
}

function focusedTypesetUrl(record) {
  const identity = identityParams("t");
  if (identity.has("id")) {
    const id = identity.get("id");
    return `/t/${encodeURIComponent(id)}/${encodeURIComponent(record.line_no)}`;
  }
  identity.set("line", String(record.line_no));
  return `/typeset?${identity}`;
}

function debugTypesetUrl(record) {
  const identity = identityParams("t");
  if (identity.has("id")) {
    const id = identity.get("id");
    return `/debug/typeset/${encodeURIComponent(id)}/${encodeURIComponent(record.line_no)}`;
  }
  identity.set("line", String(record.line_no));
  return `/debug/typeset?${identity}`;
}

function typesetDebugBar(record) {
  const route = focusedRoute();
  if (!route.active) {
    return `
      <header class="typeset-answer-bar">
        <span>Assistant answer · line ${escapeHtml(record.line_no)}</span>
        <a href="${escapeHtml(focusedTypesetUrl(record))}" target="_blank" rel="noopener">Open answer ↗</a>
      </header>
    `;
  }
  if (!typesetDebugEnabled) return "";
  if (route.debug) {
    return `
      <header class="typeset-debug-bar">
        <span>TYPESET DEBUG · line ${escapeHtml(record.line_no)}</span>
        <span>Refresh recompiles</span>
      </header>
    `;
  }
  return `
    <header class="typeset-debug-bar">
      <span>TYPESET DEBUG · line ${escapeHtml(record.line_no)}</span>
      <a href="${escapeHtml(debugTypesetUrl(record))}" target="_blank" rel="noopener">Fresh render ↗</a>
    </header>
  `;
}

function pdfPage(record) {
  const url = record.typeset?.pdfUrl || "";
  const hasResources = record.attachments?.length || record.externalLinks?.length;
  const linkClass = hasResources ? " has-resources" : "";
  return `
    <div class="typeset-pdf-page is-loading${linkClass}" data-pdf-url="${escapeHtml(url)}"
      data-copy-line="${escapeHtml(record.line_no)}">
      <span>Rendering PDF...</span>
    </div>
  `;
}

function resourceLinks(record) {
  const attachments = Array.isArray(record.attachments) ? record.attachments : [];
  const externalLinks = Array.isArray(record.externalLinks) ? record.externalLinks : [];
  if (!attachments.length && !externalLinks.length) return "";
  const attachmentHeading = attachments.length === 1 ? "Attachment" : "Attachments";
  const linkHeading = externalLinks.length === 1 ? "Link" : "Links";
  return `
    <footer class="typeset-attachments">
      ${attachments.length ? `
        <div class="typeset-resource-group">
          <strong>${attachmentHeading}</strong>
          <span class="typeset-attachment-list">
            ${attachments.map((link) => `
              <a href="${escapeHtml(link.url)}" target="_blank" rel="noopener">${escapeHtml(link.label)}<span class="typeset-attachment-kind">${escapeHtml(link.kind)}</span> ↗</a>
            `).join("")}
          </span>
        </div>
      ` : ""}
      ${externalLinks.length ? `
        <div class="typeset-resource-group">
          <strong>${linkHeading}</strong>
          <span class="typeset-attachment-list">
            ${externalLinks.map((link) => `
              <a href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)}${link.label !== link.url ? `<span class="typeset-attachment-kind">${escapeHtml(link.host)}</span>` : ""} ↗</a>
            `).join("")}
          </span>
        </div>
      ` : ""}
    </footer>
  `;
}

function resourceLinkIsVisible(link) {
  const rect = link.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  if (rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
  if (rect.right <= 0 || rect.left >= window.innerWidth) return false;
  const style = window.getComputedStyle(link);
  return style.visibility !== "hidden" && style.display !== "none";
}

function removeLinkHintLabels() {
  for (const link of hintedResourceLinks) delete link.dataset.linkHint;
  hintedResourceLinks = [];
}

function assignVisibleLinkHints() {
  removeLinkHintLabels();
  hintedResourceLinks = Array.from(
    document.querySelectorAll(".typeset-attachments a[href]"),
  ).filter(resourceLinkIsVisible).slice(0, 9);
  hintedResourceLinks.forEach((link, index) => {
    link.dataset.linkHint = String(index + 1);
  });
  linkHintsActive = true;
}

function clearLinkHints() {
  if (linkHintTimer !== null) window.clearTimeout(linkHintTimer);
  if (linkHintFrame !== null) window.cancelAnimationFrame(linkHintFrame);
  linkHintTimer = null;
  linkHintFrame = null;
  linkHintsActive = false;
  removeLinkHintLabels();
}

function scheduleLinkHints() {
  if (linkHintsActive || linkHintTimer !== null) return;
  linkHintTimer = window.setTimeout(() => {
    linkHintTimer = null;
    assignVisibleLinkHints();
  }, LINK_HINT_DELAY_MS);
}

function refreshVisibleLinkHints() {
  if (!linkHintsActive || linkHintFrame !== null) return;
  linkHintFrame = window.requestAnimationFrame(() => {
    linkHintFrame = null;
    if (linkHintsActive) assignVisibleLinkHints();
  });
}

function linkHintEditableTarget(target) {
  return Boolean(target?.closest?.("input, textarea, select, [contenteditable='true']"));
}

function handleLinkHintKeyDown(event) {
  if (event.key === "Shift") {
    if (!event.repeat && !linkHintEditableTarget(event.target)) scheduleLinkHints();
    return;
  }

  const digitMatch = event.code?.match(/^(?:Digit|Numpad)([1-9])$/);
  if (linkHintsActive && event.shiftKey && digitMatch) {
    const link = hintedResourceLinks[Number(digitMatch[1]) - 1];
    event.preventDefault();
    clearLinkHints();
    if (link?.isConnected) link.click();
    return;
  }

  if (linkHintsActive || linkHintTimer !== null) clearLinkHints();
}

function copyMarkdownButton(record) {
  return `
    <button class="typeset-copy-markdown" type="button"
      data-copy-line="${escapeHtml(record.line_no)}"
      title="Copy Markdown" aria-label="Copy Markdown" aria-live="polite">
      <span aria-hidden="true">&#x29C9;</span>
    </button>
  `;
}

function externalTypesetHeader(record, showLabel = true) {
  const route = focusedRoute();
  const answerLink = !route.active
    ? `<a href="${escapeHtml(focusedTypesetUrl(record))}" target="_blank" rel="noopener">Open answer ↗</a>`
    : "";
  const debugLink = typesetDebugEnabled && route.active && !route.debug
    ? `<a href="${escapeHtml(debugTypesetUrl(record))}" target="_blank" rel="noopener">Fresh render ↗</a>`
    : "";
  return `
    <header class="typeset-external-header">
      ${showLabel ? "<strong>Assistant answer</strong>" : '<span aria-hidden="true"></span>'}
      <span class="typeset-external-header-actions">
        <time>${escapeHtml(record.timeLabel || record.timestamp || "")}</time>
        ${copyMarkdownButton(record)}
        ${answerLink}
        ${debugLink}
      </span>
    </header>
  `;
}

function renderFallback(record, message = "") {
  const note = message ? `<p class="typeset-error">${escapeHtml(message)}</p>` : "";
  const hasResources = record.attachments?.length || record.externalLinks?.length;
  const resourceClass = hasResources ? " has-resources" : "";
  return `
    <div class="typeset-fallback${resourceClass}">
      ${note}
      <div class="message-body">${renderMarkdown(record.text || "")}</div>
    </div>
  `;
}

function renderRecord(record, index, records) {
  const roleClass = recordClass(record.role);
  if (roleClass === "assistant") {
    const previousIsAssistant = recordClass(records[index - 1]?.role) === "assistant";
    const nextIsAssistant = recordClass(records[index + 1]?.role) === "assistant";
    const groupClasses = typesetHeaderMode === "external"
      ? [previousIsAssistant ? "assistant-continued" : "", nextIsAssistant ? "assistant-continuing" : ""].filter(Boolean).join(" ")
      : "";
    const typeset = record.typeset || {};
    const body = typeset.ok && typeset.pdfUrl
      ? pdfPage(record)
      : renderFallback(record, typeset.error ? `Typeset fallback: ${typeset.error}` : "Typeset fallback");
    const externalHeader = typesetHeaderMode === "external"
      ? externalTypesetHeader(record, !previousIsAssistant)
      : "";
    const debugBar = typesetHeaderMode === "embedded" ? typesetDebugBar(record) : "";
    return `
      <section class="message assistant typeset-message ${groupClasses}" data-line-no="${escapeHtml(record.line_no)}">
        ${debugBar}
        ${externalHeader}
        ${body}
        ${resourceLinks(record)}
      </section>
    `;
  }
  if (roleClass === "user") {
    return `
      <section class="message user collapsed-user" data-line-no="${escapeHtml(record.line_no)}">
        <details class="user-details">
          <summary>
            <span class="user-summary-main">${escapeHtml(compactText(record.text))}</span>
            <span class="user-summary-meta">${escapeHtml(record.timeLabel || `line ${record.line_no}`)}</span>
          </summary>
          <div class="message-body">${renderMarkdown(record.text || "")}</div>
        </details>
      </section>
    `;
  }
  return `
    <section class="message ${roleClass}" data-line-no="${escapeHtml(record.line_no)}">
      ${recordHeader(record)}
      <div class="message-body">${renderMarkdown(record.text || "")}</div>
    </section>
  `;
}

async function pdfjs() {
  if (window.pdfjsLib) return window.pdfjsLib;
  const module = await import(PDFJS_MODULE_URL);
  window.pdfjsLib = module;
  return module;
}

function codeCopyIndex(annotation) {
  const rawUrl = annotation?.unsafeUrl || annotation?.url || "";
  try {
    const url = new URL(rawUrl);
    if (url.origin !== CODE_COPY_ORIGIN) return null;
    const match = url.pathname.match(/^\/code\/(\d+)\/?$/);
    return match ? Number.parseInt(match[1], 10) : null;
  } catch {
    return null;
  }
}

async function addCodeCopyButtons(page, container, viewport) {
  let annotations;
  try {
    annotations = await page.getAnnotations({ intent: "display" });
  } catch {
    return;
  }
  const lineNo = String(container.dataset.copyLine || "");
  const record = lastConversationData?.records?.find(
    (candidate) => String(candidate.line_no) === lineNo,
  );
  const codeBlocks = record?.typeset?.codeBlocks || [];
  const layer = document.createElement("div");
  layer.className = "code-copy-layer";
  for (const annotation of annotations) {
    const copyIndex = codeCopyIndex(annotation);
    if (copyIndex === null || copyIndex >= codeBlocks.length || !annotation.rect) continue;
    const rectangle = viewport.convertToViewportRectangle(annotation.rect);
    const left = Math.min(rectangle[0], rectangle[2]);
    const right = Math.max(rectangle[0], rectangle[2]);
    const top = Math.min(rectangle[1], rectangle[3]);
    const bottom = Math.max(rectangle[1], rectangle[3]);
    const width = Math.max(22, right - left);
    const height = Math.max(22, bottom - top);
    const button = document.createElement("button");
    button.className = "typeset-code-copy";
    button.type = "button";
    button.dataset.copyLine = lineNo;
    button.dataset.copyIndex = String(copyIndex);
    button.dataset.copyLabel = "Copy code";
    button.title = "Copy code";
    button.setAttribute("aria-label", "Copy code");
    button.innerHTML = '<span aria-hidden="true">&#x29C9;</span>';
    button.style.left = `${right - width}px`;
    button.style.top = `${top - Math.max(0, (height - (bottom - top)) / 2)}px`;
    button.style.width = `${width}px`;
    button.style.height = `${height}px`;
    layer.appendChild(button);
  }
  if (layer.childElementCount) container.appendChild(layer);
}

async function renderPdfPage(container) {
  const url = container.dataset.pdfUrl;
  if (!url || container.dataset.rendering === "1") return;
  container.dataset.rendering = "1";
  try {
    const pdf = await pdfjs();
    pdf.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
    if (!container._pdfPage) {
      const documentTask = pdf.getDocument({ url });
      const pdfDocument = await documentTask.promise;
      container._pdfPage = await pdfDocument.getPage(1);
    }
    const page = container._pdfPage;
    const baseViewport = page.getViewport({ scale: 1 });
    const width = Math.max(1, container.clientWidth || 760);
    const scale = width / baseViewport.width;
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("Could not create canvas context");
    const pixelRatio = Math.min((window.devicePixelRatio || 1) * TYPESET_PDF_RENDER_SCALE, 4);
    canvas.width = Math.ceil(viewport.width * pixelRatio);
    canvas.height = Math.ceil(viewport.height * pixelRatio);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    container.style.aspectRatio = `${viewport.width} / ${viewport.height}`;
    container.style.setProperty("--scale-factor", String(scale));
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    await page.render({ canvasContext: context, viewport }).promise;
    const textLayerElement = document.createElement("div");
    textLayerElement.className = "textLayer";
    textLayerElement.setAttribute("aria-label", "Selectable PDF text");
    container.classList.remove("is-loading", "is-error");
    container.textContent = "";
    container.appendChild(canvas);
    container.appendChild(textLayerElement);
    const textContent = await page.getTextContent({ includeMarkedContent: true });
    const textLayer = new pdf.TextLayer({
      textContentSource: textContent,
      container: textLayerElement,
      viewport,
    });
    await textLayer.render();
    await addCodeCopyButtons(page, container, viewport);
    container.dataset.renderedWidth = String(width);
    pdfPageResizeObserver?.observe(container);
  } catch (error) {
    container.classList.remove("is-loading");
    container.classList.add("is-error");
    container.innerHTML = `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PDF</a>`;
  } finally {
    delete container.dataset.rendering;
  }
}

const pdfPageResizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver((entries) => {
  for (const { target } of entries) {
    const renderedWidth = Number.parseFloat(target.dataset.renderedWidth || "0");
    if (!renderedWidth || Math.abs(target.clientWidth - renderedWidth) < 1) continue;
    window.clearTimeout(target._pdfResizeTimer);
    target._pdfResizeTimer = window.setTimeout(() => renderPdfPage(target), 120);
  }
});

function renderPdfPages(root) {
  return Promise.all(
    Array.from(root.querySelectorAll(".typeset-pdf-page"), renderPdfPage),
  );
}

function setNavigationCurrent(message) {
  els.conversation.querySelectorAll(".message.is-navigation-current").forEach((current) => {
    current.classList.remove("is-navigation-current");
    current.removeAttribute("aria-current");
  });
  if (!message) return;
  message.classList.add("is-navigation-current");
  message.setAttribute("aria-current", "true");
}

function messageLineId(message) {
  if (message?.dataset?.lineNo) return message.dataset.lineNo;
  const text = message?.querySelector(".message-header span")?.textContent || "";
  const match = text.match(/line\s+(\d+)/);
  return match ? match[1] : "";
}

function firstVisibleMessage() {
  const messages = Array.from(els.conversation.querySelectorAll(".message"));
  return messages.find((message) => message.getBoundingClientRect().bottom > 8) || null;
}

function captureScrollAnchor() {
  const message = firstVisibleMessage();
  if (!message) return null;
  return {
    lineNo: messageLineId(message),
    top: message.getBoundingClientRect().top,
    pageY: window.scrollY,
  };
}

function restoreScrollAnchor(anchor) {
  if (!anchor?.lineNo) {
    window.scrollTo(0, anchor?.pageY || 0);
    return;
  }
  const target = Array.from(els.conversation.querySelectorAll(".message"))
    .find((message) => messageLineId(message) === String(anchor.lineNo));
  if (!target) {
    window.scrollTo(0, anchor.pageY || 0);
    return;
  }
  window.scrollBy(0, target.getBoundingClientRect().top - anchor.top);
}

function nextAnimationFrame() {
  return new Promise((resolve) => requestAnimationFrame(resolve));
}

function scrollToMessageLine(lineNo) {
  if (!lineNo) return false;
  const target = Array.from(els.conversation.querySelectorAll(".message"))
    .find((message) => messageLineId(message) === String(lineNo));
  if (!target) return false;
  target.scrollIntoView({ block: "start" });
  return true;
}

function messageScrollMargin(message) {
  const margin = Number.parseFloat(window.getComputedStyle(message).scrollMarginTop);
  return Number.isFinite(margin) ? margin : 0;
}

function directionalMessageIndex(messages, direction, anchorTop) {
  const tolerance = 2;
  if (direction > 0) {
    return messages.findIndex(
      (message) => message.getBoundingClientRect().top > anchorTop + tolerance,
    );
  }
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].getBoundingClientRect().top < anchorTop - tolerance) return index;
  }
  return -1;
}

function jumpMessage(delta) {
  const messages = Array.from(els.conversation.querySelectorAll(".message"));
  if (!messages.length) return;
  const direction = delta < 0 ? -1 : 1;
  const anchorTop = messageScrollMargin(messages[0]);
  const targetIndex = directionalMessageIndex(messages, direction, anchorTop);
  const pageDistance = Math.max(1, window.innerHeight);
  if (targetIndex < 0) {
    messageNavigationIndex = -1;
    setNavigationCurrent(null);
    window.scrollBy({ top: direction * pageDistance, left: 0, behavior: "auto" });
    return;
  }

  const target = messages[targetIndex];
  const targetDistance = target.getBoundingClientRect().top - messageScrollMargin(target);
  if (Math.abs(targetDistance) <= pageDistance) {
    messageNavigationIndex = targetIndex;
    setNavigationCurrent(target);
    target.scrollIntoView({ block: "start" });
    return;
  }

  messageNavigationIndex = -1;
  setNavigationCurrent(null);
  const movement = direction * Math.min(Math.abs(targetDistance), pageDistance);
  window.scrollBy({ top: movement, left: 0, behavior: "auto" });
}

function scrollToLatestAssistant() {
  const assistants = els.conversation.querySelectorAll(".message.assistant");
  const messages = els.conversation.querySelectorAll(".message");
  const target = assistants[assistants.length - 1] || messages[messages.length - 1];
  if (!target) return;
  messageNavigationIndex = Array.from(messages).indexOf(target);
  setNavigationCurrent(target);
  target.scrollIntoView({ block: "start" });
}

function scrollToConversationTop() {
  const target = els.conversation.querySelector(".message");
  messageNavigationIndex = 0;
  setNavigationCurrent(target);
  els.conversation.scrollIntoView({ block: "start" });
}

function updateLoadButtons(data) {
  const isolated = data.isolatedLine !== null && data.isolatedLine !== undefined;
  const allShown = Boolean(data.all) || data.shownCount >= data.totalCount;
  if (els.loadEarlier) els.loadEarlier.disabled = isolated || allShown || loadingConversation;
  if (els.loadAll) els.loadAll.disabled = isolated || allShown || loadingConversation;
}

function fallbackCopyText(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("Browser rejected clipboard access");
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Older or restricted browsers may require the selection-based fallback.
    }
  }
  fallbackCopyText(text);
}

function resetCopyButton(button) {
  button.classList.remove("is-copied", "is-error");
  const label = button.dataset.copyLabel || "Copy Markdown";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.querySelector("span").textContent = "⧉";
}

async function copyRecordMarkdown(button) {
  const lineNo = String(button.dataset.copyLine || "");
  const record = lastConversationData?.records?.find(
    (candidate) => String(candidate.line_no) === lineNo,
  );
  if (!record) return;
  button.disabled = true;
  try {
    await copyText(record.text || "");
    button.classList.add("is-copied");
    button.title = "Markdown copied";
    button.setAttribute("aria-label", "Markdown copied");
    button.querySelector("span").textContent = "✓";
  } catch {
    button.classList.add("is-error");
    button.title = "Could not copy Markdown";
    button.setAttribute("aria-label", "Could not copy Markdown");
    button.querySelector("span").textContent = "!";
  } finally {
    button.disabled = false;
    window.setTimeout(() => resetCopyButton(button), 1600);
  }
}

async function copyRecordCode(button) {
  const lineNo = String(button.dataset.copyLine || "");
  const copyIndex = Number.parseInt(button.dataset.copyIndex || "", 10);
  const record = lastConversationData?.records?.find(
    (candidate) => String(candidate.line_no) === lineNo,
  );
  const code = record?.typeset?.codeBlocks?.[copyIndex];
  if (typeof code !== "string") return;
  button.disabled = true;
  try {
    await copyText(code);
    button.classList.add("is-copied");
    button.title = "Code copied";
    button.setAttribute("aria-label", "Code copied");
    button.querySelector("span").textContent = "✓";
  } catch {
    button.classList.add("is-error");
    button.title = "Could not copy code";
    button.setAttribute("aria-label", "Could not copy code");
    button.querySelector("span").textContent = "!";
  } finally {
    button.disabled = false;
    window.setTimeout(() => resetCopyButton(button), 1600);
  }
}

async function renderConversation(data, options = {}) {
  const previousData = lastConversationData;
  lastConversationData = data;
  typesetDebugEnabled = Boolean(data.typesetDebug);
  typesetHeaderMode = data.typesetHeaderMode || "external";
  messageNavigationIndex = -1;
  document.body.classList.toggle("typeset-debug-enabled", typesetDebugEnabled);
  document.body.classList.toggle(
    "typeset-debug-isolated",
    Boolean(data.typesetFresh),
  );
  const anchorLine = options.anchorLine || "";
  const session = data.session;
  document.title = `${session.title || "Codex Conversation"} · Typeset`;
  els.sessionTitle.textContent = session.title || "Untitled conversation";
  els.sessionMeta.textContent = session.cwd || session.path;
  if (data.isolatedLine !== null && data.isolatedLine !== undefined) {
    els.sessionMeta.textContent = data.typesetFresh
      ? `Typeset debug · assistant line ${data.isolatedLine}`
      : `Focused answer · assistant line ${data.isolatedLine}`;
  }
  const normalParams = identityParams("t");
  normalParams.set("tail", String(currentTail));
  if (showAllRecords) normalParams.set("all", "1");
  if (normalParams.has("id")) {
    const id = normalParams.get("id");
    normalParams.delete("id");
    els.normalViewLink.href = `/v/${encodeURIComponent(id)}?${normalParams}`;
  } else {
    els.normalViewLink.href = `/view?${normalParams}`;
  }
  const isolated = data.isolatedLine !== null && data.isolatedLine !== undefined;
  els.conversationInfo.textContent = (isolated
    ? [
        `assistant line ${data.isolatedLine}`,
        data.typesetFresh ? "fresh render on every refresh" : "cached typeset render",
      ]
    : [
        session.lastLabel ? `Last message ${session.lastLabel}` : "",
        `${data.shownCount || data.records.length} of ${data.totalCount || data.records.length} shown`,
        data.all ? "all records" : `typeset tail ${data.tail}`,
        data.includeTools ? "tool records included" : "",
      ]
  ).filter(Boolean).join(" · ");
  updateLoadButtons(data);

  if (!data.records.length) {
    setStatus("This session has no renderable messages.", "empty");
    return;
  }

  els.conversation.className = "conversation view-conversation typeset-conversation";
  if (
    options.preserveScroll
    && !focusedRoute().debug
    && extendsConversationContent(previousData, data)
  ) {
    const previousLength = previousData.records.length;
    const addedRecords = data.records.slice(previousLength);
    if (addedRecords.length) {
      const firstAddedIndex = els.conversation.querySelectorAll(".message").length;
      els.conversation.insertAdjacentHTML(
        "beforeend",
        addedRecords.map((record, index) =>
          renderRecord(record, previousLength + index, data.records)).join(""),
      );
      const messages = Array.from(els.conversation.querySelectorAll(".message"));
      const addedMessages = messages.slice(firstAddedIndex);
      messages.forEach((message, index) => {
        const isAssistant = recordClass(data.records[index]?.role) === "assistant";
        const previousIsAssistant = recordClass(data.records[index - 1]?.role) === "assistant";
        const nextIsAssistant = recordClass(data.records[index + 1]?.role) === "assistant";
        message.classList.toggle("assistant-continued", isAssistant && previousIsAssistant);
        message.classList.toggle("assistant-continuing", isAssistant && nextIsAssistant);
      });
      for (const message of addedMessages) applyMathAndCode(message);
      await Promise.all(addedMessages.map(renderPdfPages));
    }
  } else {
    els.conversation.innerHTML = data.records.map(renderRecord).join("");
    applyMathAndCode(els.conversation);
    await renderPdfPages(els.conversation);
  }
  await nextAnimationFrame();
  if (anchorLine) {
    if (!scrollToMessageLine(anchorLine)) restoreScrollAnchor(options.scrollAnchor);
  } else if (options.preserveScroll) {
    restoreScrollAnchor(options.scrollAnchor);
  } else {
    scrollToLatestAssistant();
  }
}

async function loadConversation(options = {}) {
  if (loadingConversation) return;
  const identity = identityParams("t");
  if (![...identity.keys()].length) {
    setStatus("No conversation path or id was provided.", "error");
    return;
  }
  const route = focusedRoute();
  const scrollAnchor = options.preserveScroll ? captureScrollAnchor() : null;
  const renderOptions = { ...options, scrollAnchor };
  loadingConversation = true;
  updateLoadButtons({ shownCount: 0, totalCount: 1, all: false });
  if (route.active && !lastConversationData) {
    showFocusedAnswerLoader(route.line, route.debug);
  }
  const apiParams = new URLSearchParams(identity);
  apiParams.set("tail", String(currentTail));
  if (renderOptions.scrollAnchor?.lineNo) {
    apiParams.set("anchor", renderOptions.scrollAnchor.lineNo);
  }
  if (options.preserveScroll) {
    const firstRendered = els.conversation.querySelector(".message");
    const firstRenderedLine = messageLineId(firstRendered);
    if (firstRenderedLine) apiParams.set("anchor", firstRenderedLine);
  }
  try {
    if (showAllRecords) apiParams.set("all", "1");
    if (params.get("tools") === "1") apiParams.set("tools", "1");
    if (params.get("header")) apiParams.set("header", params.get("header"));
    if (params.get("code")) apiParams.set("code", params.get("code"));
    if (route.line) apiParams.set("line", route.line);
    const apiPath = route.debug
      ? "/api/debug/typeset"
      : route.active
        ? "/api/typeset/answer"
        : "/api/typeset";
    const response = await fetch(`${apiPath}?${apiParams}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load typeset conversation");
    if (
      renderOptions.avoidUnchangedRender
      && !route.debug
      && sameConversationContent(lastConversationData, data)
    ) {
      lastConversationData = data;
      return;
    }
    await renderConversation(data, renderOptions);
  } finally {
    loadingConversation = false;
    if (lastConversationData) updateLoadButtons(lastConversationData);
  }
}

async function refreshConversation(options = {}) {
  if (loadingConversation) return;
  const jumpToLatest = Boolean(options.jumpToLatest);
  const previousTotal = Number(lastConversationData?.totalCount || 0);
  setRefreshState(true);
  try {
    await loadConversation({
      preserveScroll: !jumpToLatest,
      avoidUnchangedRender: true,
    });
    if (jumpToLatest) scrollToLatestAssistant();
    showRefreshResult(previousTotal);
  } catch (error) {
    showRefreshResult(previousTotal, true);
    throw error;
  } finally {
    setRefreshState(false);
  }
}

function loadEarlierConversation() {
  const anchor = firstVisibleMessage();
  const anchorLine = messageLineId(anchor);
  currentTail = Math.min(currentTail * 2, 100);
  return loadConversation({ anchorLine });
}

function loadAllConversation() {
  const anchor = firstVisibleMessage();
  const anchorLine = messageLineId(anchor);
  showAllRecords = true;
  return loadConversation({ anchorLine });
}

els.refreshConversation?.addEventListener("click", () => {
  refreshConversation().catch((error) => setStatus(error.message, "error"));
});
els.statusRefresh?.addEventListener("click", () => {
  refreshConversation().catch((error) => setStatus(error.message, "error"));
});
els.conversation?.addEventListener("click", (event) => {
  const markdownButton = event.target.closest?.(".typeset-copy-markdown");
  if (markdownButton) {
    copyRecordMarkdown(markdownButton);
    return;
  }
  const codeButton = event.target.closest?.(".typeset-code-copy");
  if (codeButton) copyRecordCode(codeButton);
});
els.jumpTop?.addEventListener("click", scrollToConversationTop);
els.statusbarToggle?.addEventListener("click", toggleStatusbar);
els.jumpLatest?.addEventListener("click", scrollToLatestAssistant);
els.jumpPrevious?.addEventListener("click", () => jumpMessage(-1));
els.jumpNext?.addEventListener("click", () => jumpMessage(1));
els.loadEarlier?.addEventListener("click", () => {
  loadEarlierConversation().catch((error) => setStatus(error.message, "error"));
});
els.loadAll?.addEventListener("click", () => {
  loadAllConversation().catch((error) => setStatus(error.message, "error"));
});

window.addEventListener("keydown", handleLinkHintKeyDown, true);
window.addEventListener("keyup", (event) => {
  if (event.key === "Shift" && !event.getModifierState("Shift")) clearLinkHints();
}, true);
window.addEventListener("blur", clearLinkHints);
window.addEventListener("resize", refreshVisibleLinkHints);
window.addEventListener("scroll", refreshVisibleLinkHints, true);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearLinkHints();
});

window.addEventListener("keydown", (event) => {
  const target = event.target;
  const isEditable = target?.closest?.("input, textarea, select, [contenteditable='true']");
  const key = event.key?.toLowerCase();
  const isRefreshKey = event.code === "KeyR" || event.key?.toLowerCase() === "r";
  const isPreviousKey = event.code === "ArrowLeft";
  const isNextKey = event.code === "ArrowRight";
  const isStatusbarKey = event.key === "/";
  const isActionKey = ["u", "l", "e", "a", "t", "c"].includes(key);
  if ((!isRefreshKey && !isPreviousKey && !isNextKey && !isStatusbarKey && !isActionKey) || event.metaKey || event.ctrlKey) return;
  if (isEditable && !event.altKey) return;
  event.preventDefault();
  if (isStatusbarKey) {
    toggleStatusbar();
  } else if (isRefreshKey) {
    refreshConversation({ jumpToLatest: event.shiftKey })
      .catch((error) => setStatus(error.message, "error"));
  } else if (isPreviousKey) {
    jumpMessage(-1);
  } else if (isNextKey) {
    jumpMessage(1);
  } else if (key === "u") {
    scrollToConversationTop();
  } else if (key === "l") {
    scrollToLatestAssistant();
  } else if (key === "e" && !els.loadEarlier?.disabled) {
    loadEarlierConversation().catch((error) => setStatus(error.message, "error"));
  } else if (key === "a" && !els.loadAll?.disabled) {
    loadAllConversation().catch((error) => setStatus(error.message, "error"));
  } else if (key === "t") {
    location.assign(els.normalViewLink.href);
  } else if (key === "c") {
    location.assign("/");
  }
}, true);

loadConversation().catch((error) => {
  setStatus(error.message, "error");
});
"""


def main(argv: list[str] | None = None, *, prog: str = "codex-viewer") -> int:
    args = parse_args(argv, prog=prog)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
