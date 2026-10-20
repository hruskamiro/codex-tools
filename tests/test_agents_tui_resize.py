"""Exercise the actual macOS curses library in a disposable terminal."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "darwin", "macOS native curses regression")
class MacosResizeTests(unittest.TestCase):
    def test_meta_arrows_are_recognized_after_native_curses_decoding(self) -> None:
        import fcntl
        import pty
        import select
        import struct
        import termios

        child_code = """
import curses
from codex_tools.agents_tui import escape_action
def run(window):
    curses.raw()
    window.keypad(True)
    window.timeout(3000)
    window.refresh()
    print('READY', flush=True)
    expected = [('select_agent_up', ''), ('select_agent_down', ''),
                ('select_agent_up', ''), ('select_agent_down', ''),
                ('agent', '1'), ('agent', '9')]
    for action in expected:
        key = window.get_wch()
        assert key == chr(27), repr(key)
        actual = escape_action(window)
        assert actual == action, (actual, action)
curses.wrapper(run)
print('ARROWS_OK', flush=True)
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

        def drain(duration: float) -> None:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.02)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output.extend(data)

        try:
            deadline = time.monotonic() + 10
            while b"READY" not in output and process.poll() is None:
                drain(0.05)
                if time.monotonic() > deadline:
                    self.fail("curses child did not initialize")
            for sequence in (b"\x1b\x1bOA", b"\x1b\x1bOB", b"\x1b[1;9A",
                             b"\x1b[1;9B", b"\x1b1", b"\x1b9"):
                if process.poll() is not None:
                    break
                os.write(master, sequence)
                drain(0.1)
            drain(0.5)
            self.assertEqual(process.wait(timeout=5), 0, output.decode(errors="replace"))
            self.assertIn(b"ARROWS_OK", output)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)

    def test_styled_text_survives_repeated_terminal_resizing(self) -> None:
        import fcntl
        import pty
        import select
        import struct
        import termios

        child_code = """
import curses
from codex_tools.agents_tui import (
    add_text, draw_composer, ComposerBuffer, colors, draw_info_spans, metadata_spans,
)
def run(window):
    curses.raw()
    curses.use_default_colors()
    curses.init_pair(1, 7, 0)
    info_palette = colors()
    window.timeout(20)
    text = 'Resize regression draft Café 界 ' * 12
    draft = ComposerBuffer(text, len(text))
    while True:
        window.erase()
        height, width = window.getmaxyx()
        for row in range(height):
            add_text(window, row, 0, 'Resize regression › Café 界', width - 1,
                     curses.color_pair(1) | curses.A_BOLD)
        palette = {key: curses.color_pair(1) for key in
                   ('composer', 'composer_accent', 'composer_muted')}
        draw_composer(window, palette, draft, 0, width, focused=True)
        draw_info_spans(window, 0, 0, metadata_spans(
            'gpt-test · reasoning: medium', 'Sync live · ●', 'info_sync_ok',
        ), width - 1, info_palette)
        assert draft.text == text
        window.refresh()
        if window.getch() == ord('q'):
            return
curses.wrapper(run)
"""
        master, slave = pty.openpty()
        output = bytearray()

        def resize(rows: int, columns: int) -> None:
            fcntl.ioctl(master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, columns, 0, 0))

        def drain(duration: float) -> None:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.01)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break  # PTY closed after child exit.
                    if not data:
                        break
                    output.extend(data)
                if process.poll() is not None:
                    break

        resize(40, 140)
        process = subprocess.Popen(
            [sys.executable, "-X", "faulthandler", "-c", child_code],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, TERM="xterm-256color", LC_ALL="en_US.UTF-8"),
        )
        os.close(slave)
        try:
            deadline = time.monotonic() + 10
            while b"Resize regression" not in output and process.poll() is None:
                drain(0.05)
                if time.monotonic() > deadline:
                    self.fail("curses child did not initialize")
            self.assertIsNone(process.poll(), output[-2000:].decode(errors="replace"))
            for index in range(60):
                resize(*[(40, 140), (24, 80), (8, 45), (50, 160)][index % 4])
                process.send_signal(signal.SIGWINCH)
                drain(0.04)
                if process.poll() is not None:
                    break
            if process.poll() is None:
                os.write(master, b"q")
                drain(1)
            self.assertEqual(process.wait(timeout=5), 0,
                             output[-2000:].decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)
