from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_tools import agents_tui, relay_cache, relay_transport, search, viewer


def tool_event(name="functions.exec_command", arguments='{"cmd":"secret command"}'):
    return {"timestamp": "2026-10-19T10:00:00Z", "type": "response_item",
            "payload": {"type": "function_call", "name": name, "arguments": arguments}}


class ConsoleActivityTests(unittest.TestCase):
    def test_activity_never_contains_arguments_or_results(self):
        record = search.tool_activity_record(tool_event())
        self.assertEqual(record.text, "Running a command")
        self.assertEqual(record.role, "activity")
        self.assertNotIn("secret", record.text)
        output = tool_event()
        output["payload"] = {"type": "function_call_output", "output": "secret output"}
        self.assertIsNone(search.tool_activity_record(output))
        self.assertEqual(search.text_records_from_event(tool_event(), 1, False, False), [])

    def test_orchestration_and_custom_tools_have_short_labels(self):
        record = search.tool_activity_record(tool_event("functions.exec", "await tools.apply_patch('secret patch')"))
        self.assertEqual(record.text, "Editing files")
        event = tool_event("functions.apply_patch")
        event["payload"]["type"] = "custom_tool_call"
        self.assertEqual(search.tool_activity_record(event).text, "Editing files")

    def test_snapshot_and_incremental_stream_agree_without_outputs(self):
        events = [tool_event(), {"type": "response_item", "payload": {
            "type": "function_call_output", "output": "private output"}}, tool_event("functions.apply_patch")]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text("", encoding="utf-8")
            cursor = relay_transport.event_cursor_for_path(path, "agent", "key")
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            payload = relay_transport.read_jsonl_events(path, "agent", cursor, "key")
            snapshot = relay_cache.load_recent_session(path)
            self.assertEqual([event["record"] for event in payload["events"]],
                             [asdict(record) for record in snapshot.records])
            self.assertEqual(len(snapshot.records), 2)
            cache = relay_cache.RelayCache(Path(temporary) / "cache.sqlite3")
            agent = relay_cache.CachedAgent("agent", "agent", "agent", "agent", "Title", "index",
                                           "/work", "cli", "", "", str(path), ())
            cache.replace_host_snapshot("remote", "Remote", [agent])
            payload.update(protocol=relay_transport.PROTOCOL_VERSION, host={"id": "remote"}, agent_id="agent")
            self.assertEqual(relay_transport.apply_agent_events(cache, payload), 2)
            self.assertEqual(cache.records("remote", "agent")[0]["record_type"], "tool_activity")
            view = viewer.handle_relay_session(SimpleNamespace(relay_cache=cache),
                                               {"host": ["remote"], "agent": ["agent"]})
            self.assertEqual(view["records"], [])

    def test_only_three_muted_breadcrumbs_and_no_assistant_bullets(self):
        records = [{"role": "activity", "record_type": "tool_activity", "text": f"Action {index}"}
                   for index in range(10)]
        records.append({"role": "assistant", "text": "Done"})
        rendered = agents_tui.transcript_lines(records, 80)
        activity = [(text, style) for text, style in rendered if "Action" in text]
        self.assertEqual(len(activity), 3)
        self.assertTrue(all(style == "muted" and not text.startswith("•") for text, style in activity))
        self.assertIn(("• Done", "assistant"), rendered)

    def test_tool_block_has_one_blank_line_before_assistant_response(self):
        records = [{"role": "activity", "record_type": "tool_activity", "text": name}
                   for name in ("Running tools", "Editing files")]
        records.append({"role": "assistant", "text": "Done"})
        rendered = agents_tui.transcript_lines(records, 80)
        self.assertEqual(rendered[:4], [
            ("  · Running tools", "muted"), ("  · Editing files", "muted"),
            ("", "assistant_gap"), ("• Done", "assistant"),
        ])

    def test_working_indicator_remains_compact_after_tool_activity(self):
        records = [{"role": "activity", "record_type": "tool_activity", "text": "Running tools"}]
        rendered = agents_tui.transcript_lines(
            agents_tui.records_with_agent_activity(records, agent_working=True, frame="◐"), 80)
        self.assertEqual(rendered[:2], [("  · Running tools", "muted"),
                                       ("  ◐ Working…", "agent_activity")])

    def test_snapshot_activity_does_not_displace_all_conversation(self):
        message = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "Previous answer"}]}}
        events = [message] + [tool_event() for _ in range(100)]
        records = relay_cache.recent_records_from_lines(
            iter(json.dumps(event).encode() for event in reversed(events)), 24, 200)
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0].text, "Previous answer")


class ComposerWordWrapTests(unittest.TestCase):
    def test_words_wrap_without_inserting_newlines_in_draft(self):
        draft = agents_tui.ComposerBuffer("hello world again", 8)
        lines, positions = agents_tui.composer_layout(draft.text, 10)
        self.assertEqual(lines, ["hello ", "world ", "again"])
        self.assertEqual(positions[6], (1, 0))
        self.assertEqual(positions[8], (1, 2))
        self.assertEqual(draft.text, "hello world again")

    def test_boundary_spaces_do_not_get_their_own_line(self):
        lines, positions = agents_tui.composer_layout("hello   world", 5)
        self.assertEqual(lines, ["hello", "world", ""])
        self.assertEqual(positions[8], (1, 0))

    def test_long_urls_and_explicit_indentation_still_work(self):
        text = "https://example.test/long\n  next line"
        lines, positions = agents_tui.composer_layout(text, 8)
        self.assertTrue(all(sum(agents_tui.character_cell_width(c) for c in line) <= 8 for line in lines))
        self.assertIn("  next ", lines)
        self.assertEqual(len(positions), len(text) + 1)

    def test_vertical_cursor_uses_word_wrapped_lines(self):
        draft = agents_tui.ComposerBuffer("hello world", 2)
        agents_tui.move_composer_vertical(draft, 10, 1)
        self.assertEqual(draft.cursor, 8)
        agents_tui.move_composer_vertical(draft, 10, -1)
        self.assertEqual(draft.cursor, 2)

    def test_text_has_one_cell_of_right_background_padding(self):
        window = Mock()
        window.getmaxyx.return_value = (30, 80)
        palette = {"composer": 0, "composer_accent": 1, "composer_muted": 2}
        with patch.object(agents_tui, "add_text") as draw:
            _, width = agents_tui.draw_composer(window, palette, agents_tui.ComposerBuffer("x" * 100, 100),
                                               0, 80, focused=True)
        background = next(call.args for call in draw.call_args_list if call.args[3] == " " * 78)
        content = next(call.args for call in draw.call_args_list if call.args[3].startswith("xxx"))
        self.assertEqual(background[2] + background[4] - (content[2] + width), 1)
