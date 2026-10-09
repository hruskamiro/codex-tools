"""Interactive terminal Agent Console."""

from __future__ import annotations

import argparse
import curses
import io
import re
import sys
import textwrap
import unicodedata
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from codex_tools import (
    agents_ui,
    paths,
    relay_cache,
    relay_transport,
    search,
)


MAX_RECORDS = 240
CTRL_LEFT_KEY = curses.KEY_MAX + 21
CTRL_RIGHT_KEY = curses.KEY_MAX + 22
MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[(?P<label>[^\]\n]+)\]\((?P<target>[^)\n]+)\)"
)
SEMANTIC_TOKEN_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<link>\[[^\]\n]+\]|https?://[^\s<>()]+)"
    r'|(?P<string>"(?:\\.|[^"\\])*"|(?<!\w)\'(?:\\.|[^\'\\])+\'(?!\w))'
    r"|(?P<number>(?<![\w])[-+]?\d+(?:[.,]\d+)*(?:%|ms|s|m|h|d|KiB|MiB|GiB)?(?![\w]))"
    r"|(?P<literal>\b(?:true|false|null|none)\b)",
    re.IGNORECASE,
)
TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


@dataclass(frozen=True)
class AgentItem:
    host_id: str
    host_name: str
    agent: dict[str, Any]
    base_url: str = ""


@dataclass
class ComposerBuffer:
    text: str = ""
    cursor: int = 0
    preferred_column: int | None = None

    def insert(self, value: str) -> None:
        self.text = self.text[: self.cursor] + value + self.text[self.cursor :]
        self.cursor += len(value)
        self.preferred_column = None

    def backspace(self) -> None:
        if self.cursor:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
        self.preferred_column = None

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
        self.preferred_column = None

    def move(self, offset: int) -> None:
        self.cursor = max(0, min(len(self.text), self.cursor + offset))
        self.preferred_column = None


def compact(value: str, width: int) -> str:
    value = " ".join(value.split())
    if width < 2:
        return value[: max(0, width)]
    return value if len(value) <= width else value[: width - 1] + "…"


def compact_path_end(value: str, width: int) -> str:
    """Fit complete trailing path components without cutting a directory name."""
    value = value.strip()
    if len(value) <= width:
        return value
    if width <= 0:
        return ""
    if width == 1:
        return "…"
    components = [part for part in re.split(r"[/\\]+", value) if part]
    suffix = ""
    for component in reversed(components):
        candidate = component if not suffix else f"{component}/{suffix}"
        if len(candidate) + 2 > width:
            break
        suffix = candidate
    return f"…/{suffix}" if suffix else "…"


def all_agent_items(
    cache: relay_cache.RelayCache, *, include_local: bool = True
) -> list[AgentItem]:
    result = []
    for host in cache.hosts():
        if not include_local and host["host_id"] == "local":
            continue
        for agent in cache.agents(str(host["host_id"])):
            result.append(
                AgentItem(
                    str(host["host_id"]),
                    str(host["name"]),
                    agent,
                    str(host.get("base_url") or ""),
                )
            )
    return result


def peer_name_for_item(
    config: dict[str, Any], item: AgentItem
) -> str | None:
    """Resolve a cached remote host back to its configured peer alias."""
    if item.host_id == "local" or not item.base_url:
        return None
    base_url = item.base_url.rstrip("/")
    for name, peer in config.get("peers", {}).items():
        if (
            isinstance(peer, dict)
            and str(peer.get("url") or "").rstrip("/") == base_url
        ):
            return str(name)
    return None


def sync_peer(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    peer_name: str,
) -> tuple[int, str, dict[str, Any]]:
    peer = config.get("peers", {}).get(peer_name)
    if not isinstance(peer, dict):
        raise ValueError(f"relay peer not found: {peer_name}")
    payload = relay_transport.fetch_snapshot(peer)
    count = relay_transport.apply_snapshot(
        cache, payload, base_url=str(peer["url"])
    )
    delivery = relay_transport.deliver_pending(
        cache, peer, str(payload["host"]["id"])
    )
    return count, str(payload["host"]["name"]), delivery


def sync_agent(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    peer_name: str,
    agent_id: str,
) -> tuple[str | None, dict[str, Any]]:
    peer = config.get("peers", {}).get(peer_name)
    if not isinstance(peer, dict):
        raise ValueError(f"relay peer not found: {peer_name}")
    try:
        payload = relay_transport.fetch_agent(peer, agent_id)
    except HTTPError as exc:
        if exc.code != HTTPStatus.NOT_FOUND:
            raise
        snapshot = relay_transport.fetch_snapshot(peer)
        remote_agent = next(
            (
                value
                for value in snapshot.get("agents", [])
                if isinstance(value, dict)
                and str(value.get("agent_id") or "") == agent_id
            ),
            None,
        )
        if remote_agent is None:
            delivery = relay_transport.deliver_pending(
                cache, peer, str(snapshot["host"]["id"])
            )
            return None, delivery
        payload = {
            "protocol": snapshot.get("protocol"),
            "host": snapshot.get("host"),
            "cursor": snapshot.get("cursor"),
            "agent": remote_agent,
        }
    relay_transport.apply_agent_snapshot(
        cache, payload, base_url=str(peer["url"])
    )
    delivery = relay_transport.deliver_pending(
        cache, peer, str(payload["host"]["id"])
    )
    return str(payload["host"]["name"]), delivery


def delivery_summary_text(delivery: dict[str, Any]) -> str:
    parts = []
    for state in ("delivered", "pending", "failed"):
        count = int(delivery.get(state) or 0)
        if count:
            parts.append(f"{count} {state}")
    return ", ".join(parts)


def outbox_message(
    cache: relay_cache.RelayCache, message_id: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in cache.outbox()
            if str(item["message_id"]) == message_id
        ),
        None,
    )


def agent_delivery_counts(
    messages: list[dict[str, Any]], host_id: str, agent_id: str
) -> tuple[int, int]:
    matching = [
        item
        for item in messages
        if str(item.get("host_id")) == host_id
        and str(item.get("agent_id")) == agent_id
    ]
    return (
        sum(item.get("state") == "pending" for item in matching),
        sum(item.get("state") == "failed" for item in matching),
    )


