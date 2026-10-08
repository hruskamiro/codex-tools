"""Interactive terminal Agent Console."""

from __future__ import annotations

import argparse
import curses
import io
import re
import sys
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_tools import (
    agents_ui,
    paths,
    relay_cache,
    relay_transport,
    search,
)


MAX_RECORDS = 240
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
) -> tuple[int, str]:
    peer = config.get("peers", {}).get(peer_name)
    if not isinstance(peer, dict):
        raise ValueError(f"relay peer not found: {peer_name}")
    payload = relay_transport.fetch_snapshot(peer)
    count = relay_transport.apply_snapshot(
        cache, payload, base_url=str(peer["url"])
    )
    return count, str(payload["host"]["name"])


def sync_agent(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    peer_name: str,
    agent_id: str,
) -> str:
    peer = config.get("peers", {}).get(peer_name)
    if not isinstance(peer, dict):
        raise ValueError(f"relay peer not found: {peer_name}")
    payload = relay_transport.fetch_agent(peer, agent_id)
    relay_transport.apply_agent_snapshot(
        cache, payload, base_url=str(peer["url"])
    )
    return str(payload["host"]["name"])


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


def colors() -> dict[str, int]:
    result = {
        "normal": curses.A_NORMAL,
        "muted": curses.A_DIM,
        "path": curses.A_DIM,
        "header": curses.A_BOLD,
        "focus": curses.A_BOLD,
        "selected": curses.A_BOLD | curses.A_UNDERLINE,
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
    }
    if not curses.has_colors():
        return result
    curses.start_color()
    curses.use_default_colors()
    user_background = 237 if curses.COLORS >= 256 else curses.COLOR_BLUE
    path_foreground = 245 if curses.COLORS >= 256 else curses.COLOR_WHITE
    definitions = {
        "focus": (curses.COLOR_CYAN, -1),
        "path": (path_foreground, -1),
        "user": (curses.COLOR_WHITE, user_background),
        "user_label": (curses.COLOR_WHITE, user_background),
        "assistant_label": (curses.COLOR_CYAN, -1),
        "syntax_code": (curses.COLOR_MAGENTA, -1),
        "syntax_string": (curses.COLOR_GREEN, -1),
        "syntax_number": (curses.COLOR_CYAN, -1),
        "syntax_literal": (curses.COLOR_YELLOW, -1),
        "syntax_link": (curses.COLOR_CYAN, -1),
        "user_syntax_code": (curses.COLOR_MAGENTA, user_background),
        "user_syntax_string": (curses.COLOR_GREEN, user_background),
        "user_syntax_number": (curses.COLOR_CYAN, user_background),
        "user_syntax_literal": (curses.COLOR_YELLOW, user_background),
        "user_syntax_link": (curses.COLOR_CYAN, user_background),
        "table_header": (curses.COLOR_CYAN, -1),
        "table_separator": (curses.COLOR_BLUE, -1),
        "table_border": (curses.COLOR_BLUE, -1),
    }
    for index, (name, pair) in enumerate(definitions.items(), 1):
        try:
            curses.init_pair(index, *pair)
            result[name] = curses.color_pair(index)
        except curses.error:
            pass
    result["header"] |= curses.A_BOLD
    result["focus"] |= curses.A_BOLD
    result["user_label"] |= curses.A_BOLD
    result["assistant_label"] |= curses.A_BOLD
    result["syntax_link"] |= curses.A_UNDERLINE
    result["user_syntax_link"] |= curses.A_UNDERLINE
    result["table_header"] |= curses.A_BOLD
    result["table_separator"] |= curses.A_DIM
    result["table_border"] |= curses.A_DIM
    if curses.COLORS < 256:
        result["path"] |= curses.A_DIM
    return result


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
        palette = colors()
        items = all_agent_items(cache, include_local=include_local)
        query = ""
        selected = 0
        scroll = 0
        focus = "agents"
        maximized = False
        status = ""
        interrupt_armed = False

        def finish() -> int:
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
                    stdscr.vline(0, sidebar, curses.ACS_VLINE, height - 1)
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
                    selected_marker = "›" if index == selected else " "
                    if index == selected:
                        attr = (
                            palette["selected"]
                            if focus == "agents"
                            else palette["header"]
                        )
                    else:
                        attr = palette["normal"]
                    title = str(item.agent.get("title") or item.agent.get("agent_id") or "unnamed")
                    marker = "●" if item.agent.get("status") == "active" else "○"
                    add_text(
                        stdscr,
                        row,
                        1,
                        f"{selected_marker}{marker} " + compact(title, sidebar - 6),
                        sidebar - 2,
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
                add_text(stdscr, 1, main_x + 2, detail, main_width - 4, palette["muted"])
                records = cache.records(current.host_id, str(agent["agent_id"]))
                rendered = transcript_lines(records, main_width - 2)
                viewport = height - 5
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

            layout_action = "restore" if maximized else "max"
            footer = status or f"←→ pane  ↑↓/wheel scroll  m {layout_action}  i msg  v viewer  / find  r convo  R all  ^C×2 quit"
            add_text(stdscr, height - 1, 1, footer, width - 2, palette["status"])
            stdscr.refresh()
            status = ""
            key = stdscr.getch()
            interrupt_armed, should_exit = advance_interrupt(
                interrupt_armed, key
            )
            if should_exit:
                return finish()
            if key == 3:
                status = "Press Ctrl-C again to quit."
                continue
            if key in {curses.KEY_LEFT, curses.KEY_RIGHT, ord("m")}:
                focus, maximized = change_pane(focus, maximized, key)
            elif key == curses.KEY_MOUSE and shown:
                try:
                    _, _, _, _, button_state = curses.getmouse()
                except curses.error:
                    button_state = 0
                delta = mouse_scroll_delta(button_state)
                if delta:
                    focus = "conversation"
                    scroll = max(0, min(max_scroll, scroll + delta))
            elif key in (curses.KEY_UP, ord("k")) and shown:
                if focus == "agents":
                    selected = max(0, selected - 1)
                    scroll = 0
                else:
                    scroll = min(max_scroll, scroll + 1)
            elif key in (curses.KEY_DOWN, ord("j")) and shown:
                if focus == "agents":
                    selected = min(len(shown) - 1, selected + 1)
                    scroll = 0
                else:
                    scroll = max(0, scroll - 1)
            elif key == curses.KEY_PPAGE:
                focus = "conversation"
                scroll += max(1, height - 7)
            elif key == curses.KEY_NPAGE:
                focus = "conversation"
                scroll = max(0, scroll - max(1, height - 7))
            elif key == ord("/"):
                entered = edit_line(stdscr, "Find: ", query)
                if entered is not None:
                    query = entered; selected = 0; scroll = 0
            elif key in (ord("i"), curses.KEY_ENTER, 10, 13) and current:
                message = edit_line(stdscr, "Message: ")
                if message:
                    try:
                        result = agents_ui.send_message(
                            cache,
                            {"host": current.host_id, "agent": current.agent["agent_id"], "message": message},
                            codex_bin,
                        )
                        status = "Message sent." if result["delivery"] == "sent" else "Message queued for synchronization."
                    except (OSError, RuntimeError, ValueError) as exc:
                        status = f"Send failed: {exc}"
            elif key == ord("v") and current:
                show_status(stdscr, "Opening viewer…", palette["status"])
                try:
                    open_agent_viewer(current)
                    status = "Opened conversation in viewer."
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Viewer failed: {exc}"
            elif key == ord("r"):
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
                        host_name = sync_agent(
                            cache,
                            config,
                            peer_name,
                            str(current.agent["agent_id"]),
                        )
                        status = f"Refreshed conversation from {host_name}."
                    items = all_agent_items(
                        cache, include_local=include_local
                    )
                    selected = restored_selection(
                        filtered_items(items, query), current
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Refresh failed: {exc}"
            elif key == ord("R"):
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
                    for peer_name in peer_names:
                        try:
                            count, _ = sync_peer(cache, config, peer_name)
                            synchronized += 1
                            agents_count += count
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
                    if failures:
                        status += " Failed: " + "; ".join(failures)
                    items = all_agent_items(
                        cache, include_local=include_local
                    )
                    selected = restored_selection(
                        filtered_items(items, query), current
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Synchronization failed: {exc}"
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
