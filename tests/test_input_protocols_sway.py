"""zwp_virtual_keyboard_v1 and zwlr_virtual_pointer_v1 against a real wlroots.

tests/test_vkbd.py and tests/test_vptr.py prove the *bytes*: a fake compositor
on a real socket reads back every request and every field. What they cannot
prove is that wlroots accepts them -- a request with a plausible body and the
wrong opcode, an axis event sent without the frame that delivers it, a keymap
fd wlroots refuses to mmap, all look identical to a fake that was written
from the same reading of the XML as the client. sway raises a protocol error
and hangs up instead, so here every group of requests ends in the round trip
(`flush()`) that turns that into an exception.

Measured against sway 1.11 (the resolute-sway golden's compositor), headless,
booted per class on its own XDG_RUNTIME_DIR by `support.HeadlessSway`.

The last class is the session these protocols exist for: no /dev/uinput, no
root, nothing to fall back to. It spawns a real daemon with
WDOTOOL_UINPUT_PATH pointed at a path that cannot be opened and drives it
over its socket, which is what `wdotool --vkbd on` does on a stock sway.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# ...and the tests directory itself, for the bare `import support`
# below: running this file by path puts it on sys.path for free,
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support
from w11common.errors import CmdError
from wdotool import daemon
from hacks.input import vkbd, vptr


def _inputs(rig):
    """`swaymsg -t get_inputs`, as a list of dicts."""
    out = subprocess.run(["swaymsg", "-s", rig.sock, "-t", "get_inputs"],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise AssertionError("swaymsg get_inputs failed: %s" % out.stderr)
    return json.loads(out.stdout)


def _of_type(rig, kind):
    return [d for d in _inputs(rig) if d.get("type") == kind]


class SwayRig(unittest.TestCase):
    """One headless sway for the class, with the environment pointed at it.

    WDOTOOL_NO_KEYSTATE: the daemon must not read the *runner's* real
    keyboards to decide what is held -- there are none in this rig, and on a
    developer's box reading theirs would make the answers depend on what they
    were holding."""

    extra_env: dict = {}

    @classmethod
    def setUpClass(cls):
        if not shutil.which("sway"):
            raise unittest.SkipTest("sway not on PATH")
        if not shutil.which("swaymsg"):
            raise unittest.SkipTest("swaymsg not on PATH")
        cls.rig = support.HeadlessSway(prefix="wdotool-proto-", need_display=False)
        # Cleanups run last-in-first-out, so this pair is in the order it
        # reads: stop whatever daemon a test spawned, and only then let the
        # rig remove the directory that daemon listens in. A daemon whose
        # socket directory is gone is unreachable, which used to mean
        # immortal (tests/test_daemon_lifetime.py). Registered before the
        # first spawn, which is in the tests below.
        cls.addClassCleanup(cls.rig.stop)
        cls.addClassCleanup(support.stop_daemons_under, cls.rig.rtdir)
        kw = dict(XDG_RUNTIME_DIR=cls.rig.rtdir,
                  WAYLAND_DISPLAY=cls.rig.wayland_display(),
                  SWAYSOCK=cls.rig.sock,
                  WDOTOOL_NO_KEYSTATE="1")
        kw.update(cls.extra_env)
        cls._env = support.env(**kw)
        cls._env.__enter__()
        cls.addClassCleanup(cls._env.__exit__, None, None, None)


class TheVirtualKeyboardOnRealWlroots(SwayRig):
    def keyboard(self):
        vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(vk.close)
        return vk

    def test_open_uploads_a_keymap_wlroots_accepts(self):
        """The keymap travels as an fd wlroots mmaps and hands to
        xkb_keymap_new_from_string; a blob it cannot compile is a protocol
        error on this round trip, and the fake cannot tell us that because it
        only measures the bytes' length."""
        vk = self.keyboard()
        vk.flush()

    def test_a_press_and_a_release_raise_nothing(self):
        vk = self.keyboard()
        vk.key(30, True)          # KEY_A
        vk.key(30, False)
        vk.flush()

    def test_a_modifier_carries_its_mask_and_clear_modifiers_is_accepted(self):
        """wlroots does not run a virtual keyboard's keys through xkb state,
        so the client sends `modifiers` itself -- a mask wlroots rejects, or
        a group index outside the uploaded keymap, is a protocol error."""
        vk = self.keyboard()
        vk.key(42, True)          # KEY_LEFTSHIFT: sends modifiers first
        vk.key(30, True)
        vk.key(30, False)
        vk.key(42, False)
        vk.clear_modifiers()
        vk.flush()

    def test_a_request_wlroots_rejects_really_does_surface_on_the_flush(self):
        """The same control on the keyboard half: an opcode
        zwp_virtual_keyboard_v1 does not have, reported where every `type`
        would report one."""
        vk = self.keyboard()
        vk.conn.send(vk.vk, 99, [])
        with self.assertRaises(vkbd.VkbdError):
            vk.flush()

    def test_sway_lists_it_while_it_lives_and_not_after(self):
        before = len(_of_type(self.rig, "keyboard"))
        vk = vkbd.VirtualKeyboard.open()
        try:
            vk.flush()
            self.assertEqual(len(_of_type(self.rig, "keyboard")), before + 1)
        finally:
            vk.close()
        self.assertEqual(len(_of_type(self.rig, "keyboard")), before)


