from __future__ import annotations

import unittest
from unittest.mock import patch

from codex_tools import agents_tui, viewer


class AgentTuiTests(unittest.TestCase):
    def test_path_compaction_preserves_the_end(self) -> None:
        path = "/home/hruskam/projects/codex-tools"

        compacted = agents_tui.compact_path_end(path, 22)

        self.assertEqual(len(compacted), 22)
        self.assertTrue(compacted.startswith("…"))
        self.assertTrue(compacted.endswith("projects/codex-tools"))
        self.assertEqual(agents_tui.compact_path_end("/work/app", 20), "/work/app")

    def test_path_compaction_never_shows_partial_directory_names(self) -> None:
        path = "/home/hruskam/projects/codex-tools"

        self.assertEqual(
            agents_tui.compact_path_end(path, 18),
            "…/codex-tools",
        )
        self.assertEqual(agents_tui.compact_path_end(path, 10), "…")

    def test_agent_items_can_exclude_local_host(self) -> None:
        class Cache:
            def hosts(self) -> list[dict[str, str]]:
                return [
                    {"host_id": "local", "name": "This machine"},
                    {"host_id": "remote-1", "name": "Desktop"},
                ]

            def agents(self, host_id: str) -> list[dict[str, str]]:
                return [{"agent_id": f"{host_id}-agent"}]

        items = agents_tui.all_agent_items(Cache(), include_local=False)  # type: ignore[arg-type]

        self.assertEqual([item.host_id for item in items], ["remote-1"])

    def test_remote_item_resolves_to_peer_by_base_url(self) -> None:
        item = agents_tui.AgentItem(
            "host-id",
            "Desktop",
            {"agent_id": "agent-1"},
            "https://desktop.example:8780",
        )
        config = {
            "peers": {
                "workstation": {
                    "url": "https://desktop.example:8780/",
                    "token": "secret",
                }
            }
        }

        self.assertEqual(
            agents_tui.peer_name_for_item(config, item), "workstation"
        )

    def test_selection_is_preserved_after_host_refresh(self) -> None:
        previous = agents_tui.AgentItem(
            "remote", "Desktop", {"agent_id": "chosen"}
        )
        refreshed = [
            agents_tui.AgentItem("remote", "Desktop", {"agent_id": "other"}),
            agents_tui.AgentItem("remote", "Desktop", {"agent_id": "chosen"}),
        ]

        self.assertEqual(
            agents_tui.restored_selection(refreshed, previous), 1
        )

    def test_sync_peer_fetches_and_caches_remote_snapshot(self) -> None:
        config = {
            "peers": {
                "desktop": {
                    "url": "https://desktop.example:8780",
                    "token": "secret",
                }
            }
        }
        payload = {
            "host": {"id": "host-1", "name": "Desktop"},
            "agents": [],
        }

        with (
            patch.object(
                agents_tui.relay_transport,
                "fetch_snapshot",
                return_value=payload,
            ) as fetch,
            patch.object(
                agents_tui.relay_transport,
                "apply_snapshot",
                return_value=3,
            ) as apply,
        ):
            result = agents_tui.sync_peer(object(), config, "desktop")  # type: ignore[arg-type]

        self.assertEqual(result, (3, "Desktop"))
        fetch.assert_called_once_with(config["peers"]["desktop"])
        self.assertEqual(
            apply.call_args.kwargs["base_url"],
            "https://desktop.example:8780",
        )

    def test_sync_agent_fetches_only_selected_conversation(self) -> None:
        config = {
            "peers": {
                "desktop": {
                    "url": "https://desktop.example:8780",
                    "token": "secret",
                }
            }
        }
        payload = {
            "host": {"id": "host-1", "name": "Desktop"},
            "agent": {"agent_id": "selected"},
        }

        with (
            patch.object(
                agents_tui.relay_transport,
                "fetch_agent",
                return_value=payload,
            ) as fetch,
            patch.object(
                agents_tui.relay_transport,
                "apply_agent_snapshot",
            ) as apply,
        ):
            host = agents_tui.sync_agent(
                object(), config, "desktop", "selected"  # type: ignore[arg-type]
            )

        self.assertEqual(host, "Desktop")
        fetch.assert_called_once_with(
            config["peers"]["desktop"], "selected"
        )
        self.assertEqual(
            apply.call_args.kwargs["base_url"],
            "https://desktop.example:8780",
        )

    def test_remote_agent_opens_source_aware_viewer_route(self) -> None:
        item = agents_tui.AgentItem(
            "remote-host",
            "Desktop",
            {"agent_id": "agent-1"},
            "https://desktop:8780",
        )

        with (
            patch.object(
                viewer,
                "running_daemon_state",
                return_value={"url": "http://127.0.0.1:8765/"},
            ),
            patch.object(
                viewer,
                "viewer_health",
                return_value=(True, "responding"),
            ),
            patch.object(
                viewer,
                "viewer_source_is_current",
                return_value=True,
            ),
            patch.object(
                viewer,
                "open_browser",
                return_value=True,
            ) as opened,
        ):
            url = agents_tui.open_agent_viewer(item)

        self.assertEqual(
            url,
            "http://127.0.0.1:8765/v/relay/remote-host/agent-1?tail=24",
        )
        self.assertEqual(opened.call_args.args[0], url)

    def test_pane_navigation_can_maximize_and_restore_conversation(self) -> None:
        focus, maximized = agents_tui.change_pane(
            "agents", False, agents_tui.curses.KEY_RIGHT
        )
        self.assertEqual((focus, maximized), ("conversation", False))

        focus, maximized = agents_tui.change_pane(
            focus, maximized, agents_tui.curses.KEY_RIGHT
        )
        self.assertEqual((focus, maximized), ("conversation", True))

        focus, maximized = agents_tui.change_pane(
            focus, maximized, agents_tui.curses.KEY_LEFT
        )
        self.assertEqual((focus, maximized), ("conversation", False))

        focus, maximized = agents_tui.change_pane(
            focus, maximized, agents_tui.curses.KEY_LEFT
        )
        self.assertEqual((focus, maximized), ("agents", False))

    def test_m_toggles_maximized_conversation(self) -> None:
        self.assertEqual(
            agents_tui.change_pane("agents", False, ord("m")),
            ("conversation", True),
        )
        self.assertEqual(
            agents_tui.change_pane("conversation", True, ord("m")),
            ("conversation", False),
        )

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

        self.assertTrue(any(line.startswith("  ┌") for line, _ in lines))
        self.assertTrue(any(style == "table_header" for _, style in lines))
        self.assertTrue(any(style == "table_row" for _, style in lines))
        self.assertIn(("|", "table_border"), agents_tui.table_spans("| 12 |"))
        self.assertIn(("12", "number"), agents_tui.table_spans("| 12 |"))

    def test_terminal_table_wraps_long_cells_to_pane_width(self) -> None:
        table = agents_tui.format_terminal_table(
            ["Family", "Guardrail"],
            ["---", "---"],
            [["Folk-modern", "A long description that must wrap cleanly"]],
            38,
        )

        self.assertTrue(all(len(line) <= 38 for line, _ in table))
        self.assertGreater(
            sum(style == "table_row" for _, style in table),
            1,
        )

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
