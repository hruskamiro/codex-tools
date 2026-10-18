"""Authenticated snapshot transport for Codex relay peers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import ssl
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from codex_tools import paths, relay_cache, search


PROTOCOL_VERSION = 1
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_BYTES = 128 * 1024
MAX_EVENT_SCAN_BYTES = 4 * 1024 * 1024
MAX_EVENT_BATCH = 500
MAX_EVENT_WAIT_SECONDS = 20.0
EVENT_POLL_SECONDS = 0.2


def file_generation(stat_result: Any) -> str:
    return f"{int(stat_result.st_dev):x}:{int(stat_result.st_ino):x}"


def encode_event_cursor(
    agent_id: str,
    generation: str,
    offset: int,
    secret: str,
    fingerprint: str,
) -> str:
    body = json.dumps(
        {"a": agent_id, "g": generation, "h": fingerprint, "o": offset},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def decode_event_cursor(cursor: str, agent_id: str, secret: str) -> dict[str, Any]:
    try:
        encoded, signature = cursor.rsplit(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if (
            not isinstance(payload, dict)
            or payload.get("a") != agent_id
            or not isinstance(payload.get("g"), str)
            or not isinstance(payload.get("h"), str)
            or not isinstance(payload.get("o"), int)
            or payload["o"] < 0
        ):
            raise ValueError
        return payload
    except (binascii.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid relay event cursor") from exc


def event_cursor_for_path(path: Path, agent_id: str, secret: str) -> str:
    with path.open("rb") as handle:
        stat_result = os.fstat(handle.fileno())
        offset = relay_cache.complete_jsonl_size(handle, int(stat_result.st_size))
        fingerprint = relay_cache.jsonl_boundary_fingerprint(handle, offset)
    return encode_event_cursor(
        agent_id, file_generation(stat_result), offset, secret, fingerprint
    )


def read_jsonl_events(
    path: Path,
    agent_id: str,
    cursor: str,
    secret: str,
    *,
    limit: int = MAX_EVENT_BATCH,
) -> dict[str, Any]:
    """Read only complete records appended after an authenticated cursor."""
    cursor_payload = decode_event_cursor(cursor, agent_id, secret)
    with path.open("rb") as handle:
        stat_result = os.fstat(handle.fileno())
        generation = file_generation(stat_result)
        complete_size = relay_cache.complete_jsonl_size(
            handle, int(stat_result.st_size)
        )
        offset = int(cursor_payload["o"])
        rewritten = (
            offset <= complete_size
            and relay_cache.jsonl_boundary_fingerprint(handle, offset)
            != cursor_payload["h"]
        )
        if (
            cursor_payload["g"] != generation
            or offset > complete_size
            or rewritten
        ):
            return {
                "reset_required": True,
                "generation": generation,
                "events": [],
                "has_more": False,
            }
        if offset == complete_size:
            return {
                "reset_required": False,
                "generation": generation,
                "events": [],
                "next_cursor": cursor,
                "has_more": False,
            }
        handle.seek(offset)
        scan_size = min(MAX_EVENT_SCAN_BYTES, complete_size - offset)
        data = handle.read(scan_size)
        next_offset = offset
        if offset + len(data) < complete_size:
            boundary = data.rfind(b"\n")
            if boundary < 0:
                # Codex records can contain large inline tool results (notably
                # images).  They are intentionally excluded from the relay's
                # text event stream, but an old cursor must still be able to
                # advance past one.  Scan for the boundary without retaining
                # the oversized record in memory.
                scanned_offset = offset + len(data)
                while scanned_offset < complete_size:
                    chunk = handle.read(
                        min(MAX_EVENT_SCAN_BYTES, complete_size - scanned_offset)
                    )
                    boundary = chunk.find(b"\n")
                    if boundary >= 0:
                        next_offset = scanned_offset + boundary + 1
                        break
                    scanned_offset += len(chunk)
                else:  # pragma: no cover - complete_size always ends at a newline
                    next_offset = complete_size
                data = b""
            else:
                data = data[: boundary + 1]

    events = []
    for raw_line in data.splitlines(keepends=True):
        next_offset += len(raw_line)
        if not raw_line.endswith(b"\n"):
            break
        try:
            value = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        records = search.text_records_from_event(
            value, line_no=0, include_tools=False, include_system=False
        )
        for record in records:
            if record.role == "user" and search.is_internal_prompt_text(
                record.text
            ):
                continue
            events.append(
                {
                    "sequence": f"{generation}:{next_offset}",
                    "type": "record.appended",
                    "record": {
                        "timestamp": record.timestamp,
                        "role": record.role,
                        "text": record.text,
                        "line_no": record.line_no,
                        "record_type": record.record_type,
                    },
                }
            )
        if len(events) >= max(1, min(limit, MAX_EVENT_BATCH)):
            break
    with path.open("rb") as handle:
        fingerprint = relay_cache.jsonl_boundary_fingerprint(handle, next_offset)
    next_cursor = encode_event_cursor(
        agent_id, generation, next_offset, secret, fingerprint
    )
    return {
        "reset_required": False,
        "generation": generation,
        "events": events,
        "next_cursor": next_cursor,
        "has_more": next_offset < complete_size,
    }


class JsonlChangeMonitor:
    """One lightweight stat watcher shared by all long-poll requests."""

    def __init__(self, interval: float = EVENT_POLL_SECONDS) -> None:
        self.interval = interval
        self._condition = threading.Condition()
        self._paths: dict[Path, tuple[int, int, int, int] | None] = {}
        self._thread: threading.Thread | None = None
        self._stopped = False

    @staticmethod
    def signature(path: Path) -> tuple[int, int, int, int] | None:
        try:
            value = path.stat()
        except OSError:
            return None
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)

    def wait(self, path: Path, previous: Any, timeout: float) -> None:
        if timeout <= 0:
            return
        with self._condition:
            self._paths[path] = previous
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._watch, name="relay-jsonl-watch", daemon=True
                )
                self._thread.start()
            self._condition.wait_for(
                lambda: self._stopped
                or self._paths.get(path) != previous,
                timeout=timeout,
            )

    def _watch(self) -> None:
        while True:
            with self._condition:
                if self._stopped:
                    return
                paths = list(self._paths)
            changes = []
            for path in paths:
                signature = self.signature(path)
                with self._condition:
                    if self._paths.get(path) != signature:
                        self._paths[path] = signature
                        changes.append(path)
            if changes:
                with self._condition:
                    self._condition.notify_all()
            time.sleep(self.interval)

    def close(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()


def load_config(path: Path = paths.RELAY_CONFIG_FILE) -> dict[str, Any]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported or invalid relay configuration")
    identity = payload.get("identity")
    peers = payload.get("peers")
    if not isinstance(identity, dict) or not isinstance(peers, dict):
        raise ValueError("invalid relay configuration")
    if not all(
        isinstance(identity.get(field), str) and identity[field].strip()
        for field in ("id", "name", "token")
    ):
        raise ValueError("relay configuration has an invalid identity")
    for name, peer in peers.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(peer, dict)
            or not isinstance(peer.get("url"), str)
            or not isinstance(peer.get("token"), str)
            or not peer["token"].strip()
        ):
            raise ValueError("relay configuration has an invalid peer")
        validate_url(
            peer["url"],
            allow_insecure_http=bool(peer.get("allow_insecure_http")),
        )
    return payload


def initialize_config(path: Path, name: str, *, force: bool = False) -> dict[str, Any]:
    path = path.expanduser()
    if path.exists() and not force:
        raise FileExistsError(f"relay configuration already exists: {path}")
    payload = {
        "version": PROTOCOL_VERSION,
        "identity": {
            "id": str(uuid.uuid4()),
            "name": name.strip() or "This machine",
            "token": secrets.token_urlsafe(32),
        },
        "peers": {},
    }
    paths.ensure_private_dir(path.parent)
    paths.write_private_text(path, json.dumps(payload, indent=2) + "\n")
    return payload


def save_config(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser()
    paths.ensure_private_dir(path.parent)
    paths.write_private_text(path, json.dumps(payload, indent=2) + "\n")


def is_loopback_name(host: str | None) -> bool:
    value = (host or "").strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    return is_loopback_name(urlparse(url).hostname)


def validate_url(url: str, *, allow_insecure_http: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("relay URL must use http:// or https://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("relay URL must not contain credentials, query, or fragment")
    if (
        parsed.scheme == "http"
        and not is_loopback_name(parsed.hostname)
        and not allow_insecure_http
    ):
        raise ValueError(
            "remote HTTP is unencrypted; use HTTPS or pass --allow-insecure-http "
            "for an encrypted VPN/tunnel"
        )
    return url.rstrip("/")


def add_peer(
    path: Path,
    name: str,
    url: str,
    token: str,
    *,
    allow_insecure_http: bool = False,
    ca_file: Path | None = None,
) -> dict[str, Any]:
    config = load_config(path)
    if not name.strip():
        raise ValueError("peer name must not be empty")
    if not token.strip():
        raise ValueError("pairing key must not be empty")
    peer = {
        "name": name.strip(),
        "url": validate_url(url, allow_insecure_http=allow_insecure_http),
        "token": token.strip(),
        "allow_insecure_http": allow_insecure_http,
        "ca_file": str(ca_file.expanduser().resolve()) if ca_file else None,
    }
    config["peers"][name.strip()] = peer
    save_config(path, config)
    return peer


def public_agent_payload(
    cache: relay_cache.RelayCache,
    agent: dict[str, Any],
    *,
    cursor_secret: str = "",
) -> dict[str, Any]:
    public = dict(agent)
    public.pop("host_id", None)
    public.pop("event_cursor", None)
    origin_path = Path(str(public.pop("origin_path", "")))
    public["records"] = cache.records("local", str(agent["agent_id"]))
    if cursor_secret and origin_path.is_file():
        try:
            (
                session,
                stat_result,
                offset,
                fingerprint,
            ) = relay_cache.load_recent_session_snapshot(origin_path)
            public["records"] = [
                {
                    "timestamp": record.timestamp,
                    "role": record.role,
                    "text": record.text,
                    "line_no": record.line_no,
                    "record_type": record.record_type,
                }
                for record in session.records
                if not (
                    record.role == "user"
                    and search.is_internal_prompt_text(record.text)
                )
            ]
            public["events_cursor"] = encode_event_cursor(
                str(agent["agent_id"]),
                file_generation(stat_result),
                offset,
                cursor_secret,
                fingerprint,
            )
        except OSError:
            pass
    return public


def snapshot_payload(cache: relay_cache.RelayCache, config: dict[str, Any]) -> dict[str, Any]:
    identity = config["identity"]
    agents = [
        public_agent_payload(
            cache, agent, cursor_secret=str(identity["token"])
        )
        for agent in cache.agents("local")
    ]
    return {
        "protocol": PROTOCOL_VERSION,
        "host": {"id": identity["id"], "name": identity["name"]},
        "cursor": relay_cache.utc_now(),
        "agents": agents,
    }


def agent_snapshot_payload(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    agent_id: str,
) -> dict[str, Any] | None:
    agent = next(
        (
            value
            for value in cache.agents("local")
            if str(value["agent_id"]) == agent_id
        ),
        None,
    )
    if agent is None:
        return None
    identity = config["identity"]
    return {
        "protocol": PROTOCOL_VERSION,
        "host": {"id": identity["id"], "name": identity["name"]},
        "cursor": relay_cache.utc_now(),
        "agent": public_agent_payload(
            cache, agent, cursor_secret=str(identity["token"])
        ),
    }


def cached_agent_from_payload(value: dict[str, Any]) -> relay_cache.CachedAgent:
    if not value.get("agent_id"):
        raise ValueError("invalid agent in relay snapshot")
    records = value.get("records") or []
    if not isinstance(records, list):
        raise ValueError("invalid records in relay snapshot")
    return relay_cache.CachedAgent(
        agent_id=str(value["agent_id"]),
        session_id=str(value.get("session_id") or ""),
        thread_id=str(value.get("thread_id") or ""),
        rollout_id=str(value.get("rollout_id") or ""),
        title=str(value.get("title") or value["agent_id"]),
        title_source=str(value.get("title_source") or "relay"),
        cwd=str(value.get("cwd") or ""),
        source=str(value.get("source") or ""),
        created_at=str(value.get("created_at") or ""),
        updated_at=str(value.get("updated_at") or ""),
        origin_path="",
        status=str(value.get("status") or "unknown"),
        status_label=str(value.get("status_label") or "unknown"),
        event_cursor=str(value.get("events_cursor") or ""),
        records=tuple(
            relay_cache.CachedRecord(
                timestamp=str(record.get("timestamp") or ""),
                role=str(record.get("role") or ""),
                text=str(record.get("text") or ""),
                line_no=int(record.get("line_no") or 0),
                record_type=str(record.get("record_type") or "message"),
            )
            for record in records
            if isinstance(record, dict)
        ),
    )


def apply_snapshot(
    cache: relay_cache.RelayCache,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
) -> int:
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("unsupported relay protocol version")
    host = payload.get("host")
    raw_agents = payload.get("agents")
    if not isinstance(host, dict) or not isinstance(raw_agents, list):
        raise ValueError("invalid relay snapshot")
    host_id = str(host.get("id") or "")
    host_name = str(host.get("name") or "")
    if not host_id or not host_name:
        raise ValueError("relay snapshot has no host identity")
    cached = []
    for value in raw_agents:
        if not isinstance(value, dict):
            raise ValueError("invalid agent in relay snapshot")
        cached.append(cached_agent_from_payload(value))
    cache.ensure_host(host_id, host_name, base_url=base_url)
    cache.replace_host_snapshot(
        host_id, host_name, cached, cursor=str(payload.get("cursor") or "")
    )
    return len(cached)


def apply_agent_snapshot(
    cache: relay_cache.RelayCache,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
) -> str:
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("unsupported relay protocol version")
    host = payload.get("host")
    value = payload.get("agent")
    if not isinstance(host, dict) or not isinstance(value, dict):
        raise ValueError("invalid relay agent snapshot")
    host_id = str(host.get("id") or "")
    host_name = str(host.get("name") or "")
    if not host_id or not host_name:
        raise ValueError("relay snapshot has no host identity")
    cached = cached_agent_from_payload(value)
    cache.upsert_agent(
        host_id,
        host_name,
        cached,
        cursor=str(payload.get("cursor") or ""),
        base_url=base_url,
    )
    return cached.agent_id


def apply_agent_events(
    cache: relay_cache.RelayCache,
    payload: dict[str, Any],
) -> int:
    if payload.get("protocol") != PROTOCOL_VERSION:
        raise ValueError("unsupported relay protocol version")
    if payload.get("reset_required"):
        raise ValueError("relay event cursor requires a fresh snapshot")
    host = payload.get("host")
    agent_id = str(payload.get("agent_id") or "")
    events = payload.get("events")
    next_cursor = str(payload.get("next_cursor") or "")
    if (
        not isinstance(host, dict)
        or not str(host.get("id") or "")
        or not agent_id
        or not isinstance(events, list)
        or not next_cursor
    ):
        raise ValueError("invalid relay event response")
    records = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "record.appended":
            continue
        record = event.get("record")
        if not isinstance(record, dict):
            raise ValueError("invalid relay record event")
        records.append(
            relay_cache.CachedRecord(
                timestamp=str(record.get("timestamp") or ""),
                role=str(record.get("role") or ""),
                text=str(record.get("text") or ""),
                line_no=int(record.get("line_no") or 0),
                record_type=str(record.get("record_type") or "message"),
            )
        )
    appended = cache.append_agent_records(
        str(host["id"]), agent_id, records, event_cursor=next_cursor
    )
    agent_status = payload.get("agent_status")
    if isinstance(agent_status, dict):
        status = str(agent_status.get("status") or "")
        status_label = str(agent_status.get("status_label") or "")
        if status and status_label:
            cache.update_agent_status(
                str(host["id"]),
                agent_id,
                status=status,
                status_label=status_label,
                updated_at=str(agent_status.get("updated_at") or ""),
            )
    return appended


def fetch_snapshot(peer: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    return fetch_json(
        peer,
        str(peer["url"]).rstrip("/") + "/v1/snapshot",
        timeout,
    )


def fetch_agent(
    peer: dict[str, Any], agent_id: str, timeout: float = 10.0
) -> dict[str, Any]:
    url = (
        str(peer["url"]).rstrip("/")
        + "/v1/agents/"
        + quote(agent_id, safe="")
    )
    return fetch_json(peer, url, timeout)


def fetch_agent_events(
    peer: dict[str, Any],
    agent_id: str,
    cursor: str,
    *,
    wait: float = 0,
    limit: int = MAX_EVENT_BATCH,
    timeout: float | None = None,
) -> dict[str, Any]:
    bounded_wait = max(0.0, min(float(wait), MAX_EVENT_WAIT_SECONDS))
    query = urlencode(
        {"after": cursor, "wait": f"{bounded_wait:g}", "limit": limit}
    )
    url = (
        str(peer["url"]).rstrip("/")
        + "/v1/agents/"
        + quote(agent_id, safe="")
        + "/events?"
        + query
    )
    request_timeout = timeout if timeout is not None else max(10.0, bounded_wait + 5.0)
    return fetch_json(peer, url, request_timeout)


def fetch_json(
    peer: dict[str, Any], url: str, timeout: float
) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {peer['token']}",
            "Accept": "application/json",
        },
    )
    context = None
    if str(peer["url"]).startswith("https://"):
        context = ssl.create_default_context(cafile=peer.get("ca_file") or None)
    with urlopen(request, timeout=timeout, context=context) as response:
        body = response.read(MAX_SNAPSHOT_BYTES + 1)
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise ValueError("relay snapshot exceeds the 32 MiB safety limit")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("relay returned an invalid snapshot")
    return payload


def post_message(
    peer: dict[str, Any], message: dict[str, Any], timeout: float = 10.0
) -> dict[str, Any]:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ValueError("relay message exceeds the 128 KiB safety limit")
    request = Request(
        str(peer["url"]).rstrip("/") + "/v1/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {peer['token']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    context = None
    if str(peer["url"]).startswith("https://"):
        context = ssl.create_default_context(cafile=peer.get("ca_file") or None)
    with urlopen(request, timeout=timeout, context=context) as response:
        response_body = response.read(MAX_MESSAGE_BYTES + 1)
    if len(response_body) > MAX_MESSAGE_BYTES:
        raise ValueError("relay response exceeds the 128 KiB safety limit")
    payload = json.loads(response_body)
    if not isinstance(payload, dict):
        raise ValueError("relay returned an invalid message response")
    return payload


def deliver_pending(
    cache: relay_cache.RelayCache,
    peer: dict[str, Any],
    host_id: str,
) -> dict[str, Any]:
    """Attempt every pending message once, preserving failures for retry."""
    result: dict[str, Any] = {
        "delivered": 0,
        "pending": 0,
        "failed": 0,
        "errors": [],
    }
    pending_items = cache.pending(host_id)
    for index, item in enumerate(pending_items):
        message_id = str(item["message_id"])
        request_payload = {
            "message_id": message_id,
            "agent_id": str(item["agent_id"]),
            "kind": str(item["kind"]),
            "payload": item["payload"],
        }
        try:
            response = post_message(peer, request_payload)
            if response.get("delivery") != "accepted":
                raise ValueError("relay did not acknowledge the message")
        except HTTPError as exc:
            permanent = exc.code in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.CONFLICT,
                HTTPStatus.GONE,
            }
            state = "failed" if permanent else "pending"
            error = f"HTTP {exc.code}: {exc.reason}"
            cache.record_outbox_attempt(message_id, state=state, error=error)
            result[state] += 1
            result["errors"].append({"message_id": message_id, "error": error})
        except (OSError, ValueError) as exc:
            error = str(exc)
            cache.record_outbox_attempt(
                message_id, state="pending", error=error
            )
            result["pending"] += len(pending_items) - index
            result["errors"].append({"message_id": message_id, "error": error})
            break
        else:
            cache.record_outbox_attempt(message_id, state="delivered")
            result["delivered"] += 1
    return result


def agent_events_payload(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    agent_id: str,
    cursor: str,
    *,
    wait: float = 0,
    limit: int = MAX_EVENT_BATCH,
    monitor: JsonlChangeMonitor | None = None,
) -> dict[str, Any] | None:
    agent = next(
        (
            value
            for value in cache.agents("local")
            if str(value["agent_id"]) == agent_id
        ),
        None,
    )
    if agent is None:
        return None
    path = Path(str(agent.get("origin_path") or ""))
    if not path.is_file():
        return None
    identity = config["identity"]
    deadline = time.monotonic() + max(
        0.0, min(float(wait), MAX_EVENT_WAIT_SECONDS)
    )
    while True:
        before = JsonlChangeMonitor.signature(path)
        result = read_jsonl_events(
            path,
            agent_id,
            cursor,
            str(identity["token"]),
            limit=limit,
        )
        result.update(
            {
                "protocol": PROTOCOL_VERSION,
                "host": {"id": identity["id"], "name": identity["name"]},
                "agent_id": agent_id,
            }
        )
        if (
            result.get("reset_required")
            or result.get("events")
            or result.get("has_more")
            or result.get("next_cursor") != cursor
        ):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        after = JsonlChangeMonitor.signature(path)
        if after != before:
            continue
        if monitor is None:
            time.sleep(min(EVENT_POLL_SECONDS, remaining))
        else:
            monitor.wait(path, after, remaining)


def agent_status_payload(
    cache: relay_cache.RelayCache, agent_id: str
) -> dict[str, str] | None:
    agent = next(
        (
            value
            for value in cache.agents("local")
            if str(value["agent_id"]) == agent_id
        ),
        None,
    )
    if agent is None:
        return None
    return {
        "status": str(agent.get("status") or "unknown"),
        "status_label": str(agent.get("status_label") or "unknown"),
        "updated_at": str(agent.get("updated_at") or ""),
    }


def make_handler(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    refresh: Callable[[], None] | None = None,
    refresh_agent: Callable[[str], None] | None = None,
    deliver_message: Callable[[str, str, dict[str, Any]], None] | None = None,
    event_monitor: JsonlChangeMonitor | None = None,
) -> type[BaseHTTPRequestHandler]:
    token = str(config["identity"]["token"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            request_path = parsed.path
            if request_path == "/v1/health":
                self._json({"service": "codex-tools-relay", "protocol": PROTOCOL_VERSION})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            if request_path == "/v1/snapshot":
                if refresh is not None:
                    refresh()
                self._json(snapshot_payload(cache, config))
            elif request_path.startswith("/v1/agents/") and request_path.endswith(
                "/events"
            ):
                encoded_id = request_path.removeprefix("/v1/agents/")[: -len("/events")]
                agent_id = unquote(encoded_id)
                if not agent_id or "/" in agent_id:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    cursor = str(query.get("after", [""])[0])
                    wait = float(query.get("wait", ["0"])[0])
                    limit = int(query.get("limit", [str(MAX_EVENT_BATCH)])[0])
                    if not cursor or wait < 0 or limit < 1:
                        raise ValueError("invalid relay event request")
                    payload = agent_events_payload(
                        cache,
                        config,
                        agent_id,
                        cursor,
                        wait=wait,
                        limit=limit,
                        monitor=event_monitor,
                    )
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                if payload is None:
                    self._json({"error": "agent not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    if refresh_agent is not None:
                        refresh_agent(agent_id)
                except ValueError:
                    self._json({"error": "agent not found"}, HTTPStatus.NOT_FOUND)
                    return
                current_status = agent_status_payload(cache, agent_id)
                if current_status is not None:
                    payload["agent_status"] = current_status
                self._json(payload)
            elif request_path.startswith("/v1/agents/"):
                agent_id = unquote(request_path.removeprefix("/v1/agents/"))
                if not agent_id or "/" in agent_id:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    if refresh_agent is not None:
                        refresh_agent(agent_id)
                except ValueError:
                    self._json({"error": "agent not found"}, HTTPStatus.NOT_FOUND)
                    return
                payload = agent_snapshot_payload(cache, config, agent_id)
                if payload is None:
                    self._json({"error": "agent not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._json(payload)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            request_path = urlparse(self.path).path
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            if request_path != "/v1/messages" or deliver_message is None:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    self.headers.get_content_type() != "application/json"
                    or length < 1
                    or length > MAX_MESSAGE_BYTES
                ):
                    raise ValueError("invalid message request")
                request_payload = json.loads(self.rfile.read(length))
                if not isinstance(request_payload, dict):
                    raise ValueError("JSON object required")
                message_id = str(request_payload.get("message_id") or "")
                agent_id = str(request_payload.get("agent_id") or "")
                kind = str(request_payload.get("kind") or "")
                payload = request_payload.get("payload")
                if (
                    not message_id
                    or not agent_id
                    or kind != "message"
                    or not isinstance(payload, dict)
                    or not isinstance(payload.get("text"), str)
                    or not payload["text"].strip()
                ):
                    raise ValueError("invalid relay message")
                reservation = cache.begin_incoming(
                    message_id, agent_id, kind, payload
                )
                if reservation in {"accepted", "processing"}:
                    self._json(
                        {
                            "delivery": reservation,
                            "message_id": message_id,
                            "duplicate": True,
                        }
                    )
                    return
                try:
                    deliver_message(message_id, agent_id, payload)
                except FileNotFoundError as exc:
                    cache.finish_incoming(
                        message_id, accepted=False, error=str(exc)
                    )
                    self._json({"error": str(exc)}, HTTPStatus.GONE)
                    return
                except (OSError, RuntimeError, ValueError) as exc:
                    cache.finish_incoming(
                        message_id, accepted=False, error=str(exc)
                    )
                    self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                    return
                cache.finish_incoming(message_id, accepted=True)
                self._json(
                    {"delivery": "accepted", "message_id": message_id}
                )
            except ValueError as exc:
                status = (
                    HTTPStatus.CONFLICT
                    if "already used" in str(exc)
                    else HTTPStatus.BAD_REQUEST
                )
                self._json({"error": str(exc)}, status)

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A polling client may cancel an in-flight request when the UI
                # changes agent, refreshes, or exits.  The response is no longer
                # useful, and socketserver would otherwise print a noisy traceback.
                return

    return Handler


def serve(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    host: str,
    port: int,
    *,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    allow_insecure_http: bool = False,
    refresh: Callable[[], None] | None = None,
    refresh_agent: Callable[[str], None] | None = None,
    deliver_message: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> None:
    validate_server_transport(
        host,
        port,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_insecure_http=allow_insecure_http,
    )
    event_monitor = JsonlChangeMonitor()
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(
            cache,
            config,
            refresh,
            refresh_agent,
            deliver_message,
            event_monitor,
        ),
    )
    if tls_cert and tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls_cert), str(tls_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        event_monitor.close()
        server.server_close()


def validate_server_transport(
    host: str,
    port: int,
    *,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    allow_insecure_http: bool = False,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("relay port must be between 1 and 65535")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("--tls-cert and --tls-key must be provided together")
    if not tls_cert and not is_loopback_name(host) and not allow_insecure_http:
        raise ValueError(
            "refusing unencrypted remote relay; configure TLS or pass "
            "--allow-insecure-http for an encrypted VPN/tunnel"
        )