class TheVirtualPointerOnRealWlroots(SwayRig):
    def pointer(self):
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        return vp

    def test_a_warp_a_move_a_click_and_a_detent_raise_nothing(self):
        """Every request shape the daemon's pointer ops emit, in one go: the
        absolute motion with its extent pair, a relative delta in the fixed
        24.8 encoding, a button with its evdev code, and one wheel detent as
        axis_source + axis_discrete. A wrong opcode or a short body is a
        protocol error on the flush."""
        vp = self.pointer()
        vp.warp(100, 100, 0, 0, 1280, 720)
        vp.move(5, 5)
        vp.button(0x110, True)         # BTN_LEFT
        vp.button(0x110, False)
        vp.wheel(5)
        vp.flush()

    def test_a_request_wlroots_rejects_really_does_surface_on_the_flush(self):
        """The control for every "raises nothing" in this file. If sway's
        protocol errors did not reach the client, those assertions would hold
        whatever bytes were sent and prove nothing at all. An opcode
        zwlr_virtual_pointer_v1 does not have is `invalid_method`: sway
        answers wl_display.error and hangs up, and `flush()` -- the round
        trip the daemon ends every injection with -- is where it arrives."""
        vp = self.pointer()
        vp._send(99, [])
        with self.assertRaises(vptr.VptrError):
            vp.flush()

    def test_sway_lists_it_while_it_lives_and_not_after(self):
        before = len(_of_type(self.rig, "pointer"))
        vp = vptr.VirtualPointer.open()
        try:
            vp.flush()
            self.assertEqual(len(_of_type(self.rig, "pointer")), before + 1)
        finally:
            vp.close()
        self.assertEqual(len(_of_type(self.rig, "pointer")), before)


class TheDaemonOnASessionWithNoUinput(SwayRig):
    """The whole reason both protocols are in the tree: a stock sway session
    where /dev/uinput cannot be opened, driven through a really spawned
    daemon rather than by calling the protocol classes directly."""

    extra_env = {"WDOTOOL_UINPUT_PATH": "/nonexistent/wdotool-uinput",
                 "WDOTOOL_FAKE_UINPUT": None}

    def client(self, fresh=False):
        """`fresh`: stop whatever daemon the previous test in this class left
        behind first. One daemon serves the whole runtime directory and
        outlives the command that spawned it, so a test about what a daemon
        knows *before* anything has moved the pointer cannot share one with
        a test that moves it."""
        if fresh:
            support.stop_daemons_under(self.rig.rtdir)
        c = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(c.close)
        return c

    def test_typing_falls_back_to_the_keyboard_protocol_and_says_so(self):
        c = self.client()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c.type_text("a", 0)
        said = err.getvalue()
        self.assertIn("zwp_virtual_keyboard_v1", said)
        self.assertIn("/dev/uinput", said)

    def test_moving_falls_back_to_the_pointer_protocol_and_says_so(self):
        c = self.client()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c.mousemove_abs(10, 10)
        self.assertIn("zwlr_virtual_pointer_v1", err.getvalue())

    def test_the_pointer_refuses_before_the_first_move_and_answers_after(self):
        """F3.3, fix 38. zwlr_virtual_pointer_v1 delivers no events at all
        and sway's IPC has no cursor position, so before the daemon has moved
        anything there is nothing to report and 0,0 is not an answer -- and
        the reason names the protocol rather than /dev/uinput, which is not
        what the user would have to fix. One move later the daemon knows
        exactly where it put the cursor."""
        c = self.client(fresh=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(CmdError) as caught:
                c.pointer()
            self.assertIn("zwlr_virtual_pointer_v1", str(caught.exception))
            self.assertNotIn("/dev/uinput", str(caught.exception))
            c.mousemove_abs(10, 10)
            self.assertEqual(c.pointer(), (10, 10, True))


if __name__ == "__main__":
    unittest.main()
