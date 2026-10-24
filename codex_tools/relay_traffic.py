"""Best-effort relay payload accounting shared with the supervisor and viewer.

These are application bytes, not SSH/interface counters. No tokens, URLs with
query strings, or message contents are stored. Accounting must never break sync.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator


_category: ContextVar[str] = ContextVar("relay_traffic_category", default="console")


@contextmanager
def category(name: str) -> Iterator[None]:
    token = _category.set(name)
    try:
        yield
    finally:
        _category.reset(token)


class TrafficStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path, timeout=0.1)) as connection, connection:
            path.chmod(0o600)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS traffic "
                "(peer TEXT PRIMARY KEY, uploaded INTEGER NOT NULL, downloaded INTEGER NOT NULL)"
            )
            # Additive: an older console/supervisor can keep writing `traffic`.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS viewer_traffic "
                "(peer TEXT PRIMARY KEY, uploaded INTEGER NOT NULL, downloaded INTEGER NOT NULL)"
            )

    def connect(self) -> sqlite3.Connection:
        # Do not recreate a removed cache or block message delivery for metrics.
        return sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.1)

    def add(self, peer: str, uploaded: int, downloaded: int, *, category: str = "console") -> None:
        table = {"console": "traffic", "viewer": "viewer_traffic"}[category]
        with closing(self.connect()) as connection, connection:
            connection.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?) ON CONFLICT(peer) DO UPDATE SET "
                "uploaded = uploaded + excluded.uploaded, "
                "downloaded = downloaded + excluded.downloaded",
                (peer.rstrip("/"), uploaded, downloaded),
            )

    def totals(self, peer: str | None) -> tuple[int, int]:
        console_up, console_down, viewer_up, viewer_down = self.snapshot(peer)
        return console_up + viewer_up, console_down + viewer_down

    def snapshot(self, peer: str | None) -> tuple[int, int, int, int]:
        """Read both categories in one SQLite snapshot, including other processes."""
        with closing(self.connect()) as connection:
            query = (
                "SELECT kind, COALESCE(SUM(uploaded), 0), COALESCE(SUM(downloaded), 0) FROM ("
                "SELECT 0 AS kind, * FROM traffic UNION ALL SELECT 1 AS kind, * FROM viewer_traffic)"
            )
            rows = connection.execute(
                query + (" WHERE peer = ?" if peer else "") + " GROUP BY kind",
                (peer.rstrip("/"),) if peer else (),
            ).fetchall()
            totals = {int(kind): (int(up), int(down)) for kind, up, down in rows}
            return (*totals.get(0, (0, 0)), *totals.get(1, (0, 0)))


_store: TrafficStore | None = None


def enable(cache_path: Path) -> TrafficStore | None:
    global _store
    if not isinstance(cache_path, Path):
        _store = None
        return None
    try:
        _store = TrafficStore(cache_path.with_name(cache_path.name + ".traffic"))
    except (OSError, sqlite3.Error):
        _store = None
    return _store


def record(peer: str, *, uploaded: int = 0, downloaded: int = 0,
           category: str | None = None) -> None:
    if _store is not None:
        try:
            _store.add(peer, uploaded, downloaded, category=category or _category.get())
        except (OSError, sqlite3.Error):
            pass


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


class SessionTraffic:
    def __init__(self, store: TrafficStore | None, peer: str | None) -> None:
        self.store = store
        self.peer = peer
        self.baseline = self.read()
        self.updated_at = 0.0
        self.label = "Relay data unavailable"
        self.detail = ""

    def read(self) -> tuple[int, int, int, int] | None:
        try:
            return self.store.snapshot(self.peer) if self.store else None
        except (OSError, sqlite3.Error):
            return None

    def summary(self) -> str:
        now = time.monotonic()
        if now - self.updated_at < 1:
            return self.label
        self.updated_at = now
        totals = self.read()
        if totals is None or self.baseline is None:
            self.label = "Relay data unavailable"
            self.detail = ""
        else:
            console_up, console_down, viewer_up, viewer_down = (
                max(0, value - initial) for value, initial in zip(totals, self.baseline)
            )
            uploaded, downloaded = console_up + viewer_up, console_down + viewer_down
            self.label = (f"Relay data this session (approx.): down {format_bytes(downloaded)}"
                          f" / up {format_bytes(uploaded)} / total {format_bytes(uploaded + downloaded)}")
            self.detail = (f"Console/sync: {format_bytes(console_up + console_down)}"
                           f"  |  Viewer/files: {format_bytes(viewer_up + viewer_down)}")
        return self.label

    def details(self) -> str:
        self.summary()
        return self.detail
