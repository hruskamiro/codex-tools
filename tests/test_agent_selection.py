from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import agent_selection, agents_tui


class AgentSelectionTests(unittest.TestCase):
    def items(self):
        return [agents_tui.AgentItem("desktop", "Desktop", {"agent_id": "one", "title": "One"}),
                agents_tui.AgentItem("laptop", "Laptop", {"agent_id": "two", "title": "Two"})]

    def test_default_is_all_available_agents_and_toggles_persist_by_id(self):
        items = self.items()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch.object(agent_selection.paths, "user_config_dir", return_value=root / "config"),
                  patch.object(agent_selection.paths, "AGENTS_UI_STATE_DIR", root / "state")):
                state = agent_selection.load(items)
                self.assertTrue(all(agent_selection.visible(state, item) for item in items))
                agent_selection.toggle(state, items[0])
                agent_selection.save(state)
                self.assertEqual(agent_selection.config_path().stat().st_mode & 0o777, 0o600)
                state = agent_selection.load(items)
                items[0].agent["title"] = "Renamed"
                self.assertFalse(agent_selection.visible(state, items[0]))
                self.assertTrue(agent_selection.visible(state, items[1]))

    def test_one_global_order_across_hosts_and_absent_entries_are_preserved(self):
        items = self.items()
        state = {"version": 1, "hidden": [["gone", "absent"]],
                 "order": [["gone", "absent"], ["laptop", "two"], ["desktop", "one"]]}
        self.assertEqual(agent_selection.ordered(state, items), list(reversed(items)))
        agent_selection.remember_order(state, items)
        self.assertEqual(state["order"], [["desktop", "one"], ["laptop", "two"], ["gone", "absent"]])
        self.assertEqual(state["hidden"], [["gone", "absent"]])

    def test_migration_preserves_explicit_hidden_choices_order_and_drops_group_filters(self):
        items = self.items()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir()
            state_dir = root / "state"
            state_dir.mkdir()
            legacy = {"version": 1, "groups": {"Work": ["Two"]}, "active": ["Work"],
                      "order": ["Work", "", "Hidden"],
                      "hidden_overrides": {"absent/agent": True}}
            original = json.dumps(legacy)
            (config / "agent-groups.json").write_text(original)
            old_order = json.dumps({"version": 1, "order": [["desktop", "one"], ["laptop", "two"]]})
            (state_dir / "agent-order.json").write_text(old_order)
            with (patch.object(agent_selection.paths, "user_config_dir", return_value=config),
                  patch.object(agent_selection.paths, "AGENTS_UI_STATE_DIR", state_dir)):
                result = agent_selection.load(items)
                self.assertTrue(agent_selection.visible(result, items[0]))
                self.assertTrue(agent_selection.visible(result, items[1]))
                self.assertEqual(result["order"][:2], [["laptop", "two"], ["desktop", "one"]])
                self.assertIn(["absent", "agent"], result["hidden"])
                self.assertEqual((config / "agent-groups.json").read_text(), original)
                self.assertEqual((state_dir / "agent-order.json").read_text(), old_order)
                agent_selection.toggle(result, items[0])
                agent_selection.save(result)
                self.assertFalse(agent_selection.visible(agent_selection.load(items), items[0]))

    def test_hidden_legacy_agent_stays_hidden_with_grouping_disabled(self):
        items = self.items()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "agent-groups.json").write_text(json.dumps({
                "version": 1, "groups": {}, "active": [], "enabled": False,
                "hidden_overrides": {"desktop/one": True},
            }))
            with (patch.object(agent_selection.paths, "user_config_dir", return_value=root),
                  patch.object(agent_selection.paths, "AGENTS_UI_STATE_DIR", root / "unused")):
                state = agent_selection.load(items)
            self.assertFalse(agent_selection.visible(state, items[0]))
            self.assertTrue(agent_selection.visible(state, items[1]))

    def test_malformed_preferences_are_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            location = Path(temporary) / "selection.json"
            for text in ("bad", '{"version":1,"hidden":["not-an-id-pair"]}'):
                location.write_text(text)
                with self.assertRaises(ValueError):
                    agent_selection.read(location)
                self.assertEqual(location.read_text(), text)

    def test_main_console_reorder_crosses_hosts_and_does_not_wrap_at_edges(self):
        items = self.items()
        before = list(items)
        self.assertFalse(agents_tui.move_agent_item(items, items, items[0], -1))
        self.assertEqual(items, before)
        self.assertTrue(agents_tui.move_agent_item(items, items, items[1], -1))
        self.assertEqual(items, list(reversed(before)))
