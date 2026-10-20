"""One persistent list: agent visibility and a global, stable order."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from codex_tools import paths


def config_path() -> Path:
    return paths.user_config_dir() / "agent-selection.json"


def key(item: Any) -> tuple[str, str]:
    return item.host_id, str(item.agent["agent_id"])


def read(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "hidden": [], "order": []}
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read agent preferences {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"invalid agent preferences: {path}")
    for field in ("hidden", "order"):
        values = payload.setdefault(field, [])
        if not isinstance(values, list) or any(
            not isinstance(value, list) or len(value) != 2
            or any(not isinstance(part, str) or not part for part in value) for value in values
        ):
            raise ValueError(f"invalid {field} in {path}")
    return payload


def save(state: dict, path: Path | None = None) -> None:
    location = path or config_path()
    paths.ensure_private_dir(location.parent)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=location.parent,
                                     prefix=".agent-selection-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.close()
            os.replace(temporary, location)
        finally:
            temporary.unlink(missing_ok=True)


def ordered(state: dict, items: list[Any]) -> list[Any]:
    rank = {tuple(value): index for index, value in enumerate(state["order"])}
    return sorted(items, key=lambda item: rank.get(key(item), len(rank)))


def visible(state: dict, item: Any) -> bool:
    return list(key(item)) not in state["hidden"]


def toggle(state: dict, item: Any) -> None:
    value = list(key(item))
    if value in state["hidden"]:
        state["hidden"].remove(value)
    else:
        state["hidden"].append(value)


def remember_order(state: dict, items: list[Any]) -> None:
    current = [list(key(item)) for item in items]
    state["order"] = current + [value for value in state["order"] if value not in current]


def _matches(item: Any, selector: str) -> bool:
    value = str(selector).strip().casefold()
    if value == str(item.agent.get("title") or "").casefold():
        return True
    identifiers = {str(item.agent.get(field) or "").casefold()
                   for field in ("agent_id", "session_id", "thread_id")} - {""}
    return (value in identifiers or (len(value) >= 8 and any(identifier.startswith(value) for identifier in identifiers))
            or any(value == f"{host}/{identifier}".casefold()
                   for host in (item.host_id, item.host_name) for identifier in identifiers))


def load(items: list[Any], path: Path | None = None) -> dict:
    location = path or config_path()
    if location.exists():
        return read(location)
    state = {"version": 1, "hidden": [], "order": []}
    old_order = paths.AGENTS_UI_STATE_DIR / "agent-order.json"
    old_groups = paths.user_config_dir() / "agent-groups.json"
    if old_order.exists():
        state["order"] = read(old_order)["order"]
    current = ordered(state, items)
    if old_groups.exists():
        try:
            legacy = json.loads(old_groups.read_text(encoding="utf-8"))
            if not isinstance(legacy, dict) or legacy.get("version") != 1 or not isinstance(legacy.get("groups"), dict):
                raise ValueError("unsupported group configuration")
            groups = legacy["groups"]
            enabled = legacy.get("enabled", True)
            assignments = legacy.get("assignments", {})
            overrides = legacy.get("hidden_overrides", {})
            hidden_rules = [*legacy.get("hidden", []), *groups.get("Hidden", [])]
            names = list(dict.fromkeys([*legacy.get("order", []), "", *groups, "Hidden"]))
            rank = {name: index for index, name in enumerate(names)}
            memberships = {}
            for item in current:
                reference = "/".join(key(item))
                assigned = assignments.get(reference)
                if assigned is None:
                    assigned = [name for name, selectors in groups.items() if name != "Hidden"
                                and any(_matches(item, selector) for selector in selectors)]
                memberships[key(item)] = assigned
                hidden = overrides.get(reference, any(_matches(item, selector) for selector in hidden_rules))
                # Group filters are intentionally discarded, not converted into
                # hidden choices. Only explicitly hidden agents stay hidden.
                if hidden:
                    state["hidden"].append(list(key(item)))
            # Preserve explicit preferences for agents that are currently absent.
            for reference in set(assignments) | set(overrides):
                host, separator, identifier = reference.partition("/")
                value = [host, identifier]
                if not separator or not host or not identifier or value in state["hidden"]:
                    continue
                if overrides.get(reference):
                    state["hidden"].append(value)
            for selector in hidden_rules:
                host, separator, identifier = str(selector).partition("/")
                value = [host, identifier]
                if (separator and host and identifier and overrides.get(selector, True)
                        and value not in state["hidden"]):
                    state["hidden"].append(value)
            if enabled:
                current.sort(key=lambda item: min((rank.get(name, len(rank)) for name in memberships[key(item)]),
                                                   default=rank[""]))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"cannot migrate old group preferences {old_groups}: {exc}") from exc
    remember_order(state, current)
    if old_order.exists() or old_groups.exists():
        # Copy preferences once. Old group/order files remain untouched backups.
        save(state, location)
    return state
