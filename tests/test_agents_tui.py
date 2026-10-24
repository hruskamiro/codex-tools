from __future__ import annotations

import re
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

from codex_tools import agents_tui, viewer


class AgentTuiTests(unittest.TestCase):
    DELIVERY = {"delivered": 0, "pending": 0, "failed": 0, "errors": []}

    def test_only_latest_assistant_reply_gets_its_local_timestamp(self) -> None:
        timestamp = "2026-10-21T11:22:33Z"
        records = [
            {"role": "assistant", "text": "Earlier", "timestamp": "2026-10-21T10:00:00Z"},
            {"role": "assistant", "text": "Latest", "timestamp": timestamp},
            {"role": "assistant", "text": "Running tools", "timestamp": "2026-10-21T12:00:00Z",
             "record_type": "tool_activity"},
        ]
        lines = agents_tui.transcript_lines(records, 80)
        expected = agents_tui.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
        self.assertEqual([line for line, style in lines if style == "muted"
                          and re.fullmatch(r"  \d{2}:\d{2}:\d{2}", line)],
                         ["  " + expected])
        index = lines.index(("  " + expected, "muted"))
        self.assertEqual(lines[index - 1], ("", "assistant_gap"))

    def test_invalid_assistant_timestamp_does_not_break_transcript(self) -> None:
        lines = agents_tui.transcript_lines([
            {"role": "assistant", "text": "Reply", "timestamp": "invalid"},
        ], 80)
        self.assertFalse(any(style == "muted" for _, style in lines))

    def test_macos_draws_text_without_inline_attributes(self) -> None:
        window = Mock()
        with patch.object(agents_tui.sys, "platform", "darwin"):
            agents_tui.add_text(window, 2, 3, "› Café", 20, 17)
        window.addnstr.assert_called_once_with(2, 3, "› Café".encode(), 9)
        window.chgat.assert_called_once_with(2, 3, 6, 17)

    def test_macos_style_is_clipped_to_display_cells(self) -> None:
        window = Mock()
        with patch.object(agents_tui.sys, "platform", "darwin"):
            agents_tui.add_text(window, 0, 0, "界e\u0301界", 3, 7)
        encoded = "界e\u0301".encode()
        window.addnstr.assert_called_once_with(0, 0, encoded, len(encoded))
        window.chgat.assert_called_once_with(0, 0, 3, 7)

    def test_macos_text_write_does_not_cross_lines(self) -> None:
        window = Mock()
        with patch.object(agents_tui.sys, "platform", "darwin"):
            agents_tui.add_text(window, 0, 0, "one\ntwo", 20, 0)
        window.addnstr.assert_called_once_with(0, 0, b"one", 3)
        window.chgat.assert_called_once_with(0, 0, 3, 0)

    def test_auto_sync_worker_refreshes_selected_local_agent(self) -> None:
        refreshed = threading.Event()
        item = agents_tui.AgentItem(
            "local", "This machine", {"agent_id": "agent-1"}
        )
        with patch.object(
            agents_tui.relay_cache,
            "refresh_local_agent",
            side_effect=lambda *_args: refreshed.set(),
        ):
            worker = agents_tui.AutoSyncWorker(
                object(),  # type: ignore[arg-type]
                agents_tui.Path("/sessions"),
                agents_tui.Path("/index"),
                None,
                agents_tui.Path("/relay.json"),
            )
            try:
                worker.select(item, "live")
                self.assertTrue(refreshed.wait(1))
                update = worker.updates.get(timeout=1)
            finally:
                worker.close()

        self.assertEqual(update.target, ("local", "agent-1"))
        self.assertEqual(update.error, "")

    def test_stop_agent_turn_uses_daemon_for_local_agent(self) -> None:
        item = agents_tui.AgentItem(
            "local",
            "This machine",
            {"agent_id": "agent-1", "thread_id": "thread-1"},
        )
        expected = {"status": "interrupt_requested", "turn_id": "turn-1"}
        with patch.object(
            agents_tui.codex_daemon, "interrupt_thread", return_value=expected
        ) as interrupt:
            result = agents_tui.stop_agent_turn(
                item, agents_tui.Path("/control.sock"), agents_tui.Path("/relay.json")
            )

        self.assertEqual(result, expected)
        interrupt.assert_called_once_with("thread-1", agents_tui.Path("/control.sock"))

    def test_stop_agent_turn_uses_remote_peer_without_queueing(self) -> None:
        item = agents_tui.AgentItem(
            "host-1",
            "Desktop",
            {"agent_id": "agent-1"},
            "https://desktop.example:8780",
        )
        peer = {"url": "https://desktop.example:8780", "token": "secret"}
        config = {"peers": {"desktop": peer}}
        expected = {"status": "interrupt_requested", "turn_id": "turn-1"}
        with (
            patch.object(
                agents_tui.relay_transport, "load_config", return_value=config
            ),
            patch.object(
                agents_tui.relay_transport,
                "interrupt_agent",
                return_value=expected,
            ) as interrupt,
        ):
            result = agents_tui.stop_agent_turn(
                item, None, agents_tui.Path("/relay.json")
            )

        self.assertEqual(result, expected)
        interrupt.assert_called_once_with(peer, "agent-1")

    def test_adaptive_worker_long_polls_selected_remote_agent(self) -> None:
        synchronized = threading.Event()
        item = agents_tui.AgentItem(
            "remote-host",
            "Desktop",
            {"agent_id": "agent-1"},
            "https://desktop.example:8780",
        )
        config = {
            "peers": {
                "desktop": {
                    "url": "https://desktop.example:8780",
                    "token": "secret",
                }
            }
        }

        def synchronize(*_args: object, **kwargs: object) -> tuple[str, dict[str, object]]:
            self.assertEqual(kwargs["wait"], 8.0)
            synchronized.set()
            return "Desktop", self.DELIVERY

        with (
            patch.object(agents_tui.relay_transport, "load_config", return_value=config),
            patch.object(agents_tui, "sync_agent", side_effect=synchronize),
        ):
            worker = agents_tui.AutoSyncWorker(
                object(),  # type: ignore[arg-type]
                agents_tui.Path("/sessions"),
                agents_tui.Path("/index"),
                None,
                agents_tui.Path("/relay.json"),
            )
            try:
                worker.select(item, "adaptive")
                self.assertTrue(synchronized.wait(1))
                update = worker.updates.get(timeout=1)
            finally:
                worker.close()

        self.assertEqual(update.target, ("remote-host", "agent-1"))
        self.assertEqual(update.error, "")

    def test_composer_buffer_edits_at_cursor(self) -> None:
        buffer = agents_tui.ComposerBuffer("helo", 3)

        buffer.insert("l")
        buffer.move(-1)
        buffer.delete()
        buffer.insert("l")

        self.assertEqual(buffer.text, "hello")
        self.assertEqual(buffer.cursor, 4)

    def test_composer_layout_wraps_and_preserves_explicit_lines(self) -> None:
        lines, positions = agents_tui.composer_layout("abcd\nef", 3)

        self.assertEqual(lines, ["abc", "d", "ef"])
        self.assertEqual(positions[4], (1, 1))
        self.assertEqual(positions[5], (2, 0))
        self.assertEqual(positions[-1], (2, 2))

    def test_composer_vertical_movement_keeps_visual_column(self) -> None:
        buffer = agents_tui.ComposerBuffer("abcdef", 2)

        agents_tui.move_composer_vertical(buffer, 3, 1)
        self.assertEqual(buffer.cursor, 5)
        agents_tui.move_composer_vertical(buffer, 3, -1)
        self.assertEqual(buffer.cursor, 2)

    def test_composer_cursor_wraps_at_exact_boundary(self) -> None:
        for text, width in (("abcdef", 3), ("界界", 4)):
            with self.subTest(text=text):
                lines, positions = agents_tui.composer_layout(text, width)
                self.assertEqual(lines[-1], "")
                self.assertEqual(positions[-1], (len(lines) - 1, 0))

    def test_composer_reflows_without_changing_draft_on_resize(self) -> None:
        buffer = agents_tui.ComposerBuffer("abcdef\nghij", 11)
        for width in (12, 3, 6, 2, 12):
            lines, positions = agents_tui.composer_layout(buffer.text, width)
            self.assertTrue(all(sum(agents_tui.character_cell_width(c) for c in line)
                                <= width for line in lines))
            row, column = positions[buffer.cursor]
            self.assertLess(row, len(lines))
            self.assertLess(column, width)
        self.assertEqual(buffer.text, "abcdef\nghij")

    def test_ctrl_p_and_ctrl_n_move_between_visual_lines(self) -> None:
        buffer = agents_tui.ComposerBuffer("abcdef", 2)

        agents_tui.edit_composer_buffer(buffer, "\x0e", 3)
        self.assertEqual(buffer.cursor, 5)
        agents_tui.edit_composer_buffer(buffer, "\x10", 3)
        self.assertEqual(buffer.cursor, 2)

    def test_ctrl_arrows_move_between_words(self) -> None:
        buffer = agents_tui.ComposerBuffer("one two_three four", 18)

        agents_tui.edit_composer_buffer(buffer, agents_tui.CTRL_LEFT_KEY, 40)
        self.assertEqual(buffer.cursor, 14)
        agents_tui.edit_composer_buffer(buffer, agents_tui.CTRL_LEFT_KEY, 40)
        self.assertEqual(buffer.cursor, 4)
        agents_tui.edit_composer_buffer(buffer, agents_tui.CTRL_RIGHT_KEY, 40)
        self.assertEqual(buffer.cursor, 14)

    def test_ncurses_extended_ctrl_arrow_keys_move_between_words(self) -> None:
        buffer = agents_tui.ComposerBuffer("one two three", 13)

        with patch.object(agents_tui.curses, "keyname", return_value=b"kLFT5"):
            agents_tui.edit_composer_buffer(buffer, 554, 40)
        self.assertEqual(buffer.cursor, 8)

        with patch.object(agents_tui.curses, "keyname", return_value=b"kRIT5"):
            agents_tui.edit_composer_buffer(buffer, 569, 40)
        self.assertEqual(buffer.cursor, 13)

    def test_backward_word_deletion_keeps_text_after_cursor(self) -> None:
        buffer = agents_tui.ComposerBuffer("one two three", 7)

        agents_tui.delete_composer_word(buffer)

        self.assertEqual(buffer.text, "one  three")
        self.assertEqual(buffer.cursor, 4)

    def test_persistent_composer_uses_ctrl_j_for_newline_and_enter_to_send(self) -> None:
        buffer = agents_tui.ComposerBuffer("hello", 5)

        self.assertIsNone(agents_tui.edit_composer_buffer(buffer, "\n", 40))
        self.assertEqual(buffer.text, "hello\n")
        self.assertEqual(
            agents_tui.edit_composer_buffer(buffer, "\r", 40),
            "send",
        )

    def test_tab_queues_draft_without_inserting_a_tab(self) -> None:
        buffer = agents_tui.ComposerBuffer("draft", 5)

        self.assertEqual(agents_tui.edit_composer_buffer(buffer, "\t", 40), "queue")

        self.assertEqual(buffer.text, "draft")

    def test_tab_does_not_submit_an_empty_draft(self) -> None:
        self.assertIsNone(agents_tui.edit_composer_buffer(
            agents_tui.ComposerBuffer("  ", 2), "\t", 40
        ))

    def test_explicit_queue_status_distinguishes_delivery_from_execution(self) -> None:
        message = agents_tui.OptimisticMessage("later", 0, "sent", queued=True)
        self.assertEqual(agents_tui.optimistic_message_status(message, {}, agent_working=True),
                         "Queued for agent")
        message = agents_tui.OptimisticMessage("later", 0, "queued", "message-id", queued=True)
        self.assertEqual(agents_tui.optimistic_message_status(message, {}),
                         "Waiting to sync")
        self.assertEqual(agents_tui.optimistic_message_status(
            message, {"message-id": {"state": "delivered"}}, agent_working=True),
            "Queued in Codex")

    def test_composer_has_background_padding_above_and_below(self) -> None:
        window = Mock()
        window.getmaxyx.return_value = (20, 80)
        top, _ = agents_tui.draw_composer(
            window, {"composer": 0, "composer_accent": 0, "composer_muted": 0},
            agents_tui.ComposerBuffer("draft", 5), 20, 60, focused=True,
        )
        self.assertEqual(top, 17)
        writes = window.addnstr.call_args_list
        for row in (17, 19):
            self.assertTrue(any(call.args[0] == row and not call.args[2].strip()
                                for call in writes))
            self.assertFalse(any(call.args[0] == row and call.args[2].strip()
                                 for call in writes))
        self.assertEqual(window.move.call_args.args[0], 18)

    def test_agent_drafts_remain_independent_when_switching(self) -> None:
        drafts = {
            ("local", "one"): agents_tui.ComposerBuffer("first", 5),
            ("remote", "two"): agents_tui.ComposerBuffer("second", 6),
        }

        agents_tui.edit_composer_buffer(drafts[("local", "one")], "!", 40)

        self.assertEqual(drafts[("local", "one")].text, "first!")
        self.assertEqual(drafts[("remote", "two")].text, "second")

    def test_agent_delivery_counts_pending_and_failed_messages(self) -> None:
        messages = [
            {"host_id": "home", "agent_id": "one", "state": "pending"},
            {"host_id": "home", "agent_id": "one", "state": "failed"},
            {"host_id": "home", "agent_id": "two", "state": "pending"},
            {"host_id": "home", "agent_id": "one", "state": "delivered"},
        ]

        self.assertEqual(
            agents_tui.agent_delivery_counts(messages, "home", "one"),
            (1, 1),
        )

    def test_optimistic_message_is_rendered_with_delivery_status(self) -> None:
        message = agents_tui.OptimisticMessage(
            "Continue please", 0, "pending", "message-1"
        )
        outbox = {
            "message-1": {
                "message_id": "message-1",
                "state": "delivered",
            }
        }

        records = agents_tui.records_with_optimistic_messages(
            [], [message], outbox
        )
        lines = agents_tui.transcript_lines(records, 60)

        self.assertEqual(records[0]["role"], "user")
        self.assertEqual(records[0]["text"], "Continue please")
        self.assertIn(
            ("  ◌ Queued in Codex", "message_status"),
            lines,
        )

    def test_agent_activity_does_not_imply_queued_message_has_started(self) -> None:
        message = agents_tui.OptimisticMessage(
            "Continue please", 0, "pending", "message-1"
        )

        status = agents_tui.optimistic_message_status(
            message,
            {"message-1": {"state": "delivered"}},
            agent_working=True,
        )

        self.assertEqual(status, "Queued in Codex")

    def test_enter_and_tab_have_consistent_queue_status_after_stop(self) -> None:
        for explicit_queue in (False, True):
            for working in (False, True):
                with self.subTest(queued=explicit_queue, working=working):
                    local = agents_tui.OptimisticMessage("Continue", 0, "sent", queued=explicit_queue)
                    self.assertEqual(agents_tui.optimistic_message_status(
                        local, {}, agent_working=working), "Queued for agent")
                    remote = agents_tui.OptimisticMessage("Continue", 0, "queued", "id", queued=explicit_queue)
                    self.assertEqual(agents_tui.optimistic_message_status(
                        remote, {"id": {"state": "delivered"}}, agent_working=working),
                        "Queued in Codex")

    def test_pending_messages_distinguish_waiting_from_delivery_errors(self) -> None:
        message = agents_tui.OptimisticMessage("Continue", 0, "queued", "id")
        self.assertEqual(agents_tui.optimistic_message_status(
            message, {"id": {"state": "pending"}}), "Waiting to sync")
        self.assertEqual(agents_tui.optimistic_message_status(
            message, {"id": {"state": "pending", "last_error": "timeout"}}),
            "Not delivered · waiting to retry")
        self.assertEqual(agents_tui.optimistic_message_status(
            message, {"id": {"state": "failed", "last_error": "session unavailable"}}),
            "Delivery failed · session unavailable")

    def test_start_failure_is_visible_without_implying_delivery_failed(self) -> None:
        local = agents_tui.OptimisticMessage("Continue", 0, "sent", start_error="unavailable")
        remote = agents_tui.OptimisticMessage("Continue", 0, "queued", "id")
        expected = "Queued · could not start: unavailable"
        self.assertEqual(agents_tui.optimistic_message_status(local, {}), expected)
        self.assertEqual(agents_tui.optimistic_message_status(
            remote, {"id": {"state": "delivered", "last_error": "unavailable"}}), expected)

    def test_working_indicator_requires_active_working_state(self) -> None:
        self.assertTrue(
            agents_tui.agent_is_working(
                {"status": "active", "status_label": "working"}
            )
        )
        self.assertFalse(
            agents_tui.agent_is_working(
                {"status": "active", "status_label": "needs-you"}
            )
        )
        self.assertEqual(agents_tui.working_frame(0.0), "◐")
        self.assertEqual(agents_tui.working_frame(0.2), "◓")

    def test_working_agent_adds_transient_assistant_activity(self) -> None:
        records = [{"role": "user", "text": "Please continue"}]

        displayed = agents_tui.records_with_agent_activity(
            records, agent_working=True, frame="◐"
        )
        lines = agents_tui.transcript_lines(displayed, 60)

        self.assertIsNot(displayed, records)
        self.assertEqual(displayed[-1]["record_type"], "activity")
        self.assertEqual(displayed[-1]["text"], "◐ Working…")
        self.assertIn(("  ◐ Working…", "agent_activity"), lines)
        self.assertNotIn(("• ◐ Working…", "agent_activity"), lines)

    def test_idle_agent_does_not_add_transient_activity(self) -> None:
        records = [{"role": "assistant", "text": "Done"}]

        displayed = agents_tui.records_with_agent_activity(
            records, agent_working=False
        )

        self.assertIs(displayed, records)

    def test_real_transcript_reconciles_one_identical_optimistic_message(self) -> None:
        messages = [
            agents_tui.OptimisticMessage("Same", 1, "sent"),
            agents_tui.OptimisticMessage("Same", 1, "sent"),
        ]
        records = [
            {"role": "user", "text": "Same"},
            {"role": "user", "text": "Same"},
        ]

        remaining = agents_tui.reconcile_optimistic_messages(
            records, messages
        )

        self.assertEqual(len(remaining), 1)

    def test_failed_optimistic_message_preserves_error(self) -> None:
        message = agents_tui.OptimisticMessage(
            "Continue", 0, "pending", "message-1"
        )

        status = agents_tui.optimistic_message_status(
            message,
            {
                "message-1": {
                    "state": "failed",
                    "last_error": "agent disappeared",
                }
            },
        )

        self.assertEqual(
            status, "Delivery failed · agent disappeared"
        )

    def test_composer_draws_only_prompt_and_draft(self) -> None:
        class Window:
            def __init__(self) -> None:
                self.writes: list[str] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (20, 80)

            def addnstr(self, y: int, x: int, text: str | bytes, width: int, attr: int = 0) -> None:
                self.writes.append(text[:width].decode() if isinstance(text, bytes) else text[:width])

            def chgat(self, *args: object) -> None:
                pass

            def move(self, y: int, x: int) -> None:
                pass

        window = Window()
        agents_tui.draw_composer(
            window,  # type: ignore[arg-type]
            {"composer": 0, "composer_accent": 0, "composer_muted": 0},
            agents_tui.ComposerBuffer("draft", 5),
            20,
            60,
            focused=True,
        )

        content = "".join(window.writes)
        self.assertIn("›", content)
        self.assertIn("draft", content)
        self.assertNotIn("Enter", content)
        self.assertNotIn("Message", content)

    def test_composer_recognizes_alt_enter_and_multiline_paste(self) -> None:
        class Window:
            def __init__(self, keys: list[str]) -> None:
                self.keys = iter(keys)

            def get_wch(self) -> str:
                return next(self.keys)

            def timeout(self, value: int) -> None:
                pass

        action, value = agents_tui.escape_action(Window(["\r"]))  # type: ignore[arg-type]
        pasted = agents_tui.read_bracketed_paste(
            Window(list("first\nsecond\x1b[201~"))  # type: ignore[arg-type]
        )

        self.assertEqual((action, value), ("newline", ""))
        self.assertEqual(pasted, "first\nsecond")

        action, value = agents_tui.escape_action(  # type: ignore[arg-type]
            Window(["\x7f"])
        )
        self.assertEqual((action, value), ("word_backspace", ""))

        action, value = agents_tui.escape_action(Window(["m"]))  # type: ignore[arg-type]
        self.assertEqual((action, value), ("maximize", ""))

        action, value = agents_tui.escape_action(Window(["4"]))  # type: ignore[arg-type]
        self.assertEqual((action, value), ("agent", "4"))

    def test_composer_recognizes_modified_arrow_sequences(self) -> None:
        class Window:
            def __init__(self, keys: list[str]) -> None:
                self.keys = iter(keys)

            def get_wch(self) -> str:
                return next(self.keys)

            def timeout(self, value: int) -> None:
                pass

        cases = {
            "1;5D": "word_left",
            "5C": "word_right",
            "1;2D": "agents",
            "1;2C": "conversation",
            "1;3A": "select_agent_up",
            "3B": "select_agent_down",
            "1;4A": "move_agent_up",
            "4B": "move_agent_down",
        }
        for sequence, expected in cases.items():
            with self.subTest(sequence=sequence):
                action, value = agents_tui.escape_action(
                    Window(["[", *sequence])  # type: ignore[arg-type]
                )
                self.assertEqual((action, value), (expected, ""))

    def test_soft_palette_uses_muted_accents_and_charcoal_surfaces(self) -> None:
        palette = agents_tui.soft_color_definitions(256)

        self.assertEqual(palette["normal"], (252, -1))
        self.assertEqual(palette["muted"], (245, -1))
        self.assertEqual(palette["focus"], (110, -1))
        self.assertEqual(palette["active_dot"], (114, -1))
        self.assertEqual(palette["selected"], (252, 238))
        self.assertEqual(palette["user"], (252, 237))
        self.assertEqual(palette["code"], (252, 235))

    def test_basic_color_fallback_avoids_bright_blue_backgrounds(self) -> None:
        palette = agents_tui.soft_color_definitions(8)

        self.assertEqual(
            palette["user"][1], agents_tui.curses.COLOR_BLACK
        )
        self.assertEqual(
            palette["selected"][1], agents_tui.curses.COLOR_BLACK
        )

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

    def test_agent_items_use_stable_creation_order_not_recent_activity(self) -> None:
        class Cache:
            def hosts(self) -> list[dict[str, str]]:
                return [{"host_id": "local", "name": "This machine"}]

            def agents(self, _host_id: str) -> list[dict[str, str]]:
                return [
                    {
                        "agent_id": "older",
                        "created_at": "2026-10-01T10:00:00Z",
                        "updated_at": "2026-10-10T10:00:00Z",
                    },
                    {
                        "agent_id": "newer",
                        "created_at": "2026-10-02T10:00:00Z",
                        "updated_at": "2026-10-02T10:00:00Z",
                    },
                ]

        items = agents_tui.all_agent_items(Cache())  # type: ignore[arg-type]

        self.assertEqual(
            [item.agent["agent_id"] for item in items], ["newer", "older"]
        )

    def test_preferred_agent_order_overrides_creation_order_within_host(self) -> None:
        class Cache:
            def hosts(self) -> list[dict[str, str]]:
                return [{"host_id": "local", "name": "This machine"}]

            def agents(self, _host_id: str) -> list[dict[str, str]]:
                return [
                    {"agent_id": "newer", "created_at": "2026-10-02"},
                    {"agent_id": "older", "created_at": "2026-10-01"},
                ]

        items = agents_tui.all_agent_items(  # type: ignore[arg-type]
            Cache(), preferred_order=[("local", "older"), ("local", "newer")]
        )

        self.assertEqual(
            [item.agent["agent_id"] for item in items], ["older", "newer"]
        )

    def test_agent_order_can_move_and_round_trip_through_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = agents_tui.Path(temporary) / "agent-order.json"
            items = [
                agents_tui.AgentItem("local", "Local", {"agent_id": "one"}),
                agents_tui.AgentItem("local", "Local", {"agent_id": "two"}),
                agents_tui.AgentItem("local", "Local", {"agent_id": "three"}),
            ]

            moved = agents_tui.move_agent_item(items, items, items[1], -1)
            order = agents_tui.save_agent_order(items, [], path)

            self.assertTrue(moved)
            self.assertEqual(
                [item.agent["agent_id"] for item in items],
                ["two", "one", "three"],
            )
            self.assertEqual(agents_tui.load_agent_order(path), order)

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
            patch.object(
                agents_tui.relay_transport,
                "deliver_pending",
                return_value=self.DELIVERY,
            ),
        ):
            result = agents_tui.sync_peer(object(), config, "desktop")  # type: ignore[arg-type]

        self.assertEqual(result, (3, "Desktop", self.DELIVERY))
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
            patch.object(
                agents_tui.relay_transport,
                "deliver_pending",
                return_value=self.DELIVERY,
            ),
        ):
            host = agents_tui.sync_agent(
                object(), config, "desktop", "selected"  # type: ignore[arg-type]
            )

        self.assertEqual(host, ("Desktop", self.DELIVERY))
        fetch.assert_called_once_with(
            config["peers"]["desktop"], "selected"
        )
        self.assertEqual(
            apply.call_args.kwargs["base_url"],
            "https://desktop.example:8780",
        )

    def test_sync_agent_uses_incremental_cursor_when_available(self) -> None:
        class Cache:
            def hosts(self) -> list[dict[str, str]]:
                return [
                    {
                        "host_id": "host-1",
                        "base_url": "https://desktop.example:8780",
                    }
                ]

            def agents(self, _host_id: str) -> list[dict[str, str]]:
                return [
                    {"agent_id": "selected", "event_cursor": "cursor-1"}
                ]

        config = {
            "peers": {
                "desktop": {
                    "url": "https://desktop.example:8780",
                    "token": "secret",
                }
            }
        }
        payload = {
            "protocol": 1,
            "host": {"id": "host-1", "name": "Desktop"},
            "agent_id": "selected",
            "events": [],
            "next_cursor": "cursor-2",
            "has_more": False,
        }

        with (
            patch.object(
                agents_tui.relay_transport,
                "fetch_agent_events",
                return_value=payload,
            ) as fetch_events,
            patch.object(
                agents_tui.relay_transport, "apply_agent_events"
            ) as apply_events,
            patch.object(
                agents_tui.relay_transport, "fetch_agent"
            ) as fetch_agent,
            patch.object(
                agents_tui.relay_transport,
                "deliver_pending",
                return_value=self.DELIVERY,
            ),
        ):
            host = agents_tui.sync_agent(
                Cache(), config, "desktop", "selected"  # type: ignore[arg-type]
            )

        self.assertEqual(host, ("Desktop", self.DELIVERY))
        fetch_events.assert_called_once_with(
            config["peers"]["desktop"], "selected", "cursor-1"
        )
        apply_events.assert_called_once_with(ANY, payload)
        fetch_agent.assert_not_called()

    def test_sync_agent_falls_back_to_snapshot_when_agent_route_is_missing(self) -> None:
        config = {
            "peers": {
                "desktop": {
                    "url": "https://desktop.example:8780",
                    "token": "secret",
                }
            }
        }
        not_found = agents_tui.HTTPError(
            "https://desktop.example:8780/v1/agents/selected",
            404,
            "Not Found",
            {},
            None,
        )
        snapshot = {
            "protocol": 1,
            "host": {"id": "host-1", "name": "Desktop"},
            "cursor": "now",
            "agents": [
                {"agent_id": "other"},
                {"agent_id": "selected", "records": []},
            ],
        }

        with (
            patch.object(
                agents_tui.relay_transport,
                "fetch_agent",
                side_effect=not_found,
            ),
            patch.object(
                agents_tui.relay_transport,
                "fetch_snapshot",
                return_value=snapshot,
            ) as fetch_snapshot,
            patch.object(
                agents_tui.relay_transport,
                "apply_agent_snapshot",
            ) as apply,
            patch.object(
                agents_tui.relay_transport,
                "deliver_pending",
                return_value=self.DELIVERY,
            ),
        ):
            host = agents_tui.sync_agent(
                object(), config, "desktop", "selected"  # type: ignore[arg-type]
            )

        self.assertEqual(host, ("Desktop", self.DELIVERY))
        fetch_snapshot.assert_called_once_with(config["peers"]["desktop"])
        self.assertEqual(apply.call_args.args[1]["agent"]["agent_id"], "selected")

    def test_sync_agent_keeps_cached_copy_when_agent_is_no_longer_active(self) -> None:
        peer = {"url": "https://desktop.example:8780", "token": "secret"}
        config = {"peers": {"desktop": peer}}
        not_found = agents_tui.HTTPError(
            "https://desktop.example:8780/v1/agents/selected",
            404,
            "Not Found",
            {},
            None,
        )
        snapshot = {
            "protocol": 1,
            "host": {"id": "host-1", "name": "Desktop"},
            "agents": [{"agent_id": "other"}],
        }

        with (
            patch.object(
                agents_tui.relay_transport,
                "fetch_agent",
                side_effect=not_found,
            ),
            patch.object(
                agents_tui.relay_transport,
                "fetch_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                agents_tui.relay_transport,
                "apply_agent_snapshot",
            ) as apply,
            patch.object(
                agents_tui.relay_transport,
                "deliver_pending",
                return_value=self.DELIVERY,
            ),
        ):
            host = agents_tui.sync_agent(
                object(), config, "desktop", "selected"  # type: ignore[arg-type]
            )

        self.assertEqual(host, (None, self.DELIVERY))
        apply.assert_not_called()

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
            typeset_url = agents_tui.open_agent_viewer(item, typeset=True)

        self.assertEqual(
            url,
            "http://127.0.0.1:8765/v/relay/remote-host/agent-1?tail=24",
        )
        self.assertEqual(opened.call_args_list[0].args[0], url)
        self.assertEqual(
            typeset_url,
            "http://127.0.0.1:8765/t/relay/remote-host/agent-1?tail=8",
        )
        self.assertEqual(opened.call_args_list[1].args[0], typeset_url)

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

    def test_tab_switches_panes_and_restores_sidebar(self) -> None:
        self.assertEqual(
            agents_tui.change_pane("agents", False, 9),
            ("conversation", False),
        )
        self.assertEqual(
            agents_tui.change_pane("conversation", True, 9),
            ("agents", False),
        )

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
        with patch.object(agents_tui.curses, "BUTTON5_PRESSED", 1 << 21, create=True):
            self.assertEqual(agents_tui.mouse_scroll_delta(1 << 21), -3)
        self.assertEqual(agents_tui.mouse_scroll_delta(0), 0)

    def test_mouse_wheel_supports_curses_without_button_five(self) -> None:
        with patch.object(agents_tui, "curses") as terminal:
            terminal.BUTTON4_PRESSED = 1 << 16
            del terminal.BUTTON5_PRESSED
            self.assertEqual(agents_tui.mouse_scroll_delta(1 << 16), 3)
            self.assertEqual(agents_tui.mouse_scroll_delta(0), 0)

    def test_semantic_spans_highlight_code_strings_numbers_and_literals(self) -> None:
        spans = agents_tui.semantic_spans(
            'Use `limit=12`, name="soft blue", enabled=true, ratio=1.5.'
        )

        self.assertIn(("`limit=12`", "code"), spans)
        self.assertIn(('"soft blue"', "string"), spans)
        self.assertIn(("true", "literal"), spans)
        self.assertIn(("1.5", "number"), spans)

    def test_markdown_bold_hides_only_matched_markers_outside_code(self) -> None:
        text = agents_tui.markdown_text("Plain **bold** and **two words**; `**literal**` / ``**also literal**``.")
        self.assertEqual(text, "Plain bold and two words; `**literal**` / ``**also literal**``.")
        self.assertEqual([text[left:right] for left, right in text.bold_ranges], ["bold", "two words"])
        for literal in ("unmatched **bold", "** leading**", "**trailing **", "***triple***", r"\**escaped**"):
            with self.subTest(literal=literal):
                parsed = agents_tui.markdown_text(literal)
                self.assertEqual(parsed, literal)
                self.assertEqual(parsed.bold_ranges, ())

    def test_bold_survives_wrapping_and_assistant_bullet_without_wrapping_markers(self) -> None:
        lines = agents_tui.transcript_lines([
            {"role": "assistant", "text": "**abcdefghijklmno** plain"},
        ], 14)
        rendered = [line for line, style in lines if style == "assistant"]
        self.assertTrue(rendered[0].startswith("• "))
        self.assertFalse(any("**" in line for line in rendered))
        bold = "".join(line[left:right] for line in rendered for left, right in line.bold_ranges)
        self.assertEqual(bold, "abcdefghijklmno")
        self.assertTrue(all(len(line) <= 14 for line in rendered))

    def test_bold_inside_inline_code_and_fences_remains_literal(self) -> None:
        lines = agents_tui.transcript_lines([
            {"role": "assistant", "text": "`**inline**`\n\n```bash\necho '**literal**'\n```"},
        ], 80)
        self.assertTrue(any("`**inline**`" in line for line, style in lines if style == "assistant"))
        self.assertTrue(any("**literal**" in line for line, style in lines if style == "code:bash"))
        self.assertFalse(any(getattr(line, "bold_ranges", ()) for line, _ in lines))

    def test_bold_tables_keep_alignment_and_wrapped_emphasis(self) -> None:
        table = agents_tui.format_terminal_table(["Name", "Value"], ["---", "---"],
                                                [["**long bold cell**", "plain"]], 28)
        rows = [line for line, style in table if style == "table_row"]
        self.assertTrue(all("**" not in line for line in rows))
        bold = "".join(line[left:right] for line in rows for left, right in line.bold_ranges)
        self.assertEqual(bold.replace(" ", ""), "longboldcell")
        self.assertEqual(len({len(line) for line, _ in table}), 1)

    def test_bold_drawing_preserves_token_colors_and_plain_text(self) -> None:
        with patch.object(agents_tui, "add_text") as draw:
            agents_tui.add_semantic_text(Mock(), 2, 3, 'Plain **bold 42 "yes"** end', 80, 8,
                                         {"syntax_number": 16, "syntax_string": 32})
        calls = draw.call_args_list
        self.assertEqual("".join(call.args[3] for call in calls), 'Plain bold 42 "yes" end')
        self.assertTrue(any(call.args[3] == "bold " and call.args[5] == (8 | agents_tui.curses.A_BOLD)
                            for call in calls))
        self.assertTrue(any(call.args[3] == "42" and call.args[5] == (16 | agents_tui.curses.A_BOLD)
                            for call in calls))
        self.assertTrue(any(call.args[3] == '"yes"' and call.args[5] == (32 | agents_tui.curses.A_BOLD)
                            for call in calls))
        self.assertEqual(calls[-1].args[5], 8)

    def test_bold_link_labels_preserve_emphasis_without_brackets(self) -> None:
        with patch.object(agents_tui, "add_text") as draw:
            agents_tui.add_semantic_text(Mock(), 0, 0, "See [**report**] now", 80, 0, {"syntax_link": 16})
        self.assertEqual("".join(call.args[3] for call in draw.call_args_list), "See report now")
        self.assertTrue(any(call.args[3] == "report" and call.args[5] == (16 | agents_tui.curses.A_BOLD)
                            for call in draw.call_args_list))

    def test_bold_drawing_on_mac_uses_safe_cell_attributes(self) -> None:
        window = Mock()
        with patch.object(agents_tui.sys, "platform", "darwin"):
            agents_tui.add_semantic_text(window, 0, 0, "**bold**", 20, 0, {})
        window.addnstr.assert_called_once_with(0, 0, b"bold", 4)
        window.chgat.assert_called_once_with(0, 0, 4, agents_tui.curses.A_BOLD)

    def test_fenced_code_blocks_hide_fences_and_preserve_indentation(self) -> None:
        records = [
            {
                "role": "assistant",
                "text": (
                    "Run this:\n"
                    "```bash\n"
                    "if [[ -f report.txt ]]; then\n"
                    "    printf '%s\\n' \"ready\"\n"
                    "fi\n"
                    "```\n"
                    "Done."
                ),
            }
        ]

        lines = agents_tui.transcript_lines(records, 80)

        self.assertIn(("  bash", "code_label"), lines)
        self.assertIn(
            ("  │     printf '%s\\n' \"ready\"", "code:bash"), lines
        )
        self.assertNotIn("```", "\n".join(line for line, _ in lines))

    def test_pygments_highlights_known_language_and_falls_back_to_text(self) -> None:
        bash = agents_tui.highlighted_code_spans(
            'if [[ "$count" -gt 2 ]]; then # enough', "bash"
        )
        unknown = agents_tui.highlighted_code_spans("value = 12", "made-up-lang")

        self.assertTrue(any(style == "code_keyword" for _, style in bash))
        self.assertTrue(any(style == "code_comment" for _, style in bash))
        self.assertEqual(unknown, [("value = 12", "code")])

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

        self.assertTrue(any(line.startswith("• ┌") for line, _ in lines))
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

            def chgat(self, *args: object) -> None:
                self.calls.append(("style", *args))

            def refresh(self) -> None:
                self.calls.append(("refresh",))

        window = Window()

        with patch.object(agents_tui.sys, "platform", "linux"):
            agents_tui.show_status(window, "Refreshing…", 7)

        self.assertIn(("text", 23, 1, "Refreshing…", 78, 7), window.calls)
        self.assertEqual(window.calls[-1], ("refresh",))

    def test_ctrl_d_requests_console_exit_but_ctrl_c_does_not(self) -> None:
        self.assertTrue(agents_tui.requests_console_exit(4))
        self.assertFalse(agents_tui.requests_console_exit(3))
        self.assertFalse(agents_tui.requests_console_exit(ord("j")))

    def test_bare_escape_requests_stop_but_unknown_alt_sequence_does_not(self) -> None:
        window = Mock()
        window.get_wch.side_effect = agents_tui.curses.error
        self.assertEqual(agents_tui.escape_action(window), ("stop", ""))
        window.get_wch.side_effect = None
        window.get_wch.return_value = "z"
        self.assertEqual(agents_tui.escape_action(window), ("cancel", ""))

    def test_escape_does_not_request_console_exit(self) -> None:
        self.assertFalse(agents_tui.requests_console_exit(27))

    def test_q_does_not_request_console_exit(self) -> None:
        self.assertFalse(agents_tui.requests_console_exit(ord("q")))

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

        self.assertEqual(lines[0][1], "user")
        self.assertIn(("• An answer", "assistant"), lines)
        self.assertFalse(
            any(style in {"user_label", "assistant_label"} for _, style in lines)
        )
        self.assertIn(("", "user_gap"), lines)
        self.assertIn(("", "assistant_gap"), lines)
        self.assertNotIn("hidden", "\n".join(line for line, _ in lines))
        self.assertNotIn("environment_context", "\n".join(line for line, _ in lines))
        self.assertNotIn("/private/work", "\n".join(line for line, _ in lines))


if __name__ == "__main__":
    unittest.main()
