"""Durable local cache shared by relay clients, agents, and the viewer."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from codex_tools import agents, paths, search


SCHEMA_VERSION = 2
DEFAULT_CACHE_PATH = paths.RELAY_CACHE_FILE
RECENT_RECORD_LIMIT = 20
RECENT_LINE_LIMIT = 20_000
REVERSE_READ_SIZE = 64 * 1024


@dataclass(frozen=True)
class CachedRecord:
    timestamp: str
    role: str
    text: str
    line_no: int
    record_type: str


@dataclass(frozen=True)
class CachedAgent:
    agent_id: str
    session_id: str
    thread_id: str
    rollout_id: str
    title: str
    title_source: str
    cwd: str
    source: str
    created_at: str
    updated_at: str
    origin_path: str
    records: tuple[CachedRecord, ...]
    status: str = "unknown"
    status_label: str = "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RelayCache:
    def __init__(self, path: Path = DEFAULT_CACHE_PATH) -> None:
        self.path = path.expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        parent = self.path.parent
        if parent.is_symlink():
            raise ValueError(f"relay cache directory must not be a symbolic link: {parent}")
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
            parent.chmod(0o700)
        connection = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._ensure_schema(connection)
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, 1, SCHEMA_VERSION}:
            raise ValueError(
                f"unsupported relay cache schema {version}; expected {SCHEMA_VERSION}"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS relay_hosts (
                host_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                base_url TEXT,
                sync_cursor TEXT,
                created_at TEXT NOT NULL,
                last_sync_at TEXT
            );

            CREATE TABLE IF NOT EXISTS relay_agents (
                host_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                rollout_id TEXT NOT NULL,
                title TEXT NOT NULL,
                title_source TEXT NOT NULL,
                cwd TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                origin_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                status_label TEXT NOT NULL DEFAULT 'unknown',
                PRIMARY KEY (host_id, agent_id),
                FOREIGN KEY (host_id) REFERENCES relay_hosts(host_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS relay_records (
                host_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                line_no INTEGER NOT NULL,
                record_type TEXT NOT NULL,
                PRIMARY KEY (host_id, agent_id, ordinal),
                FOREIGN KEY (host_id, agent_id)
                    REFERENCES relay_agents(host_id, agent_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS relay_outbox (
                message_id TEXT PRIMARY KEY,
                host_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                FOREIGN KEY (host_id) REFERENCES relay_hosts(host_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS relay_agents_updated
                ON relay_agents(host_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS relay_outbox_state
                ON relay_outbox(host_id, state, created_at);
            """
        )
        agent_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(relay_agents)")
        }
        if "status" not in agent_columns:
            connection.execute(
                "ALTER TABLE relay_agents ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "status_label" not in agent_columns:
            connection.execute(
                "ALTER TABLE relay_agents ADD COLUMN status_label TEXT NOT NULL DEFAULT 'unknown'"
            )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    def ensure_host(
        self,
        host_id: str,
        name: str,
        *,
        base_url: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self.connect() as opened:
                self.ensure_host(
                    host_id, name, base_url=base_url, connection=opened
                )
                opened.commit()
            return
        connection.execute(
            """
            INSERT INTO relay_hosts(host_id, name, base_url, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(host_id) DO UPDATE SET
                name = excluded.name,
                base_url = COALESCE(excluded.base_url, relay_hosts.base_url)
            """,
            (host_id, name, base_url, utc_now()),
        )

    def replace_host_snapshot(
        self,
        host_id: str,
        name: str,
        cached_agents: Sequence[CachedAgent],
        *,
        cursor: str | None = None,
    ) -> None:
        synchronized_at = utc_now()
        with self.connect() as connection, connection:
            self.ensure_host(host_id, name, connection=connection)
            connection.execute(
                "DELETE FROM relay_agents WHERE host_id = ?", (host_id,)
            )
            for agent in cached_agents:
                connection.execute(
                    """
                    INSERT INTO relay_agents(
                        host_id, agent_id, session_id, thread_id, rollout_id,
                        title, title_source, cwd, source, created_at, updated_at,
                        origin_path, status, status_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        host_id,
                        agent.agent_id,
                        agent.session_id,
                        agent.thread_id,
                        agent.rollout_id,
                        agent.title,
                        agent.title_source,
                        agent.cwd,
                        agent.source,
                        agent.created_at,
                        agent.updated_at,
                        agent.origin_path,
                        agent.status,
                        agent.status_label,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO relay_records(
                        host_id, agent_id, ordinal, timestamp, role, text,
                        line_no, record_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            host_id,
                            agent.agent_id,
                            ordinal,
                            record.timestamp,
                            record.role,
                            record.text,
                            record.line_no,
                            record.record_type,
                        )
                        for ordinal, record in enumerate(agent.records)
                    ],
                )
            connection.execute(
                """
                UPDATE relay_hosts
                SET sync_cursor = ?, last_sync_at = ?
                WHERE host_id = ?
                """,
                (cursor, synchronized_at, host_id),
            )

    def hosts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT h.*,
                       COUNT(DISTINCT a.agent_id) AS agent_count,
                       COUNT(DISTINCT CASE WHEN o.state = 'pending'
                                          THEN o.message_id END) AS pending_count
                FROM relay_hosts h
                LEFT JOIN relay_agents a ON a.host_id = h.host_id
                LEFT JOIN relay_outbox o ON o.host_id = h.host_id
                GROUP BY h.host_id
                ORDER BY h.name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def agents(self, host_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM relay_agents
                WHERE host_id = ?
                ORDER BY updated_at DESC
                """,
                (host_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def records(self, host_id: str, agent_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, role, text, line_no, record_type
                FROM relay_records
                WHERE host_id = ? AND agent_id = ?
                ORDER BY ordinal
                """,
                (host_id, agent_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue(
        self,
        host_id: str,
        agent_id: str,
        payload: dict[str, Any],
        *,
        kind: str = "message",
        message_id: str | None = None,
    ) -> str:
        selected_id = message_id or str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO relay_outbox(
                    message_id, host_id, agent_id, kind, payload_json, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    selected_id,
                    host_id,
                    agent_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
        return selected_id

    def pending(self, host_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM relay_outbox WHERE state = 'pending'"
        values: tuple[Any, ...] = ()
        if host_id is not None:
            query += " AND host_id = ?"
            values = (host_id,)
        query += " ORDER BY created_at, message_id"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result


def cached_agent_from_local(
    summary: dict[str, Any], session: search.Session
) -> CachedAgent:
    agent_id = agents.agent_identifier(summary)
    if not agent_id:
        raise ValueError(f"session has no stable ID: {summary.get('path')}")
    return CachedAgent(
        agent_id=agent_id,
        session_id=str(summary.get("sessionId") or ""),
        thread_id=str(summary.get("threadId") or ""),
        rollout_id=str(summary.get("rolloutId") or ""),
        title=agents.display_name(summary),
        title_source=str(summary.get("titleSource") or ""),
        cwd=str(summary.get("cwd") or ""),
        source=str(summary.get("source") or ""),
        created_at=str(summary.get("createdAt") or ""),
        updated_at=str(summary.get("lastAt") or ""),
        origin_path=str(summary.get("path") or ""),
        records=tuple(
            CachedRecord(
                timestamp=record.timestamp,
                role=record.role,
                text=record.text,
                line_no=record.line_no,
                record_type=record.record_type,
            )
            for record in session.records
            if not (
                record.role == "user"
                and search.is_internal_prompt_text(record.text)
            )
        ),
        status=str(summary.get("status") or "unknown"),
        status_label=str(summary.get("statusLabel") or "unknown"),
    )


def reverse_lines(path: Path) -> Iterator[bytes]:
    """Yield non-empty lines from a file, newest first, without reading it all."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position:
            size = min(REVERSE_READ_SIZE, position)
            position -= size
            handle.seek(position)
            pieces = (handle.read(size) + remainder).split(b"\n")
            remainder = pieces.pop(0)
            for line in reversed(pieces):
                if line:
                    yield line
        if remainder:
            yield remainder


def load_recent_session(
    path: Path,
    limit: int = RECENT_RECORD_LIMIT,
    line_limit: int = RECENT_LINE_LIMIT,
) -> search.Session:
    records: list[search.TextRecord] = []
    for line_count, raw_line in enumerate(reverse_lines(path), start=1):
        if b'"response_item"' not in raw_line or b'"message"' not in raw_line:
            if line_count >= line_limit:
                break
            continue
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        extracted = search.text_records_from_event(
            event, line_no=0, include_tools=False, include_system=False
        )
        records.extend(reversed(extracted))
        if len(records) >= limit or line_count >= line_limit:
            break
    return search.Session(path=path, records=list(reversed(records[:limit])))


def refresh_local(
    cache: RelayCache,
    sessions_root: Path,
    session_index: Path,
    control_socket: Path | None = None,
) -> int:
    cached = []
    for summary in agents.agent_summaries(
        sessions_root, session_index, control_socket
    ):
        session_path = Path(str(summary.get("path") or ""))
        try:
            session = load_recent_session(session_path)
        except (OSError, ValueError):
            session = search.Session(path=session_path)
        cached.append(cached_agent_from_local(summary, session))
    cache.replace_host_snapshot("local", "This machine", cached)
    return len(cached)
