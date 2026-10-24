from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import agents_tui, relay_cache, relay_transport


class DeliveryWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = relay_cache.RelayCache(Path(temporary.name) / "cache.sqlite3")
        self.cache.ensure_host("remote", "Remote", base_url="http://peer")
        self.peer = {"url": "http://peer", "token": "test-only"}
        self.config = {"peers": {"selected": self.peer,
                                 "other-route": {"url": "http://other", "token": "unused"}}}
        self.item = agents_tui.AgentItem("remote", "Remote", {"agent_id": "agent"}, "http://peer")
        configured = patch.object(relay_transport, "load_config", return_value=self.config)
        configured.start()
        self.addCleanup(configured.stop)

    def enqueue(self, text: str) -> str:
        return self.cache.enqueue("remote", "agent", {"text": text, "start": True}, track=True)

    def test_slow_delivery_is_off_thread_and_second_submission_is_not_lost(self) -> None:
        entered, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        posted = []

        def post(peer, payload):
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertEqual(peer, self.peer)
            posted.append(payload["message_id"])
            if len(posted) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            return {"delivery": "accepted"}

        first = self.enqueue("first")
        with patch.object(relay_transport, "post_message", side_effect=post):
            worker = agents_tui.DeliveryWorker(self.cache, Path("/unused-config"))
            try:
                worker.submit(self.item)
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.cache.pending()[0]["message_id"], first)
                second = self.enqueue("second")
                worker.submit(self.item)
                release.set()
                for _ in range(2):
                    update = worker.updates.get(timeout=2)
                    self.assertEqual(update.target, ("remote", "agent"))
                    self.assertEqual(update.error, "")
                self.assertEqual(posted, [first, second])
                self.assertEqual(self.cache.pending(), [])
                receipts = self.cache.reconcile_message_receipts("remote", "agent", [])
                self.assertTrue(all(receipt["current_delivery"] == "delivered" for receipt in receipts))
            finally:
                release.set()
                worker.close()
                worker._thread.join(timeout=2)

    def test_offline_delivery_keeps_message_and_updates_its_receipt(self) -> None:
        message_id = self.enqueue("offline message")
        with patch.object(relay_transport, "post_message", side_effect=OSError("offline")):
            worker = agents_tui.DeliveryWorker(self.cache, Path("/unused-config"))
            try:
                worker.submit(self.item)
                worker.updates.get(timeout=2)
                pending = self.cache.pending()
                self.assertEqual(pending[0]["message_id"], message_id)
                self.assertEqual(pending[0]["last_error"], "offline")
                receipt = self.cache.reconcile_message_receipts("remote", "agent", [])[0]
                self.assertEqual(agents_tui.receipt_status(receipt), "Not delivered · waiting to retry")
            finally:
                worker.close()

    def test_missing_peer_is_reported_in_receipt_and_worker_can_recover(self) -> None:
        self.enqueue("keep me")
        self.config["peers"].pop("selected")
        with patch.object(relay_transport, "post_message", return_value={"delivery": "accepted"}):
            worker = agents_tui.DeliveryWorker(self.cache, Path("/unused-config"))
            try:
                worker.submit(self.item)
                update = worker.updates.get(timeout=2)
                self.assertIn("no configured peer", update.error)
                self.assertIn("no configured peer", self.cache.pending()[0]["last_error"])
                self.config["peers"]["selected"] = self.peer
                worker.submit(self.item)
                self.assertEqual(worker.updates.get(timeout=2).error, "")
                self.assertEqual(self.cache.pending(), [])
            finally:
                worker.close()

    def test_close_does_not_wait_for_network_or_discard_unattempted_messages(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def post(*_args):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"delivery": "accepted"}

        self.enqueue("in flight")
        with patch.object(relay_transport, "post_message", side_effect=post) as posted:
            worker = agents_tui.DeliveryWorker(self.cache, Path("/unused-config"))
            try:
                worker.submit(self.item)
                self.assertTrue(entered.wait(1))
                waiting = self.enqueue("still queued")
                worker.submit(self.item)
                started = time.monotonic()
                worker.close()
                self.assertLess(time.monotonic() - started, 0.8)
                worker.submit(self.item)  # Closed workers do not accept new jobs.
                release.set()
                worker._thread.join(timeout=2)
                self.assertFalse(worker._thread.is_alive())
                posted.assert_called_once()
                self.assertEqual([item["message_id"] for item in self.cache.pending()], [waiting])
            finally:
                release.set()
                worker.close()
                worker._thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
