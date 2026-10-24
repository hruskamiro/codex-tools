from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from codex_tools import relay_traffic, relay_transport, viewer


class BinaryResponse(io.BytesIO):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = "application/pdf"
        self.headers["Content-Length"] = str(len(body))
        self.headers["X-Codex-Attachment-Suffix"] = ".pdf"


class RelayTrafficTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "traffic.sqlite3"
        self.store = relay_traffic.TrafficStore(self.path)
        self.peer = {"url": "http://localhost:9876/", "token": "private-key"}
        active = patch.object(relay_traffic, "_store", self.store)
        active.start()
        self.addCleanup(active.stop)

    def test_shared_store_normalizes_routes_and_scopes_totals(self) -> None:
        other_process = relay_traffic.TrafficStore(self.path)
        self.store.add(self.peer["url"], 17, 23)
        other_process.add(self.peer["url"].rstrip("/"), 3, 7)
        other_process.add("http://other-peer", 100, 200)
        self.assertEqual(self.store.totals(self.peer["url"]), (20, 30))
        self.assertEqual(self.store.totals(None), (120, 230))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_session_baseline_excludes_previous_traffic_and_other_peers(self) -> None:
        self.store.add(self.peer["url"], 1000, 2000)
        session = relay_traffic.SessionTraffic(self.store, self.peer["url"])
        self.store.add(self.peer["url"], 10, 2048)
        self.store.add("http://other-peer", 8000, 16000)
        self.assertIn("down 2.0 KiB / up 10 B", session.summary())
        new_session = relay_traffic.SessionTraffic(self.store, self.peer["url"])
        self.assertIn("down 0 B / up 0 B", new_session.summary())

    def test_successful_requests_count_utf8_bytes_not_characters(self) -> None:
        body = '{"text":"čau"}'.encode("utf-8")
        message = {"text": "áhoj"}
        with patch.object(relay_transport, "urlopen", return_value=io.BytesIO(body)):
            relay_transport.fetch_json(self.peer, self.peer["url"] + "v1/health", 1)
        with patch.object(relay_transport, "urlopen", return_value=io.BytesIO(body)):
            relay_transport.post_message(self.peer, message)
        with patch.object(relay_transport, "urlopen", return_value=io.BytesIO(body)):
            relay_transport.interrupt_agent(self.peer, "agent")
        self.assertEqual(self.store.totals(self.peer["url"]),
                         (len(json.dumps(message, ensure_ascii=False).encode("utf-8")) + 2,
                          len(body) * 3))

    def test_failed_request_is_not_counted_as_transferred(self) -> None:
        with patch.object(relay_transport, "urlopen", side_effect=URLError("offline")):
            with self.assertRaises(URLError):
                relay_transport.post_message(self.peer, {"text": "hello"})
        self.assertEqual(self.store.totals(None), (0, 0))

    def test_invalid_json_still_counts_received_body(self) -> None:
        with patch.object(relay_transport, "urlopen", return_value=io.BytesIO(b"invalid")):
            with self.assertRaises(ValueError):
                relay_transport.fetch_json(self.peer, self.peer["url"], 1)
        self.assertEqual(self.store.totals(None), (0, 7))

    def test_accounting_failure_does_not_break_sync(self) -> None:
        with patch.object(self.store, "add", side_effect=sqlite3.OperationalError("locked")):
            with patch.object(relay_transport, "urlopen", return_value=io.BytesIO(b"{}")):
                self.assertEqual(relay_transport.fetch_json(self.peer, self.peer["url"], 1), {})

    def test_unavailable_meter_and_binary_units(self) -> None:
        self.assertEqual(relay_traffic.SessionTraffic(None, None).summary(), "Relay data unavailable")
        for size, expected in ((0, "0 B"), (1024, "1.0 KiB"), (2**20, "1.0 MiB"),
                               (2**30, "1.0 GiB")):
            self.assertEqual(relay_traffic.format_bytes(size), expected)

    def test_existing_console_database_is_upgraded_without_resetting_totals(self) -> None:
        path = self.path.with_name("old.sqlite3")
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE traffic (peer TEXT PRIMARY KEY, uploaded INTEGER, downloaded INTEGER)")
            connection.execute("INSERT INTO traffic VALUES ('http://old', 7, 9)")
            connection.commit()
        finally:
            connection.close()
        store = relay_traffic.TrafficStore(path)
        store.add("http://old", 0, 100, category="viewer")
        self.assertEqual(store.snapshot(None), (7, 9, 0, 100))
        self.assertEqual(store.totals(None), (7, 109))

    def test_separate_categories_share_combined_session_baseline(self) -> None:
        self.store.add(self.peer["url"], 100, 200, category="viewer")
        session = relay_traffic.SessionTraffic(self.store, self.peer["url"])
        self.store.add(self.peer["url"], 10, 90)
        self.store.add(self.peer["url"], 0, 2048, category="viewer")
        self.store.add("http://other-peer", 0, 9000, category="viewer")
        self.assertIn("down 2.1 KiB / up 10 B / total 2.1 KiB", session.summary())
        self.assertEqual(session.details(), "Console/sync: 100 B  |  Viewer/files: 2.0 KiB")

    def test_viewer_json_refresh_is_separate_from_console_json(self) -> None:
        state = SimpleNamespace(relay_cache=None)
        with patch.object(viewer, "relay_peer_for_host", return_value=("peer", self.peer)), \
             patch.object(relay_transport, "apply_agent_snapshot"), \
             patch.object(relay_transport, "urlopen", return_value=io.BytesIO(b"{}")):
            viewer.refresh_relay_agent(state, "host", "agent")
        with patch.object(relay_transport, "urlopen", return_value=io.BytesIO(b"{}")):
            relay_transport.fetch_json(self.peer, self.peer["url"], 1)
        self.assertEqual(self.store.snapshot(None), (0, 2, 0, 2))

    def test_viewer_category_is_reset_even_after_failure(self) -> None:
        with self.assertRaises(ValueError):
            with relay_traffic.category("viewer"):
                relay_traffic.record(self.peer["url"], downloaded=10)
                raise ValueError("test")
        relay_traffic.record(self.peer["url"], downloaded=3)
        self.assertEqual(self.store.snapshot(None), (0, 3, 0, 10))

    def test_remote_pdf_counts_once_and_cached_reopen_adds_nothing(self) -> None:
        state = SimpleNamespace()
        session = {"hostId": "host", "id": "agent"}
        record = {"text": "Example", "timestamp": ""}
        body = b"%PDF-1.4\nexample"
        with patch.object(viewer.typeset, "typeset_cache_dir", return_value=self.path.parent / "pdfs"), \
             patch.object(viewer, "relay_peer_for_host", return_value=("peer", self.peer)), \
             patch.object(relay_transport, "urlopen", return_value=BinaryResponse(body)) as opened:
            first = viewer.render_relay_pdf(state, session, record, header_mode="external", code_mode="auto")
            self.assertTrue(first.ok)
            self.assertFalse(first.cached)
            self.assertEqual(self.store.snapshot(None), (0, 0, 0, len(body)))
            cached = viewer.render_relay_pdf(state, session, record, header_mode="external", code_mode="auto")
            self.assertTrue(cached.cached)
            opened.assert_called_once()
            self.assertEqual(self.store.snapshot(None), (0, 0, 0, len(body)))

    def test_remote_attachment_counts_once_and_cached_reopen_adds_nothing(self) -> None:
        state = SimpleNamespace(relay_attachment_dir=self.path.parent / "attachments")
        reference = ("host", "agent", "a" * 64, 0, "Example", ".pdf", "/remote/example.pdf")
        body = b"%PDF-1.4\nexample"
        with patch.object(viewer, "remote_attachment_reference", return_value=reference), \
             patch.object(viewer, "relay_peer_for_host", return_value=("peer", self.peer)), \
             patch.object(relay_transport, "urlopen", return_value=BinaryResponse(body)) as opened:
            first = viewer.resolve_remote_attachment(state, {})
            self.assertEqual(first[0].read_bytes(), body)
            self.assertEqual(self.store.snapshot(None), (0, 0, 0, len(body)))
            cached = viewer.resolve_remote_attachment(state, {})
            self.assertEqual(first, cached)
            opened.assert_called_once()
            self.assertEqual(self.store.snapshot(None), (0, 0, 0, len(body)))


if __name__ == "__main__":
    unittest.main()
