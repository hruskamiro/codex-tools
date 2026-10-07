from __future__ import annotations

import unittest

from codex_tools import agents_tui


class AgentTuiTests(unittest.TestCase):
    def test_mouse_wheel_maps_to_conversation_scroll(self) -> None:
        self.assertEqual(
            agents_tui.mouse_scroll_delta(agents_tui.curses.BUTTON4_PRESSED),
            3,
        )
        self.assertEqual(
            agents_tui.mouse_scroll_delta(agents_tui.curses.BUTTON5_PRESSED),
            -3,
        )
        self.assertEqual(agents_tui.mouse_scroll_delta(0), 0)

    def test_semantic_spans_highlight_code_strings_numbers_and_literals(self) -> None:
        spans = agents_tui.semantic_spans(
            'Use `limit=12`, name="soft blue", enabled=true, ratio=1.5.'
        )

        self.assertIn(("`limit=12`", "code"), spans)
        self.assertIn(('"soft blue"', "string"), spans)
        self.assertIn(("true", "literal"), spans)
        self.assertIn(("1.5", "number"), spans)

    def test_markdown_links_hide_destinations_and_style_labels(self) -> None:
        text = agents_tui.display_markdown_links(
            "Changed [localization.dart](/home/me/project/localization.dart)."
        )
        spans = agents_tui.semantic_spans(text)

        self.assertEqual(text, "Changed [localization.dart].")
        self.assertIn(("localization.dart", "link"), spans)
        self.assertNotIn("/home/me/project", "".join(value for value, _ in spans))

    def test_raw_web_urls_are_styled_as_links(self) -> None:
        spans = agents_tui.semantic_spans("See https://example.com/docs now.")

        self.assertIn(("https://example.com/docs", "link"), spans)

    def test_markdown_table_preserves_rows_and_styles_structure(self) -> None:
        records = [
            {
                "role": "assistant",
                "text": (
                    "| Name | Count |\n"
                    "| --- | ---: |\n"
                    "| alpha | 12 |\n"
                    "\nAfter the table."
                ),
            }
        ]

        lines = agents_tui.transcript_lines(records, 18)

        self.assertIn(("  | Name | Count |", "table_header"), lines)
        self.assertIn(("  | --- | ---: |", "table_separator"), lines)
        self.assertIn(("  | alpha | 12 |", "table_row"), lines)
        self.assertIn(("|", "table_border"), agents_tui.table_spans("| 12 |"))
        self.assertIn(("12", "number"), agents_tui.table_spans("| 12 |"))

    def test_show_status_repaints_and_flushes_footer(self) -> None:
        class Window:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return 24, 80

            def move(self, *args: object) -> None:
                self.calls.append(("move", *args))

            def clrtoeol(self) -> None:
                self.calls.append(("clear",))

            def addnstr(self, *args: object) -> None:
                self.calls.append(("text", *args))

            def refresh(self) -> None:
                self.calls.append(("refresh",))

        window = Window()

        agents_tui.show_status(window, "Refreshing…", 7)

        self.assertIn(("text", 23, 1, "Refreshing…", 78, 7), window.calls)
        self.assertEqual(window.calls[-1], ("refresh",))

    def test_ctrl_c_must_be_pressed_twice_consecutively(self) -> None:
        armed, should_exit = agents_tui.advance_interrupt(False, 3)
        self.assertTrue(armed)
        self.assertFalse(should_exit)

        armed, should_exit = agents_tui.advance_interrupt(armed, ord("j"))
        self.assertFalse(armed)
        self.assertFalse(should_exit)

        armed, should_exit = agents_tui.advance_interrupt(False, 3)
        armed, should_exit = agents_tui.advance_interrupt(armed, 3)
        self.assertTrue(should_exit)

    def test_escape_does_not_request_console_exit(self) -> None:
        armed, should_exit = agents_tui.advance_interrupt(True, 27)

        self.assertFalse(armed)
        self.assertFalse(should_exit)

    def test_q_does_not_request_console_exit(self) -> None:
        armed, should_exit = agents_tui.advance_interrupt(True, ord("q"))

        self.assertFalse(armed)
        self.assertFalse(should_exit)

    def test_filter_matches_host_title_basedir_and_id(self) -> None:
        item = agents_tui.AgentItem(
            "home", "Home workstation",
            {"title": "Viewer fixes", "cwd": "/work/codex-tools", "agent_id": "thread-123"},
        )

        for query in ("workstation", "viewer", "codex-tools", "thread-123"):
            self.assertEqual(agents_tui.filtered_items([item], query), [item])
        self.assertEqual(agents_tui.filtered_items([item], "missing"), [])

    def test_transcript_wraps_renderable_roles(self) -> None:
        records = [
            {"role": "system", "text": "hidden"},
            {
                "role": "user",
                "text": "<environment_context>\n<cwd>/private/work</cwd>",
            },
            {"role": "user", "text": "A fairly long question for wrapping"},
            {"role": "assistant", "text": "An answer"},
        ]

        lines = agents_tui.transcript_lines(records, 18)

        self.assertEqual(lines[0], ("› You", "user_label"))
        self.assertIn(("• Assistant", "assistant_label"), lines)
        self.assertIn(("", "user_gap"), lines)
        self.assertIn(("", "assistant_gap"), lines)
        self.assertNotIn("hidden", "\n".join(line for line, _ in lines))
        self.assertNotIn("environment_context", "\n".join(line for line, _ in lines))
        self.assertNotIn("/private/work", "\n".join(line for line, _ in lines))


if __name__ == "__main__":
    unittest.main()
