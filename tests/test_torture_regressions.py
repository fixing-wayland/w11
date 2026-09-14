"""Regression tests from the input-torture pass: hex keysyms, X buttons
10-12, zero-delay burst pacing, per-user daemon log, spawn-race lock, and
the key command's failure-count exit code."""

import contextlib
import io
import os
import signal
import socket
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common.errors import CmdError
from support import RecorderDev
from wdotool import cli, daemon, input_cmds
from hacks.input import keymap, uinput
from wdotool.ctx import Context

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")


def make_daemon():
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = (1920, 1080)
    return d


class TestHexKeysymTokens(unittest.TestCase):
    """XStringToKeysym accepts '0x<hex>' as a raw keysym number."""

    def test_hex_lowercase_letter(self):
        self.assertEqual(keymap.resolve_token("0x61"), (30, False))  # 'a'

    def test_hex_uppercase_letter_is_shifted(self):
        self.assertEqual(keymap.resolve_token("0x41"), (30, True))  # 'A'

    def test_hex_dollar(self):
        self.assertEqual(keymap.resolve_token("0x24"), (5, True))

    def test_hex_function_keysym(self):
        self.assertEqual(keymap.resolve_token("0xff0d"), (28, False))  # Return

    def test_hex_capital_x(self):
        self.assertEqual(keymap.resolve_token("0X61"), (30, False))

    def test_hex_unreachable_warns(self):
        hit = keymap.resolve_token("0x2")
        self.assertIsInstance(hit, str)
        self.assertIn("not reachable", hit)

    def test_hex_in_sequence(self):
        keys, warns = keymap.parse_keyseq("ctrl+0x74")  # 't'
        self.assertEqual(warns, [])
        self.assertEqual(keys, [(29, False), (20, False)])

    def test_plain_digits_still_x_keycode(self):
        self.assertEqual(keymap.resolve_token("38"), (30, False))  # X keycode


class TestButtons10To12(unittest.TestCase):
    def test_forward_back_task(self):
        d = make_daemon()
        for btn, code in [(10, uinput.BTN_FORWARD), (11, uinput.BTN_BACK),
                          (12, uinput.BTN_TASK)]:
            d.op_button(btn, True)
            d.op_button(btn, False)
            self.assertEqual(d.mouse.events[-2:],
                             [("KEY", code, 1), ("KEY", code, 0)])

    def test_button_13_invalid(self):
        d = make_daemon()
        with self.assertRaises(RuntimeError):
            d.op_button(13, True)

    def test_device_declares_new_buttons(self):
        # rel_mouse() must declare the keybits or the events would be dropped
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"
        try:
            dev = uinput.rel_mouse()
            dev.close()
        finally:
            os.environ.pop("WDOTOOL_UINPUT_PATH")
            os.environ.pop("WDOTOOL_FAKE_UINPUT")


class TestRatePacing(unittest.TestCase):
    """Zero-delay typing must be rate-capped or the compositor's evdev buffer
    overflows and the kernel drops keystrokes (SYN_DROPPED)."""

    def test_zero_delay_is_rate_capped(self):
        d = make_daemon()
        n = 30
        t0 = time.monotonic()
        for _ in range(n):
            d._key_gap(0)
        dt = time.monotonic() - t0
        # n gaps at the floor should take about n * _MIN_GAP
        self.assertGreaterEqual(dt, (n - 1) * d._MIN_GAP * 0.8)

    def test_explicit_delay_above_floor_is_honored(self):
        d = make_daemon()
        d._key_gap(0.02)  # 20ms, primes the deadline
        t0 = time.monotonic()
        d._key_gap(0.02)
        dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, 0.02 * 0.8)

    def test_deadline_does_not_accumulate_after_idle(self):
        d = make_daemon()
        d._key_gap(0)
        time.sleep(0.05)  # daemon idle; deadline is now in the past
        t0 = time.monotonic()
        d._key_gap(0)
        dt = time.monotonic() - t0
        self.assertLess(dt, d._MIN_GAP * 2)

    def test_type_zero_delay_paces(self):
        d = make_daemon()
        n = 40
        t0 = time.monotonic()
        d.op_type("x" * n, 0, False)
        dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, (n - 1) * d._MIN_GAP * 0.7)


class TestLogPath(unittest.TestCase):
    def test_per_user_log_path(self):
        if os.geteuid() == 0:
            self.assertEqual(daemon.LOG_PATH, "/tmp/wdotool-daemon.log")
        else:
            self.assertEqual(daemon.LOG_PATH,
                             f"/tmp/wdotool-daemon-{os.geteuid()}.log")


class TestKeyFailureExitCode(unittest.TestCase):
    """xdotool sums the per-sequence failures into its exit status, and a
    sequence is converted once per press/release pass: `key` fails twice per
    bad sequence, `keydown`/`keyup` once (B12, xdo.c
    xdo_send_keysequence_window)."""

    class _BadSeqDaemon:
        def key(self, spec, direction, delay_ms, clearmods):
            if "." in spec:
                raise CmdError(f"Error: Invalid key sequence '{spec}'")

    def _ctx(self):
        ctx = Context()
        ctx._daemon = self._BadSeqDaemon()
        return ctx

    def test_two_bad_sequences_exit_4(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_key(self._ctx(), ["bad.a", "bad.b"])
        self.assertEqual(cm.exception.code, 4)  # 2 sequences x 2 passes

    def test_one_bad_sequence_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_key(self._ctx(), ["only.bad"])
        self.assertEqual(cm.exception.code, 2)

    def test_keydown_counts_one_pass(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_keydown(self._ctx(), ["only.bad"])
        self.assertEqual(cm.exception.code, 1)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_keyup(self._ctx(), ["only.bad"])
        self.assertEqual(cm.exception.code, 1)

    def test_repeat_multiplies_failures(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_key(self._ctx(), ["--repeat", "3", "bad.a"])
        self.assertEqual(cm.exception.code, 6)  # 3 repeats x 2 passes


class TestDaemonStartupLock(unittest.TestCase):
    """A second daemon_main must bow out without touching the winner's
    socket, even under SIGKILL-stale leftovers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wdotool-lock-test-")
        self.env_backup = {
            k: os.environ.get(k)
            for k in ("XDG_RUNTIME_DIR", "WDOTOOL_UINPUT_PATH",
                      "WDOTOOL_FAKE_UINPUT", "WAYLAND_DISPLAY", "SWAYSOCK",
                      "I3SOCK")
        }
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"
        for k in ("WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _wait_listening(self, path, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(path)
                s.close()
                return True
            except OSError:
                s.close()
                time.sleep(0.02)
        return False

    def test_second_daemon_bows_out(self):
        path = daemon.socket_path()
        pid = os.fork()
        if pid == 0:  # child: the winning daemon
            try:
                daemon.daemon_main()
            finally:
                os._exit(0)
        try:
            self.assertTrue(self._wait_listening(path))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), \
                    contextlib.redirect_stdout(io.StringIO()):
                ret = daemon.daemon_main()
            self.assertEqual(ret, 0)
            self.assertIn("already running", stderr.getvalue())
            # the winner's socket must still be intact
            self.assertTrue(self._wait_listening(path, timeout=1.0))
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
