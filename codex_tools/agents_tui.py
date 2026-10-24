"""Interactive terminal Agent Console."""

from __future__ import annotations

import argparse
import curses
import io
import json
import queue
import re
import sys
import textwrap
import threading
import time
import unicodedata
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from pygments.lexers import get_lexer_by_name
from pygments.lexers.special import TextLexer
from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, String
from pygments.util import ClassNotFound

from codex_tools import (
    agent_selection,
    agent_selection_tui,
    agents_ui,
    codex_daemon,
    config as user_config,
    paths,
    relay_cache,
    relay_transport,
    search,
)


MAX_RECORDS = 240
SYNC_MODES = ("manual", "economy", "adaptive", "live")
SYNC_MODE_DETAILS = {
    "manual": "Refresh only when you request it with Ctrl-R or Ctrl-S.",
    "economy": "Check the selected conversation about every 10 seconds.",
    "adaptive": "Keep a low-bandwidth event request open; back off on errors.",
    "live": "Keep the selected conversation as immediate as possible.",
}
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
FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})\s*(?P<info>[^\s`]*)?.*$")
WORKING_FRAMES = ("◐", "◓", "◑", "◒")
AGENT_ORDER_FILE = agent_selection.config_path()


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


@dataclass(frozen=True)
class OptimisticMessage:
    text: str
    baseline_matches: int
    delivery: str
    message_id: str = ""
    queued: bool = False
    start_error: str = ""


def normalized_message_text(value: Any) -> str:
    return str(value or "").strip()


def user_message_count(records: list[dict[str, Any]], text: str) -> int:
    expected = normalized_message_text(text)
    return sum(
        record.get("role") == "user"
        and normalized_message_text(record.get("text")) == expected
        for record in records
    )


def reconcile_optimistic_messages(
    records: list[dict[str, Any]], messages: list[OptimisticMessage]
) -> list[OptimisticMessage]:
    """Drop optimistic bubbles as matching real transcript entries appear."""
    real_counts: dict[str, int] = {}
    for record in records:
        if record.get("role") != "user":
            continue
        text = normalized_message_text(record.get("text"))
        real_counts[text] = real_counts.get(text, 0) + 1
    remaining = []
    for message in messages:
        text = normalized_message_text(message.text)
        available = real_counts.get(text, 0)
        if available > message.baseline_matches:
            real_counts[text] = available - 1
        else:
            remaining.append(message)
    return remaining


def optimistic_message_status(
    message: OptimisticMessage,
    outbox: dict[str, dict[str, Any]],
    *,
    agent_working: bool = False,
) -> str:
    # Activity belongs to the thread, not necessarily to this submission. Until
    # reconciliation observes its real user record, only claim queue delivery.
    del agent_working
    if message.message_id:
        item = outbox.get(message.message_id)
        state = str((item or {}).get("state") or message.delivery)
        if state == "delivered":
            if (item or {}).get("last_error"):
                return f"Queued · could not start: {item['last_error']}"
            return "Queued in Codex"
        if state == "failed":
            error = str((item or {}).get("last_error") or "delivery failed")
            return f"Delivery failed · {error}"
        if (item or {}).get("last_error"):
            return "Not delivered · waiting to retry"
        return "Waiting to sync"
    if message.start_error:
        return f"Queued · could not start: {message.start_error}"
    return "Queued for agent"


def records_with_optimistic_messages(
    records: list[dict[str, Any]],
    messages: list[OptimisticMessage],
    outbox: dict[str, dict[str, Any]],
    *,
    agent_working: bool = False,
) -> list[dict[str, Any]]:
    return [
        *records,
        *(
            {
                "role": "user",
                "text": message.text,
                "record_type": "optimistic",
                "_status": optimistic_message_status(
                    message, outbox, agent_working=agent_working
                ),
            }
            for message in messages
        ),
    ]


def agent_is_working(agent: dict[str, Any]) -> bool:
    return (
        str(agent.get("status") or "") == "active"
        and str(agent.get("status_label") or "").lower() == "working"
    )


def receipt_status(receipt: dict[str, Any]) -> str:
    if receipt.get("record_key"):
        return "Delivered · in conversation"
    delivery = receipt["current_delivery"]
    error = str(receipt.get("current_error") or "")
    if delivery == "delivered":
        return f"Queued in Codex · start request failed: {error}" if error else "Queued in Codex"
    if delivery == "failed":
        return f"Delivery failed · {error or 'unknown error'}"
    if delivery == "sending":
        return "Delivery unconfirmed · check conversation"
    return "Not delivered · waiting to retry" if error else "Waiting to sync"


def records_with_message_receipts(
    records: list[dict[str, Any]], receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    associated = {receipt["record_key"]: receipt for receipt in receipts if receipt.get("record_key")}
    result = []
    for record in records:
        receipt = associated.get(relay_cache.user_record_key(record)) if record.get("role") == "user" else None
        result.append({**record, "_status": receipt_status(receipt),
                       "_message_id": receipt["message_id"]} if receipt else record)
    for receipt in receipts:
        if receipt.get("record_key"):
            continue
        result.append({"role": "user", "text": receipt["text"],
                       "timestamp": receipt["created_at"], "record_type": "outgoing",
                       "_message_id": receipt["message_id"], "_status": receipt_status(receipt)})
    return result


def working_frame(now: float | None = None) -> str:
    moment = time.monotonic() if now is None else now
    return WORKING_FRAMES[int(moment * 5) % len(WORKING_FRAMES)]


def records_with_agent_activity(
    records: list[dict[str, Any]],
    *,
    agent_working: bool,
    frame: str | None = None,
) -> list[dict[str, Any]]:
    if not agent_working:
        return records
    return [
        *records,
        {
            "role": "assistant",
            "text": f"{frame or working_frame()} Working…",
            "record_type": "activity",
        },
    ]


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
    cache: relay_cache.RelayCache,
    *,
    include_local: bool = True,
    preferred_order: list[tuple[str, str]] | None = None,
) -> list[AgentItem]:
    result = []
    hosts = cache.hosts()
    for host in hosts:
        if not include_local and host["host_id"] == "local":
            continue
        host_agents = sorted(
            cache.agents(str(host["host_id"])),
            key=lambda agent: (
                str(agent.get("created_at") or ""),
                str(agent.get("agent_id") or ""),
            ),
            reverse=True,
        )
        for agent in host_agents:
            result.append(
                AgentItem(
                    str(host["host_id"]),
                    str(host["name"]),
                    agent,
                    str(host.get("base_url") or ""),
                )
            )
    if not preferred_order:
        return result
    rank = {key: index for index, key in enumerate(preferred_order)}
    base_rank = {
        (item.host_id, str(item.agent.get("agent_id") or "")): index
        for index, item in enumerate(result)
    }
    return sorted(
        result,
        key=lambda item: rank.get(
            (item.host_id, str(item.agent.get("agent_id") or "")),
            len(rank) + base_rank[(item.host_id, str(item.agent.get("agent_id") or ""))],
        ),
    )


def agent_item_key(item: AgentItem) -> tuple[str, str]:
    return item.host_id, str(item.agent.get("agent_id") or "")


def load_agent_order(path: Path = AGENT_ORDER_FILE) -> list[tuple[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    values = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    order: list[tuple[str, str]] = []
    for value in values:
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(part, str) and part for part in value)
        ):
            order.append((value[0], value[1]))
    return order


def save_agent_order(
    items: list[AgentItem],
    previous_order: list[tuple[str, str]],
    path: Path = AGENT_ORDER_FILE,
) -> list[tuple[str, str]]:
    current = [agent_item_key(item) for item in items]
    current_set = set(current)
    order = current + [key for key in previous_order if key not in current_set]
    state = agent_selection.read(path)
    state["order"] = [list(key) for key in order]
    agent_selection.save(state, path)
    return order


def move_agent_item(
    items: list[AgentItem],
    visible_items: list[AgentItem],
    current: AgentItem,
    direction: int,
) -> bool:
    try:
        visible_index = next(
            index
            for index, item in enumerate(visible_items)
            if agent_item_key(item) == agent_item_key(current)
        )
        target_index = visible_index + direction
        if not 0 <= target_index < len(visible_items):
            return False
        target = visible_items[target_index]
    except (StopIteration, IndexError):
        return False
    current_index = next(
        index
        for index, item in enumerate(items)
        if agent_item_key(item) == agent_item_key(current)
    )
    target_index = next(
        index
        for index, item in enumerate(items)
        if agent_item_key(item) == agent_item_key(target)
    )
    items[current_index], items[target_index] = items[target_index], items[current_index]
    return True


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
    *,
    wait: float = 0,
) -> tuple[str | None, dict[str, Any]]:
    peer = config.get("peers", {}).get(peer_name)
    if not isinstance(peer, dict):
        raise ValueError(f"relay peer not found: {peer_name}")
    peer_url = str(peer["url"]).rstrip("/")
    cached_hosts = cache.hosts() if hasattr(cache, "hosts") else []
    cached_host = next(
        (
            host
            for host in cached_hosts
            if str(host.get("base_url") or "").rstrip("/") == peer_url
        ),
        None,
    )
    cached_agent = None
    if cached_host is not None and hasattr(cache, "agents"):
        cached_agent = next(
            (
                value
                for value in cache.agents(str(cached_host["host_id"]))
                if str(value["agent_id"]) == agent_id
            ),
            None,
        )
    event_cursor = str((cached_agent or {}).get("event_cursor") or "")
    if event_cursor:
        try:
            if wait:
                event_payload = relay_transport.fetch_agent_events(
                    peer, agent_id, event_cursor, wait=wait
                )
            else:
                event_payload = relay_transport.fetch_agent_events(
                    peer, agent_id, event_cursor
                )
        except HTTPError as exc:
            if exc.code != HTTPStatus.NOT_FOUND:
                raise
        else:
            if not event_payload.get("reset_required"):
                while True:
                    relay_transport.apply_agent_events(cache, event_payload)
                    if not event_payload.get("has_more"):
                        break
                    event_payload = relay_transport.fetch_agent_events(
                        peer,
                        agent_id,
                        str(event_payload["next_cursor"]),
                    )
                    if event_payload.get("reset_required"):
                        break
                if not event_payload.get("reset_required"):
                    delivery = relay_transport.deliver_pending(
                        cache, peer, str(event_payload["host"]["id"])
                    )
                    return str(event_payload["host"]["name"]), delivery
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


@dataclass(frozen=True)
class AutoSyncUpdate:
    target: tuple[str, str]
    error: str = ""
    completed_at: str = ""


def parse_sync_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError, AttributeError):
        return None


def sync_status_text(
    agent: dict[str, Any], mode: str, result: AutoSyncUpdate | None = None,
    *, syncing: bool = False, now: datetime | None = None,
) -> tuple[str, str]:
    """A quiet health indicator; routine polling must not animate the metadata."""
    now = now or datetime.now(timezone.utc)
    synced_at = parse_sync_time(str(agent.get("synced_at") or ""))
    error_at = parse_sync_time(result.completed_at) if result else None
    error = result.error if result else ""
    if error and synced_at and error_at and synced_at > error_at:
        error = ""  # Another successful sync superseded this earlier error.
    age = max(0, (now - synced_at).total_seconds()) if synced_at else None
    if error:
        style = "info_sync_error"
    elif mode == "manual" or age is None or age > 30:
        style = "info_sync_unknown"
    else:
        style = "info_sync_ok"
    return f"Sync {mode} · ●", style


def metadata_groups(agent: dict[str, Any], sync_label: str, width: int) -> tuple[str, str]:
    """Fit two distinct metadata groups on one terminal row."""
    model = str(agent.get("model") or "Model unavailable")
    effort = str(agent.get("reasoning_effort") or "unavailable")
    model_label = f"{model} · reasoning: {effort}"

    def cells(text: str) -> int:
        return sum(character_cell_width(c) for c in text)

    def clip(text: str, budget: int) -> str:
        if cells(text) <= budget:
            return text
        prefix = ""
        for character in text:
            if cells(prefix) + character_cell_width(character) > budget - 1:
                break
            prefix += character
        return prefix + "…" if budget > 0 else ""

    if cells(model_label) + 5 + cells(sync_label) > width:
        model_label = f"{model} · {effort}"
    if cells(model_label) + 5 + cells(sync_label) > width:
        model_label = clip(model_label, max(1, (width - 5) // 2))
    return model_label, clip(sync_label, max(0, width - cells(model_label) - 5))


def metadata_spans(model_label: str, sync_label: str, sync_style: str) -> list[tuple[str, str]]:
    spans = []
    for index, part in enumerate(model_label.split(" · ")):
        if index:
            spans.append((" · ", "muted"))
        spans.append((part, "info_model" if index == 0 else "info_effort"))
    spans.append(("  │  ", "muted"))
    for index, part in enumerate(sync_label.split(" · ")):
        if index:
            spans.append((" · ", "muted"))
        style = "info_sync_mode" if index == 0 else sync_style
        spans.append((part, style))
    return spans


def draw_info_spans(window: Any, y: int, x: int, spans: list[tuple[str, str]],
                    width: int, palette: dict[str, int]) -> None:
    for text, style in spans:
        if width <= 0:
            break
        add_text(window, y, x, text, width, palette.get(style, palette["muted"]))
        cells = min(width, sum(character_cell_width(c) for c in text))
        x += cells
        width -= cells


class DeliveryWorker:
    """Attempt durable remote outbox deliveries without blocking a redraw.

    Each submission wakes the worker even if a previous delivery is in flight;
    otherwise a second message could be stranded in manual sync mode.
    """

    def __init__(self, cache: relay_cache.RelayCache, relay_config: Path) -> None:
        self.cache = cache
        self.relay_config = relay_config
        self.updates: queue.SimpleQueue[AutoSyncUpdate] = queue.SimpleQueue()
        self._jobs: queue.SimpleQueue[AgentItem | None] = queue.SimpleQueue()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="agent-console-delivery", daemon=True)
        self._thread.start()

    def submit(self, item: AgentItem) -> None:
        if not self._stopped.is_set():
            self._jobs.put(item)

    def close(self) -> None:
        self._stopped.set()
        self._jobs.put(None)
        self._thread.join(timeout=0.25)

    def _run(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None or self._stopped.is_set():
                return
            error = ""
            try:
                config = relay_transport.load_config(self.relay_config)
                peer_name = peer_name_for_item(config, item)
                if peer_name is None:
                    raise ValueError(f"no configured peer matches {item.host_name}")
                relay_transport.deliver_pending(self.cache, config["peers"][peer_name], item.host_id)
            except (OSError, RuntimeError, ValueError) as exc:
                error = str(exc)
                # Preserve the message and make setup failures visible in its
                # receipt, just like a failed network delivery attempt.
                for pending in self.cache.pending(item.host_id):
                    if str(pending["agent_id"]) == str(item.agent["agent_id"]):
                        self.cache.record_outbox_attempt(str(pending["message_id"]),
                                                        state="pending", error=error)
            self.updates.put(AutoSyncUpdate(
                (item.host_id, str(item.agent["agent_id"])), error, relay_cache.utc_now()
            ))


class AutoSyncWorker:
    """Synchronize only the selected agent without blocking terminal input."""

    def __init__(
        self,
        cache: relay_cache.RelayCache,
        sessions_root: Path,
        session_index: Path,
        control_socket: Path | None,
        relay_config: Path,
    ) -> None:
        self.cache = cache
        self.sessions_root = sessions_root
        self.session_index = session_index
        self.control_socket = control_socket
        self.relay_config = relay_config
        self.updates: queue.SimpleQueue[AutoSyncUpdate] = queue.SimpleQueue()
        self._condition = threading.Condition()
        self._target: AgentItem | None = None
        self._mode = "manual"
        self._revision = 0
        self._stopped = False
        self._syncing_target: tuple[str, str] | None = None
        self._thread = threading.Thread(
            target=self._run, name="agent-console-sync", daemon=True
        )
        self._thread.start()

    def select(self, item: AgentItem | None, mode: str) -> None:
        if mode not in SYNC_MODES:
            raise ValueError(f"unknown sync mode: {mode}")
        with self._condition:
            key = self._item_key(item)
            if key != self._item_key(self._target) or mode != self._mode:
                self._target = item
                self._mode = mode
                self._revision += 1
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._thread.join(timeout=0.25)

    def is_syncing(self, item: AgentItem) -> bool:
        with self._condition:
            return self._syncing_target == self._item_key(item)

    @staticmethod
    def _item_key(item: AgentItem | None) -> tuple[str, str] | None:
        if item is None:
            return None
        return item.host_id, str(item.agent["agent_id"])

    def _run(self) -> None:
        failure_count = 0
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stopped
                    or (self._target is not None and self._mode != "manual")
                )
                if self._stopped:
                    return
                item = self._target
                mode = self._mode
                revision = self._revision
            if item is None:
                continue
            target = self._item_key(item)
            if target is None:
                continue
            error = ""
            with self._condition:
                self._syncing_target = target
            try:
                if item.host_id == "local":
                    relay_cache.refresh_local_agent(
                        self.cache,
                        str(item.agent["agent_id"]),
                        self.sessions_root,
                        self.session_index,
                        self.control_socket,
                    )
                else:
                    config = relay_transport.load_config(self.relay_config)
                    peer_name = peer_name_for_item(config, item)
                    if peer_name is None:
                        raise ValueError(
                            f"no configured peer matches {item.host_name}"
                        )
                    wait = 8.0 if mode in {"adaptive", "live"} else 0.0
                    host_name, _ = sync_agent(
                        self.cache,
                        config,
                        peer_name,
                        str(item.agent["agent_id"]),
                        wait=wait,
                    )
                    if host_name is None:
                        raise ValueError("Remote session is no longer active; showing cached copy")
                failure_count = 0
            except (OSError, RuntimeError, ValueError) as exc:
                failure_count += 1
                error = str(exc)
            finally:
                with self._condition:
                    self._syncing_target = None
            self.updates.put(AutoSyncUpdate(target, error, relay_cache.utc_now()))
            if error:
                delay = min(
                    30.0,
                    (1.0 if mode == "live" else 2.0)
                    * 2 ** min(failure_count - 1, 4),
                )
            elif item.host_id != "local" and mode in {"adaptive", "live"}:
                delay = 0.05
            else:
                delay = {
                    "economy": 10.0,
                    "adaptive": 1.0,
                    "live": 0.25,
                }[mode]
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stopped or self._revision != revision,
                    timeout=delay,
                )
                if self._stopped:
                    return


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


def outbox_views(
    cache: relay_cache.RelayCache,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    items = cache.outbox()
    visible = [
        item for item in items if item["state"] in {"pending", "failed"}
    ]
    return visible, {str(item["message_id"]): item for item in items}


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


def open_agent_viewer(item: AgentItem, *, typeset: bool = False) -> str:
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
        url = viewer.view_url(
            base_url,
            "",
            8 if typeset else 24,
            False,
            session_id,
            "latex" if typeset else "markdown",
        )
    else:
        url = base_url + viewer.relay_view_path(
            item.host_id,
            agent_id,
            mode="latex" if typeset else "markdown",
            tail=8 if typeset else 24,
        )
    browser_args = viewer.default_server_args()
    if not viewer.open_browser(url, browser_args):
        raise RuntimeError("browser could not be opened")
    return url


def stop_agent_turn(
    item: AgentItem,
    control_socket: Path | None,
    relay_config: Path,
) -> dict[str, Any]:
    """Request an immediate stop locally or through the selected relay peer."""
    if item.host_id == "local":
        thread_id = str(
            item.agent.get("thread_id")
            or item.agent.get("session_id")
            or item.agent.get("agent_id")
            or ""
        )
        if not thread_id:
            raise ValueError("selected agent has no thread ID")
        return codex_daemon.interrupt_thread(thread_id, control_socket)
    config = relay_transport.load_config(relay_config)
    peer_name = peer_name_for_item(config, item)
    if peer_name is None:
        raise ValueError(f"no configured peer matches {item.host_name}")
    peer = config["peers"][peer_name]
    return relay_transport.interrupt_agent(
        peer, str(item.agent["agent_id"])
    )


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


def peer_agent_items(cache: relay_cache.RelayCache, items: list[AgentItem], peer_url: str | None) -> list[AgentItem]:
    if peer_url is None:
        return items
    host_id = cache.peer_host_id(peer_url)
    return [AgentItem(item.host_id, item.host_name, item.agent, peer_url)
            for item in items if item.host_id == host_id and item.host_id != "local"]


def confirm_pending_disconnect(window: Any, count: int, palette: dict[str, int]) -> bool:
    show_status(window, f"{count} message(s) waiting to sync. Ctrl-D again disconnects; any other key stays. Messages are saved for next time.", palette["status"])
    try:
        return window.get_wch() == "\x04"
    except curses.error:
        return False


class EmphasizedText(str):
    """Visible text with bold ranges that survive wrapping and table padding."""

    def __new__(cls, text: str, bold_ranges: tuple[tuple[int, int], ...] = ()) -> EmphasizedText:
        value = super().__new__(cls, text)
        value.bold_ranges = bold_ranges
        return value


def markdown_text(text: str) -> EmphasizedText:
    if isinstance(text, EmphasizedText):
        return text
    # Code spans and escaped characters are consumed before emphasis markers.
    tokens = re.finditer(
        r"(?P<code>(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`))"
        r"|(?P<escaped>\\.)|(?P<strong>(?<!\*)\*\*(?!\*))",
        text,
    )
    opening = None
    pairs = []
    for token in tokens:
        if token.lastgroup != "strong":
            continue
        can_close = token.start() > 0 and not text[token.start() - 1].isspace()
        can_open = token.end() < len(text) and not text[token.end()].isspace()
        if opening is not None and can_close:
            pairs.append((opening, token))
            opening = None
        elif opening is None and can_open:
            opening = token
    pieces = []
    ranges = []
    cursor = size = 0
    for opening, closing in pairs:
        prefix = text[cursor:opening.start()]
        content = text[opening.end():closing.start()]
        pieces.extend((prefix, content))
        size += len(prefix)
        ranges.append((size, size + len(content)))
        size += len(content)
        cursor = closing.end()
    pieces.append(text[cursor:])
    return EmphasizedText("".join(pieces), tuple(ranges))


