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
    def run_console(self, keys, *, hidden=None, order=None, sync_after_keys=()):
        state = {"version": 1, "hidden": hidden or [], "order": order or []}
        items = [agents_tui.AgentItem("desktop", "Desktop", {"agent_id": "one", "title": "One"}),
                 agents_tui.AgentItem("laptop", "Laptop", {"agent_id": "two", "title": "Two"})]
        window = Mock()
        window.getmaxyx.return_value = (40, 120)
        cache = Mock(path=Path("/nonexistent/cache"))
        cache.records.return_value = []
        cache.reconcile_message_receipts.return_value = []
        worker = Mock()
        worker.updates = queue.Queue()
        worker.is_syncing.return_value = False
        key_iterator = iter(keys)
        key_count = 0

        def read_key():
            nonlocal key_count
            key = next(key_iterator)
            key_count += 1
            if key_count in sync_after_keys:
                worker.updates.put(agents_tui.AutoSyncUpdate(("desktop", "one")))
            if isinstance(key, BaseException):
                raise key
            return key

        window.get_wch.side_effect = read_key
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
            stack.enter_context(patch.object(agents_tui.agent_selection, "load", return_value=state))
            stack.enter_context(patch.object(agents_tui.agent_selection, "save"))
            stack.enter_context(patch.object(agents_tui.user_config, "set_value", applied))
            stack.enter_context(patch.object(agents_tui, "stop_agent_turn", stop))
            stack.enter_context(patch.object(agents_tui.relay_transport.JsonlChangeMonitor, "signature", return_value=None))
            result = agents_tui.run_console(cache, Path("/sessions"), Path("/index"), "codex")
        self.assertEqual(result, 0)
        worker.close.assert_called_once()
        return state, stop, cache, applied

    def test_only_checked_agents_are_selected_and_ctrl_c_or_escape_stops_them(self):
        _, stop, cache, _ = self.run_console(["\x03", "\x1b", agents_tui.curses.error(), "\x04"],
                                           hidden=[["desktop", "one"]])
        self.assertEqual(stop.call_count, 2)
        self.assertTrue(all(call.args[0].agent["agent_id"] == "two" for call in stop.call_args_list))
        self.assertTrue(all(call.args == ("laptop", "two") for call in cache.records.call_args_list))

    def test_ctrl_d_does_not_stop_anything(self):
        _, stop, _, _ = self.run_console(["\x04"])
        stop.assert_not_called()

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

    def test_tab_submits_draft_and_keeps_composer_focused(self):
        with patch.object(agents_tui.agents_ui, "send_message",
                          return_value={"delivery": "sent", "message_id": ""}) as send:
            self.run_console([*"first", "\t", *"second", "\r", "\x04"])
        self.assertEqual([call.args[1]["message"] for call in send.call_args_list],
                         ["first", "second"])
        self.assertEqual([call.args[1]["start"] for call in send.call_args_list], [False, True])

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
