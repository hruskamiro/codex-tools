from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from codex_tools import agents_tui, relay_cache


class SyncStatusTests(unittest.TestCase):
    now = datetime(2026, 10, 19, 20, 0, 30, tzinfo=timezone.utc)

    def test_metadata_parts_have_distinct_gentle_styles_without_changing_text(self):
        model = "test-model · reasoning: high"
        sync = "Sync live · ●"
        spans = agents_tui.metadata_spans(model, sync, "info_sync_ok")
        self.assertEqual("".join(text for text, _ in spans), model + "  │  " + sync)
        self.assertIn(("test-model", "info_model"), spans)
        self.assertIn(("reasoning: high", "info_effort"), spans)
        self.assertIn(("Sync live", "info_sync_mode"), spans)
        self.assertIn(("●", "info_sync_ok"), spans)

    def test_sync_errors_are_highlighted_without_marking_cached_state_healthy(self):
        spans = agents_tui.metadata_spans("model · low", "Sync live · ●", "info_sync_error")
        self.assertIn(("●", "info_sync_error"), spans)
        cached = agents_tui.metadata_spans("model · low", "Sync manual · ●", "info_sync_unknown")
        self.assertIn(("●", "info_sync_unknown"), cached)
        self.assertNotIn("info_sync_ok", [style for _, style in cached])

    def test_metadata_color_definitions_have_no_background(self):
        for count in (8, 256):
            colors = agents_tui.soft_color_definitions(count)
            keys = ("info_model", "info_effort", "info_sync_mode", "info_sync_ok", "info_sync_error")
            self.assertEqual(len({colors[key][0] for key in keys}), len(keys))
            self.assertTrue(all(colors[key][1] == -1 for key in keys))
            self.assertEqual(colors["info_sync_unknown"], colors["info_sync_mode"])

    def test_metadata_groups_share_one_row_with_full_details_when_space_allows(self):
        model, sync = agents_tui.metadata_groups(
            {"model": "test-model", "reasoning_effort": "high"}, "Sync live · ●", 100
        )
        self.assertEqual(model, "test-model · reasoning: high")
        self.assertEqual(sync, "Sync live · ●")

    def test_metadata_groups_compact_without_losing_sync_state_on_narrow_terminals(self):
        agent = {"model": "gpt-6.1-sol", "reasoning_effort": "medium"}
        for width in (30, 50, 70):
            model, sync = agents_tui.metadata_groups(agent, "Sync live · ●", width)
            self.assertLessEqual(len(model) + 5 + len(sync), width)
            self.assertTrue(sync.startswith("Sync live"))
            self.assertTrue(sync.endswith("●"))
            self.assertNotIn("\n", model + sync)

    def test_metadata_groups_respect_wide_character_cell_widths(self):
        model, sync = agents_tui.metadata_groups(
            {"model": "模型模型模型模型", "reasoning_effort": "high"}, "Sync live · ●", 35
        )
        self.assertLessEqual(sum(agents_tui.character_cell_width(c) for c in model + "  │  " + sync), 35)

    def label(self, synced_at="2026-10-19T20:00:25Z", mode="live", result=None, syncing=False):
        return agents_tui.sync_status_text(
            {"synced_at": synced_at}, mode, result, syncing=syncing, now=self.now
        )

    def test_manual_mode_does_not_claim_an_active_connection(self):
        text, style = self.label(mode="manual")
        self.assertEqual(text, "Sync manual · ●")
        self.assertEqual(style, "info_sync_unknown")

    def test_routine_checks_do_not_flicker_or_change_the_label(self):
        self.assertEqual(self.label(syncing=True), self.label())
        self.assertEqual(self.label(), ("Sync live · ●", "info_sync_ok"))
        for second in ("25", "26", "29"):
            self.assertEqual(self.label(f"2026-10-19T20:00:{second}Z"), self.label())

    def test_failed_connection_keeps_last_success_visible(self):
        error = agents_tui.AutoSyncUpdate(("host", "one"), "connection refused", "2026-10-19T20:00:29Z")
        self.assertEqual(self.label(result=error), ("Sync live · ●", "info_sync_error"))
        self.assertEqual(self.label(result=error, syncing=True), self.label(result=error))

    def test_new_success_supersedes_older_error_including_external_sync(self):
        error = agents_tui.AutoSyncUpdate(("host", "one"), "old failure", "2026-10-19T20:00:20Z")
        text, style = self.label(result=error)
        self.assertNotIn("old failure", text)
        self.assertEqual(style, "info_sync_ok")

    def test_stale_missing_and_invalid_timestamps_are_not_live(self):
        self.assertEqual(self.label("2026-10-19T19:58:00Z"), ("Sync live · ●", "info_sync_unknown"))
        self.assertEqual(self.label("2026-10-19T19:58:00Z", syncing=True)[1], "info_sync_unknown")
        for value in ("", "not-a-date"):
            self.assertEqual(self.label(value), ("Sync live · ●", "info_sync_unknown"))
        self.assertEqual(self.label("2026-10-19T20:01:00Z")[1], "info_sync_ok")

    def test_errors_remain_single_line(self):
        error = agents_tui.AutoSyncUpdate(("host", "one"), "network\nfailed")
        self.assertEqual(self.label(result=error), ("Sync live · ●", "info_sync_error"))
        self.assertNotIn("\n", self.label(result=error)[0])

    def test_worker_reports_inflight_target_then_successful_completion(self):
        started, release = threading.Event(), threading.Event()
        item = agents_tui.AgentItem("local", "Local", {"agent_id": "one"})
        other = agents_tui.AgentItem("local", "Local", {"agent_id": "two"})

        def refresh(*args):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test sync timed out")

        with patch.object(agents_tui.relay_cache, "refresh_local_agent", side_effect=refresh):
            worker = agents_tui.AutoSyncWorker(object(), Path("/sessions"), Path("/index"), None, Path("/relay"))
            try:
                worker.select(item, "economy")
                self.assertTrue(started.wait(1))
                self.assertTrue(worker.is_syncing(item))
                self.assertFalse(worker.is_syncing(other))
                release.set()
                update = worker.updates.get(timeout=2)
                self.assertEqual(update.target, ("local", "one"))
                self.assertEqual(update.error, "")
                self.assertIsNotNone(agents_tui.parse_sync_time(update.completed_at))
                self.assertFalse(worker.is_syncing(item))
            finally:
                release.set()
                worker.close()

    def test_worker_error_does_not_leave_inflight_indicator_stuck(self):
        item = agents_tui.AgentItem("local", "Local", {"agent_id": "one"})
        with patch.object(agents_tui.relay_cache, "refresh_local_agent", side_effect=OSError("offline")):
            worker = agents_tui.AutoSyncWorker(object(), Path("/sessions"), Path("/index"), None, Path("/relay"))
            try:
                worker.select(item, "economy")
                update = worker.updates.get(timeout=2)
                self.assertEqual(update.error, "offline")
                self.assertFalse(worker.is_syncing(item))
            finally:
                worker.close()

    def test_inactive_remote_session_is_not_reported_as_successfully_synced(self):
        item = agents_tui.AgentItem("remote", "Remote", {"agent_id": "one"}, "http://127.0.0.1:8780")
        config = {"peers": {"remote": {"url": item.base_url}}}
        with patch.object(agents_tui.relay_transport, "load_config", return_value=config), \
             patch.object(agents_tui, "sync_agent", return_value=(None, {})):
            worker = agents_tui.AutoSyncWorker(object(), Path("/sessions"), Path("/index"), None, Path("/relay"))
            try:
                worker.select(item, "economy")
                update = worker.updates.get(timeout=2)
                self.assertIn("no longer active", update.error)
                self.assertFalse(worker.is_syncing(item))
            finally:
                worker.close()


