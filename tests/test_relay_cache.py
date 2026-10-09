from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_tools import relay, relay_cache, search


def cached_agent(agent_id: str, title: str = "viewer") -> relay_cache.CachedAgent:
    return relay_cache.CachedAgent(
        agent_id=agent_id,
        session_id=agent_id,
        thread_id=agent_id,
        rollout_id=agent_id,
        title=title,
        title_source="index",
        cwd="/work/project",
        source="cli",
        created_at="2026-10-07T10:00:00Z",
        updated_at="2026-10-07T11:00:00Z",
        origin_path=f"/sessions/{agent_id}.jsonl",
        records=(
            relay_cache.CachedRecord(
                timestamp="2026-10-07T10:00:00Z",
                role="user",
                text="Question",
                line_no=1,
                record_type="message",
            ),
            relay_cache.CachedRecord(
                timestamp="2026-10-07T11:00:00Z",
                role="assistant",
                text="Answer",
                line_no=2,
                record_type="message",
            ),
        ),
    )


class RelayCacheTests(unittest.TestCase):
    def test_existing_cache_parent_permissions_are_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "shared"
            parent.mkdir(mode=0o755)
            cache = relay_cache.RelayCache(parent / "relay.sqlite3")

            cache.hosts()

            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(cache.path.stat().st_mode & 0o777, 0o600)

    def test_snapshot_round_trips_and_prunes_stale_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "relay.sqlite3"
            cache = relay_cache.RelayCache(path)
            cache.replace_host_snapshot(
                "local", "This machine", [cached_agent("one"), cached_agent("two")]
            )
            cache.replace_host_snapshot(
                "local", "This machine", [cached_agent("two", "docs")]
            )

            hosts = cache.hosts()
            cached = cache.agents("local")
            records = cache.records("local", "two")

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(hosts[0]["agent_count"], 1)
            self.assertEqual([item["agent_id"] for item in cached], ["two"])
            self.assertEqual([item["text"] for item in records], ["Question", "Answer"])

    def test_outbox_ids_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot(
                "home", "Home", [cached_agent("viewer")]
            )

            first = cache.enqueue(
                "home", "viewer", {"text": "First"}, message_id="message-1"
            )
            second = cache.enqueue(
                "home", "viewer", {"text": "Duplicate"}, message_id="message-1"
            )

            self.assertEqual(first, second)
            pending = cache.pending("home")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["payload"], {"text": "First"})

    def test_outbox_tracks_attempts_delivery_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot(
                "home", "Home", [cached_agent("viewer")]
            )
            message_id = cache.enqueue("home", "viewer", {"text": "Continue"})

            cache.record_outbox_attempt(
                message_id, state="pending", error="offline"
            )
            cache.record_outbox_attempt(message_id, state="delivered")

            message = cache.outbox()[0]
            self.assertEqual(message["state"], "delivered")
            self.assertEqual(message["attempt_count"], 2)
            self.assertIsNotNone(message["delivered_at"])
            self.assertIsNone(message["last_error"])
            self.assertEqual(cache.pending("home"), [])

    def test_incoming_message_ids_are_idempotent_and_retry_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            payload = {"text": "Continue"}

            self.assertEqual(
                cache.begin_incoming("message-1", "agent-1", "message", payload),
                "new",
            )
            cache.finish_incoming("message-1", accepted=True)
            self.assertEqual(
                cache.begin_incoming("message-1", "agent-1", "message", payload),
                "accepted",
            )
            with self.assertRaisesRegex(ValueError, "different content"):
                cache.begin_incoming(
                    "message-1", "agent-1", "message", {"text": "Changed"}
                )

            self.assertEqual(
                cache.begin_incoming("message-2", "agent-1", "message", payload),
                "new",
            )
            cache.finish_incoming("message-2", accepted=False, error="busy")
            self.assertEqual(
                cache.begin_incoming("message-2", "agent-1", "message", payload),
                "retry",
            )

    def test_upsert_agent_preserves_other_host_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot(
                "remote", "Desktop", [cached_agent("one"), cached_agent("two")]
            )

            cache.upsert_agent(
                "remote",
                "Desktop",
                cached_agent("one", "updated title"),
                base_url="https://desktop:8780",
            )

            agents = cache.agents("remote")
            self.assertEqual({item["agent_id"] for item in agents}, {"one", "two"})
            updated = next(item for item in agents if item["agent_id"] == "one")
            self.assertEqual(updated["title"], "updated title")
            self.assertEqual(cache.hosts()[0]["base_url"], "https://desktop:8780")

    def test_refresh_local_normalizes_session_records(self) -> None:
        summary = {
            "title": "viewer",
            "titleSource": "index",
            "id": "thread-1",
            "threadId": "thread-1",
            "sessionId": "session-1",
            "rolloutId": "rollout-1",
            "path": "/sessions/one.jsonl",
            "cwd": "/work/project",
            "source": "cli",
            "createdAt": "2026-10-07T10:00:00Z",
            "lastAt": "2026-10-07T11:00:00Z",
        }
        session = search.Session(
            path=Path(summary["path"]),
            records=[
                search.TextRecord(
                    "0",
                    "user",
                    "<environment_context>secret</environment_context>",
                    1,
                    "message",
                ),
                search.TextRecord("1", "user", "Hello", 2, "message"),
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            with (
                patch.object(
                    relay_cache.agents, "agent_summaries", return_value=[summary]
                ),
                patch.object(
                    relay_cache, "load_recent_session", return_value=session
                ),
            ):
                count = relay_cache.refresh_local(
                    cache, Path("/sessions"), Path("/index")
                )

            self.assertEqual(count, 1)
            self.assertEqual(cache.agents("local")[0]["agent_id"], "thread-1")
            records = cache.records("local", "thread-1")
            self.assertEqual([record["text"] for record in records], ["Hello"])

    def test_refresh_local_agent_updates_only_selected_session(self) -> None:
        selected_summary = {
            "title": "selected",
            "titleSource": "index",
            "id": "selected",
            "threadId": "selected",
            "sessionId": "selected",
            "rolloutId": "selected",
            "path": "/sessions/selected.jsonl",
            "cwd": "/work/project",
            "source": "cli",
            "createdAt": "2026-10-07T10:00:00Z",
            "lastAt": "2026-10-07T12:00:00Z",
        }
        session = search.Session(
            path=Path(selected_summary["path"]),
            records=[search.TextRecord("1", "assistant", "New", 1, "message")],
        )
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot(
                "local",
                "This machine",
                [cached_agent("selected"), cached_agent("untouched")],
            )
            with (
                patch.object(
                    relay_cache.agents,
                    "agent_summaries",
                    return_value=[selected_summary],
                ),
                patch.object(
                    relay_cache,
                    "load_recent_session",
                    return_value=session,
                ),
            ):
                refreshed = relay_cache.refresh_local_agent(
                    cache,
                    "selected",
                    Path("/sessions"),
                    Path("/index"),
                )

            self.assertEqual(refreshed.agent_id, "selected")
            self.assertEqual(
                {item["agent_id"] for item in cache.agents("local")},
                {"selected", "untouched"},
            )
            self.assertEqual(
                cache.records("local", "selected")[0]["text"], "New"
            )

    def test_load_recent_session_reads_newest_renderable_records(self) -> None:
        def event(role: str, text: str) -> str:
            return json.dumps(
                {
                    "timestamp": "2026-10-07T12:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text(
                "\n".join(
                    [event("user", "old"), "not json", event("assistant", "new")]
                )
                + "\n",
                encoding="utf-8",
            )

            session = relay_cache.load_recent_session(path, limit=1)

        self.assertEqual([record.text for record in session.records], ["new"])

    def test_status_json_reports_cache_schema_and_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "relay.sqlite3"
            relay_cache.RelayCache(path).replace_host_snapshot(
                "local", "This machine", [cached_agent("one")]
            )
            args = argparse.Namespace(cache_file=path, json=True)
            output = io.StringIO()

            with redirect_stdout(output):
                result = relay.command_status(args)

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], 3)
        self.assertEqual(payload["hosts"][0]["agent_count"], 1)

    def test_outbox_json_reports_persisted_delivery_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "relay.sqlite3"
            cache = relay_cache.RelayCache(path)
            cache.replace_host_snapshot(
                "home", "Home", [cached_agent("one")]
            )
            message_id = cache.enqueue("home", "one", {"text": "Continue"})
            cache.record_outbox_attempt(
                message_id, state="pending", error="offline"
            )
            output = io.StringIO()

            with redirect_stdout(output):
                result = relay.command_outbox(
                    argparse.Namespace(cache_file=path, json=True)
                )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload[0]["message_id"], message_id)
        self.assertEqual(payload[0]["state"], "pending")
        self.assertEqual(payload[0]["last_error"], "offline")


if __name__ == "__main__":
    unittest.main()
