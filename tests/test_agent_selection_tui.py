from __future__ import annotations

import copy
import unittest
from collections import defaultdict
from unittest.mock import Mock, patch

from codex_tools import agent_selection_tui, agents_tui


class AgentChooserTests(unittest.TestCase):
    def dialog(self, keys, state=None, *, fail=False, items=None):
        if items is None:
            items = [agents_tui.AgentItem("desktop", "Desktop", {"agent_id": "one", "title": "One"}),
                     agents_tui.AgentItem("laptop", "Laptop", {"agent_id": "two", "title": "Two"})]
        if state is None:
            state = {"version": 1, "hidden": [], "order": []}
        window = Mock()
        window.getmaxyx.return_value = (30, 100)
        window.get_wch.side_effect = keys
        with patch.object(agent_selection_tui.agent_selection, "save", side_effect=OSError("disk full") if fail else None) as save:
            action = agent_selection_tui.manage(window, defaultdict(int), state, items)
        return action, state, save

    def test_space_toggles_availability(self):
        _, state, save = self.dialog([" ", "\x02"])
        self.assertEqual(state["hidden"], [["desktop", "one"]])
        save.assert_called_once()
        _, state, _ = self.dialog([" ", "\x02"], state)
        self.assertEqual(state["hidden"], [])

    def test_alt_arrow_moves_between_hosts(self):
        keys = [agents_tui.curses.KEY_DOWN, "\x1b", "[", *"1;3A", "\x02"]
        _, state, _ = self.dialog(keys)
        self.assertEqual(state["order"], [["laptop", "two"], ["desktop", "one"]])

    def test_show_all_restores_only_available_agents(self):
        state = {"version": 1, "hidden": [["desktop", "one"], ["gone", "absent"]], "order": []}
        _, state, _ = self.dialog(["a", "\x02"], state)
        self.assertEqual(state["hidden"], [["gone", "absent"]])

    def test_cancel_and_exit_do_not_change_preferences(self):
        for keys, expected in ((["\x1b", agents_tui.curses.error()], "close"), (["\x04"], "exit"), (["\x03"], "close")):
            action, _, save = self.dialog(keys)
            self.assertEqual(action, expected)
            save.assert_not_called()

    def test_failure_does_not_change_state_or_selection(self):
        state = {"version": 1, "hidden": [], "order": []}
        expected = copy.deepcopy(state)
        _, state, _ = self.dialog([" ", "\x02"], state, fail=True)
        self.assertEqual(state, expected)

    def test_repeated_escape_dismisses_chooser_without_saving(self):
        action, _, save = self.dialog(["\x1b", "\x1b", agents_tui.curses.error()])
        self.assertEqual(action, "close")
        save.assert_not_called()

    def test_incomplete_escape_sequence_dismisses_chooser_without_saving(self):
        action, _, save = self.dialog(["\x1b", "[", agents_tui.curses.error()])
        self.assertEqual(action, "close")
        save.assert_not_called()

    def test_mouse_click_and_unknown_alt_shortcut_do_not_dismiss_chooser(self):
        for suffix in ("[<0;10;20M", "z"):
            action, _, save = self.dialog(["\x1b", *suffix, "\x02"])
            self.assertEqual(action, "close")
            save.assert_not_called()

    def test_no_agents_or_nonexistent_saved_agents_do_not_create_placeholder_rows(self):
        state = {"version": 1, "hidden": [["gone", "one"]], "order": [["gone", "one"]]}
        action, state, _ = self.dialog(["\x04"], state, items=[])
        self.assertEqual(action, "exit")
        self.assertEqual(state["order"], [["gone", "one"]])
