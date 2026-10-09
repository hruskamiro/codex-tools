"""Authenticated snapshot transport for Codex relay peers."""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import ssl
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from codex_tools import paths, relay_cache


PROTOCOL_VERSION = 1
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_MESSAGE_BYTES = 128 * 1024


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
    cache: relay_cache.RelayCache, agent: dict[str, Any]
) -> dict[str, Any]:
    public = dict(agent)
    public.pop("host_id", None)
    public.pop("origin_path", None)
    public["records"] = cache.records("local", str(agent["agent_id"]))
    return public


def snapshot_payload(cache: relay_cache.RelayCache, config: dict[str, Any]) -> dict[str, Any]:
    agents = [public_agent_payload(cache, agent) for agent in cache.agents("local")]
    identity = config["identity"]
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
        "agent": public_agent_payload(cache, agent),
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


def make_handler(
    cache: relay_cache.RelayCache,
    config: dict[str, Any],
    refresh: Callable[[], None] | None = None,
    refresh_agent: Callable[[str], None] | None = None,
    deliver_message: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    token = str(config["identity"]["token"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            request_path = urlparse(self.path).path
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
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

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
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(cache, config, refresh, refresh_agent, deliver_message),
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