class CachedSyncTimeTests(unittest.TestCase):
    def agent(self, agent_id):
        return relay_cache.CachedAgent(
            agent_id, agent_id, agent_id, agent_id, "Test", "test", "/work", "cli",
            "", "", "", (),
        )

    def test_timestamps_are_per_agent_and_empty_incremental_poll_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            cache = relay_cache.RelayCache(path)
            with patch.object(relay_cache, "utc_now", return_value="first"):
                cache.replace_host_snapshot("remote", "Remote", [self.agent("one"), self.agent("two")])
            with patch.object(relay_cache, "utc_now", return_value="second"):
                cache.append_agent_records("remote", "one", [], event_cursor="cursor")
            rows = {row["agent_id"]: row for row in relay_cache.RelayCache(path).agents("remote")}
            self.assertEqual(rows["one"]["synced_at"], "second")
            self.assertEqual(rows["two"]["synced_at"], "first")
            with patch.object(relay_cache, "utc_now", return_value="third"):
                cache.update_agent_status("remote", "one", status="idle", status_label="ready")
            rows = {row["agent_id"]: row for row in cache.agents("remote")}
            self.assertEqual(rows["one"]["synced_at"], "third")

    def test_existing_cache_migrates_without_inventing_successful_sync_time(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = relay_cache.RelayCache(Path(directory) / "cache.sqlite3")
            cache.replace_host_snapshot("remote", "Remote", [self.agent("one")])
            with cache.connect() as connection, connection:
                connection.execute("ALTER TABLE relay_agents DROP COLUMN synced_at")
            self.assertEqual(cache.agents("remote")[0]["synced_at"], "")