def open_agent_viewer(item: AgentItem) -> str:
    # Imported lazily because viewer reads the relay cache, whose local refresh
    # path imports the agents package.
    from codex_tools import viewer

    output = io.StringIO()
    state = viewer.running_daemon_state()
    if state is not None:
        base_url = str(state.get("url") or "").rstrip("/")
        if not viewer.viewer_source_is_current(base_url):
            with redirect_stdout(output), redirect_stderr(output):
                result = viewer.command_restart(viewer.default_server_args())
            if result:
                detail = output.getvalue().strip() or "viewer could not be restarted"
                raise RuntimeError(detail.splitlines()[0])
            state = viewer.running_daemon_state()
    if state is None:
        with redirect_stdout(output), redirect_stderr(output):
            result = viewer.command_start(viewer.default_server_args())
        if result:
            detail = output.getvalue().strip() or "viewer could not be started"
            raise RuntimeError(detail.splitlines()[0])
        state = viewer.running_daemon_state()
    if state is None:
        raise RuntimeError("viewer did not start")
    base_url = str(state.get("url") or "").rstrip("/")
    healthy, detail = viewer.viewer_health(base_url)
    if not healthy:
        raise RuntimeError(f"viewer is not responding: {detail}")
    agent_id = str(item.agent.get("agent_id") or "")
    if item.host_id == "local":
        session_id = str(
            item.agent.get("rollout_id")
            or item.agent.get("session_id")
            or item.agent.get("thread_id")
            or agent_id
        )
        url = viewer.view_url(base_url, "", 24, False, session_id, "markdown")
    else:
        url = base_url + viewer.relay_view_path(item.host_id, agent_id)
    browser_args = viewer.default_server_args()
    if not viewer.open_browser(url, browser_args):
        raise RuntimeError("browser could not be opened")
    return url


def filtered_items(items: list[AgentItem], query: str) -> list[AgentItem]:
    needle = query.strip().casefold()
    if not needle:
        return items
    return [
        item
        for item in items
        if needle
        in " ".join(
            (
                item.host_name,
                str(item.agent.get("title") or ""),
                str(item.agent.get("cwd") or ""),
                str(item.agent.get("agent_id") or ""),
            )
        ).casefold()
    ]


def restored_selection(items: list[AgentItem], previous: AgentItem | None) -> int:
    if previous is None:
        return 0
    previous_agent = str(previous.agent.get("agent_id") or "")
    for index, item in enumerate(items):
        if (
            item.host_id == previous.host_id
            and str(item.agent.get("agent_id") or "") == previous_agent
        ):
            return index
    return 0


