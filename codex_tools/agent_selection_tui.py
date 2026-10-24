"""Choose visible agents and reorder the same flat list shown by the console."""

from __future__ import annotations

import copy
import curses
from typing import Any

from codex_tools import agent_selection


def manage(window: Any, palette: dict[str, int], state: dict, items: list[Any], current: Any = None) -> str:
    from codex_tools.agents_tui import add_text, compact, compact_path_end, escape_action, key_code, terminal_key_name

    available = agent_selection.ordered(state, items)
    selected = next((index for index, item in enumerate(available)
                     if current is not None and agent_selection.key(item) == agent_selection.key(current)), 0)
    notice = "Choose which available agents appear in the console. Changes are saved."
    while True:
        height, width = window.getmaxyx()
        window.erase()
        add_text(window, 0, 2, "Choose agents", width - 4, palette["focus"])
        add_text(window, 1, 2, notice, width - 4, palette["muted"])
        count = max(1, (height - 6) // 2)
        first = max(0, selected - count + 1)
        for index in range(first, min(len(available), first + count)):
            item = available[index]
            row = 3 + (index - first) * 2
            attr = palette["selected"] if index == selected else palette["normal"]
            label = "[x]" if agent_selection.visible(state, item) else "[ ]"
            title = str(item.agent.get("title") or item.agent["agent_id"])
            if index == selected:
                add_text(window, row, 1, " " * max(0, width - 2), width - 2, attr)
            add_text(window, row, 2, f"{'›' if index == selected else ' '} {label} {title}", width - 4, attr)
            host = compact(item.host_name, max(5, (width - 10) // 3))
            cwd = str(item.agent.get("cwd") or "no workdir")
            detail = host + " · " + compact_path_end(cwd, max(1, width - 13 - len(host)))
            add_text(window, row + 1, 8, detail, width - 10, palette["path"])
        if not available:
            add_text(window, 3, 2, "No agents available. Synchronize to load them.", width - 4, palette["muted"])
        add_text(window, height - 2, 2, "Space show/hide · Alt-↑/↓ reorder · a show all · Esc back · Ctrl-D quit", width - 4, palette["muted"])
        window.refresh()
        try:
            key = window.get_wch()
        except curses.error:
            continue
        code = key_code(key)
        if code == 4:
            return "exit"
        if code in (2, 3) or key == "q":
            return "close"
        direction = {"kUP3": -1, "kUP4": -1, "kDN3": 1, "kDN4": 1}.get(terminal_key_name(key), 0)
        if code == 27:
            action, _ = escape_action(window, modal=True)
            # A repeated Escape (or a truncated sequence) still dismisses a
            # modal; it must never fall through into the agent's stop command.
            if action == "stop":
                return "close"
            direction = {"select_agent_up": -1, "move_agent_up": -1,
                         "select_agent_down": 1, "move_agent_down": 1}.get(action, 0)
            if not direction:
                continue
        if code in (curses.KEY_UP, 16):
            selected = max(0, selected - 1)
            continue
        if code in (curses.KEY_DOWN, 14):
            selected = min(max(0, len(available) - 1), selected + 1)
            continue
        pending = copy.deepcopy(state)
        reordered = list(available)
        next_selected = selected
        if direction and available:
            target = selected + direction
            if not 0 <= target < len(available):
                continue
            reordered[selected], reordered[target] = reordered[target], reordered[selected]
            agent_selection.remember_order(pending, reordered)
            next_selected = target
        elif code in (32, 10, 13, curses.KEY_ENTER) and available:
            agent_selection.toggle(pending, available[selected])
        elif key == "a":
            present = {agent_selection.key(item) for item in available}
            pending["hidden"] = [value for value in pending["hidden"] if tuple(value) not in present]
        else:
            continue
        try:
            agent_selection.save(pending)
            state.clear()
            state.update(pending)
            available = reordered
            selected = next_selected
            notice = "Agent selection and order saved."
        except (OSError, ValueError) as exc:
            notice = f"Could not save: {exc}"
