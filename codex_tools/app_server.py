"""ChatGPT-plan connections and the Codex app-server transport."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from codex_tools import __version__, paths


APP_NAME = "Codex Tools App Server"
APP_ID = "codex-tools-app-server"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
SHARING_SCOPE = "chatgpt.tokens.use.direct"
DEFAULT_ROOT = paths.user_config_dir() / "app-server"
DEFAULT_CODEX_HOME = paths.user_state_dir() / "app-server" / "codex-home"
DEFAULT_USAGE_CODEX_HOME = Path("~/.codex").expanduser()
BASE_INSTRUCTIONS = "Complete the requested structured task. Do not use tools. Return only the schema-conforming result."
TOKEN_EXPIRY_MARGIN_SECONDS = 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_name(value: str) -> str:
    if not value or not all(char.isalnum() or char in {"-", "_"} for char in value):
        raise argparse.ArgumentTypeError(
            "connection names may contain only letters, numbers, '-' and '_'"
        )
    return value


def _connections_path(root: Path) -> Path:
    return root.expanduser().resolve() / "connections.json"


def _host_path(root: Path) -> Path:
    return root.expanduser().resolve() / "host.json"


def _lock_path(root: Path) -> Path:
    return root.expanduser().resolve() / ".connections.lock"


def _private_json(path: Path, payload: dict[str, Any]) -> None:
    paths.ensure_private_dir(path.parent)
    if path.parent.is_symlink():
        raise ValueError(f"credential directory must not be a symbolic link: {path.parent}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return default
    if path.is_symlink() or not path.is_file() or info.st_mode & 0o077:
        raise ValueError(f"credential file must be owner-only (0600): {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError(f"credential file must belong to the current user: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid credential file: {path}")
    return value


class ConnectionLock:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.handle = None

    def __enter__(self) -> "ConnectionLock":
        import fcntl

        paths.ensure_private_dir(self.root)
        self.handle = _lock_path(self.root).open("a+")
        _lock_path(self.root).chmod(0o600)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        import fcntl

        assert self.handle is not None
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def load_connections(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    payload = _read_private_json(
        _connections_path(root), {"version": 1, "connections": {}}
    )
    if payload.get("version") != 1 or not isinstance(payload.get("connections"), dict):
        raise ValueError("unsupported app-server connection file")
    return payload


def save_connections(payload: dict[str, Any], root: Path = DEFAULT_ROOT) -> None:
    _private_json(_connections_path(root), payload)


def host_id(root: Path = DEFAULT_ROOT) -> str:
    path = _host_path(root)
    payload = _read_private_json(path, {})
    value = payload.get("ext_agent_host_id")
    if value is None:
        value = f"urn:uuid:{uuid.uuid4()}"
        _private_json(path, {"version": 1, "ext_agent_host_id": value})
    if payload and (payload.get("version") != 1 or not isinstance(value, str)):
        raise ValueError(f"invalid app-server host record: {path}")
    return str(value)


def _post_form(url: str, fields: dict[str, str], timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI authorization request failed ({exc.code}): {body}") from exc
    try:
        value = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI authorization returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("OpenAI authorization returned an invalid response")
    return value


def _get_json(url: str, timeout: int = 60) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not retrieve OpenAI metadata: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("OpenAI metadata response is invalid")
    return value


def _verify_id_token(token: str, client_id: str, nonce: str | None) -> dict[str, Any]:
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:  # pragma: no cover - packaging catches this
        raise RuntimeError("PyJWT is required for ChatGPT sign-in") from exc

    metadata = _get_json(DISCOVERY_URL)
    jwks_uri = metadata.get("jwks_uri")
    if not isinstance(jwks_uri, str):
        raise RuntimeError("OpenAI discovery metadata has no JWKS URI")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "ES256"}:
            raise RuntimeError(f"unsupported ID-token algorithm: {algorithm!r}")
        key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=[algorithm], audience=client_id, issuer=ISSUER
        )
    except Exception as exc:
        raise RuntimeError(f"could not validate OpenAI identity token: {exc}") from exc
    if nonce is not None and claims.get("nonce") != nonce:
        raise RuntimeError("OpenAI identity token has an invalid nonce")
    if not isinstance(claims.get("sub"), str):
        raise RuntimeError("OpenAI identity token has no subject")
    return claims


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_CallbackServer"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_error(404)
            return
        values = urllib.parse.parse_qs(parsed.query)
        self.server.callback = {key: entries[0] for key, entries in values.items()}
        body = b"ChatGPT authorization received. You can close this window."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _CallbackServer(ThreadingHTTPServer):
    callback: dict[str, str] | None = None


def _wait_for_callback(server: _CallbackServer, timeout: int) -> dict[str, str]:
    server.timeout = 0.25
    deadline = time.monotonic() + timeout
    while server.callback is None and time.monotonic() < deadline:
        server.handle_request()
    if server.callback is None:
        raise TimeoutError("ChatGPT authorization timed out")
    return server.callback


def _connection_from_tokens(
    *, name: str, client_id: str, host: str, tokens: dict[str, Any], nonce: str | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    id_token = tokens.get("id_token")
    access_token = tokens.get("access_token")
    if not isinstance(id_token, str) or not isinstance(access_token, str):
        raise RuntimeError("OpenAI token response is missing required tokens")
    claims = _verify_id_token(id_token, client_id, nonce)
    if previous and previous.get("subject") and previous["subject"] != claims["sub"]:
        raise RuntimeError("the selected ChatGPT account does not match this connection")
    scopes = str(tokens.get("scope", "")).split()
    if SHARING_SCOPE not in scopes:
        raise RuntimeError("ChatGPT plan usage was not granted")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str):
        refresh_token = previous.get("refresh_token") if previous else None
    expires_in = int(tokens.get("expires_in", 3600))
    return {
        "name": name,
        "status": "connected",
        "client_id": client_id,
        "ext_agent_host_id": host,
        "subject": claims["sub"],
        "email": claims.get("email"),
        "display_name": claims.get("name"),
        "scopes": scopes,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_at": time.time() + expires_in,
        "earliest_refresh_at": tokens.get("earliest_refresh_at"),
        "saved_at": utc_now(),
    }


def authorize(
    name: str = "default", root: Path = DEFAULT_ROOT, timeout: int = 600,
    browser_open: Any = webbrowser.open,
) -> dict[str, Any]:
    valid_name(name)
    with ConnectionLock(root):
        saved = load_connections(root)
        previous = saved["connections"].get(name)
        host = host_id(root)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
        redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
        client_id = previous.get("client_id") if previous else "dynamic_agent_client"
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "ext_agent_host_id": host,
        }
        if previous:
            if previous.get("email"):
                params["login_hint"] = previous["email"]
        else:
            params["agent_name_hint"] = APP_NAME
        url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        print(f"Opening ChatGPT authorization for connection {name!r}...")
        if not browser_open(url):
            print(f"Open this URL in a browser:\n{url}")
        try:
            callback = _wait_for_callback(server, timeout)
        finally:
            server.server_close()
        if callback.get("state") != state:
            raise RuntimeError("ChatGPT authorization returned an invalid state")
        if callback.get("error"):
            raise RuntimeError(f"ChatGPT authorization failed: {callback['error']}")
        code = callback.get("code")
        issued_client = callback.get("client_id") or client_id
        if not code or issued_client == "dynamic_agent_client":
            raise RuntimeError("ChatGPT authorization did not return a complete registration")
        if previous and issued_client != previous.get("client_id"):
            raise RuntimeError("ChatGPT authorization returned a different client registration")
        tokens = _post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": issued_client,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "resource": RESOURCE,
            },
        )
        connection = _connection_from_tokens(
            name=name, client_id=issued_client, host=host, tokens=tokens,
            nonce=nonce, previous=previous,
        )
        saved["connections"][name] = connection
        save_connections(saved, root)
        return connection


def _refresh(connection: dict[str, Any]) -> dict[str, Any]:
    refresh_token = connection.get("refresh_token")
    if not isinstance(refresh_token, str):
        raise RuntimeError("connection has no refresh token; sign in again")
    tokens = _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": connection["client_id"],
            "refresh_token": refresh_token,
            "resource": RESOURCE,
        },
    )
    return _connection_from_tokens(
        name=connection["name"], client_id=connection["client_id"],
        host=connection["ext_agent_host_id"], tokens=tokens,
        nonce=None, previous=connection,
    )


def access_token(
    name: str = "default",
    root: Path = DEFAULT_ROOT,
    minimum_validity_seconds: int = TOKEN_EXPIRY_MARGIN_SECONDS,
) -> str:
    valid_name(name)
    if minimum_validity_seconds < 0:
        raise ValueError("minimum token validity cannot be negative")
    with ConnectionLock(root):
        saved = load_connections(root)
        connection = saved["connections"].get(name)
        if not isinstance(connection, dict) or connection.get("status") != "connected":
            raise RuntimeError(f"app-server connection is not signed in: {name}")
        deadline = time.time() + minimum_validity_seconds
        if float(connection.get("expires_at", 0)) <= deadline:
            connection = _refresh(connection)
            saved["connections"][name] = connection
            save_connections(saved, root)
        if float(connection.get("expires_at", 0)) <= deadline:
            raise RuntimeError(
                "the requested timeout exceeds the access token lifetime; "
                "use a shorter timeout"
            )
        return str(connection["access_token"])


def disconnect(name: str = "default", root: Path = DEFAULT_ROOT) -> bool:
    valid_name(name)
    with ConnectionLock(root):
        saved = load_connections(root)
        connection = saved["connections"].get(name)
        if not isinstance(connection, dict):
            raise FileNotFoundError(f"app-server connection does not exist: {name}")
        refresh_token = connection.get("refresh_token")
        revoked = False
        if isinstance(refresh_token, str):
            metadata = _get_json(DISCOVERY_URL)
            endpoint = metadata.get("revocation_endpoint")
            if not isinstance(endpoint, str):
                raise RuntimeError("OpenAI discovery metadata has no revocation endpoint")
            _post_form(
                endpoint,
                {
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": connection["client_id"],
                },
            )
            revoked = True
        for key in ("access_token", "refresh_token", "id_token", "expires_at"):
            connection.pop(key, None)
        connection["status"] = "disconnected"
        connection["scopes"] = []
        connection["saved_at"] = utc_now()
        saved["connections"][name] = connection
        save_connections(saved, root)
        return revoked


def public_connection(connection: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: connection.get(key)
        for key in (
            "name", "status", "email", "display_name", "client_id",
            "expires_at", "saved_at",
        )
    }
    result["sharing"] = (
        connection.get("status") == "connected"
        and SHARING_SCOPE in connection.get("scopes", [])
    )
    return result


def list_models(access_token_value: str) -> list[dict[str, str]]:
    request = urllib.request.Request(
        f"{RESOURCE}/models",
        headers={"Authorization": f"Bearer {access_token_value}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not list ChatGPT-plan models: {exc}") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("ChatGPT-plan model response is invalid")
    return [
        {"slug": item["slug"], "display_name": item.get("display_name", item["slug"])}
        for item in models
        if isinstance(item, dict)
        and item.get("visibility") == "list"
        and isinstance(item.get("slug"), str)
    ]


def app_server_command(codex_bin: str = "codex") -> list[str]:
    return [
        codex_bin, "app-server", "--listen", "stdio://",
        "--disable", "shell_tool",
        "-c", 'model_provider="openai_chatgpt_plan"',
        "-c", 'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
        "-c", f'model_providers.openai_chatgpt_plan.base_url="{RESOURCE}"',
        "-c", 'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
        "-c", 'model_providers.openai_chatgpt_plan.wire_api="responses"',
        "-c", "model_providers.openai_chatgpt_plan.requires_openai_auth=false",
        "-c", "model_providers.openai_chatgpt_plan.supports_websockets=false",
    ]


def initialize_params() -> dict[str, Any]:
    return {
        "clientInfo": {"name": APP_NAME, "title": APP_NAME, "version": __version__},
        "capabilities": {"experimentalApi": True},
    }


def request_app_server(
    *, method: str, params: dict[str, Any], access_token_value: str | None,
    timeout: int = 120, codex_bin: str = "codex",
    codex_home: Path = DEFAULT_CODEX_HOME,
    isolated: bool = True,
) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    if isolated:
        paths.ensure_private_dir(codex_home)
        runtime = tempfile.TemporaryDirectory(prefix="runtime-", dir=codex_home)
        runtime_home = Path(runtime.name)
        workspace = runtime_home / "workspace"
        paths.ensure_private_dir(workspace)
    else:
        if not codex_home.is_dir():
            raise FileNotFoundError(f"Codex home does not exist: {codex_home}")
        runtime = tempfile.TemporaryDirectory(prefix="codex-tools-usage-")
        runtime_home = codex_home
        workspace = Path(runtime.name)
    env = dict(os.environ)
    if access_token_value is not None:
        env["ACCESS_TOKEN"] = access_token_value
    else:
        env.pop("ACCESS_TOKEN", None)
    env["CODEX_HOME"] = str(runtime_home)
    process = subprocess.Popen(
        app_server_command(codex_bin), cwd=workspace, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr_parts: list[str] = []
    stderr_thread = threading.Thread(
        target=lambda: stderr_parts.extend(process.stderr.readlines()), daemon=True
    )
    stderr_thread.start()
    stdout_lines: Queue[str | None] = Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            stdout_lines.put(line)
        stdout_lines.put(None)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stdout_thread.start()
    deadline = time.monotonic() + timeout

    def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def request(request_id: int, request_method: str, request_params: dict[str, Any]) -> dict[str, Any]:
        send({"method": request_method, "id": request_id, "params": request_params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(app_server_command(codex_bin), timeout)
            try:
                line = stdout_lines.get(timeout=remaining)
            except Empty as exc:
                raise subprocess.TimeoutExpired(app_server_command(codex_bin), timeout) from exc
            if line is None:
                detail = "".join(stderr_parts).strip()
                raise RuntimeError(
                    f"codex app-server exited with status {process.poll()}"
                    + (f": {detail}" if detail else "")
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(
                    f"app-server {request_method} failed: {message['error']}"
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    try:
        request(1, "initialize", initialize_params())
        send({"method": "initialized", "params": {}})
        return request(2, method, params)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        runtime.cleanup()


def _rate_limit_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("usedPercent"), int):
        return None
    resets_at = value.get("resetsAt")
    return {
        "used_percent": value["usedPercent"],
        "remaining_percent": max(0, 100 - value["usedPercent"]),
        "window_duration_minutes": value.get("windowDurationMins"),
        "resets_at": resets_at,
        "resets_at_utc": (
            datetime.fromtimestamp(resets_at, timezone.utc).isoformat()
            if isinstance(resets_at, int) else None
        ),
    }


def public_rate_limits(response: dict[str, Any]) -> dict[str, Any]:
    raw_limits = response.get("rateLimitsByLimitId")
    if not isinstance(raw_limits, dict):
        single = response.get("rateLimits")
        raw_limits = {"codex": single} if isinstance(single, dict) else {}
    limits = {}
    for limit_id, raw in raw_limits.items():
        if not isinstance(raw, dict):
            continue
        limits[str(limit_id)] = {
            "limit_name": raw.get("limitName"),
            "normal_model_slug": raw.get("normalModelSlug"),
            "plan_type": raw.get("planType"),
            "primary": _rate_limit_window(raw.get("primary")),
            "secondary": _rate_limit_window(raw.get("secondary")),
            "credits": raw.get("credits"),
            "spend_control_reached": raw.get("spendControlReached"),
            "rate_limit_reached_type": raw.get("rateLimitReachedType"),
        }
    reset_credits = response.get("rateLimitResetCredits")
    return {
        "captured_at": utc_now(),
        "ordinary_usage_allowed": response.get("ordinaryUsageAllowed"),
        "limits": limits,
        "reset_credits_available": (
            reset_credits.get("availableCount")
            if isinstance(reset_credits, dict) else None
        ),
    }


def rate_limits(
    timeout: int = 120, codex_bin: str = "codex",
    codex_home: Path = DEFAULT_USAGE_CODEX_HOME,
) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    response = request_app_server(
        method="account/rateLimits/read",
        params={"excludeResetCreditDetails": True, "supportsLunaReserve": False},
        access_token_value=None, timeout=timeout, codex_bin=codex_bin,
        codex_home=codex_home, isolated=False,
    )
    return {
        **public_rate_limits(response),
        "source": {"kind": "codex-home", "path": str(codex_home)},
    }


def rate_limit_delta(before: Any, after: Any) -> dict[str, Any]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {}
    before_limits = before.get("limits", {})
    after_limits = after.get("limits", {})
    deltas = {}
    for limit_id in sorted(set(before_limits) & set(after_limits)):
        first = before_limits[limit_id].get("primary")
        last = after_limits[limit_id].get("primary")
        if not isinstance(first, dict) or not isinstance(last, dict):
            continue
        same_window = first.get("resets_at") == last.get("resets_at")
        deltas[limit_id] = {
            "same_window": same_window,
            "used_percentage_points": (
                last["used_percent"] - first["used_percent"] if same_window else None
            ),
        }
    return deltas


@dataclass
class AppServerResult:
    returncode: int
    events: list[dict[str, Any]]
    parse_errors: list[dict[str, Any]]
    stderr: str
    duration_s: float
    thread_id: str | None
    usage: dict[str, int] | None
    response_text: str
    turn_status: str


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    mapping = {
        "totalTokens": "total_tokens",
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "cacheWriteInputTokens": "cache_write_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
    }
    return {
        target: value[source]
        for source, target in mapping.items()
        if isinstance(value.get(source), int)
    }


def app_server_tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {"agentMessage", "reasoning", "userMessage"}
    found = []
    for event in events:
        if event.get("method") not in {"item/started", "item/completed"}:
            continue
        item = event.get("params", {}).get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type and item_type not in allowed:
            found.append({"method": event["method"], "item_type": item_type})
    return found


def run_structured_task(
    *, prompt: str, schema: dict[str, Any], model: str, reasoning_effort: str,
    timeout: int, access_token_value: str, codex_bin: str = "codex",
    codex_home: Path = DEFAULT_CODEX_HOME,
) -> AppServerResult:
    started = time.monotonic()
    codex_home = codex_home.expanduser().resolve()
    paths.ensure_private_dir(codex_home)
    runtime = tempfile.TemporaryDirectory(prefix="runtime-", dir=codex_home)
    runtime_home = Path(runtime.name)
    workspace = runtime_home / "workspace"
    paths.ensure_private_dir(workspace)
    env = dict(os.environ)
    env["ACCESS_TOKEN"] = access_token_value
    env["CODEX_HOME"] = str(runtime_home)
    process = subprocess.Popen(
        app_server_command(codex_bin), cwd=workspace, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr_parts: list[str] = []
    stderr_thread = threading.Thread(
        target=lambda: stderr_parts.extend(process.stderr.readlines()), daemon=True
    )
    stderr_thread.start()
    stdout_lines: Queue[str | None] = Queue()

    def read_stdout() -> None:
        for line in process.stdout:
            stdout_lines.put(line)
        stdout_lines.put(None)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stdout_thread.start()
    events: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    next_id = 1
    deadline = time.monotonic() + timeout

    def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def receive() -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(app_server_command(codex_bin), timeout)
        try:
            line = stdout_lines.get(timeout=remaining)
        except Empty as exc:
            raise subprocess.TimeoutExpired(app_server_command(codex_bin), timeout) from exc
        if line is None:
            raise RuntimeError(f"codex app-server exited with status {process.poll()}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append({"error": str(exc), "text": line.rstrip()})
            return receive()
        events.append(message)
        return message

    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal next_id
        request_id = next_id
        next_id += 1
        send({"method": method, "id": request_id, "params": params})
        while True:
            message = receive()
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"app-server {method} failed: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    response_parts: list[str] = []
    usage = None
    thread_id = None
    turn_status = "failed"
    try:
        request(
            "initialize",
            initialize_params(),
        )
        send({"method": "initialized", "params": {}})
        thread = request(
            "thread/start",
            {
                "model": model, "modelProvider": "openai_chatgpt_plan",
                "cwd": str(workspace), "approvalPolicy": "never",
                "sandbox": "read-only", "ephemeral": True,
                "dynamicTools": [], "environments": [],
                "selectedCapabilityRoots": [],
                "baseInstructions": BASE_INSTRUCTIONS,
            },
        )
        thread_id = thread.get("thread", {}).get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError("app-server did not return a thread ID")
        request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "outputSchema": schema,
                "effort": reasoning_effort,
                "summary": "none",
            },
        )
        while True:
            message = receive()
            method = message.get("method")
            params = message.get("params", {})
            if method == "item/agentMessage/delta":
                response_parts.append(str(params.get("delta", "")))
            elif method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage", {})
                usage = _usage(token_usage.get("last"))
            elif method == "turn/completed":
                turn = params.get("turn", {})
                turn_status = str(turn.get("status", "failed"))
                if turn_status != "completed":
                    raise RuntimeError(f"app-server turn ended with status {turn_status}")
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        runtime.cleanup()
    return AppServerResult(
        returncode=0, events=events, parse_errors=parse_errors,
        stderr="".join(stderr_parts), duration_s=time.monotonic() - started,
        thread_id=thread_id, usage=usage, response_text="".join(response_parts).strip(),
        turn_status=turn_status,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools app-server",
        description="Manage ChatGPT-plan connections for the app-server backend.",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Connect a ChatGPT account/workspace.")
    login.add_argument("--connection", type=valid_name, default="default")
    login.add_argument("--timeout", type=int, default=600)
    status = commands.add_parser("status", help="Show a saved connection.")
    status.add_argument("--connection", type=valid_name, default="default")
    usage = commands.add_parser("usage", help="Show current ChatGPT-plan usage windows.")
    usage.add_argument("--timeout", type=int, default=120)
    usage.add_argument("--codex-bin", default="codex")
    usage.add_argument(
        "--codex-home", type=Path, default=DEFAULT_USAGE_CODEX_HOME,
        help=f"Authenticated Codex home. Default: {DEFAULT_USAGE_CODEX_HOME}",
    )
    commands.add_parser("list", help="List saved connections.")
    logout = commands.add_parser("logout", help="Revoke a connection's renewable session.")
    logout.add_argument("--connection", type=valid_name, default="default")
    test = commands.add_parser("test", help="Run one minimal plan-backed app-server turn.")
    test.add_argument("--connection", type=valid_name, default="default")
    test.add_argument("--model", help="Model slug. Default: first available model.")
    test.add_argument("--reasoning-effort", default="low")
    test.add_argument("--timeout", type=int, default=120)
    test.add_argument("--codex-bin", default="codex")
    test.add_argument("--app-server-home", type=Path, default=DEFAULT_CODEX_HOME)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "login":
            result = public_connection(
                authorize(args.connection, args.root, args.timeout)
            )
        elif args.command == "status":
            connection = load_connections(args.root)["connections"].get(args.connection)
            if not isinstance(connection, dict):
                raise FileNotFoundError(
                    f"app-server connection does not exist: {args.connection}"
                )
            result = public_connection(connection)
        elif args.command == "usage":
            result = rate_limits(
                args.timeout, args.codex_bin, args.codex_home,
            )
        elif args.command == "list":
            saved = load_connections(args.root)["connections"]
            result = [public_connection(saved[name]) for name in sorted(saved)]
        elif args.command == "logout":
            result = {"connection": args.connection, "revoked": disconnect(args.connection, args.root)}
        elif args.command == "test":
            token = access_token(
                args.connection,
                args.root,
                args.timeout + TOKEN_EXPIRY_MARGIN_SECONDS,
            )
            models = list_models(token)
            model = args.model or (models[0]["slug"] if models else None)
            if model is None:
                raise RuntimeError("no ChatGPT-plan model is available")
            schema = {
                "type": "object",
                "properties": {"message": {"type": "string", "const": "App server works."}},
                "required": ["message"],
                "additionalProperties": False,
            }
            execution = run_structured_task(
                prompt='Return {"message":"App server works."}.', schema=schema,
                model=model, reasoning_effort=args.reasoning_effort,
                timeout=args.timeout, access_token_value=token,
                codex_bin=args.codex_bin, codex_home=args.app_server_home,
            )
            result = {
                "connection": args.connection, "model": model,
                "response": json.loads(execution.response_text),
                "usage": execution.usage, "duration_s": execution.duration_s,
            }
        else:
            raise AssertionError(f"unhandled app-server command: {args.command}")
    except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
