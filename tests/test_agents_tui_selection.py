from __future__ import annotations

import io
import queue
import unittest
from collections import defaultdict
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from codex_tools import agents_tui


class ConsoleSelectionTests(unittest.TestCase):
    DELIVERY = {"delivered": 0, "pending": 0, "failed": 0, "errors": []}

    def run_console(self, keys, *, hidden=None, order=None, sync_after_keys=(), sync_error="", peer=None, pending=0,
                    agent_status="unknown", receipts=None, size=(40, 120), window=None,
                    delivery_worker=None, sync_mode="adaptive"):
        state = {"version": 1, "hidden": hidden or [], "order": order or []}
        items = [agents_tui.AgentItem("desktop", "Desktop", {"agent_id": "one", "title": "One"}),
                 agents_tui.AgentItem("laptop", "Laptop", {"agent_id": "two", "title": "Two"})]
        for item in items:
            item.agent["status"] = agent_status
        window = window if window is not None else Mock()
        window.getmaxyx.return_value = size
        cache = Mock(path=Path("/nonexistent/cache"))
        cache.records.return_value = []
        cache.reconcile_message_receipts.return_value = receipts or []
        cache.peer_host_id.return_value = "laptop"
        cache.pending.return_value = [{} for _ in range(pending)]
        worker = Mock()
        worker.updates = queue.Queue()
        worker.is_syncing.return_value = False
        delivery_worker = delivery_worker if delivery_worker is not None else Mock()
        delivery_worker.updates = queue.Queue()
        key_iterator = iter(keys)
        key_count = 0

        def read_key():
            nonlocal key_count
            key = next(key_iterator)
            key_count += 1
            if key_count in sync_after_keys:
                worker.updates.put(agents_tui.AutoSyncUpdate(("desktop", "one"), error=sync_error))
            if isinstance(key, BaseException):
                raise key
            return key

        window.get_wch.side_effect = read_key
        window.getch.side_effect = read_key
        stop = Mock(return_value={"status": "interrupt_requested"})
        applied = Mock()
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            for name in ("curs_set", "raw", "nonl", "mousemask", "mouseinterval", "nl", "noraw"):
                stack.enter_context(patch.object(agents_tui.curses, name))
            stack.enter_context(patch.object(agents_tui.curses, "ACS_VLINE", 0, create=True))
            stack.enter_context(patch.object(agents_tui.curses, "wrapper", side_effect=lambda callback: callback(window)))
            stack.enter_context(patch.object(agents_tui, "colors", return_value=defaultdict(int)))
            stack.enter_context(patch.object(agents_tui, "all_agent_items", return_value=items))
            stack.enter_context(patch.object(agents_tui, "outbox_views", return_value=([], {})))
            stack.enter_context(patch.object(agents_tui, "AutoSyncWorker", return_value=worker))
            stack.enter_context(patch.object(agents_tui, "DeliveryWorker", return_value=delivery_worker))
            stack.enter_context(patch.object(agents_tui.agent_selection, "load", return_value=state))
            stack.enter_context(patch.object(agents_tui.agent_selection, "save"))
            stack.enter_context(patch.object(agents_tui.user_config, "set_value", applied))
            stack.enter_context(patch.object(agents_tui, "stop_agent_turn", stop))
            stack.enter_context(patch.object(agents_tui.relay_transport, "load_config", return_value={
                "peers": {"chosen": {"url": "http://127.0.0.1:18781"},
                          "other": {"url": "http://127.0.0.1:18780"}},
            }))
            stack.enter_context(patch.object(agents_tui.relay_transport.JsonlChangeMonitor, "signature", return_value=None))
            result = agents_tui.run_console(cache, Path("/sessions"), Path("/index"), "codex",
                                           peer=peer, sync_mode=sync_mode)
        self.assertEqual(result, 0)
        worker.close.assert_called_once()
        delivery_worker.close.assert_called_once()
        return state, stop, cache, applied

    def test_peer_console_cannot_select_or_stop_other_servers(self):
        _, stop, cache, _ = self.run_console([*"\x1b1", "\x03", "\x04"], peer="chosen")
        self.assertEqual(stop.call_args.args[0].host_id, "laptop")
        self.assertEqual(stop.call_args.args[0].base_url, "http://127.0.0.1:18781")
        self.assertTrue(all(call.args == ("laptop", "two") for call in cache.records.call_args_list))

    def test_pending_messages_require_second_ctrl_d_and_stay_cached(self):
        with patch.object(agents_tui, "confirm_pending_disconnect", side_effect=[False, True]) as confirm:
            _, stop, cache, _ = self.run_console(["\x04", "\x04"], peer="chosen", pending=2)
        self.assertEqual(confirm.call_count, 2)
        cache.pending.assert_called_with("laptop")
        stop.assert_not_called()

    def test_refresh_all_in_scoped_console_contacts_only_selected_peer(self):
        with patch.object(agents_tui.Path, "exists", return_value=True), \
             patch.object(agents_tui, "sync_peer", return_value=(1, "Laptop", self.DELIVERY)) as sync, \
             patch.object(agents_tui.relay_cache, "refresh_local") as local:
            self.run_console(["\x13", "\x04"], peer="chosen")
        sync.assert_called_once()
        self.assertEqual(sync.call_args.args[2], "chosen")
        local.assert_not_called()

    def test_manual_refresh_feedback_persists_below_metadata_while_typing(self):
        with patch.object(agents_tui.Path, "exists", return_value=True), \
             patch.object(agents_tui, "sync_peer", return_value=(1, "Laptop", self.DELIVERY)), \
             patch.object(agents_tui, "add_text", wraps=agents_tui.add_text) as draw:
            self.run_console(["\x13", "x", "\x04"], peer="chosen")
        feedback = [call for call in draw.call_args_list
                    if str(call.args[3]).startswith("Refreshed 1/1 peers")]
        self.assertGreaterEqual(len(feedback), 3)
        self.assertTrue(all(call.args[1] == 3 for call in feedback))
        self.assertTrue(all(" · " in call.args[3] for call in feedback))

    def test_agent_chooser_is_also_scoped_to_selected_peer(self):
        state, _, _, _ = self.run_console(["\x02", " ", "\x02", "\x04"], peer="chosen")
        self.assertEqual(state["hidden"], [["laptop", "two"]])

    def test_only_checked_agents_are_selected_and_ctrl_c_or_escape_stops_them(self):
        _, stop, cache, _ = self.run_console(["\x03", "\x1b", agents_tui.curses.error(), "\x04"],
                                           hidden=[["desktop", "one"]])
        self.assertEqual(stop.call_count, 2)
        self.assertTrue(all(call.args[0].agent["agent_id"] == "two" for call in stop.call_args_list))
        self.assertTrue(all(call.args == ("laptop", "two") for call in cache.records.call_args_list))

    def test_ctrl_d_does_not_stop_anything(self):
        _, stop, _, _ = self.run_console(["\x04"])
        stop.assert_not_called()

    def test_sidebar_divider_reaches_the_bottom_row(self):
        window = Mock()
        self.run_console(["\x04"], window=window)
        window.vline.assert_called_once()
        self.assertEqual(window.vline.call_args.args[0], 0)
        self.assertEqual(window.vline.call_args.args[3], 40)

    def test_ctrl_c_exits_idle_console_without_requesting_stop(self):
        for peer in (None, "chosen"):
            with self.subTest(peer=peer):
                _, stop, _, _ = self.run_console(["\x03"], agent_status="idle", peer=peer)
                stop.assert_not_called()

    def test_ctrl_c_still_stops_working_agent(self):
        _, stop, _, _ = self.run_console(["\x03", "\x04"], agent_status="active")
        stop.assert_called_once()

    def test_ctrl_c_keeps_console_open_with_pending_deliveries(self):
        _, stop, _, _ = self.run_console(["\x03", "\x04"], agent_status="idle", pending=1)
        stop.assert_called_once()

    def test_ctrl_c_keeps_console_open_with_delivered_but_unconsumed_message(self):
        _, stop, _, _ = self.run_console(["\x03", "\x04"], agent_status="idle",
                                        receipts=[{"record_key": None, "current_delivery": "delivered",
                                                   "text": "Queued prompt", "created_at": "",
                                                   "message_id": "message"}])
        stop.assert_called_once()

    def test_ctrl_c_exits_with_no_agent_or_with_reconciled_receipts(self):
        for kwargs in ({"hidden": [["desktop", "one"], ["laptop", "two"]]},
                       {"receipts": [{"record_key": "record", "current_delivery": "delivered"}]}):
            with self.subTest(kwargs=kwargs):
                _, stop, _, _ = self.run_console(["\x03"], agent_status="idle", **kwargs)
                stop.assert_not_called()

    def test_ctrl_c_exits_idle_console_on_small_screen(self):
        _, stop, _, _ = self.run_console([3], agent_status="idle", size=(8, 50))
        stop.assert_not_called()

    def test_escape_and_ctrl_g_remain_stop_only_when_idle(self):
        _, stop, _, _ = self.run_console(["\x07", "\x1b", agents_tui.curses.error(), "\x04"],
                                        agent_status="idle")
        self.assertEqual(stop.call_count, 2)

    def test_transfer_meter_is_scoped_and_only_drawn_in_settings(self):
        with patch.object(agents_tui.relay_traffic, "enable", return_value=None), \
             patch.object(agents_tui.relay_traffic, "SessionTraffic") as meter, \
             patch.object(agents_tui, "add_text", wraps=agents_tui.add_text) as draw:
            meter.return_value.summary.return_value = "Relay data: down 2 KiB / up 1 KiB"
            meter.return_value.details.return_value = "Console/sync: 1 KiB | Viewer/files: 2 KiB"
            self.run_console(["\t", agents_tui.curses.KEY_UP, "\x04"], peer="chosen")
        meter.assert_called_once_with(None, "http://127.0.0.1:18781")
        labels = [call.args[3] for call in draw.call_args_list]
        self.assertEqual(labels.count("Relay data: down 2 KiB / up 1 KiB"), 1)
        self.assertEqual(labels.count("Console/sync: 1 KiB | Viewer/files: 2 KiB"), 1)

    def test_settings_leave_an_empty_line_above_tabs(self):
        with patch.object(agents_tui, "add_text", wraps=agents_tui.add_text) as draw:
            self.run_console(["\t", agents_tui.curses.KEY_UP, "\x04"])
        tabs = [call for call in draw.call_args_list if call.args[3] == " Sync "]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0].args[1], 5)
        settings_start = max(index for index, call in enumerate(draw.call_args_list)
                             if call.args[3] == "Settings" and call.args[2] > 36)
        self.assertFalse(any(call.args[1] == 4 and call.args[2] > 36
                             for call in draw.call_args_list[settings_start:]))

    def test_conversation_leaves_an_empty_line_after_status_panel(self):
        with patch.object(agents_tui, "transcript_lines", return_value=[("Hello", "assistant")]), \
             patch.object(agents_tui, "add_semantic_text") as draw:
            self.run_console(["\x04"])
        draw.assert_called_once()
        self.assertEqual(draw.call_args.args[1], 4)

    def test_background_connection_errors_change_dot_without_flashing_header_text(self):
        with patch.object(agents_tui, "add_text", wraps=agents_tui.add_text) as draw, \
             patch.object(agents_tui, "metadata_spans", wraps=agents_tui.metadata_spans) as metadata:
            self.run_console(["x", "\x04"], sync_after_keys=(1,), sync_error="Connection refused")
        self.assertFalse(any("Connection refused" in str(call.args[3]) for call in draw.call_args_list))
        self.assertTrue(any(call.args[2] == "info_sync_error" for call in metadata.call_args_list))

    def test_ctrl_n_and_ctrl_p_navigate_the_agent_list(self):
        state, stop, _, _ = self.run_console([
            agents_tui.curses.KEY_BTAB, "\x0e", "\x03",
            "\x10", "\x03", "\x04",
        ])
        self.assertEqual([call.args[0].agent["agent_id"]
                          for call in stop.call_args_list], ["two", "one"])
        self.assertEqual(state["order"], [])

    def test_ctrl_n_and_ctrl_p_in_composer_do_not_switch_agents(self):
        _, stop, _, _ = self.run_console([*"draft", "\x0e", "\x10", "\x03", "\x04"])
        self.assertEqual(stop.call_args.args[0].agent["agent_id"], "one")

    def test_hidden_agents_can_be_restored_in_the_chooser(self):
        state, stop, _, _ = self.run_console(["\x02", " ", "\x02", "\x03", "\x04"],
                                            hidden=[["desktop", "one"], ["laptop", "two"]])
        self.assertEqual(state["hidden"], [["laptop", "two"]])
        self.assertEqual(stop.call_args.args[0].agent["agent_id"], "one")

    def test_reordered_agents_remain_globally_ordered_across_hosts(self):
        keys = ["\x02", agents_tui.curses.KEY_DOWN, "\x1b", "[", *"1;3A", "\x02", "\x04"]
        state, stop, _, _ = self.run_console(keys)
        self.assertEqual(state["order"], [["laptop", "two"], ["desktop", "one"]])
        stop.assert_not_called()

    def test_settings_keep_sync_and_agents_separate_and_open_chooser(self):
        keys = ["\t", agents_tui.curses.KEY_UP, agents_tui.curses.KEY_RIGHT,
                agents_tui.curses.KEY_DOWN,  # Sync -> live.
                agents_tui.curses.KEY_RIGHT, "\r",  # Agents -> chooser.
                " ", "\x02",  # Hide current agent, return to Settings.
                agents_tui.curses.KEY_LEFT, "\r", "\x04"]
        state, stop, _, applied = self.run_console(keys)
        self.assertEqual(state["hidden"], [["desktop", "one"]])
        applied.assert_called_once_with("agents.sync_mode", "live")
        stop.assert_not_called()

    def test_unknown_alt_key_is_not_a_stop_request(self):
        _, stop, _, _ = self.run_console(["\x1b", "z", "\x04"])
        stop.assert_not_called()

    def test_left_from_first_settings_tab_returns_to_sidebar(self):
        for tab_navigation in ([], [agents_tui.curses.KEY_RIGHT, agents_tui.curses.KEY_LEFT]):
            with self.subTest(tab_navigation=tab_navigation):
                _, stop, _, applied = self.run_console([
                    "\t", agents_tui.curses.KEY_UP, agents_tui.curses.KEY_RIGHT,
                    *tab_navigation, agents_tui.curses.KEY_LEFT,
                    agents_tui.curses.KEY_DOWN, "\x03", "\x04",
                ])
                stop.assert_called_once()
                self.assertEqual(stop.call_args.args[0].agent["agent_id"], "one")
                applied.assert_not_called()

    def test_deliveries_tab_clears_selected_conversation(self):
        _, stop, cache, applied = self.run_console([
            "\t", agents_tui.curses.KEY_UP, agents_tui.curses.KEY_RIGHT,
            agents_tui.curses.KEY_RIGHT, agents_tui.curses.KEY_RIGHT, "\r", "\x04",
        ])
        cache.clear_failed_deliveries.assert_called_once_with("desktop", "one")
        stop.assert_not_called()
        applied.assert_not_called()

    def test_deliveries_tab_clear_all_is_scoped_to_connected_server(self):
        for peer, expected_host in ((None, None), ("chosen", "laptop")):
            with self.subTest(peer=peer):
                _, _, cache, _ = self.run_console([
                    "\t", agents_tui.curses.KEY_UP, agents_tui.curses.KEY_RIGHT,
                    agents_tui.curses.KEY_RIGHT, agents_tui.curses.KEY_RIGHT,
                    agents_tui.curses.KEY_DOWN, "\r", "\x04",
                ], peer=peer)
                cache.clear_failed_deliveries.assert_called_once_with(expected_host, None)

    def test_escape_leaves_settings_and_does_not_stop_an_agent(self):
        _, stop, _, _ = self.run_console([
            "\t", agents_tui.curses.KEY_UP, agents_tui.curses.KEY_RIGHT,
            "\x1b", agents_tui.curses.error(), "\x03", "\x04",
        ])
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0].agent["agent_id"], "one")

    def test_tab_submits_draft_and_keeps_composer_focused(self):
        with patch.object(agents_tui.agents_ui, "send_message",
                          return_value={"delivery": "sent", "message_id": ""}) as send:
            self.run_console([*"first", "\t", *"second", "\r", "\x04"])
        self.assertEqual([call.args[1]["message"] for call in send.call_args_list],
                         ["first", "second"])
        self.assertEqual([call.args[1]["start"] for call in send.call_args_list], [False, True])

    def test_remote_message_appears_and_typing_continues_without_foreground_delivery(self):
        worker = Mock()

        def save_message(cache, payload, *_args, **_kwargs):
            cache.reconcile_message_receipts.return_value = [{
                "text": payload["message"], "message_id": "message", "created_at": "",
                "record_key": None, "current_delivery": "pending", "current_error": "",
            }]
            return {"delivery": "queued", "message_id": "message"}

        with patch.object(agents_tui.agents_ui, "send_message", side_effect=save_message), \
             patch.object(agents_tui.relay_transport, "deliver_pending") as foreground, \
             patch.object(agents_tui, "add_semantic_text", wraps=agents_tui.add_semantic_text) as draw, \
             patch.object(agents_tui, "add_text", wraps=agents_tui.add_text) as text, \
             patch.object(agents_tui, "draw_composer", wraps=agents_tui.draw_composer) as composer:
            self.run_console([*"hello", "\r", *"next draft", "\x04"], delivery_worker=worker,
                             sync_mode="manual")
        foreground.assert_not_called()
        worker.submit.assert_called_once()
        self.assertTrue(any(call.args[3].strip() == "hello" for call in draw.call_args_list))
        self.assertTrue(any("Waiting to sync" in str(call.args[3]) for call in text.call_args_list))
        self.assertEqual(composer.call_args.args[2].text, "next draft")

    def test_macos_meta_arrows_select_agents_without_reordering_or_stopping(self):
        for down, up in (("[1;9B", "[1;9A"), ("\x1b[B", "\x1b[A"),
                         ("\x1bOB", "\x1bOA"),
                         (["\x1b", agents_tui.curses.KEY_DOWN],
                          ["\x1b", agents_tui.curses.KEY_UP]),
                         ([agents_tui.curses.KEY_DOWN], [agents_tui.curses.KEY_UP])):
            with self.subTest(down=down):
                state, stop, _, _ = self.run_console([
                    "\x1b", *down, "\x03", "\x1b", *up, "\x03", "\x04",
                ])
                self.assertEqual([call.args[0].agent["agent_id"]
                                  for call in stop.call_args_list], ["two", "one"])
                self.assertEqual(state["order"], [])

    def test_background_sync_does_not_undo_a_just_selected_agent(self):
        for shortcut in ("\x1b2", "\x1b[1;9B", ["\x1b", agents_tui.curses.KEY_DOWN]):
            with self.subTest(shortcut=shortcut):
                _, stop, cache, _ = self.run_console(
                    [*shortcut, "\x04"], sync_after_keys=(len(shortcut),))
                self.assertEqual(cache.records.call_args.args, ("laptop", "two"))
                stop.assert_not_called()

    def test_modified_arrows_cycle_across_both_ends_without_reordering(self):
        keys = [*"\x1b[1;9A", "\x03",  # First -> last.
                *"\x1b[1;9B", "\x03",  # Last -> first.
                *"\x1b[1;9B", *"\x1b[1;9B", "\x03", "\x04"]
        state, stop, _, _ = self.run_console(keys)
        self.assertEqual([call.args[0].agent["agent_id"] for call in stop.call_args_list],
                         ["two", "one", "one"])
        self.assertEqual(state["order"], [])

    def test_modified_arrows_with_one_or_no_visible_agents(self):
        keys = [*"\x1b[1;9A", *"\x1b[1;9B", "\x04"]
        for hidden in ([["desktop", "one"]], [["desktop", "one"], ["laptop", "two"]]):
            with self.subTest(hidden=hidden):
                _, stop, cache, _ = self.run_console(keys, hidden=hidden)
                stop.assert_not_called()
                if len(hidden) == 1:
                    self.assertEqual(cache.records.call_args.args, ("laptop", "two"))
                else:
                    cache.records.assert_not_called()

    def test_failed_tab_submission_preserves_draft_for_retry(self):
        with patch.object(agents_tui.agents_ui, "send_message", side_effect=[
            RuntimeError("unavailable"), {"delivery": "sent", "message_id": ""}
        ]) as send:
            self.run_console([*"keep me", "\t", "\t", "\x04"])
        self.assertEqual([call.args[1]["message"] for call in send.call_args_list],
                         ["keep me", "keep me"])

    def test_shift_tab_switches_panes_without_sending_draft(self):
        with patch.object(agents_tui.agents_ui, "send_message") as send:
            _, stop, _, _ = self.run_console([
                *"draft", agents_tui.curses.KEY_BTAB, agents_tui.curses.KEY_DOWN,
                "\x03", "\x04",
            ])
        send.assert_not_called()
        self.assertEqual(stop.call_args.args[0].agent["agent_id"], "two")

    def test_shift_right_returns_to_composer_and_preserves_draft(self):
        for left, right in (
            ([agents_tui.curses.KEY_SLEFT], [agents_tui.curses.KEY_SRIGHT]),
            (list("\x1b[1;2D"), list("\x1b[1;2C")),
        ):
            with self.subTest(right=right), patch.object(
                agents_tui.agents_ui, "send_message",
                return_value={"delivery": "sent", "message_id": ""},
            ) as send:
                _, stop, _, _ = self.run_console([
                    *"draft", *left, *right, *" continued", "\r", "\x04",
                ])
                send.assert_called_once()
                self.assertEqual(send.call_args.args[1]["message"], "draft continued")
                self.assertTrue(send.call_args.args[1]["start"])
                stop.assert_not_called()
