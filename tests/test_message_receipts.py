from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import agents_tui, agents_ui, relay_cache


def agent(records=()):
    return relay_cache.CachedAgent(
        agent_id="one", session_id="one", thread_id="one", rollout_id="one",
        title="Test", title_source="test", cwd="/work", source="cli",
        created_at="", updated_at="", origin_path="", records=tuple(records),
    )


def record(text, timestamp):
    return relay_cache.CachedRecord(timestamp, "user", text, 0, "message")


class MessageReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cache.sqlite3"
        self.cache = relay_cache.RelayCache(self.path)
        self.cache.replace_host_snapshot("home", "Home", [agent()])

    def displayed(self, host="home", cache=None):
        cache = cache or self.cache
        records = cache.records(host, "one")
        receipts = cache.reconcile_message_receipts(host, "one", records)
        return agents_tui.records_with_message_receipts(records, receipts)

    def test_remote_delivery_survives_reopen_and_attaches_to_real_message(self):
        result = agents_ui.send_message(self.cache, {
            "host": "home", "agent": "one", "message": "Continue", "start": True,
        }, "codex")
        message_id = result["message_id"]
        reopened = relay_cache.RelayCache(self.path)
        self.assertEqual(self.displayed(cache=reopened)[0]["_status"], "Waiting to sync")
        reopened.record_outbox_attempt(message_id, state="delivered")
        self.assertEqual(self.displayed(cache=reopened)[0]["_status"], "Queued in Codex")
        reopened.replace_host_snapshot("home", "Home", [agent([record("Continue", "2026-10-19T20:00:00Z")])])
        displayed = self.displayed(cache=reopened)
        self.assertEqual(len(displayed), 1)
        self.assertEqual(displayed[0]["_status"], "Delivered · in conversation")
        self.assertEqual(displayed[0]["_message_id"], message_id)
        self.assertEqual(self.displayed(cache=relay_cache.RelayCache(self.path)), displayed)

    def test_clear_failed_deliveries_preserves_other_states_scopes_and_history(self):
        self.cache.replace_host_snapshot("other", "Other", [agent()])
        ids = {}
        for host, target, state in (("home", "one", "failed"), ("home", "two", "failed"),
                                    ("other", "one", "failed"), ("home", "one", "pending"),
                                    ("home", "one", "delivered")):
            message_id = self.cache.enqueue(host, target, {"text": state}, track=True)
            self.cache.record_outbox_attempt(message_id, state=state)
            ids[host, target, state] = message_id
        local_failure = self.cache.record_message_receipt("home", "one", "local failure")
        self.cache.update_message_receipt(local_failure, delivery="failed")
        self.cache.replace_host_snapshot("home", "Home", [agent([record("History", "old")])])
        self.assertEqual(self.cache.clear_failed_deliveries("home", "one"), 2)
        self.assertEqual({r["message_id"] for r in self.cache.outbox()},
                         set(ids.values()) - {ids["home", "one", "failed"]})
        self.assertEqual([r["text"] for r in self.displayed()], ["History", "pending", "delivered"])
        self.assertEqual(self.cache.clear_failed_deliveries("home"), 1)
        self.assertEqual(self.cache.clear_failed_deliveries(), 1)
        self.assertEqual(self.cache.clear_failed_deliveries(), 0)
        self.assertEqual(len(self.cache.pending()), 1)
        self.assertEqual(len(self.cache.records("home", "one")), 1)

    def test_clear_uses_outbox_state_over_stale_failed_receipt(self):
        message_id = self.cache.enqueue("home", "one", {"text": "Retry"}, track=True)
        self.cache.update_message_receipt(message_id, delivery="failed")
        self.assertEqual(self.cache.clear_failed_deliveries(), 0)
        self.assertEqual(len(self.cache.pending()), 1)

    def test_old_identical_question_is_not_mistaken_for_new_delivery(self):
        old = record("Again", "2026-10-19T19:00:00Z")
        self.cache.replace_host_snapshot("home", "Home", [agent([old])])
        self.cache.enqueue("home", "one", {"text": "Again"}, track=True)
        displayed = self.displayed()
        self.assertEqual(len(displayed), 2)
        self.assertNotIn("_status", displayed[0])
        self.assertEqual(displayed[1]["_status"], "Waiting to sync")
        self.cache.replace_host_snapshot("home", "Home", [agent([
            old, record("Again", "2026-10-19T20:00:00Z"),
        ])])
        displayed = self.displayed()
        self.assertEqual(len(displayed), 2)
        self.assertNotIn("_status", displayed[0])
        self.assertEqual(displayed[1]["_status"], "Delivered · in conversation")

    def test_two_identical_submissions_match_two_distinct_transcript_entries(self):
        ids = [self.cache.enqueue("home", "one", {"text": "Again"}, track=True) for _ in range(2)]
        self.cache.replace_host_snapshot("home", "Home", [agent([
            record("Again", "2026-10-19T20:00:00Z"), record("Again", "2026-10-19T20:01:00Z"),
        ])])
        displayed = self.displayed()
        self.assertEqual(len(displayed), 2)
        self.assertEqual({r["_message_id"] for r in displayed}, set(ids))

    def test_observed_message_does_not_reappear_when_it_leaves_the_cached_tail(self):
        self.cache.enqueue("home", "one", {"text": "Continue"}, track=True)
        self.cache.replace_host_snapshot("home", "Home", [agent([record("Continue", "old")])])
        self.assertEqual(len(self.displayed()), 1)
        self.cache.replace_host_snapshot("home", "Home", [agent([record("Different", "new")])])
        self.assertEqual([r["text"] for r in self.displayed()], ["Different"])
        self.cache.replace_host_snapshot("home", "Home", [agent([record("Continue", "old")])])
        self.assertEqual(self.displayed()[0]["_status"], "Delivered · in conversation")

    def test_pending_failure_and_start_warning_remain_visible_after_reopen(self):
        message_id = self.cache.enqueue("home", "one", {"text": "Continue"}, track=True)
        for state, error, expected in (
            ("pending", "timeout", "Not delivered · waiting to retry"),
            ("failed", "unavailable", "Delivery failed · unavailable"),
            ("delivered", "cannot start", "Queued in Codex · start request failed: cannot start"),
        ):
            self.cache.record_outbox_attempt(message_id, state=state, error=error)
            self.assertEqual(self.displayed(cache=relay_cache.RelayCache(self.path))[0]["_status"], expected)

    def test_local_send_is_persistent_and_has_no_retryable_remote_outbox(self):
        self.cache.replace_host_snapshot("local", "Local", [agent()])
        with patch.object(agents_ui.subprocess, "run") as run:
            run.return_value.returncode = 0
            result = agents_ui.send_message(self.cache, {
                "host": "local", "agent": "one", "message": "Continue",
            }, "codex")
        self.assertTrue(result["message_id"])
        self.assertEqual(self.cache.pending("local"), [])
        self.assertEqual(self.displayed("local", relay_cache.RelayCache(self.path))[0]["_status"],
                         "Queued in Codex")

    def test_local_failure_is_preserved_and_not_associated_with_later_retry(self):
        self.cache.replace_host_snapshot("local", "Local", [agent()])
        with patch.object(agents_ui.subprocess, "run", side_effect=OSError("cannot execute")):
            with self.assertRaises(OSError):
                agents_ui.send_message(self.cache, {
                    "host": "local", "agent": "one", "message": "Continue",
                }, "codex")
        self.cache.replace_host_snapshot("local", "Local", [agent([record("Continue", "later")])])
        displayed = self.displayed("local")
        self.assertNotIn("_status", displayed[0])
        self.assertEqual(displayed[1]["_status"], "Delivery failed · cannot execute")

    def test_record_key_ignores_line_number_changes_but_not_message_time(self):
        first = {"text": "Continue", "timestamp": "time", "line_no": 0}
        self.assertEqual(relay_cache.user_record_key(first),
                         relay_cache.user_record_key({**first, "line_no": 100}))
        self.assertNotEqual(relay_cache.user_record_key(first),
                            relay_cache.user_record_key({**first, "timestamp": "other"}))

    def test_sending_state_survives_unconfirmed_process_exit_without_retry(self):
        self.cache.replace_host_snapshot("local", "Local", [agent()])
        self.cache.record_message_receipt("local", "one", "Continue")
        self.assertEqual(self.displayed("local", relay_cache.RelayCache(self.path))[0]["_status"],
                         "Delivery unconfirmed · check conversation")
        self.assertEqual(self.cache.pending("local"), [])

    def test_tracking_same_outbox_id_is_idempotent(self):
        for _ in range(2):
            self.cache.enqueue("home", "one", {"text": "Continue"}, message_id="same", track=True)
        self.assertEqual(len(self.cache.pending("home")), 1)
        self.assertEqual(len(self.displayed()), 1)

    def test_receipt_is_written_atomically_with_remote_outbox(self):
        with patch.object(self.cache, "_insert_message_receipt", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.cache.enqueue("home", "one", {"text": "Continue"}, track=True)
        self.assertEqual(self.cache.pending("home"), [])

    def test_transcript_observation_clears_start_warning_without_claiming_completion(self):
        self.cache.replace_host_snapshot("local", "Local", [agent()])
        message_id = self.cache.record_message_receipt("local", "one", "Continue")
        self.cache.update_message_receipt(message_id, delivery="delivered", error="start failed")
        self.cache.replace_host_snapshot("local", "Local", [agent([record("Continue", "time")])])
        self.assertEqual(self.displayed("local")[0]["_status"], "Delivered · in conversation")
