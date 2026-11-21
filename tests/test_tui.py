"""Exercise the real fzf in a private terminal; never run package transactions."""

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


@unittest.skipUnless(shutil.which("fzf"), "fzf is required")
class TerminalTests(unittest.TestCase):
    def picker(self, keys, *, multi=False, empty=False, description="First action", steps=None, rows=40,
               term="xterm-256color"):
        with tempfile.TemporaryDirectory(prefix="package-menu-tui-") as directory:
            root = Path(directory)
            result = root / "result.json"
            sys.stdout.flush()
            sys.stderr.flush()
            pid, terminal = pty.fork()
            if pid == 0:
                try:
                    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, 140, 0, 0))
                    for name in ("FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE", "XDG_RUNTIME_DIR"):
                        os.environ.pop(name, None)
                    os.environ.update(TERM=term, SHELL="/bin/sh", XDG_CONFIG_HOME=str(root))
                    app.CONFIG_FILE = root / "package-menu/config.json"
                    app.configure_helper("pacman")
                    app.TRAUR_ENABLED = app.WRAPPER_ENABLED = False
                    records = [] if empty else app.menu_records([
                        ("first", "First", description), ("second", "Second", "Second action"),
                    ])
                    selected = app.picker(
                        records, "Terminal test", multi=multi, searchable=multi,
                        global_shortcuts=True,
                    )
                    result.write_text(json.dumps(selected))
                except BaseException as error:
                    result.write_text(json.dumps({"error": repr(error)}))
                finally:
                    os._exit(0)

            output = bytearray()
            pending = list(steps) if steps is not None else [(b"Show or hide panel", keys)]
            start = 0
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([terminal], [], [], max(0, deadline - time.monotonic()))
                    if not readable:
                        break
                    try:
                        chunk = os.read(terminal, 65536)
                    except OSError:
                        break  # Linux PTYs report EIO after the child exits.
                    if not chunk:
                        break
                    output.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        os.write(terminal, b"\x1b[1;1R")
                    if pending and pending[0][0] in output[start:]:
                        _, keypress = pending.pop(0)
                        start = len(output)
                        os.write(terminal, keypress)
                self.assertFalse(pending, output.decode(errors="replace"))
                self.assertTrue(result.exists(), output.decode(errors="replace"))
                selected = json.loads(result.read_text())
                self.assertNotIsInstance(selected, dict, selected)
                return selected, output.decode(errors="replace")
            finally:
                os.close(terminal)
                # Terminate the private session if a failed assertion left fzf open.
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

    def test_ctrl_a_exits_main_selector_as_update_shortcut(self):
        selected, output = self.picker(b"\x01")
        self.assertEqual(selected[1], "ctrl-a")
        self.assertIn("Update system", output)
        self.assertIn("Ctrl+O", output)

    def test_ctrl_o_exits_main_selector_as_settings_shortcut(self):
        selected, _ = self.picker(b"\x0f")
        self.assertEqual(selected[1], "ctrl-o")

    def test_enter_keeps_the_original_menu_action(self):
        selected, _ = self.picker(b"\r")
        self.assertEqual(selected[1], "")
        self.assertEqual(selected[2][0]["id"], "first")

    def test_ctrl_a_in_package_list_selects_all_and_does_not_exit(self):
        selected, _ = self.picker(b"\x01\r", multi=True)
        self.assertEqual(selected[1], "")
        self.assertEqual([record["id"] for record in selected[2]], ["first", "second"])

    def test_empty_results_keep_panel_and_settings_shortcut(self):
        selected, output = self.picker(b"\x0f", multi=True, empty=True)
        self.assertIsNotNone(selected)
        self.assertEqual(selected[1], "ctrl-o")
        self.assertEqual(selected[2], [])
        self.assertIn("No matching packages.", output)

    def test_resize_and_preview_modes_do_not_consume_the_selection(self):
        selected, _ = self.picker(b"\x1b[1;2D\x1bd\x1bb\x01\r", multi=True)
        self.assertEqual([record["id"] for record in selected[2]], ["first", "second"])

    def test_shift_arrows_really_scroll_down_and_back_up_in_kitty(self):
        description = "SCROLL_START " + "Panel text " * 220 + "SCROLL_END"
        selected, _ = self.picker(
            None, rows=20, term="xterm-kitty", description=description,
            steps=[
                (b"Navigation", b"\x1b[1;2B" * 50),
                (b"SCROLL_END", b"\x1b[1;2A" * 50),
                (b"Navigation", b"\x0f"),
            ],
        )
        self.assertEqual(selected[1], "ctrl-o")


if __name__ == "__main__":
    unittest.main()