def transcript_lines(
    records: list[dict[str, Any]], width: int
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    content_width = max(8, width - 4)
    renderable = [
        record
        for record in records
        if record.get("role") in {"user", "assistant"}
        and not (
            record.get("role") == "user"
            and search.is_internal_prompt_text(str(record.get("text") or ""))
        )
    ]
    for record in renderable[-MAX_RECORDS:]:
        role = str(record["role"])
        label = "› You" if role == "user" else "• Assistant"
        lines.append((label, f"{role}_label"))
        paragraphs = [
            display_markdown_links(line)
            for line in (str(record.get("text") or "").splitlines() or [""])
        ]
        index = 0
        while index < len(paragraphs):
            paragraph = paragraphs[index]
            if (
                index + 1 < len(paragraphs)
                and "|" in paragraph
                and TABLE_SEPARATOR_RE.match(paragraphs[index + 1])
            ):
                header = split_table_row(paragraph)
                separator = split_table_row(paragraphs[index + 1])
                index += 2
                rows: list[list[str]] = []
                while index < len(paragraphs) and "|" in paragraphs[index]:
                    rows.append(split_table_row(paragraphs[index]))
                    index += 1
                lines.extend(
                    format_terminal_table(
                        header,
                        separator,
                        rows,
                        content_width + 2,
                    )
                )
                continue
            wrapped = textwrap.wrap(
                paragraph,
                width=content_width,
                replace_whitespace=False,
                drop_whitespace=False,
                break_long_words=True,
                break_on_hyphens=True,
            ) or [""]
            lines.extend(("  " + line, role) for line in wrapped)
            index += 1
        lines.append(("", f"{role}_gap"))
    return lines


def split_table_row(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in line.strip():
        if escaped:
            if char != "|":
                current.append("\\")
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            in_code = not in_code
            current.append(char)
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def terminal_table_widths(rows: list[list[str]], width: int) -> list[int]:
    column_count = max((len(row) for row in rows), default=0)
    if not column_count:
        return []
    inner_width = max(column_count, width - (3 * column_count + 3))
    natural = [
        max((len(row[index]) if index < len(row) else 0 for row in rows), default=1)
        for index in range(column_count)
    ]
    minimum = [min(max(3, value), 12) for value in natural]
    while sum(minimum) > inner_width and any(value > 1 for value in minimum):
        widest = max(range(column_count), key=minimum.__getitem__)
        minimum[widest] -= 1
    widths = [max(floor, value) for floor, value in zip(minimum, natural)]
    while sum(widths) > inner_width:
        candidates = [
            index for index, value in enumerate(widths) if value > minimum[index]
        ]
        if not candidates:
            break
        widest = max(candidates, key=widths.__getitem__)
        widths[widest] -= 1
    return widths


def format_terminal_table(
    header: list[str],
    separator: list[str],
    rows: list[list[str]],
    width: int,
) -> list[tuple[str, str]]:
    column_count = max(len(header), len(separator), *(len(row) for row in rows))
    normalized = [
        (row + [""] * column_count)[:column_count]
        for row in [header, *rows]
    ]
    widths = terminal_table_widths(normalized, width)
    if not widths:
        return []
    alignments = [
        (
            "center"
            if cell.startswith(":") and cell.endswith(":")
            else "right" if cell.endswith(":") else "left"
        )
        for cell in (separator + ["---"] * column_count)[:column_count]
    ]

    def border(left: str, middle: str, right: str) -> str:
        return "  " + left + middle.join("─" * (value + 2) for value in widths) + right

    def rendered_row(cells: list[str], style: str) -> list[tuple[str, str]]:
        wrapped = [
            textwrap.wrap(
                cell,
                width=max(1, column_width),
                break_long_words=True,
                break_on_hyphens=True,
            )
            or [""]
            for cell, column_width in zip(cells, widths)
        ]
        height = max(len(cell_lines) for cell_lines in wrapped)
        result = []
        for row_index in range(height):
            pieces = []
            for column_index, column_width in enumerate(widths):
                value = (
                    wrapped[column_index][row_index]
                    if row_index < len(wrapped[column_index])
                    else ""
                )
                alignment = alignments[column_index]
                if alignment == "right":
                    value = value.rjust(column_width)
                elif alignment == "center":
                    value = value.center(column_width)
                else:
                    value = value.ljust(column_width)
                pieces.append(value)
            result.append(("  │ " + " │ ".join(pieces) + " │", style))
        return result

    output = [(border("┌", "┬", "┐"), "table_separator")]
    output.extend(rendered_row(normalized[0], "table_header"))
    output.append((border("├", "┼", "┤"), "table_separator"))
    for row in normalized[1:]:
        output.extend(rendered_row(row, "table_row"))
    output.append((border("└", "┴", "┘"), "table_separator"))
    return output


def add_text(window: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    if width <= 0:
        return
    try:
        window.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def semantic_spans(text: str) -> list[tuple[str, str | None]]:
    spans: list[tuple[str, str | None]] = []
    position = 0
    for match in SEMANTIC_TOKEN_RE.finditer(text):
        if match.start() > position:
            spans.append((text[position : match.start()], None))
        value = match.group(0)
        if match.lastgroup == "link" and value.startswith("["):
            value = value[1:-1]
        spans.append((value, str(match.lastgroup)))
        position = match.end()
    if position < len(text):
        spans.append((text[position:], None))
    return spans or [(text, None)]


def display_markdown_links(text: str) -> str:
    return MARKDOWN_LINK_RE.sub(lambda match: f"[{match.group('label')}]", text)


def table_spans(text: str) -> list[tuple[str, str | None]]:
    spans: list[tuple[str, str | None]] = []
    for part in re.split(r"([|│├┼┤┌┬┐└┴┘])", text):
        if part and part in "|│├┼┤┌┬┐└┴┘":
            spans.append((part, "table_border"))
        elif part:
            spans.extend(semantic_spans(part))
    return spans or [(text, None)]


def add_semantic_text(
    window: Any,
    y: int,
    x: int,
    text: str,
    width: int,
    base_attr: int,
    palette: dict[str, int],
    *,
    table: bool = False,
    syntax_prefix: str = "syntax_",
) -> None:
    remaining = width
    column = x
    spans = table_spans(text) if table else semantic_spans(text)
    for value, token in spans:
        if remaining <= 0:
            break
        shown = value[:remaining]
        add_text(
            window,
            y,
            column,
            shown,
            remaining,
            (
                palette.get(
                    token if token.startswith("table_") else f"{syntax_prefix}{token}",
                    base_attr,
                )
                if token
                else base_attr
            ),
        )
        column += len(shown)
        remaining -= len(shown)


def show_status(window: Any, text: str, attr: int) -> None:
    height, width = window.getmaxyx()
    try:
        window.move(height - 1, 0)
        window.clrtoeol()
    except curses.error:
        pass
    add_text(window, height - 1, 1, text, width - 2, attr)
    window.refresh()


def advance_interrupt(armed: bool, key: int) -> tuple[bool, bool]:
    if key == 3:  # Ctrl-C in curses raw mode.
        return True, armed
    return False, False


def mouse_scroll_delta(button_state: int, amount: int = 3) -> int:
    if button_state & curses.BUTTON4_PRESSED:
        return amount
    if button_state & curses.BUTTON5_PRESSED:
        return -amount
    return 0


def change_pane(
    focus: str, maximized: bool, key: int
) -> tuple[str, bool]:
    if key == curses.KEY_LEFT:
        if maximized:
            return "conversation", False
        return "agents", False
    if key == curses.KEY_RIGHT:
        if focus == "agents":
            return "conversation", False
        return "conversation", True
    if key == ord("m"):
        return "conversation", not maximized
    return focus, maximized


def soft_color_definitions(color_count: int) -> dict[str, tuple[int, int]]:
    if color_count >= 256:
        # Restrained xterm colors chosen to sit comfortably on the charcoal
        # backgrounds used by Codex and most dark terminal themes.
        warm_text = 252
        muted_text = 245
        soft_blue = 110
        soft_green = 114
        soft_gold = 180
        soft_coral = 174
        soft_lavender = 146
        user_background = 237
        selected_background = 238
        table_blue = 67
        return {
            "normal": (warm_text, -1),
            "muted": (muted_text, -1),
            "path": (muted_text, -1),
            "header": (warm_text, -1),
            "focus": (soft_blue, -1),
            "selected": (warm_text, selected_background),
            "active_dot": (soft_green, -1),
            "ready_dot": (muted_text, -1),
            "selected_active_dot": (soft_green, selected_background),
            "selected_ready_dot": (muted_text, selected_background),
            "divider": (238, -1),
            "composer": (warm_text, selected_background),
            "composer_accent": (soft_blue, selected_background),
            "composer_muted": (muted_text, selected_background),
            "user": (warm_text, user_background),
            "user_label": (soft_blue, user_background),
            "assistant": (warm_text, -1),
            "assistant_label": (soft_blue, -1),
            "status": (muted_text, -1),
            "syntax_code": (soft_lavender, -1),
            "syntax_string": (soft_green, -1),
            "syntax_number": (soft_blue, -1),
            "syntax_literal": (soft_gold, -1),
            "syntax_link": (soft_blue, -1),
            "user_syntax_code": (soft_lavender, user_background),
            "user_syntax_string": (soft_green, user_background),
            "user_syntax_number": (soft_blue, user_background),
            "user_syntax_literal": (soft_gold, user_background),
            "user_syntax_link": (soft_blue, user_background),
            "table_header": (soft_blue, -1),
            "table_separator": (table_blue, -1),
            "table_border": (table_blue, -1),
            "error": (soft_coral, -1),
        }
    return {
        "focus": (curses.COLOR_CYAN, -1),
        "selected": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "active_dot": (curses.COLOR_GREEN, -1),
        "ready_dot": (curses.COLOR_WHITE, -1),
        "selected_active_dot": (curses.COLOR_GREEN, curses.COLOR_BLACK),
        "selected_ready_dot": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "divider": (curses.COLOR_WHITE, -1),
        "composer": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "composer_accent": (curses.COLOR_CYAN, curses.COLOR_BLACK),
        "composer_muted": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "user": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "user_label": (curses.COLOR_CYAN, curses.COLOR_BLACK),
        "assistant_label": (curses.COLOR_CYAN, -1),
        "syntax_code": (curses.COLOR_MAGENTA, -1),
        "syntax_string": (curses.COLOR_GREEN, -1),
        "syntax_number": (curses.COLOR_CYAN, -1),
        "syntax_literal": (curses.COLOR_YELLOW, -1),
        "syntax_link": (curses.COLOR_CYAN, -1),
        "user_syntax_code": (curses.COLOR_MAGENTA, curses.COLOR_BLACK),
        "user_syntax_string": (curses.COLOR_GREEN, curses.COLOR_BLACK),
        "user_syntax_number": (curses.COLOR_CYAN, curses.COLOR_BLACK),
        "user_syntax_literal": (curses.COLOR_YELLOW, curses.COLOR_BLACK),
        "user_syntax_link": (curses.COLOR_CYAN, curses.COLOR_BLACK),
        "table_header": (curses.COLOR_CYAN, -1),
        "table_separator": (curses.COLOR_BLUE, -1),
        "table_border": (curses.COLOR_BLUE, -1),
        "error": (curses.COLOR_RED, -1),
    }


def colors() -> dict[str, int]:
    result = {
        "normal": curses.A_NORMAL,
        "muted": curses.A_DIM,
        "path": curses.A_DIM,
        "header": curses.A_BOLD,
        "focus": curses.A_BOLD,
        "selected": curses.A_BOLD,
        "active_dot": curses.A_BOLD,
        "ready_dot": curses.A_DIM,
        "selected_active_dot": curses.A_BOLD,
        "selected_ready_dot": curses.A_DIM,
        "divider": curses.A_DIM,
        "composer": curses.A_NORMAL,
        "composer_accent": curses.A_BOLD,
        "composer_muted": curses.A_DIM,
        "user": curses.A_NORMAL,
        "user_label": curses.A_BOLD,
        "assistant": curses.A_NORMAL,
        "assistant_label": curses.A_BOLD,
        "status": curses.A_DIM,
        "syntax_code": curses.A_NORMAL,
        "syntax_string": curses.A_NORMAL,
        "syntax_number": curses.A_NORMAL,
        "syntax_literal": curses.A_NORMAL,
        "syntax_link": curses.A_UNDERLINE,
        "user_syntax_code": curses.A_NORMAL,
        "user_syntax_string": curses.A_NORMAL,
        "user_syntax_number": curses.A_NORMAL,
        "user_syntax_literal": curses.A_NORMAL,
        "user_syntax_link": curses.A_UNDERLINE,
        "table_header": curses.A_BOLD,
        "table_separator": curses.A_DIM,
        "table_border": curses.A_DIM,
        "error": curses.A_BOLD,
    }
    if not curses.has_colors():
        return result
    curses.start_color()
    curses.use_default_colors()
    definitions = soft_color_definitions(curses.COLORS)
    for index, (name, pair) in enumerate(definitions.items(), 1):
        try:
            curses.init_pair(index, *pair)
            result[name] = curses.color_pair(index)
        except curses.error:
            pass
    result["header"] |= curses.A_BOLD
    result["focus"] |= curses.A_BOLD
    result["selected"] |= curses.A_BOLD
    result["active_dot"] |= curses.A_BOLD
    result["selected_active_dot"] |= curses.A_BOLD
    result["user_label"] |= curses.A_BOLD
    result["assistant_label"] |= curses.A_BOLD
    result["syntax_link"] |= curses.A_UNDERLINE
    result["user_syntax_link"] |= curses.A_UNDERLINE
    result["table_header"] |= curses.A_BOLD
    result["table_separator"] |= curses.A_DIM
    result["table_border"] |= curses.A_DIM
    result["ready_dot"] |= curses.A_DIM
    result["selected_ready_dot"] |= curses.A_DIM
    result["divider"] |= curses.A_DIM
    result["composer_accent"] |= curses.A_BOLD
    result["composer_muted"] |= curses.A_DIM
    result["error"] |= curses.A_BOLD
    if curses.COLORS < 256:
        result["path"] |= curses.A_DIM
    return result


def character_cell_width(value: str) -> int:
    if not value or unicodedata.combining(value):
        return 0
    return 2 if unicodedata.east_asian_width(value) in {"W", "F"} else 1


def composer_layout(
    text: str, width: int
) -> tuple[list[str], list[tuple[int, int]]]:
    width = max(1, width)
    lines = [""]
    positions = [(0, 0)] * (len(text) + 1)
    row = 0
    column = 0
    for index, value in enumerate(text):
        positions[index] = (row, column)
        if value == "\n":
            row += 1
            column = 0
            lines.append("")
            positions[index + 1] = (row, column)
            continue
        cell_width = character_cell_width(value)
        if column and column + cell_width > width:
            row += 1
            column = 0
            lines.append("")
            positions[index] = (row, column)
        lines[row] += value
        column += cell_width
        positions[index + 1] = (row, column)
    return lines, positions


def move_composer_vertical(
    buffer: ComposerBuffer, width: int, direction: int
) -> None:
    _, positions = composer_layout(buffer.text, width)
    row, column = positions[buffer.cursor]
    preferred = buffer.preferred_column
    if preferred is None:
        preferred = column
    target_row = row + direction
    candidates = [
        (abs(candidate_column - preferred), index)
        for index, (candidate_row, candidate_column) in enumerate(positions)
        if candidate_row == target_row
    ]
    if not candidates:
        return
    _, buffer.cursor = min(candidates, key=lambda item: (item[0], -item[1]))
    buffer.preferred_column = preferred


def move_composer_line_edge(
    buffer: ComposerBuffer, width: int, *, end: bool
) -> None:
    _, positions = composer_layout(buffer.text, width)
    row, _ = positions[buffer.cursor]
    candidates = [
        index
        for index, (candidate_row, _) in enumerate(positions)
        if candidate_row == row
    ]
    if candidates:
        buffer.cursor = max(candidates) if end else min(candidates)
    buffer.preferred_column = None


def move_composer_word(buffer: ComposerBuffer, direction: int) -> None:
    """Move to the previous or next word boundary."""
    cursor = buffer.cursor
    if direction < 0:
        while cursor and not (
            buffer.text[cursor - 1].isalnum() or buffer.text[cursor - 1] == "_"
        ):
            cursor -= 1
        while cursor and (
            buffer.text[cursor - 1].isalnum() or buffer.text[cursor - 1] == "_"
        ):
            cursor -= 1
    else:
        length = len(buffer.text)
        while cursor < length and (
            buffer.text[cursor].isalnum() or buffer.text[cursor] == "_"
        ):
            cursor += 1
        while cursor < length and not (
            buffer.text[cursor].isalnum() or buffer.text[cursor] == "_"
        ):
            cursor += 1
    buffer.cursor = cursor
    buffer.preferred_column = None


def read_bracketed_paste(stdscr: Any) -> str:
    content = ""
    marker = "\x1b[201~"
    while True:
        key = stdscr.get_wch()
        if isinstance(key, str):
            content += key
            if content.endswith(marker):
                return content[: -len(marker)].replace("\r\n", "\n").replace(
                    "\r", "\n"
                )


def escape_action(stdscr: Any) -> tuple[str, str]:
    """Distinguish Escape, modified arrows, Alt-Enter, and paste."""
    try:
        stdscr.timeout(40)
        following = stdscr.get_wch()
    except curses.error:
        return "cancel", ""
    finally:
        stdscr.timeout(-1)
    if following in ("\n", "\r", curses.KEY_ENTER):
        return "newline", ""
    if following == "[":
        suffix = ""
        try:
            stdscr.timeout(20)
            for _ in range(8):
                part = stdscr.get_wch()
                if not isinstance(part, str):
                    return "cancel", ""
                suffix += part
                if part.isalpha() or part == "~":
                    break
        except curses.error:
            return "cancel", ""
        finally:
            stdscr.timeout(-1)
        if suffix == "200~":
            return "paste", read_bracketed_paste(stdscr)
        if suffix in {"1;5D", "5D"}:
            return "word_left", ""
        if suffix in {"1;5C", "5C"}:
            return "word_right", ""
        if suffix == "1;2D":
            return "agents", ""
    return "cancel", ""


def key_code(key: Any) -> int | None:
    if isinstance(key, int):
        return key
    if isinstance(key, str) and len(key) == 1:
        return ord(key)
    return None


def edit_composer_buffer(buffer: ComposerBuffer, key: Any, width: int) -> str | None:
    """Apply an editing key; return ``send`` when the draft should be sent."""
    code = key_code(key)
    if key == "\r" or code == curses.KEY_ENTER:
        return "send"
    if key == "\n" or code == 10:  # Ctrl-J in raw terminal mode.
        buffer.insert("\n")
    elif code in (8, 127, curses.KEY_BACKSPACE):
        buffer.backspace()
    elif code == curses.KEY_DC:
        buffer.delete()
    elif code == curses.KEY_LEFT:
        buffer.move(-1)
    elif code == curses.KEY_RIGHT:
        buffer.move(1)
    elif code == CTRL_LEFT_KEY:
        move_composer_word(buffer, -1)
    elif code == CTRL_RIGHT_KEY:
        move_composer_word(buffer, 1)
    elif code in (curses.KEY_UP, 16):  # Up / Ctrl-P
        move_composer_vertical(buffer, width, -1)
    elif code in (curses.KEY_DOWN, 14):  # Down / Ctrl-N
        move_composer_vertical(buffer, width, 1)
    elif code in (curses.KEY_HOME, 1):
        move_composer_line_edge(buffer, width, end=False)
    elif code in (curses.KEY_END, 5):
        move_composer_line_edge(buffer, width, end=True)
    elif code == 21:
        buffer.text = buffer.text[buffer.cursor :]
        buffer.cursor = 0
        buffer.preferred_column = None
    elif code == 11:
        buffer.text = buffer.text[: buffer.cursor]
        buffer.preferred_column = None
    elif code == 23:
        start = buffer.cursor
        while start and buffer.text[start - 1].isspace():
            start -= 1
        while start and not buffer.text[start - 1].isspace():
            start -= 1
        buffer.text = buffer.text[:start] + buffer.text[buffer.cursor :]
        buffer.cursor = start
        buffer.preferred_column = None
    elif code == 15:
        buffer.insert("\n")
    elif key == "\t":
        buffer.insert("  ")
    elif isinstance(key, str) and key.isprintable():
        buffer.insert(key)
    return None


def draw_composer(
    stdscr: Any,
    palette: dict[str, int],
    buffer: ComposerBuffer,
    x: int,
    pane_width: int,
    *,
    focused: bool,
) -> tuple[int, int]:
    """Draw the persistent composer and return its top row and inner width."""
    height, terminal_width = stdscr.getmaxyx()
    left = max(0, min(x + 1, terminal_width - 4))
    available_width = max(4, min(pane_width - 2, terminal_width - left))
    inner_x = left + 2
    inner_width = max(1, available_width - 4)
    lines, positions = composer_layout(buffer.text, inner_width)
    cursor_row, cursor_column = positions[buffer.cursor]
    visible_rows = max(1, min(8, height // 3, len(lines)))
    first_line = max(0, cursor_row - visible_rows + 1)
    panel_height = visible_rows
    top = max(3, height - panel_height)

    for row in range(top, height):
        add_text(
            stdscr,
            row,
            left,
            " " * available_width,
            available_width,
            palette["composer"],
        )
    visible_lines = lines[first_line : first_line + visible_rows]
    for offset, line in enumerate(visible_lines):
        add_text(
            stdscr,
            top + offset,
            inner_x,
            "›" if offset == 0 else " ",
            1,
            palette["composer_accent" if focused else "composer_muted"],
        )
        add_text(
            stdscr,
            top + offset,
            inner_x + 2,
            line,
            inner_width,
            palette["composer"],
        )
    if focused:
        try:
            stdscr.move(
                top + cursor_row - first_line,
                inner_x + 2 + min(cursor_column, inner_width - 1),
            )
        except curses.error:
            pass
    return top, inner_width


def edit_line(stdscr: Any, prompt: str, initial: str = "") -> str | None:
    value = list(initial)
    curses.curs_set(1)
    try:
        while True:
            height, width = stdscr.getmaxyx()
            y = height - 2
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            shown = prompt + "".join(value)
            add_text(stdscr, y, 1, shown, width - 2, curses.A_BOLD)
            try:
                stdscr.move(y, min(width - 2, 1 + len(shown)))
            except curses.error:
                pass
            stdscr.refresh()
            key = stdscr.get_wch()
            if key in ("\n", "\r"):
                return "".join(value).strip()
            if key == "\x03":
                return None
            if key == "\x1b":
                return None
            if key in ("\b", "\x7f") or key == curses.KEY_BACKSPACE:
                if value:
                    value.pop()
            elif key == "\x15":
                value.clear()
            elif isinstance(key, str) and key.isprintable():
                value.append(key)
    finally:
        curses.curs_set(0)


def run_console(
    cache: relay_cache.RelayCache,
    sessions_root: Path,
    session_index: Path,
    codex_bin: str,
    control_socket: Path | None = None,
    *,
    include_local: bool = True,
    relay_config: Path = paths.RELAY_CONFIG_FILE,
) -> int:
    def run(stdscr: Any) -> int:
        curses.curs_set(0)
        curses.raw()
        stdscr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        palette = colors()
        items = all_agent_items(cache, include_local=include_local)
        query = ""
        selected = 0
        scroll = 0
        focus = "conversation"
        maximized = False
        status = ""
        interrupt_armed = False
        drafts: dict[tuple[str, str], ComposerBuffer] = {}
        outgoing = cache.outbox(states=("pending", "failed"))

        def finish() -> int:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            try:
                curses.mousemask(0)
            except curses.error:
                pass
            curses.noraw()
            return 0

        while True:
            shown = filtered_items(items, query)
            selected = max(0, min(selected, len(shown) - 1)) if shown else 0
            current = shown[selected] if shown else None
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            if height < 10 or width < 60:
                add_text(stdscr, 1, 2, "Agent Console needs a terminal at least 60×10.", width - 4, palette["status"])
                stdscr.refresh()
                key = stdscr.getch()
                interrupt_armed, should_exit = advance_interrupt(
                    interrupt_armed, key
                )
                if should_exit:
                    return finish()
                if key == 3:
                    status = "Press Ctrl-C again to quit."
                continue

            sidebar = 0 if maximized else min(36, max(25, width // 3))
            main_x = 0 if maximized else sidebar + 1
            main_width = width - main_x
            if not maximized:
                sidebar_heading = palette["focus"] if focus == "agents" else palette["header"]
                add_text(stdscr, 0, 1, "Agent Console", sidebar - 2, sidebar_heading)
                working = sum(item.agent.get("status") == "active" for item in items)
                ready = sum(item.agent.get("status") == "idle" for item in items)
                add_text(
                    stdscr, 1, 1, f"{working} working · {ready} ready",
                    sidebar - 2, palette["muted"],
                )
                if query:
                    add_text(stdscr, 2, 1, f"filter: {query}", sidebar - 2, palette["status"])
                try:
                    stdscr.vline(
                        0,
                        sidebar,
                        curses.ACS_VLINE,
                        height - 1,
                        palette["divider"],
                    )
                except curses.error:
                    pass

                row = 3
                previous_host = None
                visible_start = max(0, selected - max(0, (height - 6) // 3))
                for index, item in enumerate(shown[visible_start:], visible_start):
                    if row >= height - 2:
                        break
                    if item.host_id != previous_host:
                        add_text(stdscr, row, 1, compact(item.host_name, sidebar - 3), sidebar - 2, palette["muted"] | curses.A_BOLD)
                        row += 1
                        previous_host = item.host_id
                    if row >= height - 2:
                        break
                    is_selected = index == selected
                    selected_marker = "›" if is_selected else " "
                    selected_focused = is_selected and focus == "agents"
                    attr = (
                        palette["selected"]
                        if selected_focused
                        else palette["header"]
                        if is_selected
                        else palette["normal"]
                    )
                    title = str(item.agent.get("title") or item.agent.get("agent_id") or "unnamed")
                    pending, failed = agent_delivery_counts(
                        outgoing,
                        item.host_id,
                        str(item.agent.get("agent_id") or ""),
                    )
                    if pending:
                        title += f" · {pending} queued"
                    if failed:
                        title += f" · {failed} failed"
                    active = item.agent.get("status") == "active"
                    marker = "●" if active else "○"
                    marker_attr = palette[
                        ("selected_" if selected_focused else "")
                        + ("active_dot" if active else "ready_dot")
                    ]
                    if selected_focused:
                        add_text(
                            stdscr,
                            row,
                            1,
                            " " * (sidebar - 2),
                            sidebar - 2,
                            palette["selected"],
                        )
                    add_text(
                        stdscr,
                        row,
                        1,
                        selected_marker,
                        1,
                        attr,
                    )
                    add_text(stdscr, row, 2, marker, 1, marker_attr)
                    add_text(
                        stdscr,
                        row,
                        3,
                        " " + compact(title, sidebar - 6),
                        sidebar - 4,
                        attr,
                    )
                    row += 1
                    cwd = str(item.agent.get("cwd") or "").strip()
                    if cwd and row < height - 2:
                        add_text(
                            stdscr,
                            row,
                            4,
                            compact_path_end(cwd, sidebar - 6),
                            sidebar - 5,
                            palette["path"],
                        )
                        row += 1

            if current:
                agent = current.agent
                draft_key = (current.host_id, str(agent["agent_id"]))
                draft = drafts.setdefault(draft_key, ComposerBuffer())
                title = str(agent.get("title") or agent.get("agent_id"))
                conversation_heading = (
                    palette["focus"]
                    if focus == "conversation"
                    else palette["header"]
                )
                add_text(
                    stdscr,
                    0,
                    main_x + 2,
                    title,
                    main_width - 4,
                    conversation_heading,
                )
                detail = f"{agent.get('status_label') or agent.get('status') or 'unknown'} · {current.host_name} · {agent.get('cwd') or 'no basedir'} · {agent.get('agent_id')}"
                if status:
                    detail = status
                add_text(stdscr, 1, main_x + 2, detail, main_width - 4, palette["muted"])
                records = cache.records(current.host_id, str(agent["agent_id"]))
                rendered = transcript_lines(records, main_width - 2)
                composer_width = max(1, main_width - 6)
                composer_lines, _ = composer_layout(draft.text, composer_width)
                composer_rows = max(1, min(8, height // 3, len(composer_lines)))
                composer_top = max(3, height - composer_rows)
                viewport = max(0, composer_top - 3)
                max_scroll = max(0, len(rendered) - viewport)
                scroll = min(scroll, max_scroll)
                start = max(0, len(rendered) - viewport - scroll)
                for offset, (line, style) in enumerate(rendered[start : start + viewport]):
                    y = 3 + offset
                    if style in {"user", "user_label"}:
                        add_text(
                            stdscr,
                            y,
                            main_x + 1,
                            " " * (main_width - 2),
                            main_width - 2,
                            palette["user"],
                        )
                    if style == "user_gap":
                        continue
                    if style in {"user", "assistant", "table_header", "table_row"}:
                        add_semantic_text(
                            stdscr,
                            y,
                            main_x + 2,
                            line,
                            main_width - 4,
                            palette.get(style, 0),
                            palette,
                            table=style.startswith("table_"),
                            syntax_prefix=(
                                "user_syntax_" if style == "user" else "syntax_"
                            ),
                        )
                    else:
                        add_text(
                            stdscr,
                            y,
                            main_x + 2,
                            line,
                            main_width - 4,
                            palette.get(style, 0),
                        )
                if not rendered:
                    add_text(stdscr, 4, main_x + 2, "No renderable messages.", main_width - 4, palette["muted"])
                draw_composer(
                    stdscr,
                    palette,
                    draft,
                    main_x,
                    main_width,
                    focused=focus == "conversation",
                )
            else:
                empty = (
                    "No synchronized remote agents. Run `ct relay sync PEER`."
                    if not include_local
                    else "No cached agents."
                )
                add_text(
                    stdscr,
                    3,
                    main_x + 2,
                    empty,
                    main_width - 4,
                    palette["muted"],
                )

            if not current:
                footer = status or "No agent selected · ^F find · ^S sync all · ^C×2 quit"
                add_text(stdscr, height - 1, 1, footer, width - 2, palette["status"])
            try:
                curses.curs_set(1 if current and focus == "conversation" else 0)
            except curses.error:
                pass
            stdscr.refresh()
            status = ""
            key = stdscr.get_wch()
            code = key_code(key)
            interrupt_armed, should_exit = advance_interrupt(
                interrupt_armed, code if code is not None else -1
            )
            if should_exit:
                return finish()
            if code == 3:
                status = "Press Ctrl-C again to quit."
                continue
            if code == curses.KEY_MOUSE and shown:
                try:
                    _, _, _, _, button_state = curses.getmouse()
                except curses.error:
                    button_state = 0
                delta = mouse_scroll_delta(button_state)
                if delta:
                    focus = "conversation"
                    scroll = max(0, min(max_scroll, scroll + delta))
            elif code == curses.KEY_RIGHT and focus == "agents":
                focus = "conversation"
            elif code == curses.KEY_SLEFT and focus == "conversation":
                focus = "agents"
            elif code == curses.KEY_LEFT and focus == "conversation" and current:
                draft = drafts[(current.host_id, str(current.agent["agent_id"]))]
                if draft.cursor == 0:
                    focus = "agents"
                else:
                    draft.move(-1)
            elif code in (curses.KEY_UP, curses.KEY_DOWN) and shown:
                if focus == "agents":
                    selected = max(
                        0,
                        min(
                            len(shown) - 1,
                            selected + (-1 if code == curses.KEY_UP else 1),
                        ),
                    )
                    scroll = 0
                elif current:
                    draft = drafts[(current.host_id, str(current.agent["agent_id"]))]
                    move_composer_vertical(
                        draft,
                        max(1, main_width - 6),
                        -1 if code == curses.KEY_UP else 1,
                    )
            elif code == curses.KEY_PPAGE:
                focus = "conversation"
                scroll += max(1, height - 7)
            elif code == curses.KEY_NPAGE:
                focus = "conversation"
                scroll = max(0, scroll - max(1, height - 7))
            elif code == 6:  # Ctrl-F
                entered = edit_line(stdscr, "Find: ", query)
                if entered is not None:
                    query = entered; selected = 0; scroll = 0
                    focus = "agents"
            elif code == 22 and current:  # Ctrl-V
                show_status(stdscr, "Opening viewer…", palette["status"])
                try:
                    open_agent_viewer(current)
                    status = "Opened conversation in viewer."
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Viewer failed: {exc}"
            elif code == 24:  # Ctrl-X
                focus, maximized = change_pane(focus, maximized, ord("m"))
            elif code == 18:  # Ctrl-R
                show_status(stdscr, "Refreshing conversation…", palette["status"])
                try:
                    if current is None:
                        status = "No conversation selected."
                    elif current.host_id == "local":
                        refreshed = relay_cache.refresh_local_agent(
                            cache,
                            str(current.agent["agent_id"]),
                            sessions_root,
                            session_index,
                            control_socket,
                        )
                        status = f"Refreshed {refreshed.title}."
                    else:
                        config = relay_transport.load_config(relay_config)
                        peer_name = peer_name_for_item(config, current)
                        if peer_name is None:
                            raise ValueError(
                                f"no configured peer matches {current.host_name}"
                            )
                        host_name, delivery = sync_agent(
                            cache,
                            config,
                            peer_name,
                            str(current.agent["agent_id"]),
                        )
                        status = (
                            f"Refreshed conversation from {host_name}."
                            if host_name
                            else "Remote session is no longer active; showing cached copy."
                        )
                        delivery_text = delivery_summary_text(delivery)
                        if delivery_text:
                            status += f" Messages: {delivery_text}."
                    items = all_agent_items(
                        cache, include_local=include_local
                    )
                    outgoing = cache.outbox(states=("pending", "failed"))
                    selected = restored_selection(
                        filtered_items(items, query), current
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Refresh failed: {exc}"
            elif code == 19:  # Ctrl-S
                show_status(stdscr, "Refreshing everything…", palette["status"])
                try:
                    failures = []
                    local_count = None
                    if include_local:
                        try:
                            local_count = relay_cache.refresh_local(
                                cache,
                                sessions_root,
                                session_index,
                                control_socket,
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            failures.append(f"local: {exc}")
                    if relay_config.expanduser().exists():
                        config = relay_transport.load_config(relay_config)
                        peer_names = sorted(config["peers"])
                    else:
                        config = {"peers": {}}
                        peer_names = []
                    synchronized = 0
                    agents_count = 0
                    delivered_count = 0
                    pending_count = 0
                    failed_count = 0
                    for peer_name in peer_names:
                        try:
                            count, _, delivery = sync_peer(
                                cache, config, peer_name
                            )
                            synchronized += 1
                            agents_count += count
                            delivered_count += int(delivery["delivered"])
                            pending_count += int(delivery["pending"])
                            failed_count += int(delivery["failed"])
                        except (OSError, RuntimeError, ValueError) as exc:
                            failures.append(f"{peer_name}: {exc}")
                    parts = []
                    if local_count is not None:
                        parts.append(f"{local_count} local agents")
                    if peer_names:
                        parts.append(
                            f"{synchronized}/{len(peer_names)} peers "
                            f"({agents_count} agents)"
                        )
                    if not parts:
                        parts.append("nothing configured")
                    status = "Refreshed " + ", ".join(parts) + "."
                    delivery_text = delivery_summary_text(
                        {
                            "delivered": delivered_count,
                            "pending": pending_count,
                            "failed": failed_count,
                        }
                    )
                    if delivery_text:
                        status += f" Messages: {delivery_text}."
                    if failures:
                        status += " Failed: " + "; ".join(failures)
                    items = all_agent_items(
                        cache, include_local=include_local
                    )
                    outgoing = cache.outbox(states=("pending", "failed"))
                    selected = restored_selection(
                        filtered_items(items, query), current
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Synchronization failed: {exc}"
            elif current and focus == "conversation":
                draft_key = (current.host_id, str(current.agent["agent_id"]))
                draft = drafts.setdefault(draft_key, ComposerBuffer())
                if code == 27:
                    action, pasted = escape_action(stdscr)
                    if action == "paste":
                        draft.insert(pasted)
                    elif action == "newline":
                        draft.insert("\n")
                    elif action == "word_left":
                        move_composer_word(draft, -1)
                    elif action == "word_right":
                        move_composer_word(draft, 1)
                    elif action == "agents":
                        focus = "agents"
                    else:
                        focus = "agents"
                    continue
                action = edit_composer_buffer(
                    draft, key, max(1, main_width - 6)
                )
                if action == "send" and draft.text.strip():
                    message = draft.text.strip()
                    try:
                        result = agents_ui.send_message(
                            cache,
                            {
                                "host": current.host_id,
                                "agent": current.agent["agent_id"],
                                "message": message,
                            },
                            codex_bin,
                        )
                        drafts[draft_key] = ComposerBuffer()
                        status = (
                            "Message sent."
                            if result["delivery"] == "sent"
                            else "Message queued for synchronization."
                        )
                        if result["delivery"] == "queued":
                            try:
                                config = relay_transport.load_config(relay_config)
                                peer_name = peer_name_for_item(config, current)
                                if peer_name is not None:
                                    peer = config["peers"][peer_name]
                                    relay_transport.deliver_pending(
                                        cache, peer, current.host_id
                                    )
                                queued = outbox_message(
                                    cache, result["message_id"]
                                )
                                if queued and queued["state"] == "delivered":
                                    status = "Message delivered to remote Codex queue."
                                elif queued and queued["state"] == "failed":
                                    status = (
                                        "Remote delivery failed: "
                                        + str(queued.get("last_error") or "unknown error")
                                    )
                                else:
                                    status = (
                                        "Message safely queued offline; "
                                        "Ctrl-R or Ctrl-S will retry."
                                    )
                            except (OSError, RuntimeError, ValueError):
                                status = (
                                    "Message safely queued offline; "
                                    "Ctrl-R or Ctrl-S will retry."
                                )
                            outgoing = cache.outbox(
                                states=("pending", "failed")
                            )
                    except (OSError, RuntimeError, ValueError) as exc:
                        status = f"Send failed: {exc}"
        return 0

    return int(curses.wrapper(run))


def command_ui(args: argparse.Namespace) -> int:
    cache = relay_cache.RelayCache(args.cache_file)
    no_local = getattr(args, "no_local", False)
    if not args.no_refresh and not no_local:
        count = relay_cache.refresh_local(
            cache,
            args.sessions_root,
            args.session_index,
            getattr(args, "control_socket", None),
        )
        print(f"Refreshed {count} local agent session(s).")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("error: Agent Console requires an interactive terminal", file=sys.stderr)
        return 2
    try:
        return run_console(
            cache,
            args.sessions_root,
            args.session_index,
            args.codex_bin,
            getattr(args, "control_socket", None),
            include_local=not no_local,
            relay_config=getattr(
                args, "relay_config", paths.RELAY_CONFIG_FILE
            ),
        )
    except curses.error as exc:
        print(f"error: could not start Agent Console: {exc}", file=sys.stderr)
        return 1