def emphasized_slice(text: str, start: int, end: int) -> EmphasizedText:
    ranges = tuple((max(left, start) - start, min(right, end) - start)
                   for left, right in getattr(text, "bold_ranges", ())
                   if left < end and right > start)
    return EmphasizedText(text[start:end], ranges)


def emphasized_join(parts: list[str]) -> EmphasizedText:
    ranges = []
    offset = 0
    for part in parts:
        ranges.extend((left + offset, right + offset)
                      for left, right in getattr(part, "bold_ranges", ()))
        offset += len(part)
    return EmphasizedText("".join(parts), tuple(ranges))


def wrap_markdown(text: str, width: int, *, drop_whitespace: bool = False) -> list[EmphasizedText]:
    rich = markdown_text(text if isinstance(text, EmphasizedText) else text.expandtabs())
    wrapped = textwrap.wrap(rich, width=width, expand_tabs=False, replace_whitespace=False,
                            drop_whitespace=drop_whitespace, break_long_words=True,
                            break_on_hyphens=True) or [""]
    result = []
    offset = 0
    for line in wrapped:
        start = rich.find(line, offset)
        result.append(emphasized_slice(rich, start, start + len(line)))
        offset = start + len(line)
    return result


def transcript_lines(
    records: list[dict[str, Any]], width: int
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    content_width = max(8, width - 4)
    renderable = [
        record
        for record in records
        if record.get("role") in {"user", "assistant"} or record.get("record_type") == "tool_activity"
    ]
    # Keep a few understated tool breadcrumbs, not the full CLI tool log.
    activity_records = [record for record in renderable
                        if record.get("record_type") == "tool_activity"]
    recent_activity = {id(record) for record in activity_records[-3:]}
    renderable = [
        record for record in renderable
        if (record.get("record_type") != "tool_activity" or id(record) in recent_activity)
        and not (
            record.get("role") == "user"
            and search.is_internal_prompt_text(str(record.get("text") or ""))
        )
    ]
    latest_assistant = next(
        (record for record in reversed(renderable)
         if record.get("role") == "assistant"
         and record.get("record_type") not in {"activity", "tool_activity"}),
        None,
    )
    previous_tool = False
    for record in renderable[-MAX_RECORDS:]:
        role = str(record["role"])
        if record.get("record_type") == "tool_activity":
            lines.append(("  · " + compact(str(record.get("text") or ""), content_width - 2), "muted"))
            previous_tool = True
            continue
        activity = record.get("record_type") == "activity"
        if previous_tool and not activity:
            lines.append(("", f"{role}_gap"))
        previous_tool = False
        record_start = len(lines)
        paragraphs = [
            display_markdown_links(line)
            for line in (str(record.get("text") or "").splitlines() or [""])
        ]
        index = 0
        while index < len(paragraphs):
            paragraph = paragraphs[index]
            fence_match = FENCE_RE.match(paragraph)
            if fence_match:
                fence = str(fence_match.group("fence"))
                language = str(fence_match.group("info") or "").strip()
                lines.append((f"  {language or 'code'}", "code_label"))
                index += 1
                while index < len(paragraphs):
                    code_line = paragraphs[index]
                    if re.match(
                        rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$",
                        code_line,
                    ):
                        index += 1
                        break
                    lines.append(
                        ("  │ " + code_line.expandtabs(4), f"code:{language}")
                    )
                    index += 1
                continue
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
            wrapped = wrap_markdown(paragraph, content_width)
            style = "agent_activity" if activity else role
            lines.extend((emphasized_join(["  ", line]), style) for line in wrapped)
            index += 1
        message_status = str(record.get("_status") or "")
        if message_status:
            status_lines = textwrap.wrap(
                message_status,
                width=max(8, content_width - 2),
                break_long_words=True,
                break_on_hyphens=True,
            ) or [""]
            lines.extend(
                (
                    ("  ◌ " if status_index == 0 else "    ") + status_line,
                    "message_status",
                )
                for status_index, status_line in enumerate(status_lines)
            )
        if role == "assistant" and not activity:
            for line_index in range(record_start, len(lines)):
                line, style = lines[line_index]
                if not line.strip():
                    continue
                lines[line_index] = (
                    emphasized_join(["• ", emphasized_slice(line, 2, len(line))])
                    if line.startswith("  ") else emphasized_join(["• ", line]),
                    style,
                )
                break
        if record is latest_assistant:
            timestamp = str(record.get("timestamp") or "")
            if timestamp:
                try:
                    local_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    pass
                else:
                    lines.append(("", "assistant_gap"))
                    lines.append(("  " + local_time.strftime("%H:%M:%S"), "muted"))
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
        [markdown_text(cell.expandtabs()) for cell in (row + [""] * column_count)[:column_count]]
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
            wrap_markdown(cell, max(1, column_width), drop_whitespace=True)
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
                padding = max(0, column_width - len(value))
                left = padding if alignment == "right" else padding // 2 if alignment == "center" else 0
                value = emphasized_join([" " * left, value, " " * (padding - left)])
                pieces.append(value)
            parts = ["  │ "]
            for index, piece in enumerate(pieces):
                if index:
                    parts.append(" │ ")
                parts.append(piece)
            parts.append(" │")
            result.append((emphasized_join(parts), style))
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
        if sys.platform == "darwin":
            # Some macOS Python/curses builds corrupt native window state when
            # addnstr temporarily sets an attribute. The crash appears later,
            # inside resize_term on zoom/resize. Draw first, then style cells
            # with chgat, which avoids that attribute-setting path entirely.
            characters = []
            cells = 0
            for character in text:
                if character in "\r\n":
                    break
                size = character_cell_width(character)
                if cells + size > width:
                    break
                characters.append(character)
                cells += size
            encoded = "".join(characters).encode("utf-8")
            if encoded:
                window.addnstr(y, x, encoded, len(encoded))
                if cells:
                    window.chgat(y, x, cells, attr)
        else:
            window.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def indexed_semantic_spans(text: str, *, table: bool = False, offset: int = 0) -> list[tuple[str, str | None, int]]:
    spans: list[tuple[str, str | None, int]] = []
    if table:
        position = 0
        for border in re.finditer(r"[|│├┼┤┌┬┐└┴┘]", text):
            spans.extend(indexed_semantic_spans(text[position:border.start()], offset=offset + position))
            spans.append((border.group(), "table_border", offset + border.start()))
            position = border.end()
        spans.extend(indexed_semantic_spans(text[position:], offset=offset + position))
        return spans
    position = 0
    for match in SEMANTIC_TOKEN_RE.finditer(text):
        if match.start() > position:
            spans.append((text[position : match.start()], None, offset + position))
        value = match.group(0)
        start = offset + match.start()
        if match.lastgroup == "link" and value.startswith("["):
            value = value[1:-1]
            start += 1
        spans.append((value, str(match.lastgroup), start))
        position = match.end()
    if position < len(text):
        spans.append((text[position:], None, offset + position))
    return spans or [(text, None, offset)]


def semantic_spans(text: str) -> list[tuple[str, str | None]]:
    return [(value, token) for value, token, _ in indexed_semantic_spans(text)]


def pygments_token_style(token: Any) -> str:
    if token in Comment:
        return "code_comment"
    if token in Keyword:
        return "code_keyword"
    if token in String:
        return "code_string"
    if token in Number:
        return "code_number"
    if token in Name.Function or token in Name.Class or token in Name.Builtin:
        return "code_name"
    if token in Operator:
        return "code_operator"
    if token in Generic.Error:
        return "code_error"
    return "code"


def highlighted_code_spans(
    text: str, language: str
) -> list[tuple[str, str]]:
    try:
        lexer = get_lexer_by_name(language) if language else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    spans: list[tuple[str, str]] = []
    for token, value in lexer.get_tokens(text):
        value = value.rstrip("\n")
        if not value:
            continue
        style = pygments_token_style(token)
        if spans and spans[-1][1] == style:
            spans[-1] = (spans[-1][0] + value, style)
        else:
            spans.append((value, style))
    return spans or [(text, "code")]


def display_markdown_links(text: str) -> str:
    return MARKDOWN_LINK_RE.sub(lambda match: f"[{match.group('label')}]", text)


def table_spans(text: str) -> list[tuple[str, str | None]]:
    return [(value, token) for value, token, _ in indexed_semantic_spans(text, table=True)]


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
    rich = markdown_text(text)
    for value, token, source_start in indexed_semantic_spans(rich, table=table):
        attr = (palette.get(token if token.startswith("table_") else f"{syntax_prefix}{token}", base_attr)
                if token else base_attr)
        cuts = sorted({0, len(value), *(boundary - source_start
                       for pair in rich.bold_ranges for boundary in pair
                       if source_start < boundary < source_start + len(value))})
        for left, right in zip(cuts, cuts[1:]):
            if remaining <= 0:
                return
            bold = any(start <= source_start + left < end for start, end in rich.bold_ranges)
            shown = []
            cells = 0
            for char in value[left:right]:
                size = character_cell_width(char)
                if cells + size > remaining:
                    break
                shown.append(char)
                cells += size
            add_text(window, y, column, "".join(shown), remaining, attr | (curses.A_BOLD if bold else 0))
            column += cells
            remaining -= cells
            if len(shown) < right - left:
                return


def add_highlighted_code(
    window: Any,
    y: int,
    x: int,
    text: str,
    width: int,
    language: str,
    palette: dict[str, int],
) -> None:
    remaining = width
    column = x
    prefix, code = (text[:4], text[4:]) if text.startswith("  │ ") else ("", text)
    if prefix:
        add_text(window, y, column, prefix, remaining, palette["code_border"])
        shown = min(len(prefix), remaining)
        column += shown
        remaining -= shown
    for value, style in highlighted_code_spans(code, language):
        if remaining <= 0:
            break
        shown = value[:remaining]
        add_text(window, y, column, shown, remaining, palette.get(style, palette["code"]))
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


def requests_console_exit(key: int) -> bool:
    return key == 4  # Ctrl-D in curses raw mode; Ctrl-C stops the agent.


def mouse_scroll_delta(button_state: int, amount: int = 3) -> int:
    if button_state & curses.BUTTON4_PRESSED:
        return amount
    if button_state & getattr(curses, "BUTTON5_PRESSED", 0):
        return -amount
    return 0


def mouse_protocol_sequence(enabled: bool) -> str:
    """Older macOS curses cannot represent wheel-down: read SGR events ourselves."""
    if hasattr(curses, "BUTTON5_PRESSED"):
        return ""
    return "\x1b[?1006h" if enabled else "\x1b[?1006l"


def change_pane(
    focus: str, maximized: bool, key: int
) -> tuple[str, bool]:
    if key == 9:  # Tab
        if focus == "agents":
            return "conversation", False
        return "agents", False
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
        code_background = 235
        table_blue = 67
        return {
            "normal": (warm_text, -1),
            "muted": (muted_text, -1),
            "path": (muted_text, -1),
            "header": (warm_text, -1),
            "focus": (soft_blue, -1),
            "info_model": (soft_blue, -1),
            "info_effort": (soft_lavender, -1),
            "info_sync_mode": (soft_gold, -1),
            "info_sync_ok": (soft_green, -1),
            "info_sync_unknown": (soft_gold, -1),
            "info_sync_error": (soft_coral, -1),
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
            "message_status": (muted_text, user_background),
            "assistant": (warm_text, -1),
            "agent_activity": (soft_green, -1),
            "code": (warm_text, code_background),
            "code_label": (muted_text, code_background),
            "code_border": (table_blue, code_background),
            "code_comment": (muted_text, code_background),
            "code_keyword": (soft_lavender, code_background),
            "code_string": (soft_green, code_background),
            "code_number": (soft_blue, code_background),
            "code_name": (soft_gold, code_background),
            "code_operator": (soft_coral, code_background),
            "code_error": (soft_coral, code_background),
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
        "info_model": (curses.COLOR_CYAN, -1),
        "info_effort": (curses.COLOR_MAGENTA, -1),
        "info_sync_mode": (curses.COLOR_YELLOW, -1),
        "info_sync_ok": (curses.COLOR_GREEN, -1),
        "info_sync_unknown": (curses.COLOR_YELLOW, -1),
        "info_sync_error": (curses.COLOR_RED, -1),
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
        "message_status": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "agent_activity": (curses.COLOR_GREEN, -1),
        "code": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "code_label": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "code_border": (curses.COLOR_BLUE, curses.COLOR_BLACK),
        "code_comment": (curses.COLOR_WHITE, curses.COLOR_BLACK),
        "code_keyword": (curses.COLOR_MAGENTA, curses.COLOR_BLACK),
        "code_string": (curses.COLOR_GREEN, curses.COLOR_BLACK),
        "code_number": (curses.COLOR_CYAN, curses.COLOR_BLACK),
        "code_name": (curses.COLOR_YELLOW, curses.COLOR_BLACK),
        "code_operator": (curses.COLOR_RED, curses.COLOR_BLACK),
        "code_error": (curses.COLOR_RED, curses.COLOR_BLACK),
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
        "info_model": curses.A_NORMAL,
        "info_effort": curses.A_NORMAL,
        "info_sync_mode": curses.A_NORMAL,
        "info_sync_ok": curses.A_NORMAL,
        "info_sync_unknown": curses.A_NORMAL,
        "info_sync_error": curses.A_BOLD,
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
        "message_status": curses.A_DIM,
        "assistant": curses.A_NORMAL,
        "agent_activity": curses.A_DIM,
        "code": curses.A_NORMAL,
        "code_label": curses.A_DIM,
        "code_border": curses.A_DIM,
        "code_comment": curses.A_DIM,
        "code_keyword": curses.A_BOLD,
        "code_string": curses.A_NORMAL,
        "code_number": curses.A_NORMAL,
        "code_name": curses.A_NORMAL,
        "code_operator": curses.A_NORMAL,
        "code_error": curses.A_BOLD,
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
    result["message_status"] |= curses.A_DIM
    result["code_label"] |= curses.A_DIM
    result["code_border"] |= curses.A_DIM
    result["code_comment"] |= curses.A_DIM
    result["code_keyword"] |= curses.A_BOLD
    result["code_error"] |= curses.A_BOLD
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
    soft_leading = False
    for index, value in enumerate(text):
        positions[index] = (row, column)
        if value == "\n":
            row += 1
            column = 0
            lines.append("")
            soft_leading = False
            positions[index + 1] = (row, column)
            continue
        if value.isspace() and column >= width:
            row += 1
            column = 0
            lines.append("")
            soft_leading = True
        if value.isspace() and column == 0 and soft_leading:
            positions[index] = positions[index + 1] = (row, column)
            continue
        # Wrap a whole word ahead of time when it fits on a fresh line.
        # Overlong words/URLs still wrap by cells. Draft text is untouched.
        if not value.isspace() and (index == 0 or text[index - 1].isspace()):
            end = index
            word_width = 0
            while end < len(text) and not text[end].isspace() and word_width <= width:
                word_width += character_cell_width(text[end])
                end += 1
            if column and word_width <= width and column + word_width > width:
                row += 1
                column = 0
                lines.append("")
                positions[index] = (row, column)
        cell_width = character_cell_width(value)
        if column and column + cell_width > width:
            row += 1
            column = 0
            lines.append("")
            positions[index] = (row, column)
        lines[row] += value
        soft_leading = False
        column += cell_width
        positions[index + 1] = (row, column)
    # Leave room for the insertion cursor after a completely filled final row.
    if column >= width:
        lines.append("")
        positions[len(text)] = (row + 1, 0)
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


def delete_composer_word(buffer: ComposerBuffer) -> None:
    start = buffer.cursor
    while start and buffer.text[start - 1].isspace():
        start -= 1
    while start and not buffer.text[start - 1].isspace():
        start -= 1
    buffer.text = buffer.text[:start] + buffer.text[buffer.cursor :]
    buffer.cursor = start
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


def escape_action(stdscr: Any, *, modal: bool = False) -> tuple[str, str]:
    """Distinguish Escape, Alt keys, modified arrows, and paste."""
    try:
        stdscr.timeout(40)
        following = stdscr.get_wch()
    except curses.error:
        return "stop", ""
    finally:
        stdscr.timeout(-1)
    # macOS Terminal may encode Option-arrows as Meta + an ordinary arrow.
    # This also covers Command when a user's keyboard map swaps Cmd/Option.
    meta_arrow = following == "\x1b"
    if meta_arrow:
        try:
            stdscr.timeout(40)
            following = stdscr.get_wch()
        except curses.error:
            return ("stop" if modal else "cancel"), ""
        finally:
            stdscr.timeout(-1)
    # ncurses may already have decoded the arrow following the initial Meta
    # Escape. Handle that form whether or not another raw Escape was seen.
    if following in (curses.KEY_UP, curses.KEY_DOWN):
        return ("select_agent_up" if following == curses.KEY_UP
                else "select_agent_down"), ""
    if following in ("\n", "\r", curses.KEY_ENTER):
        return "newline", ""
    if following in ("\b", "\x7f", curses.KEY_BACKSPACE):
        return "word_backspace", ""
    if following in ("m", "M"):
        return "maximize", ""
    if isinstance(following, str) and following in "123456789":
        return "agent", following
    if following == "[" or (meta_arrow and following == "O"):
        suffix = ""
        try:
            stdscr.timeout(20)
            for _ in range(32):
                part = stdscr.get_wch()
                if not isinstance(part, str):
                    return "cancel", ""
                suffix += part
                if part.isalpha() or part == "~":
                    break
        except curses.error:
            return ("stop" if modal and not suffix.startswith("<") else "cancel"), ""
        finally:
            stdscr.timeout(-1)
        if suffix == "200~":
            return "paste", read_bracketed_paste(stdscr)
        mouse = re.fullmatch(r"<(\d{1,3});\d{1,6};\d{1,6}([Mm])", suffix)
        if mouse and mouse[2] == "M":
            # Ignore modifiers, but not motion, horizontal wheels, or clicks.
            button = int(mouse[1]) & ~(4 | 8 | 16)
            if button in (64, 65):
                return ("scroll_up" if button == 64 else "scroll_down"), ""
        if suffix in {"1;5D", "5D"}:
            return "word_left", ""
        if suffix in {"1;5C", "5C"}:
            return "word_right", ""
        if suffix in {"1;3A", "3A", "1;9A", "9A"} or (meta_arrow and suffix == "A"):
            return "select_agent_up", ""
        if suffix in {"1;3B", "3B", "1;9B", "9B"} or (meta_arrow and suffix == "B"):
            return "select_agent_down", ""
        if suffix in {"1;4A", "4A"}:
            return "move_agent_up", ""
        if suffix in {"1;4B", "4B"}:
            return "move_agent_down", ""
        if suffix == "1;2D":
            return "agents", ""
        if suffix == "1;2C":
            return "conversation", ""
    return "cancel", ""


def key_code(key: Any) -> int | None:
    if isinstance(key, int):
        return key
    if isinstance(key, str) and len(key) == 1:
        return ord(key)
    return None


def terminal_key_name(key: Any) -> str:
    """Return ncurses' portable name for an extended integer key."""
    if not isinstance(key, int):
        return ""
    try:
        return curses.keyname(key).decode("ascii", errors="replace")
    except (curses.error, AttributeError):
        return ""


def edit_composer_buffer(buffer: ComposerBuffer, key: Any, width: int) -> str | None:
    """Apply an editing key; return ``send`` or ``queue`` for submission."""
    code = key_code(key)
    name = terminal_key_name(key)
    if code == 9:
        return "queue" if buffer.text.strip() else None
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
    elif code == CTRL_LEFT_KEY or name == "kLFT5":
        move_composer_word(buffer, -1)
    elif code == CTRL_RIGHT_KEY or name == "kRIT5":
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
        delete_composer_word(buffer)
    elif code == 15:
        buffer.insert("\n")
    elif isinstance(key, str) and key.isprintable():
        buffer.insert(key)
    return None


def composer_visible_rows(line_count: int, height: int) -> int:
    """Reserve room for a blank background row above and below the draft."""
    return max(1, min(8, height // 3 - 2, line_count))


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
    inner_width = max(1, available_width - 5)
    lines, positions = composer_layout(buffer.text, inner_width)
    cursor_row, cursor_column = positions[buffer.cursor]
    visible_rows = composer_visible_rows(len(lines), height)
    first_line = max(0, cursor_row - visible_rows + 1)
    panel_height = visible_rows + 2
    top = max(3, height - panel_height)
    text_top = top + 1

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
            text_top + offset,
            inner_x,
            "›" if offset == 0 else " ",
            1,
            palette["composer_accent" if focused else "composer_muted"],
        )
        add_text(
            stdscr,
            text_top + offset,
            inner_x + 2,
            line,
            inner_width,
            palette["composer"],
        )
    if focused:
        try:
            stdscr.move(
                text_top + cursor_row - first_line,
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
    sync_mode: str = "adaptive",
    peer: str | None = None,
) -> int:
    peer_url = None
    if peer is not None:
        selected_peer = relay_transport.load_config(relay_config)["peers"].get(peer)
        if selected_peer is None:
            raise ValueError(f"unknown relay peer: {peer}")
        peer_url = str(selected_peer["url"])
        include_local = False

    def run(stdscr: Any) -> int:
        active_sync_mode = sync_mode
        curses.curs_set(0)
        curses.raw()
        # Keep physical Enter (CR) distinct from Ctrl-J (LF).  In curses'
        # default newline mode both arrive as LF, which makes Enter insert a
        # newline instead of sending the draft.
        curses.nonl()
        stdscr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
        sys.stdout.write("\x1b[?2004h" + mouse_protocol_sequence(True))
        sys.stdout.flush()
        palette = colors()
        items = all_agent_items(
            cache,
            include_local=include_local,
        )
        selection = agent_selection.load(items)
        agent_order = [tuple(value) for value in selection["order"]]
        items = agent_selection.ordered(selection, items)
        query = ""
        selected = 0
        settings_selected = False
        settings_tab = "sync"
        settings_choice = SYNC_MODES.index(active_sync_mode)
        settings_tabs = ("sync", "agents", "deliveries")
        settings_choices = {"sync": settings_choice, "agents": 0, "deliveries": 0}
        scroll = 0
        focus = "conversation"
        maximized = False
        status = ""
        sync_results: dict[tuple[str, str], AutoSyncUpdate] = {}
        refresh_feedback: dict[tuple[str, str] | None, tuple[str, str]] = {}
        drafts: dict[tuple[str, str], ComposerBuffer] = {}
        outgoing, outbox_by_id = outbox_views(cache)
        auto_sync = AutoSyncWorker(
            cache,
            sessions_root,
            session_index,
            control_socket,
            relay_config,
        )
        delivery_worker = DeliveryWorker(cache, relay_config)
        last_agent: AgentItem | None = None

        def scoped_items() -> list[AgentItem]:
            return peer_agent_items(cache, items, peer_url)

        def visible_items() -> list[AgentItem]:
            return [item for item in filtered_items(scoped_items(), query) if agent_selection.visible(selection, item)]

        def draw_refresh_feedback() -> None:
            key = agent_item_key(current) if current is not None else None
            feedback = refresh_feedback.get(key, refresh_feedback.get(None))
            if feedback is None:
                return
            text, style = feedback
            row = 4 if settings_selected else 3 if current is not None else 1
            add_text(stdscr, row, main_x + 1, " " * (main_width - 2), main_width - 2, palette["normal"])
            add_text(stdscr, row, main_x + 2, " ".join(text.split()), main_width - 4, palette[style])

        def show_refresh_feedback(text: str, *, all_agents: bool = False,
                                  failed: bool = False, completed: bool = False) -> None:
            if all_agents:
                refresh_feedback.clear()
            key = None if all_agents or current is None else agent_item_key(current)
            if completed:
                text += " · " + datetime.now().astimezone().strftime("%H:%M:%S")
            refresh_feedback[key] = (text, "info_sync_error" if failed else "muted")
            draw_refresh_feedback()
            stdscr.refresh()

        def remember_sync(item: AgentItem | None, error: str = "") -> None:
            key = AutoSyncWorker._item_key(item)
            if key is not None:
                sync_results[key] = AutoSyncUpdate(key, error, relay_cache.utc_now())

        def remember_host_sync(host_id: str, error: str = "") -> None:
            for item in items:
                if item.host_id == host_id:
                    remember_sync(item, error)

        def settings_options() -> list[tuple[str, str, str, bool]]:
            if settings_tab == "sync":
                return [("sync", mode, SYNC_MODE_DETAILS[mode], mode == active_sync_mode)
                        for mode in SYNC_MODES]
            if settings_tab == "deliveries":
                title = str(last_agent.agent.get("title") or "Untitled") if last_agent else "No conversation selected"
                scope = "this server" if peer_url is not None else "all servers"
                return [
                    ("clear_selected", "Clear failed deliveries for selected conversation",
                     title + " · Removes failed local messages and receipts.", False),
                    ("clear_all", f"Clear failed deliveries for {scope}",
                     "Removes failed local messages and receipts; keeps pending deliveries and conversation history.", False),
                ]
            return [
                ("manage", "Choose and order agents…", "Space shows/hides; Alt-Up/Down reorders. Also Ctrl-B.", False),
            ]

        def manage_agents(target: AgentItem | None) -> str:
            nonlocal selected, scroll, items, agent_order
            action = agent_selection_tui.manage(stdscr, palette, selection, scoped_items(), target)
            agent_order = [tuple(value) for value in selection["order"]]
            items = agent_selection.ordered(selection, items)
            selected = restored_selection(visible_items(), target)
            scroll = 0
            return action

        def request_stop(item: AgentItem | None) -> str:
            if item is None:
                return "No agent selected."
            show_status(stdscr, "Requesting stop…", palette["status"])
            try:
                result = stop_agent_turn(item, control_socket, relay_config)
                return ("Agent is already idle." if result.get("status") == "idle"
                        else "Stop requested; queued messages remain queued.")
            except (OSError, RuntimeError, ValueError) as exc:
                return f"Stop failed: {exc}"

        def may_disconnect() -> bool:
            if peer_url is None:
                return True
            host_id = cache.peer_host_id(peer_url)
            pending = cache.pending(host_id) if host_id else []
            return not pending or confirm_pending_disconnect(stdscr, len(pending), palette)

        def idle_without_queued_messages(item: AgentItem | None) -> bool:
            # Unknown/needs-you states must still take the explicit stop path.
            if item is not None and item.agent.get("status") != "idle":
                return False
            host_id = cache.peer_host_id(peer_url) if peer_url is not None else None
            if cache.pending(host_id):
                return False
            if item is not None:
                key = (item.host_id, str(item.agent["agent_id"]))
                receipts = cache.reconcile_message_receipts(*key, cache.records(*key)[-MAX_RECORDS:])
                if any(not receipt.get("record_key")
                       and receipt.get("current_delivery") != "failed" for receipt in receipts):
                    return False
            return True

        def finish() -> int:
            delivery_worker.close()
            auto_sync.close()
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            try:
                curses.mousemask(0)
            except curses.error:
                pass
            curses.nl()
            curses.noraw()
            return 0

        cache_signature = relay_transport.JsonlChangeMonitor.signature(cache.path)
        next_cache_check = 0.0
        while True:
            # A key may have changed selected since the previous frame. Keep
            # that latest choice, not the previously rendered conversation,
            # when a concurrent sync replaces the cached agent objects.
            before_refresh = visible_items()
            refresh_target = (before_refresh[max(0, min(selected, len(before_refresh) - 1))]
                              if before_refresh else None)
            received_update = False
            if time.monotonic() >= next_cache_check:
                signature = relay_transport.JsonlChangeMonitor.signature(cache.path)
                received_update = signature != cache_signature
                cache_signature = signature
                next_cache_check = time.monotonic() + 1.0
            while True:
                try:
                    update = auto_sync.updates.get_nowait()
                except queue.Empty:
                    break
                received_update = True
                sync_results[update.target] = update
            while True:
                try:
                    delivery_worker.updates.get_nowait()
                except queue.Empty:
                    break
                # Re-read durable receipts even in manual mode. Delivery is
                # independent of the selected agent and must not steal focus.
                received_update = True
            if received_update:
                items = all_agent_items(
                    cache,
                    include_local=include_local,
                    preferred_order=agent_order,
                )
                outgoing, outbox_by_id = outbox_views(cache)
                selected = restored_selection(
                    visible_items(), refresh_target
                )
            shown = visible_items()
            selected = max(0, min(selected, len(shown) - 1)) if shown else 0
            current = (
                None
                if settings_selected
                else (shown[selected] if shown else None)
            )
            if current is not None:
                last_agent = current
            auto_sync.select(current, active_sync_mode)
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            if height < 10 or width < 60:
                add_text(stdscr, 1, 2, "Agent Console needs a terminal at least 60×10.", width - 4, palette["status"])
                stdscr.refresh()
                key = stdscr.getch()
                if requests_console_exit(key) and may_disconnect():
                    return finish()
                if key == 3 and idle_without_queued_messages(current) and may_disconnect():
                    return finish()
                if key in (3, 7):
                    status = request_stop(current)
                elif key == 27 and escape_action(stdscr)[0] == "stop":
                    status = request_stop(current)
                continue

            sidebar = 0 if maximized else min(36, max(25, width // 3))
            main_x = 0 if maximized else sidebar + 1
            main_width = width - main_x
            if not maximized:
                sidebar_heading = palette["focus"] if focus == "agents" else palette["header"]
                add_text(stdscr, 0, 1, "Agent Console", sidebar - 2, sidebar_heading)
                working = sum(item.agent.get("status") == "active" for item in shown)
                ready = sum(item.agent.get("status") == "idle" for item in shown)
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
                        height,
                        palette["divider"],
                    )
                except curses.error:
                    pass

                row = 3
                settings_focused = settings_selected and focus == "agents"
                settings_attr = (
                    palette["selected"]
                    if settings_focused
                    else palette["header"]
                    if settings_selected
                    else palette["normal"]
                )
                if settings_focused:
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
                    "›" if settings_selected else " ",
                    1,
                    settings_attr,
                )
                add_text(
                    stdscr,
                    row,
                    3,
                    "Settings",
                    sidebar - 4,
                    settings_attr,
                )
                row += 1
                add_text(
                    stdscr,
                    row,
                    4,
                    f"Sync: {active_sync_mode}",
                    sidebar - 5,
                    palette["path"],
                )
                row += 1
                scoped = scoped_items()
                chosen_count = sum(agent_selection.visible(selection, item) for item in scoped)
                add_text(stdscr, row, 4, f"Agents: {chosen_count}/{len(scoped)}", sidebar - 5, palette["path"])
                row += 2
                visible_start = max(0, selected - max(0, (height - 10) // 3))
                for index, item in enumerate(shown[visible_start:], visible_start):
                    if row >= height - 2:
                        break
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
                    shortcut = str(index + 1) if index < 9 else " "
                    add_text(
                        stdscr,
                        row,
                        2,
                        shortcut,
                        1,
                        palette["muted"] if not selected_focused else attr,
                    )
                    add_text(stdscr, row, 4, marker, 1, marker_attr)
                    add_text(
                        stdscr,
                        row,
                        5,
                        " " + compact(title, sidebar - 7),
                        sidebar - 6,
                        attr,
                    )
                    row += 1
                    cwd = str(item.agent.get("cwd") or "").strip()
                    if row < height - 2:
                        host = compact(item.host_name, max(5, (sidebar - 8) // 2))
                        detail = host + (" · " + compact_path_end(cwd, max(1, sidebar - 11 - len(host))) if cwd else "")
                        add_text(
                            stdscr,
                            row,
                            6,
                            detail,
                            sidebar - 7,
                            palette["path"],
                        )
                        row += 1
                    row += 1

            if settings_selected:
                heading = (
                    palette["focus"]
                    if focus == "conversation"
                    else palette["header"]
                )
                add_text(
                    stdscr,
                    0,
                    main_x + 2,
                    "Settings",
                    main_width - 4,
                    heading,
                )
                detail = status or {"sync": "Synchronization preferences",
                                    "agents": "Choose visible agents and their order",
                                    "deliveries": "Clear permanently failed deliveries from the local cache"}[settings_tab]
                add_text(
                    stdscr,
                    1,
                    main_x + 2,
                    detail,
                    main_width - 4,
                    palette["muted"],
                )
                tab_x = main_x + 2
                for tab in settings_tabs:
                    label = f" {tab.capitalize()} "
                    attr = ((palette["selected"] | curses.A_BOLD) if tab == settings_tab
                            else palette["muted"])
                    add_text(stdscr, 3, tab_x, label, min(len(label), width - tab_x - 1), attr)
                    tab_x += len(label) + 2
                options = settings_options()
                settings_choice = min(settings_choice, len(options) - 1)
                visible_count = max(1, (height - 6) // 3)
                first_option = max(0, settings_choice - visible_count + 1)
                row = 5
                for index in range(first_option, min(len(options), first_option + visible_count)):
                    kind, name, description, chosen = options[index]
                    highlighted = (
                        index == settings_choice and focus == "conversation"
                    )
                    marker = "●" if chosen else "○"
                    attr = palette["selected"] if highlighted else palette["normal"]
                    if highlighted:
                        add_text(
                            stdscr,
                            row,
                            main_x + 1,
                            " " * (main_width - 2),
                            main_width - 2,
                            palette["selected"],
                        )
                    add_text(
                        stdscr,
                        row,
                        main_x + 2,
                        f"{marker} {('Sync: ' + name) if kind == 'sync' else name}",
                        main_width - 4,
                        attr,
                    )
                    detail_lines = textwrap.wrap(
                        description,
                        width=max(8, main_width - 6),
                        break_long_words=False,
                        break_on_hyphens=False,
                    )[:1]
                    for detail_offset, detail_line in enumerate(
                        detail_lines, start=1
                    ):
                        if row + detail_offset >= height - 1:
                            break
                        add_text(
                            stdscr,
                            row + detail_offset,
                            main_x + 4,
                            detail_line,
                            main_width - 6,
                            palette["muted"],
                        )
                    row += 2 + len(detail_lines)
                add_text(
                    stdscr,
                    height - 1,
                    main_x + 2,
                    "←→ tabs · ↑↓ choose · Enter apply · Tab switch pane",
                    main_width - 4,
                    palette["status"],
                )
            elif current:
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
                working = agent_is_working(agent)
                state = str(
                    agent.get("status_label") or agent.get("status") or "unknown"
                )
                if working:
                    state = f"{working_frame()} Agent working…"
                detail = f"{state} · {current.host_name} · {agent.get('cwd') or 'no basedir'} · {agent.get('agent_id')}"
                if status:
                    detail = status
                detail_style = (
                    palette["active_dot"]
                    if working and not status
                    else palette["muted"]
                )
                if status:
                    add_text(stdscr, 1, main_x + 2, detail, main_width - 4, detail_style)
                else:
                    draw_info_spans(stdscr, 1, main_x + 2, [
                        (state, "active_dot" if working else "muted"), (" · ", "muted"),
                        (current.host_name, "info_model"), (" · ", "muted"),
                        (str(agent.get("cwd") or "no basedir"), "path"), (" · ", "muted"),
                        (str(agent.get("agent_id") or ""), "muted"),
                    ], main_width - 4, palette)
                agent_key = (current.host_id, str(agent["agent_id"]))
                sync_label, sync_style = sync_status_text(
                    agent, active_sync_mode, sync_results.get(agent_key),
                    syncing=auto_sync.is_syncing(current),
                )
                model_label, sync_label = metadata_groups(agent, sync_label, main_width - 4)
                draw_info_spans(stdscr, 2, main_x + 2,
                                metadata_spans(model_label, sync_label, sync_style),
                                main_width - 4, palette)
                records = cache.records(*agent_key)
                receipts = cache.reconcile_message_receipts(*agent_key, records[-MAX_RECORDS:])
                displayed_records = records_with_message_receipts(
                    records, receipts,
                )
                displayed_records = records_with_agent_activity(
                    displayed_records,
                    agent_working=working,
                )
                rendered = transcript_lines(displayed_records, main_width - 2)
                composer_width = max(1, main_width - 7)
                composer_lines, _ = composer_layout(draft.text, composer_width)
                composer_rows = composer_visible_rows(len(composer_lines), height) + 2
                composer_top = max(4, height - composer_rows)
                viewport = max(0, composer_top - 4)
                max_scroll = max(0, len(rendered) - viewport)
                scroll = min(scroll, max_scroll)
                start = max(0, len(rendered) - viewport - scroll)
                for offset, (line, style) in enumerate(rendered[start : start + viewport]):
                    y = 4 + offset
                    if style in {"user", "message_status"}:
                        add_text(
                            stdscr,
                            y,
                            main_x + 1,
                            " " * (main_width - 2),
                            main_width - 2,
                            palette["user"],
                        )
                    if style == "code_label" or style.startswith("code:"):
                        add_text(
                            stdscr,
                            y,
                            main_x + 1,
                            " " * (main_width - 2),
                            main_width - 2,
                            palette["code"],
                        )
                    if style == "user_gap":
                        continue
                    if style.startswith("code:"):
                        add_highlighted_code(
                            stdscr,
                            y,
                            main_x + 2,
                            line,
                            main_width - 4,
                            style.partition(":")[2],
                            palette,
                        )
                    elif style in {"user", "assistant", "table_header", "table_row"}:
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
                    "No selected agents match. Press Ctrl-B to choose agents, or Ctrl-F to change the search."
                    if items else
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

            draw_refresh_feedback()
            if not current and not settings_selected:
                footer = status or "No agent selected · ^B choose agents · ^F find · ^S sync all · ^D quit"
                add_text(stdscr, height - 1, 1, footer, width - 2, palette["status"])
            try:
                curses.curs_set(
                    1
                    if current and focus == "conversation" and not settings_selected
                    else 0
                )
            except curses.error:
                pass
            stdscr.refresh()
            status = ""
            try:
                stdscr.timeout(200)
                key = stdscr.get_wch()
            except curses.error:
                continue
            finally:
                stdscr.timeout(-1)
            code = key_code(key)
            if focus == "agents" and code in (14, 16):  # Ctrl-N / Ctrl-P
                code = curses.KEY_DOWN if code == 14 else curses.KEY_UP
            if code == curses.KEY_RESIZE:
                # Terminal zoom can invalidate the physical screen even when
                # the stored draft has not changed. Rewrap and repaint it all.
                stdscr.clearok(True)
                continue
            if requests_console_exit(code if code is not None else -1):
                if may_disconnect():
                    return finish()
                continue
            extended_key = terminal_key_name(key)
            if code == 27 or extended_key in {"kUP3", "kDN3", "kUP9", "kDN9", "kUP4", "kDN4"}:
                if code == 27:
                    action, pasted = escape_action(stdscr)
                else:
                    action = {
                        "kUP3": "select_agent_up",
                        "kDN3": "select_agent_down",
                        "kUP9": "select_agent_up",
                        "kDN9": "select_agent_down",
                        "kUP4": "move_agent_up",
                        "kDN4": "move_agent_down",
                    }[extended_key]
                    pasted = ""
                if action == "stop":
                    if settings_selected:
                        settings_selected = False
                        focus = "conversation"
                    else:
                        status = request_stop(current)
                elif action in {"scroll_up", "scroll_down"}:
                    if current and not settings_selected:
                        focus = "conversation"
                        delta = 3 if action == "scroll_up" else -3
                        scroll = max(0, min(max_scroll, scroll + delta))
                elif action == "cancel":
                    pass  # Unknown/truncated escape sequences are not stop requests.
                elif action == "agent":
                    shortcut_index = int(pasted) - 1
                    if shortcut_index < len(shown):
                        selected = shortcut_index
                        settings_selected = False
                        focus = "conversation"
                        scroll = 0
                elif action in {"select_agent_up", "select_agent_down"} and shown:
                    direction = -1 if action == "select_agent_up" else 1
                    if settings_selected:
                        selected = 0 if direction > 0 else len(shown) - 1
                        settings_selected = False
                    else:
                        selected = (selected + direction) % len(shown)
                    scroll = 0
                elif action in {"move_agent_up", "move_agent_down"} and current:
                    previous_items = list(items)
                    direction = -1 if action == "move_agent_up" else 1
                    if move_agent_item(items, shown, current, direction):
                        try:
                            agent_order = save_agent_order(items, agent_order)
                            selection["order"] = [list(key) for key in agent_order]
                        except OSError as exc:
                            items = previous_items
                            status = f"Could not save agent order: {exc}"
                        selected = restored_selection(
                            visible_items(), current
                        )
                elif action == "conversation" and (current is not None or settings_selected):
                    focus = "conversation"
                elif current and focus == "conversation":
                    draft_key = (
                        current.host_id,
                        str(current.agent["agent_id"]),
                    )
                    draft = drafts.setdefault(draft_key, ComposerBuffer())
                    if action == "paste":
                        draft.insert(pasted)
                    elif action == "newline":
                        draft.insert("\n")
                    elif action == "word_left":
                        move_composer_word(draft, -1)
                    elif action == "word_right":
                        move_composer_word(draft, 1)
                    elif action == "word_backspace":
                        delete_composer_word(draft)
                    elif action == "maximize":
                        focus, maximized = change_pane(
                            focus, maximized, ord("m")
                        )
                    elif action == "agents":
                        focus = "agents"
                    else:
                        focus = "agents"
                continue
            if code == 2:  # Ctrl-B: choose available agents and reorder them.
                if manage_agents(current or last_agent) == "exit" and may_disconnect():
                    return finish()
            elif code == curses.KEY_BTAB or (
                code == 9 and not (
                    current and focus == "conversation" and not settings_selected
                    and drafts.get(
                        (current.host_id, str(current.agent["agent_id"])), ComposerBuffer()
                    ).text.strip()
                )
            ):
                if current is not None or settings_selected:
                    focus, maximized = change_pane(focus, maximized, 9)
                else:
                    focus = "agents"
            elif code == curses.KEY_MOUSE and shown:
                try:
                    _, _, _, _, button_state = curses.getmouse()
                except curses.error:
                    button_state = 0
                delta = mouse_scroll_delta(button_state)
                if delta:
                    focus = "conversation"
                    scroll = max(0, min(max_scroll, scroll + delta))
            elif (
                code in (curses.KEY_RIGHT, curses.KEY_SRIGHT, 13, curses.KEY_ENTER)
                and focus == "agents"
            ):
                focus = "conversation"
            elif code == curses.KEY_SLEFT and focus == "conversation":
                focus = "agents"
            elif (
                settings_selected
                and focus == "conversation"
                and code in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN, 10, 13, curses.KEY_ENTER)
            ):
                if code in (curses.KEY_LEFT, curses.KEY_RIGHT):
                    settings_choices[settings_tab] = settings_choice
                    if code == curses.KEY_LEFT and settings_tab == "sync":
                        focus = "agents"
                    else:
                        tab_index = settings_tabs.index(settings_tab)
                        settings_tab = settings_tabs[min(len(settings_tabs) - 1,
                                                        tab_index + (-1 if code == curses.KEY_LEFT else 1))]
                        settings_choice = min(settings_choices[settings_tab], len(settings_options()) - 1)
                elif code in (curses.KEY_UP, curses.KEY_DOWN):
                    settings_choice = max(
                        0,
                        min(
                            len(settings_options()) - 1,
                            settings_choice
                            + (-1 if code == curses.KEY_UP else 1),
                        ),
                    )
                else:
                    kind, name, _, _ = settings_options()[settings_choice]
                    try:
                        if kind == "sync":
                            user_config.set_value("agents.sync_mode", name)
                            active_sync_mode = name
                            auto_sync.select(None, active_sync_mode)
                            status = f"Automatic sync set to {name}."
                        elif kind == "manage":
                            if manage_agents(last_agent) == "exit" and may_disconnect():
                                return finish()
                        elif kind in {"clear_selected", "clear_all"}:
                            if kind == "clear_selected" and last_agent is None:
                                status = "No conversation selected."
                            else:
                                host_id = (last_agent.host_id if kind == "clear_selected"
                                           else cache.peer_host_id(peer_url) if peer_url is not None else None)
                                if peer_url is not None and host_id is None:
                                    status = "No cached server; nothing to clear."
                                else:
                                    count = cache.clear_failed_deliveries(
                                        host_id, str(last_agent.agent["agent_id"]) if kind == "clear_selected" else None,
                                    )
                                    outgoing, outbox_by_id = outbox_views(cache)
                                    status = f"Cleared {count} failed delivery record(s)."
                    except (OSError, ValueError) as exc:
                        status = f"Could not save setting: {exc}"
            elif code == curses.KEY_LEFT and focus == "conversation" and current:
                draft = drafts[(current.host_id, str(current.agent["agent_id"]))]
                if draft.cursor == 0:
                    focus = "agents"
                else:
                    draft.move(-1)
            elif code in (curses.KEY_UP, curses.KEY_DOWN):
                if focus == "agents":
                    if settings_selected:
                        if code == curses.KEY_DOWN and shown:
                            settings_selected = False
                            selected = 0
                    elif code == curses.KEY_UP and selected == 0:
                        settings_selected = True
                        settings_choice = min(settings_choice, len(settings_options()) - 1)
                    elif shown:
                        selected = max(
                            0,
                            min(
                                len(shown) - 1,
                                selected
                                + (-1 if code == curses.KEY_UP else 1),
                            ),
                        )
                    scroll = 0
                elif current:
                    draft = drafts[(current.host_id, str(current.agent["agent_id"]))]
                    move_composer_vertical(
                        draft,
                        max(1, main_width - 7),
                        -1 if code == curses.KEY_UP else 1,
                    )
            elif code == curses.KEY_PPAGE:
                focus = "conversation"
                scroll += max(1, height - 7)
            elif code == curses.KEY_NPAGE:
                focus = "conversation"
                scroll = max(0, scroll - max(1, height - 7))
            elif code in (3, 7):  # Ctrl-C / Ctrl-G
                if code == 3 and idle_without_queued_messages(current) and may_disconnect():
                    return finish()
                status = request_stop(current)
            elif code == 6:  # Ctrl-F
                entered = edit_line(stdscr, "Find: ", query)
                if entered is not None:
                    query = entered; selected = 0; scroll = 0
                    settings_selected = False
                    focus = "agents"
            elif code == 22 and current:  # Ctrl-V
                show_status(stdscr, "Opening viewer…", palette["status"])
                try:
                    open_agent_viewer(current)
                    status = "Opened conversation in viewer."
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Viewer failed: {exc}"
            elif code == 20 and current:  # Ctrl-T
                show_status(stdscr, "Opening typeset viewer…", palette["status"])
                try:
                    open_agent_viewer(current, typeset=True)
                    status = "Opened conversation in typeset viewer."
                except (OSError, RuntimeError, ValueError) as exc:
                    status = f"Typeset viewer failed: {exc}"
            elif code == 24:  # Ctrl-X
                focus, maximized = change_pane(focus, maximized, ord("m"))
            elif code == 18:  # Ctrl-R
                show_refresh_feedback("Refreshing conversation…")
                refresh_failed = False
                try:
                    if current is None:
                        status = "No conversation selected."
                        refresh_failed = True
                    elif current.host_id == "local":
                        refreshed = relay_cache.refresh_local_agent(
                            cache,
                            str(current.agent["agent_id"]),
                            sessions_root,
                            session_index,
                            control_socket,
                        )
                        status = f"Refreshed {refreshed.title}."
                        remember_sync(current)
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
                        remember_sync(current, "" if host_name else "Remote session is no longer active")
                        refresh_failed = not host_name
                        status = (
                            f"Refreshed conversation from {host_name}."
                            if host_name
                            else "Remote session is no longer active; showing cached copy."
                        )
                        delivery_text = delivery_summary_text(delivery)
                        if delivery_text:
                            status += f" Messages: {delivery_text}."
                    items = all_agent_items(
                        cache,
                        include_local=include_local,
                        preferred_order=agent_order,
                    )
                    outgoing, outbox_by_id = outbox_views(cache)
                    selected = restored_selection(
                        visible_items(), current
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    remember_sync(current, str(exc))
                    status = f"Refresh failed: {exc}"
                    refresh_failed = True
                show_refresh_feedback(status, failed=refresh_failed, completed=True)
                status = ""
            elif code == 19:  # Ctrl-S
                show_refresh_feedback("Refreshing everything…", all_agents=True)
                refresh_failed = False
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
                            remember_host_sync("local")
                        except (OSError, RuntimeError, ValueError) as exc:
                            remember_host_sync("local", str(exc))
                            failures.append(f"local: {exc}")
                    if relay_config.expanduser().exists():
                        config = relay_transport.load_config(relay_config)
                        peer_names = [peer] if peer is not None else sorted(config["peers"])
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
                            for item in items:
                                if peer_name_for_item(config, item) == peer_name:
                                    remember_sync(item)
                            synchronized += 1
                            agents_count += count
                            delivered_count += int(delivery["delivered"])
                            pending_count += int(delivery["pending"])
                            failed_count += int(delivery["failed"])
                        except (OSError, RuntimeError, ValueError) as exc:
                            for item in items:
                                if peer_name_for_item(config, item) == peer_name:
                                    remember_sync(item, str(exc))
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
                        refresh_failed = True
                    items = all_agent_items(
                        cache,
                        include_local=include_local,
                        preferred_order=agent_order,
                    )
                    outgoing, outbox_by_id = outbox_views(cache)
                    selected = restored_selection(
                        visible_items(), current
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    remember_sync(current, str(exc))
                    status = f"Synchronization failed: {exc}"
                    refresh_failed = True
                show_refresh_feedback(status, all_agents=True, failed=refresh_failed, completed=True)
                status = ""
            elif current and focus == "conversation":
                draft_key = (current.host_id, str(current.agent["agent_id"]))
                draft = drafts.setdefault(draft_key, ComposerBuffer())
                action = edit_composer_buffer(
                    draft, key, max(1, main_width - 7)
                )
                if action in {"send", "queue"} and draft.text.strip():
                    message = draft.text.strip()
                    try:
                        result = agents_ui.send_message(
                            cache,
                            {
                                "host": current.host_id,
                                "agent": current.agent["agent_id"],
                                "message": message,
                                "start": action == "send",
                            },
                            codex_bin,
                            control_socket=control_socket,
                        )
                        drafts[draft_key] = ComposerBuffer()
                        scroll = 0
                        status = (
                            "Message queued for agent."
                            if result["delivery"] == "sent"
                            else "Message queued for synchronization."
                        )
                        if result.get("start_error"):
                            status = "Message queued, but could not start: " + result["start_error"]
                        if result["delivery"] == "queued":
                            delivery_worker.submit(current)
                            outgoing, outbox_by_id = outbox_views(cache)
                    except (OSError, RuntimeError, ValueError) as exc:
                        status = f"Send failed: {exc}"
        return 0

    previous_escape_delay = None
    try:
        original_escape_delay = curses.get_escdelay()
        curses.set_escdelay(50)
        previous_escape_delay = original_escape_delay
    except (AttributeError, curses.error):
        pass  # Older curses implementations may not expose this setting.
    try:
        return int(curses.wrapper(run))
    finally:
        if previous_escape_delay is not None:
            curses.set_escdelay(previous_escape_delay)
        # Restore the terminal even if the console raises unexpectedly.
        sequence = mouse_protocol_sequence(False)
        if sequence:
            sys.stdout.write(sequence)
            sys.stdout.flush()


def command_ui(args: argparse.Namespace) -> int:
    cache = relay_cache.RelayCache(args.cache_file)
    no_local = getattr(args, "no_local", False) or bool(getattr(args, "peer", None))
    sync_mode = getattr(args, "sync", None) or str(
        user_config.value("agents.sync_mode")
    )
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
            sync_mode=sync_mode,
            peer=getattr(args, "peer", None),
        )
    except curses.error as exc:
        print(f"error: could not start Agent Console: {exc}", file=sys.stderr)
        return 1
