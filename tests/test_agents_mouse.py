from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from codex_tools import agents_tui


class MouseCompatibilityTests(unittest.TestCase):
    def action(self, sequence):
        window = Mock()
        window.get_wch.side_effect = list(sequence) + [agents_tui.curses.error()]
        return agents_tui.escape_action(window)

    def test_raw_wheels_both_directions_with_modifiers_and_large_coordinates(self):
        for button in (64, 65, 68, 69, 80, 81, 92, 93):
            with self.subTest(button=button):
                self.assertEqual(self.action(f"[<{button};12345;99999M"),
                                 ("scroll_up" if button % 2 == 0 else "scroll_down", ""))

    def test_clicks_motion_horizontal_release_and_truncated_events_are_not_scroll_or_stop(self):
        for sequence in ("[<0;10;20M", "[<32;10;20M", "[<66;10;20M", "[<67;10;20M",
                         "[<65;10;20m", "[<65;10;", "[<96;10;20M"):
            with self.subTest(sequence=sequence):
                self.assertEqual(self.action(sequence), ("cancel", ""))

    def test_only_legacy_curses_needs_explicit_sgr_reporting(self):
        with patch.object(agents_tui, "curses") as terminal:
            del terminal.BUTTON5_PRESSED
            self.assertEqual(agents_tui.mouse_protocol_sequence(True), "\x1b[?1006h")
            self.assertEqual(agents_tui.mouse_protocol_sequence(False), "\x1b[?1006l")
            terminal.BUTTON5_PRESSED = 1 << 21
            self.assertEqual(agents_tui.mouse_protocol_sequence(True), "")
            self.assertEqual(agents_tui.mouse_protocol_sequence(False), "")

    @unittest.skipUnless(sys.platform == "darwin", "macOS native mouse regression")
    def test_native_macos_terminal_scrolls_up_and_back_down(self):
        import fcntl
        import pty
        import select
        import struct
        import termios

        child_code = """
import curses, sys
from codex_tools.agents_tui import escape_action, mouse_protocol_sequence, mouse_scroll_delta
def run(window):
    curses.raw()
    window.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    sys.stdout.write(mouse_protocol_sequence(True))
    window.refresh()
    print('MOUSE_READY', flush=True)
    scroll = 30
    for expected in ('scroll_up', 'scroll_down'):
        window.timeout(2000)
        key = window.get_wch()
        if key == chr(27):
            action, _ = escape_action(window)
        elif key == curses.KEY_MOUSE:
            delta = mouse_scroll_delta(curses.getmouse()[4])
            action = 'scroll_up' if delta > 0 else 'scroll_down' if delta < 0 else 'cancel'
        else:
            raise AssertionError(repr(key))
        assert action == expected, (action, expected)
        scroll += 3 if action == 'scroll_up' else -3
    assert scroll == 30, scroll
try:
    curses.wrapper(run)
finally:
    sys.stdout.write(mouse_protocol_sequence(False))
print('MOUSE_OK', flush=True)
"""
        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-X", "faulthandler", "-c", child_code],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, TERM="xterm-256color", LC_ALL="en_US.UTF-8"),
        )
        os.close(slave)
        output = bytearray()

        def drain(duration):
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if select.select([master], [], [], .02)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output.extend(data)

        try:
            deadline = time.monotonic() + 5
            while b"MOUSE_READY" not in output and process.poll() is None:
                drain(.05)
                if time.monotonic() > deadline:
                    self.fail("curses mouse test did not initialize")
            self.assertIn(b"MOUSE_READY", output, output.decode(errors="replace"))
            for sequence in (b"\x1b[<64;20;10M", b"\x1b[<65;20;10M"):
                os.write(master, sequence)
                drain(.1)
            drain(.5)
            self.assertEqual(process.wait(timeout=5), 0, output.decode(errors="replace"))
            self.assertIn(b"MOUSE_OK", output)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)
