"""Daemon tests: injection logic on recorder devices, plus the full
client<->daemon JSON-lines protocol over a real spawned daemon (fake uinput)."""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# ...and the tests directory itself, for the bare `import support`
# below: running this file by path puts it on sys.path for free,
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common import session
from w11common.errors import CmdError
from support import (FakeEvdev, RecorderDev, abs_report, env, key_bitmap,
                     stop_daemons_under)
from wdotool import daemon
from hacks.input import keymap, keystate, uinput

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



class KernelDev(RecorderDev):
    """RecorderDev with the kernel's own filter in front of it.

    `input_handle_event()` drops an EV_KEY event that would not change the
    *emitting* device's key state: a release for a code this device does not
    hold produces no event at all, and neither does a repeated press. That
    rule is what decides what --clearmodifiers can do, and a recorder that
    echoes everything cannot express it -- which is how "press back a
    modifier we never held" once looked right in a test and left the modifier
    stuck down on a real session."""

    def __init__(self):
        super().__init__()
        self.held = set()

    def key(self, code, down):
        if (code in self.held) == bool(down):
            return                    # no state change: the kernel drops it
        if down:
            self.held.add(code)
        else:
            self.held.discard(code)
        super().key(code, down)


# -- the evdev layer, faked -------------------------------------------------
#
# --clearmodifiers reads the real keyboards' key state (EVIOCGKEY) to know
# what to put back. A test must never read the *runner's* keyboard for that:
# it would answer differently depending on what the person at the keyboard
# happens to be holding, and there is no keyboard at all in a container. So
# keystate.Evdev -- the whole syscall layer -- is swapped for this.

KB_PATH = "/dev/input/event1"       # "the keyboard"
KB2_PATH = "/dev/input/event5"      # a second, USB keyboard
MOUSE_PATH = "/dev/input/event2"    # not a keyboard: no modifier keys
OURS_PATH = "/dev/input/event9"     # our own uinput keyboard's node
MODS = tuple(keymap.MODIFIER_KEYCODES)
CTRL, SHIFT, ALT = (keymap.KEY_LEFTCTRL, keymap.KEY_LEFTSHIFT,
                    keymap.KEY_LEFTALT)


def fake_evdev(held=(), devices=None, **kw):
    """One PS/2 keyboard holding `held`, plus a mouse that is not one."""
    if devices is None:
        devices = [(KB_PATH, "AT Translated Set 2 keyboard", MODS, held),
                   (MOUSE_PATH, "VirtualPS/2 VMware VMMouse", (0x110,), ())]
    return FakeEvdev(devices, **kw)


_DEFAULT = object()


def make_daemon(geom=(0, 0, 1920, 1080), rel_abs=False, evdev=_DEFAULT):
    """A daemon on recorder devices. `rel_abs` pins the relative-move mode
    (B1) so no test depends on whether a sway socket happens to exist where
    it runs; False is the sway/i3 contract (REL events), True the warp.

    `evdev` is the faked key-state layer: the default is a keyboard with
    nothing held (so --clearmodifiers has nothing to restore and reads
    nothing real), and None leaves the daemon to build its own reader --
    only for the WDOTOOL_NO_KEYSTATE override, which builds none."""
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = geom
    d._rel_abs = rel_abs
    if evdev is _DEFAULT:
        evdev = fake_evdev()
    if evdev is not None:
        d.evdev = evdev
        d._reader = keystate.Reader(evdev=evdev, exclude_paths=(OURS_PATH,))
    return d


class DepartedPeer:
    """The client's end of a connection that goes away in the middle of a batch.

    One side of a real socketpair, with the other side closed after `after` questions, so that `select` and
    `recv(MSG_PEEK)` answer exactly what they answer for a client that was Ctrl-C'd: readable, and a
    zero-length peek. `_peer_gone` asks `select` first and `select` asks `fileno()`, so counting the `fileno()`
    calls counts the questions and the close can be dropped between two of them."""

    def __init__(self, after: int):
        self.ours, self.theirs = socket.socketpair()
        self.after = after
        self.asked = 0

    def fileno(self):
        self.asked += 1
        if self.asked > self.after and self.theirs.fileno() != -1:
            self.theirs.close()
        return self.ours.fileno()

    def recv(self, n, flags=0):
        return self.ours.recv(n, flags)

    def close(self):
        self.ours.close()
        with contextlib.suppress(OSError):
            self.theirs.close()


def compositor_pixel(axis_value: int, span: int) -> int:
    """The pixel a tablet axis value lands on: libinput's scale_axis()
    (value * span / (max - min + 1)) truncated to a pixel."""
    return axis_value * span // 32768


def _close_quietly(fd):
    with contextlib.suppress(OSError):
        os.close(fd)



class TestInjectionLogic(unittest.TestCase):
    def test_key_press_order_and_release(self):
        d = make_daemon()
        d.op_key("ctrl+shift+t", "press", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 29, 1), ("KEY", 42, 1), ("KEY", 20, 1),
            ("KEY", 29, 0), ("KEY", 42, 0), ("KEY", 20, 0),
        ])
        self.assertEqual(d.down, set())

    def test_keydown_keyup_state(self):
        d = make_daemon()
        d.op_key("ctrl", "down", 0, False)
        self.assertEqual(d.down, {29})
        d.op_key("ctrl", "up", 0, False)
        self.assertEqual(d.down, set())

    def test_shifted_keysym_synthesizes_shift(self):
        d = make_daemon()
        d.op_key("A", "press", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 42, 1), ("KEY", 30, 1),
            ("KEY", 42, 0), ("KEY", 30, 0),
        ])

    def test_no_double_shift(self):
        d = make_daemon()
        d.op_key("shift+A", "press", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 42, 1), ("KEY", 30, 1),
            ("KEY", 42, 0), ("KEY", 30, 0),
        ])

    def test_clearmodifiers_releases_eight(self):
        d = make_daemon()
        d.down = {29, 42}
        d.op_key("a", "press", 0, True)
        ups = d.kb.events[:8]
        self.assertEqual({e[1] for e in ups}, set(keymap.MODIFIER_KEYCODES))
        self.assertTrue(all(e[0] == "KEY" and e[2] == 0 for e in ups))
        # ...and the two we were holding come back afterwards
        self.assertEqual(d.kb.events[8:], [("KEY", 30, 1), ("KEY", 30, 0),
                                           ("KEY", 42, 1), ("KEY", 29, 1)])

    def test_key_warnings(self):
        # `key` converts the sequence once per press pass and once per
        # release pass, so xdotool prints the diagnostic twice (B12).
        d = make_daemon()
        warns = d.op_key("ctrl+bogus+t", "press", 0, False)
        self.assertEqual(warns, ["(symbol) No such key name 'bogus'. Ignoring it."] * 2)

    def test_key_warnings_single_pass_for_keydown(self):
        d = make_daemon()
        for direction in ("down", "up"):
            warns = d.op_key("bogus", direction, 0, False)
            self.assertEqual(warns,
                             ["(symbol) No such key name 'bogus'. Ignoring it."])

    def test_key_invalid_sequence_raises(self):
        d = make_daemon()
        with self.assertRaises(ValueError):
            d.op_key("ctrl-x", "press", 0, False)

    def test_type_shift_wrapping(self):
        d = make_daemon()
        warns = d.op_type("aA", 0, False)
        self.assertEqual(warns, [])
        self.assertEqual(d.kb.events, [
            ("KEY", 30, 1), ("KEY", 30, 0),
            ("KEY", 42, 1), ("KEY", 30, 1), ("KEY", 30, 0), ("KEY", 42, 0),
        ])

    def test_type_newline_tab(self):
        d = make_daemon()
        d.op_type("\n\t", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 28, 1), ("KEY", 28, 0),
            ("KEY", 15, 1), ("KEY", 15, 0),
        ])

    def test_type_unmapped_char_warns_and_skips(self):
        d = make_daemon()
        warns = d.op_type("aéb", 0, False)
        self.assertEqual(len(warns), 1)
        self.assertIn("é", warns[0])
        self.assertEqual([e for e in d.kb.events if e[2] == 1],
                         [("KEY", 30, 1), ("KEY", 48, 1)])

    def test_mousemove_abs_scaling_and_tracking(self):
        # B7: ceil(x * 32768 / span), the exact inverse of the compositor's
        # floor(v * span / 32768).
        d = make_daemon()
        d.op_mousemove_abs(100, 200, [])
        self.assertEqual((d.px, d.py), (100, 200))
        self.assertEqual(d.tablet.events, [
            (uinput.EV_ABS, uinput.ABS_X, -((-100 * 32768) // 1920)),
            (uinput.EV_ABS, uinput.ABS_Y, -((-200 * 32768) // 1080)),
            ("SYN",),
        ])

    def test_mousemove_abs_clamps(self):
        d = make_daemon()
        d.op_mousemove_abs(99999, -5, [])
        self.assertEqual((d.px, d.py), (1919, 0))
        ax, ay = abs_report(d.tablet)
        self.assertEqual(compositor_pixel(ax, 1920), 1919)
        self.assertEqual(ay, 0)

    def test_mousemove_rel_emits_rel_on_sway(self):
        """sway/i3 keep the REL path (the rig runs pointer_accel 0)."""
        d = make_daemon(rel_abs=False)
        d.px, d.py = 10, 10
        d.op_mousemove_rel(-50, 5, [])
        self.assertEqual((d.px, d.py), (0, 15))
        self.assertEqual(d.mouse.events, [
            (uinput.EV_REL, uinput.REL_X, -50),
            (uinput.EV_REL, uinput.REL_Y, 5),
            ("SYN",),
        ])

    def test_buttons(self):
        d = make_daemon()
        d.op_button(1, True)
        d.op_button(1, False)
        d.op_button(3, True)
        self.assertEqual(d.mouse.events, [
            ("KEY", uinput.BTN_LEFT, 1), ("KEY", uinput.BTN_LEFT, 0),
            ("KEY", uinput.BTN_RIGHT, 1),
        ])

    def test_wheel_buttons(self):
        d = make_daemon()
        d.op_button(4, True)
        d.op_button(4, False)  # no-op
        d.op_button(5, True)
        d.op_button(7, True)
        self.assertEqual(d.mouse.events, [
            (uinput.EV_REL, uinput.REL_WHEEL, 1), ("SYN",),
            (uinput.EV_REL, uinput.REL_WHEEL, -1), ("SYN",),
            (uinput.EV_REL, uinput.REL_HWHEEL, 1), ("SYN",),
        ])

    def test_invalid_button(self):
        d = make_daemon()
        with self.assertRaises(RuntimeError):
            d.op_button(42, True)

    def test_click_sequence(self):
        d = make_daemon()
        d.op_click(1, 2, 0)
        self.assertEqual(d.mouse.events, [
            ("KEY", uinput.BTN_LEFT, 1), ("KEY", uinput.BTN_LEFT, 0),
            ("KEY", uinput.BTN_LEFT, 1), ("KEY", uinput.BTN_LEFT, 0),
        ])

    def test_no_devices_error(self):
        # _need_devices retries create_devices; point it at a missing node so
        # the retry deterministically fails no matter where the tests run.
        os.environ["WDOTOOL_UINPUT_PATH"] = "/nonexistent/wdotool-uinput"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        d = daemon._Daemon()
        d.geom = (0, 0, 1920, 1080)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(RuntimeError):
                d.op_button(1, True)



class TestClearModifiers(unittest.TestCase):
    """--clearmodifiers releases the modifier keys, injects, and presses back
    the ones *wdotool itself* was holding.

    The ones the user holds on their own keyboard are neither released (the
    kernel drops a key-up from a device that does not hold the key) nor
    pressed back (that press would be ours, and the user's release would
    never clear it -- a modifier stuck down for the rest of the session).
    They are reported instead, when the key state can be read at all."""

    def ups(self, d):
        return d.kb.events[:8]

    def rest(self, d):
        return d.kb.events[8:]

    def warn(self, *labels):
        return daemon.FOREIGN_MODS_WARNING % ", ".join(labels)

    # -- what is restored

    def test_a_modifier_we_hold_is_released_and_pressed_back(self):
        d = make_daemon()
        d.down = {CTRL}
        d.op_key("a", "press", 0, True)
        self.assertEqual({e[1] for e in self.ups(d)}, set(MODS))
        self.assertTrue(all(e[2] == 0 for e in self.ups(d)))
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", CTRL, 1)])
        self.assertIn(CTRL, d.down)      # still held afterwards, as before

    def test_a_modifier_held_on_another_keyboard_is_never_pressed_back(self):
        # The regression this suite exists for. Pressing ctrl here would put
        # it down on OUR device; the user releasing theirs would not clear it
        # (Mutter and KWin refcount key state across the seat's devices), and
        # nothing else ever would -- the session keeps a live ctrl until the
        # daemon dies.
        d = make_daemon(evdev=fake_evdev(held=(CTRL,)))
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("a", "press", 0, True)
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0)])
        self.assertNotIn(("KEY", CTRL, 1), d.kb.events)
        self.assertNotIn(CTRL, d.down)

    def test_keyup_clearmodifiers_does_not_end_with_the_modifier_down(self):
        # `wdotool keyup --clearmodifiers ctrl` used to *press* ctrl on the
        # way out: the user was holding it, so the restore put it back.
        d = make_daemon(evdev=fake_evdev(held=(CTRL,)))
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("ctrl", "up", 0, True)
        self.assertNotIn(("KEY", CTRL, 1), d.kb.events)
        self.assertEqual(d.down, set())

    def test_two_foreign_keyboards_press_nothing(self):
        d = make_daemon(evdev=fake_evdev(devices=[
            (KB_PATH, "AT Translated Set 2 keyboard", MODS, (CTRL,)),
            (KB2_PATH, "USB Keyboard", MODS, (ALT,)),
        ]))
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("a", "press", 0, True)
        self.assertEqual([e for e in d.kb.events if e[2] == 1],
                         [("KEY", 30, 1)])
        self.assertEqual(d.down, set())

    def test_a_later_command_carries_nothing_over(self):
        d = make_daemon(evdev=fake_evdev(held=(CTRL,)))
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("a", "press", 0, True)
        d.kb.events.clear()
        d.op_type("x", 0, False)
        self.assertEqual(d.kb.events, [("KEY", 45, 1), ("KEY", 45, 0)])

    def test_the_user_letting_go_meanwhile_cannot_strand_anything(self):
        # Nothing is read to decide what to press, so the state going stale
        # between the sample and the restore has nothing to corrupt: the one
        # read is the diagnostic, and it decides only what to say.
        ev = fake_evdev(held=(CTRL,))
        ev.before_read = lambda e, n: e.release(KB_PATH, CTRL)
        d = make_daemon(evdev=ev)
        d.down = {SHIFT}
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("a", "press", 0, True)
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", SHIFT, 1)])
        self.assertEqual(d.evdev.reads, [KB_PATH])   # once, for the warning

    def test_a_reader_that_raises_does_not_break_the_injection(self):
        class Boom(FakeEvdev):
            def key_state(self, fd):
                raise OSError(5, "EIO")

        d = make_daemon(evdev=Boom([
            (KB_PATH, "AT Translated Set 2 keyboard", MODS, ())]))
        d.down = {CTRL}
        warnings = d.op_key("a", "press", 0, True, {})
        self.assertEqual(warnings, [])
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", CTRL, 1)])

    def test_type_restores_too(self):
        d = make_daemon()
        d.down = {SHIFT}
        d.op_type("a", 0, True)
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", SHIFT, 1)])

    def test_restore_happens_even_when_the_sequence_is_rejected(self):
        d = make_daemon()
        d.down = {CTRL}
        with self.assertRaises(ValueError):
            d.op_key("ctrl-x", "press", 0, True)   # invalid sequence
        self.assertEqual(self.rest(d), [("KEY", CTRL, 1)])

    # -- one clear around the whole command

    def test_clearmodifiers_is_one_clear_and_one_restore_around_the_whole_command(self):
        """The oracle, measured against xdotool 3.20160805.1 on Xvfb
        (2026-09-16): `keydown ctrl; key --clearmodifiers --repeat 3 a` is one
        Control_L release, three plain `a` and one Control_L press. We used to
        restore at the end of the first sequence's own request, so every later
        sequence and repetition arrived as Ctrl+key."""
        d = make_daemon()
        d.down = {CTRL}
        warnings, results = d.op_key_batch(["a", "b"], "press", 0, 3, 0, True)
        self.assertEqual({e[1] for e in self.ups(d)}, set(MODS))
        self.assertTrue(all(e[2] == 0 for e in self.ups(d)))
        self.assertEqual(self.rest(d),
                         [("KEY", 30, 1), ("KEY", 30, 0),
                          ("KEY", 48, 1), ("KEY", 48, 0)] * 3
                         + [("KEY", CTRL, 1)])
        self.assertEqual([r.get("error") for r in results], [None] * 6)
        self.assertEqual(warnings, [])
        self.assertIn(CTRL, d.down)

    def test_a_rejected_sequence_is_one_item_and_the_batch_carries_on(self):
        # xdotool converts and injects each sequence in turn and only sums the
        # failures at the end (B12): the sequences on either side of a bad one
        # are injected, inside the same clear/restore.
        d = make_daemon()
        d.down = {CTRL}
        _warnings, results = d.op_key_batch(["a", "bad.x", "b"], "press", 0, 1, 0, True)
        self.assertEqual(results[1]["error"], "Error: Invalid key sequence 'bad.x'")
        self.assertEqual([r.get("spec") for r in results], ["a", "bad.x", "b"])
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", 48, 1), ("KEY", 48, 0),
                                        ("KEY", CTRL, 1)])

    # -- the batch stops when the client does

    def test_a_departed_client_ends_the_repeat_loop_and_the_modifiers_still_come_back(self):
        """xdotool's repeat loop is the client's own, so Ctrl-C ends the
        injection on the spot. Ours runs in the daemon, two forks and a setsid
        away from the terminal that gets the SIGINT, so `key --repeat 100
        --repeat-delay 1000 a` interrupted after a second used to type for
        another 99 with the injection lock held against every other command.
        The loop asks the socket between items and in place of the sleep, and
        the break is inside `_mods_cleared`, so the restore still happens."""
        d = make_daemon()
        d.down = {CTRL}
        peer = DepartedPeer(after=2)
        self.addCleanup(peer.close)
        _warnings, results = d.op_key_batch(["a"], "press", 0, 1000, 10, True,
                                            {"conn": peer})
        # Two passes: the first is always injected. The questions go: the
        # repeat-delay wait after pass 0, the between-items check before pass
        # 1, then the repeat-delay wait after pass 1 -- the third, which the
        # peer answers with EOF.
        self.assertEqual(len(results), 2)
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0)] * 2
                                       + [("KEY", CTRL, 1)])
        self.assertIn(CTRL, d.down)

    def test_the_repeat_delay_is_a_wait_on_the_socket_not_a_sleep(self):
        """`--repeat-delay 300000` between two passes is five minutes of held
        lock. The wait is a `select` on the client's connection, so a hang-up
        ends it immediately instead of after the full delay -- without that,
        the check between items would not be reached until the sleep was
        over."""
        d = make_daemon()
        peer = DepartedPeer(after=0)     # gone at the very first question
        self.addCleanup(peer.close)
        started = time.monotonic()
        _warnings, results = d.op_key_batch(["a"], "press", 0, 2, 300_000, False,
                                            {"conn": peer})
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(len(results), 1)

    def test_a_departed_client_ends_a_type_batch_between_characters(self):
        # `type` of a long string at --delay 300000 is the same held lock by
        # another name, so the character loop asks the same question.
        d = make_daemon()
        d.down = {CTRL}
        peer = DepartedPeer(after=1)
        self.addCleanup(peer.close)
        d.op_type_batch(["abc"], 0, True, {"conn": peer})
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", 48, 1), ("KEY", 48, 0),
                                        ("KEY", CTRL, 1)])

    def test_an_in_process_caller_has_no_socket_and_never_waits_on_one(self):
        # Every other test in this file calls the ops directly, with no `conn`
        # in the session dict: for them the question is the `time.sleep` it
        # replaced, and the answer is always "still here".
        d = make_daemon()
        _warnings, results = d.op_key_batch(["a"], "press", 0, 3, 0, False, {})
        self.assertEqual(len(results), 3)
        self.assertFalse(daemon._peer_gone(None))
        self.assertFalse(daemon._peer_gone({}))

    def test_type_batch_clears_once_around_every_piece(self):
        # cmd_type.c clears before the loop over its arguments, not inside it.
        d = make_daemon()
        d.down = {CTRL}
        d.op_type_batch(["ab", "c"], 0, True)
        self.assertEqual({e[1] for e in self.ups(d)}, set(MODS))
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", 48, 1), ("KEY", 48, 0),
                                        ("KEY", 46, 1), ("KEY", 46, 0),
                                        ("KEY", CTRL, 1)])

    # -- what actually reaches the compositor (the kernel's filter)

    def test_a_foreign_modifier_produces_no_event_at_all(self):
        # Not "the compositor ignores our key-up": the kernel generates
        # nothing. --clearmodifiers cannot clear a key it does not hold, and
        # the docs say so rather than promising otherwise.
        d = make_daemon(evdev=fake_evdev(held=(CTRL,)))
        d.kb = KernelDev()
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("a", "press", 0, True)
        self.assertEqual(d.kb.events, [("KEY", 30, 1), ("KEY", 30, 0)])
        self.assertEqual(d.kb.held, set())

    def test_our_own_modifier_is_cleared_and_restored_on_the_wire(self):
        d = make_daemon(evdev=fake_evdev(held=(CTRL,)))
        d.kb = KernelDev()
        d.kb.key(CTRL, True)
        d.down.add(CTRL)
        d.kb.events.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            d.op_key("a", "press", 0, True)
        self.assertEqual(d.kb.events, [("KEY", CTRL, 0), ("KEY", 30, 1),
                                       ("KEY", 30, 0), ("KEY", CTRL, 1)])
        self.assertEqual(d.kb.held, {CTRL})

    # -- the diagnostic

    def test_a_foreign_modifier_is_named_once_per_connection(self):
        d = make_daemon(evdev=fake_evdev(held=(CTRL, SHIFT)))
        session = {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(d.op_key("a", "press", 0, False, session), [])
            first = d.op_key("a", "press", 0, True, session)
            again = d.op_key("b", "press", 0, True, session)
            fresh = d.op_key("c", "press", 0, True, {})
        self.assertEqual(first, [self.warn("shift", "ctrl")])
        self.assertEqual(again, [])          # same command, said once
        self.assertEqual(fresh, [self.warn("shift", "ctrl")])
        self.assertIn("cannot release a key it does not hold", first[0])
        # ...and the daemon log carries it once, not once per request
        self.assertEqual(err.getvalue().count("--clearmodifiers"), 1)

    def test_nothing_held_says_nothing(self):
        d = make_daemon(evdev=fake_evdev(held=()))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(d.op_key("a", "press", 0, True, {}), [])
        self.assertEqual(err.getvalue(), "")

    def test_unreadable_devices_say_nothing_and_change_nothing(self):
        # The normal non-root case: the key state cannot be read, so there is
        # nothing to report -- and nothing about the behaviour differs, which
        # is why it is silent rather than nagging about root.
        d = make_daemon(evdev=FakeEvdev(unreadable=[KB_PATH]))
        d.down = {CTRL}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            warnings = d.op_key("a", "press", 0, True, {})
        self.assertEqual(warnings, [])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                        ("KEY", CTRL, 1)])

    def test_our_own_virtual_keyboard_is_never_read(self):
        # Our uinput keyboard is a keyboard like any other from evdev's point
        # of view, and it holds ctrl because we pressed it. Reading it would
        # make us warn about our own injection.
        d = make_daemon(evdev=fake_evdev(devices=[
            (KB_PATH, "AT Translated Set 2 keyboard", MODS, ()),
            (OURS_PATH, "wdotool virtual keyboard", MODS, (CTRL,)),
        ]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.op_key("a", "press", 0, True, {}), [])
        self.assertNotIn(OURS_PATH, d.evdev.reads)

    def test_a_wdotool_device_at_an_unknown_path_is_skipped_by_name(self):
        # Belt and braces for a kernel too old for UI_GET_SYSNAME (and for a
        # stale device left behind by a killed daemon): the name settles it.
        d = make_daemon(evdev=fake_evdev(devices=[
            (KB_PATH, "AT Translated Set 2 keyboard", MODS, ()),
            (KB2_PATH, "wdotool virtual keyboard", MODS, (SHIFT,)),
        ]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.op_key("a", "press", 0, True, {}), [])

    def test_no_keystate_override_skips_the_diagnostic(self):
        old = os.environ.get(daemon.NO_KEYSTATE_ENV)
        os.environ[daemon.NO_KEYSTATE_ENV] = "1"
        try:
            d = make_daemon(evdev=None)          # would build a real reader
            d.down = {CTRL}
            with contextlib.redirect_stderr(io.StringIO()):
                warnings = d.op_key("a", "press", 0, True, {})
            self.assertIsNone(d._key_reader())
            self.assertEqual(warnings, [])
            self.assertEqual(self.rest(d), [("KEY", 30, 1), ("KEY", 30, 0),
                                            ("KEY", CTRL, 1)])
        finally:
            if old is None:
                os.environ.pop(daemon.NO_KEYSTATE_ENV, None)
            else:
                os.environ[daemon.NO_KEYSTATE_ENV] = old

    # -- one request, one lock hold

    def test_the_wrapped_ops_clear_and_restore_in_one_request(self):
        # Three requests (clear, inject, restore) leave two gaps in which
        # another wdotool process can inject -- with the modifiers down, or
        # across the restore. Each of these does the lot under one lock.
        for req, injected in (
                ({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0}, ("BTN", 272)),
                ({"op": "button", "btn": 1, "down": True}, ("BTN", 272)),
                ({"op": "mousemove_abs", "x": 5, "y": 5}, None),
                ({"op": "mousemove_rel", "dx": 5, "dy": 5}, None)):
            d = make_daemon()
            d.down = {CTRL}
            req = dict(req, clearmods=True)
            self.assertTrue(d.handle(req, {})["ok"], req)
            self.assertEqual(d.kb.events[0], ("KEY", MODS[0], 0), req)
            self.assertEqual(d.kb.events[-1], ("KEY", CTRL, 1), req)
            self.assertIn(CTRL, d.down)

    def test_the_wrapped_ops_do_nothing_without_the_flag(self):
        d = make_daemon()
        d.down = {CTRL}
        self.assertTrue(d.handle({"op": "click", "btn": 1, "repeat": 1,
                                  "delay_ms": 0}, {})["ok"])
        self.assertEqual(d.kb.events, [])
        self.assertEqual(d.down, {CTRL})

    def test_handle_answers_key_batch_with_results(self):
        d = make_daemon()
        resp = d.handle({"op": "key_batch", "specs": ["a"], "repeat": 2,
                         "delay_ms": 0}, {})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual([r["spec"] for r in resp["results"]], ["a", "a"])
        # An empty list is a malformed request field like any other, not a
        # no-op. handle() catches nothing; serve_client is what turns the
        # raise into {"ok": false}.
        with self.assertRaises(RuntimeError):
            d.handle({"op": "key_batch", "specs": []}, {})

    def test_key_batch_repeat_has_no_ceiling_and_repeat_delay_keeps_delays(self):
        """xdotool's `--repeat` takes any count -- cmd_key.c loops for
        whatever it parsed -- so a count we refuse is a command X runs and we
        do not. The bound went away when the batch started noticing a
        departed client; `repeat_delay_ms` keeps the [0, 300000] bound
        `delay_ms` has always had, and that one difference is the what-differs
        row in docs/WDOTOOL.md."""
        d = daemon._Daemon.__new__(daemon._Daemon)
        d.lock = threading.RLock()
        seen = []

        def fake_batch(specs, direction, delay_ms, repeat, repeat_delay_ms, *a, **kw):
            seen.append((repeat, repeat_delay_ms))
            return [], []

        d.op_key_batch = fake_batch
        resp = d.handle({"op": "key_batch", "specs": ["a"], "repeat": 2_000_000})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(seen, [(2_000_000, 0)])
        # The floor is still a floor, and its message names the missing ceiling.
        with self.assertRaisesRegex(RuntimeError, r"repeat 0 out of range \[1, inf\]"):
            d.handle({"op": "key_batch", "specs": ["a"], "repeat": 0})
        with self.assertRaisesRegex(RuntimeError,
                                    r"repeat_delay_ms 400000 out of range \[0, 300000\]"):
            d.handle({"op": "key_batch", "specs": ["a"], "repeat_delay_ms": 400_000})

    def test_type_batch_warnings_reach_the_reply(self):
        # The arm has to assign `warnings` before falling through to the
        # common return, exactly as the `type` arm does: a batch that could
        # not type a character says so, and a `warnings = ...` left off here
        # would have dropped every one of those lines on the floor.
        d = daemon._Daemon.__new__(daemon._Daemon)
        d.lock = threading.RLock()
        d.op_type_batch = lambda *a, **k: ["w1"]
        self.assertEqual(d.handle({"op": "type_batch", "texts": ["hi"]})["warnings"],
                         ["w1"])

    def test_key_batch_falls_back_to_per_sequence_requests_on_an_older_daemon(self):
        """An upgrade leaves the running daemon in place for up to its
        15-minute idle life, and that one does not know the op. `unknown op
        'key_batch'` must not reach the user: the client sends one `key`
        request per sequence for that window, which is what it did before the
        batch existed."""
        sent = []

        class OldDaemonClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                if kw["op"] == "key_batch":
                    raise CmdError("unknown op 'key_batch'")
                sent.append(kw)
                return {}

        items = OldDaemonClient().key_batch(["a", "b"], "press", 12, clearmods=True)
        self.assertEqual([r["op"] for r in sent], ["key", "key"])
        self.assertEqual([r["spec"] for r in sent], ["a", "b"])
        self.assertEqual([r["clearmods"] for r in sent], [True, True])
        # No warnings on the items: _rpc already printed each reply's own on
        # the way past, and repeating them here would print them twice.
        self.assertEqual(items, [{"spec": "a", "warnings": []},
                                 {"spec": "b", "warnings": []}])

    # -- the two ops kept for the frozen client API

    def test_clear_and_restore_ops(self):
        d = make_daemon(evdev=fake_evdev(held=(ALT,)))
        d.down = {CTRL, SHIFT}
        session = {}
        with contextlib.redirect_stderr(io.StringIO()):
            resp = d.handle({"op": "clear_modifiers"}, session)
        self.assertEqual(resp["ok"], True)
        self.assertEqual(sorted(resp["held"]), sorted([CTRL, SHIFT]))
        self.assertEqual(resp["warnings"], [self.warn("alt")])
        d.kb.events.clear()
        self.assertEqual(d.handle({"op": "restore_modifiers",
                                   "held": resp["held"]}, session)["ok"], True)
        self.assertEqual(d.kb.events, [("KEY", SHIFT, 1), ("KEY", CTRL, 1)])

    def test_restore_only_ever_presses_modifier_keycodes(self):
        # A request is not allowed to name any other key: this is the one op
        # that presses a key a client did not spell out. serve_client turns
        # the rejection into {"ok": false} (TestProtocol covers that end).
        d = make_daemon()
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "restore_modifiers", "held": [30]}, {})
        self.assertIn("not a modifier keycode", str(cm.exception))
        for bad in ("x", [["a"]], [True], [None]):
            with self.assertRaises(RuntimeError):
                d.handle({"op": "restore_modifiers", "held": bad}, {})
        self.assertEqual(d.kb.events, [])


class TestKeyStateReader(unittest.TestCase):
    """keystate.Reader on its own: what "unknown" means and what is skipped."""

    def test_unknown_is_not_the_same_as_nothing_held(self):
        readable = keystate.Reader(evdev=fake_evdev(held=()))
        self.assertEqual(readable.held(MODS), set())
        nothing = keystate.Reader(evdev=FakeEvdev(unreadable=[KB_PATH]))
        self.assertIsNone(nothing.held(MODS))
        empty = keystate.Reader(evdev=FakeEvdev())
        self.assertIsNone(empty.held(MODS))

    def test_a_device_without_modifier_keys_is_not_a_keyboard(self):
        r = keystate.Reader(evdev=FakeEvdev([
            (MOUSE_PATH, "VirtualPS/2 VMware VMMouse", (0x110,), (0x110,)),
        ]))
        self.assertIsNone(r.held(MODS))

    def test_no_codes_asked_no_devices_touched(self):
        ev = fake_evdev(held=(CTRL,))
        self.assertEqual(keystate.Reader(evdev=ev).held([]), set())
        self.assertEqual(ev.opened, [])

    def test_bitmap_helper(self):
        bits = key_bitmap([0, 7, 8, keymap.KEY_RIGHTMETA])
        self.assertTrue(keystate.bit(bits, 0) and keystate.bit(bits, 7))
        self.assertTrue(keystate.bit(bits, 8))
        self.assertTrue(keystate.bit(bits, keymap.KEY_RIGHTMETA))
        self.assertFalse(keystate.bit(bits, 1))
        self.assertFalse(keystate.bit(bits, 9999))   # past the bitmap


class TestPointerMapping(unittest.TestCase):
    """B1/B2/B7: where an injected move actually lands."""

    def test_kwin_three_head_layouts_round_trip(self):
        """The same round trip over the three-head layouts KWin actually
        makes, which is where the claim had never been measured.

        Live on Plasma 6.6 and 5.27 (`repro/kde-6-pointer-3head.py`, three
        1920x1080 heads) every corner and centre of every output landed on
        the pixel asked for, and so did 100 swept x values at the layout's
        two ends -- read back from KWin's own `workspace.cursorPos`.  The
        shapes are KDE's own: KWin refuses a negative position for an
        enabled output ("Position of enabled output %1 is negative"), so
        `--pos -1920x0` shifts the whole layout instead and leaves a
        1920px hole in the middle of the bounding box, and a head at scale
        1.5 is 1280x720 logical beside one dropped 200px down.

        The one live miss is KWin's and not the map's: a 1x1 screen edge at
        an output's top-left corner pushes the cursor back a pixel, so
        (0,0) reads (1,1).  Nothing on this side can reach that pixel.
        """
        for geom in ((0, 0, 5760, 1080),      # three heads in a row
                     (0, 0, 7680, 1080),      # ...with the hole in it
                     (0, 0, 5120, 1280)):     # scale 1.5 + a y offset
            gx, gy, w, h = geom
            d = make_daemon(geom)
            for x in (gx, gx + 1, gx + w // 2, gx + w - 1):
                for y in (gy, gy + 1, gy + h // 2, gy + h - 1):
                    d.tablet.events.clear()
                    d.op_mousemove_abs(x, y, [])
                    ax, ay = abs_report(d.tablet)
                    self.assertEqual(
                        (gx + compositor_pixel(ax, w),
                         gy + compositor_pixel(ay, h)), (x, y),
                        "geom=%r target=%d,%d axes=%d,%d" % (geom, x, y, ax, ay))

    def test_abs_round_trips_through_the_compositor_inverse(self):
        """B7: every x must survive daemon -> tablet -> compositor. The old
        (x-gx)*32767//(w-1) map lost a pixel wherever the division was
        inexact: 257 of 301 x values near the origin of the 3x1920 rig."""
        for geom in ((0, 0, 5760, 1080), (0, 0, 1920, 1080),
                     (-1920, 0, 3200, 1080), (100, 50, 800, 600)):
            gx, gy, w, h = geom
            d = make_daemon(geom)
            probes = (list(range(gx, gx + 400))                   # origin head
                      + list(range(gx + w - 400, gx + w))         # far edge
                      + list(range(gx + w // 3 - 30, gx + w // 3 + 30)))
            for x in probes:
                d.tablet.events.clear()
                d.op_mousemove_abs(x, gy, [])
                ax, _ = abs_report(d.tablet)
                self.assertEqual(gx + compositor_pixel(ax, w), x,
                                 "geom=%r x=%d axis=%d" % (geom, x, ax))
            for y in (gy, gy + 1, gy + h // 2, gy + h - 2, gy + h - 1):
                d.tablet.events.clear()
                d.op_mousemove_abs(gx, y, [])
                _, ay = abs_report(d.tablet)
                self.assertEqual(gy + compositor_pixel(ay, h), y,
                                 "geom=%r y=%d axis=%d" % (geom, y, ay))

    def test_axis_values_stay_in_range(self):
        for span in (1, 2, 800, 1920, 5760, 32768, 65536):
            for delta in (0, 1, span // 2, span - 1):
                v = daemon._Daemon._axis(delta, span)
                self.assertTrue(0 <= v <= 32767, (span, delta, v))

    def test_repeated_absolute_move_is_nudged(self):
        """B2: the kernel drops an EV_ABS whose value has not changed, so
        `mousemove X Y` twice used to be a silent no-op the second time."""
        d = make_daemon()
        d.op_mousemove_abs(1500, 700, [])
        first = list(d.tablet.events)
        d.tablet.events.clear()
        d.op_mousemove_abs(1500, 700, [])
        ax, ay = abs_report(d.tablet)
        # a nudged X on its own report, then the real coordinates
        self.assertEqual(d.tablet.events[0][:2], (uinput.EV_ABS, uinput.ABS_X))
        self.assertNotEqual(d.tablet.events[0][2], ax)
        self.assertEqual(abs(d.tablet.events[0][2] - ax), 1)
        self.assertEqual(d.tablet.events[1], ("SYN",))
        self.assertEqual(d.tablet.events[2:], first)
        self.assertEqual((d.px, d.py), (1500, 700))

    def test_absolute_move_after_relative_still_warps(self):
        """B2, the field case: mousemove 1500 700; mousemove_relative 100 0;
        mousemove 1500 700 must put the pointer back."""
        d = make_daemon(rel_abs=False)
        d.op_mousemove_abs(1500, 700, [])
        d.op_mousemove_rel(100, 0, [])          # REL: tablet axes unchanged
        d.tablet.events.clear()
        d.op_mousemove_abs(1500, 700, [])
        self.assertEqual(d.tablet.events[0][:2], (uinput.EV_ABS, uinput.ABS_X))
        self.assertEqual(d.tablet.events[1], ("SYN",))
        ax, ay = abs_report(d.tablet)
        self.assertEqual((compositor_pixel(ax, 1920), compositor_pixel(ay, 1080)),
                         (1500, 700))

    def test_first_move_to_the_origin_is_nudged(self):
        """A fresh uinput device starts at axis 0,0, so a first
        `mousemove 0 0` would be dropped without the nudge."""
        d = make_daemon()
        d.op_mousemove_abs(0, 0, [])
        self.assertEqual(d.tablet.events[0], (uinput.EV_ABS, uinput.ABS_X, 1))
        self.assertEqual(d.tablet.events[1], ("SYN",))
        self.assertEqual(abs_report(d.tablet), (0, 0))
        self.assertEqual((d.px, d.py), (0, 0))

    def test_nudge_at_the_far_edge_goes_down(self):
        d = make_daemon()
        d.op_mousemove_abs(1919, 1079, [])
        top = abs_report(d.tablet)
        d.tablet.events.clear()
        d.op_mousemove_abs(1919, 1079, [])
        self.assertEqual(d.tablet.events[0][2], top[0] + (-1 if top[0] == 32767 else 1))

    def test_relative_move_warps_by_default(self):
        """B1: REL events go through libinput's acceleration curve, so a
        relative move is emitted as an absolute warp to the target."""
        d = make_daemon(rel_abs=True)
        d.op_mousemove_abs(1200, 601, [])
        d.tablet.events.clear()
        d.op_mousemove_rel(500, 0, [])
        self.assertEqual((d.px, d.py), (1700, 601))
        self.assertEqual(d.mouse.events, [])  # no REL_X/REL_Y at all
        ax, ay = abs_report(d.tablet)
        self.assertEqual((compositor_pixel(ax, 1920), compositor_pixel(ay, 1080)),
                         (1700, 601))

    def test_relative_warp_clamps_to_the_layout(self):
        d = make_daemon((0, 0, 5760, 1080), rel_abs=True)
        d.op_mousemove_abs(5000, 500, [])
        d.op_mousemove_rel(9999, -9999, [])
        self.assertEqual((d.px, d.py), (5759, 0))
        ax, ay = abs_report(d.tablet)
        self.assertEqual(compositor_pixel(ax, 5760), 5759)
        self.assertEqual(compositor_pixel(ay, 1080), 0)

    def test_rel_mode_selection(self):
        env = os.environ.get("WDOTOOL_REL_MODE")
        sway = os.environ.get("SWAYSOCK")
        self.addCleanup(lambda: (os.environ.__setitem__("WDOTOOL_REL_MODE", env)
                                 if env is not None else
                                 os.environ.pop("WDOTOOL_REL_MODE", None)))
        self.addCleanup(lambda: (os.environ.__setitem__("SWAYSOCK", sway)
                                 if sway is not None else
                                 os.environ.pop("SWAYSOCK", None)))
        for value, want in (("abs", True), ("warp", True), ("rel", False),
                            ("relative", False)):
            os.environ["WDOTOOL_REL_MODE"] = value
            d = daemon._Daemon()
            self.assertIs(d._rel_absolute(), want, value)
        # no override: sway/i3 keep REL, everything else warps
        os.environ.pop("WDOTOOL_REL_MODE", None)
        with tempfile.NamedTemporaryFile(prefix="wdotool-swaysock-") as sock:
            os.environ["SWAYSOCK"] = sock.name
            self.assertIs(daemon._Daemon()._rel_absolute(), False)
        os.environ["SWAYSOCK"] = "/nonexistent/wdotool-no-sway"
        self.assertIs(daemon._Daemon()._rel_absolute(), True)


class TestPointerModel(unittest.TestCase):
    """B6: the daemon's pointer model, and refusing to invent one."""

    def test_seed_pointer_replaces_the_model(self):
        d = make_daemon((0, 0, 5760, 1080), rel_abs=True)
        d.op_mousemove_abs(100, 100, [])
        d.op_seed_pointer(2880, 540, [])       # "the compositor says..."
        self.assertEqual((d.px, d.py), (2880, 540))
        self.assertTrue(d.pos_known)
        self.assertEqual(d.tablet.events[-1], ("SYN",))  # nothing injected
        d.tablet.events.clear()
        d.op_mousemove_rel(20, 0, [])
        self.assertEqual((d.px, d.py), (2900, 540))

    def test_seed_pointer_clamps(self):
        d = make_daemon()
        d.op_seed_pointer(99999, -5, [])
        self.assertEqual((d.px, d.py), (1919, 0))

    def test_pointer_without_devices_is_an_error_not_zero_zero(self):
        os.environ["WDOTOOL_UINPUT_PATH"] = "/nonexistent/wdotool-uinput"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        d = daemon._Daemon()
        d.geom = (0, 0, 1920, 1080)
        # serve_client turns this into {"ok": false, "error": ...}, so the
        # client raises CmdError and getmouselocation exits 1 with the real
        # reason instead of printing "x:0 y:0" with rc 0.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                d.handle({"op": "pointer"})
        self.assertIn("uinput", str(cm.exception))

    def test_a_true_delta_never_claims_to_know_where_it_landed(self):
        """The half of `known` that fix 38's refusal rests on: where the
        relative move really is a delta (sway, where `_rel_abs` is False and
        the pointer moves from wherever it is), the model's new x,y is
        arithmetic on a position nobody established, so it stays unknown and
        `getmouselocation` keeps refusing. Where it is a warp -- every other
        compositor, `_rel_abs` True -- it lands somewhere definite and says
        so."""
        d = make_daemon((0, 0, 1920, 1080), rel_abs=False)
        d.op_mousemove_rel(5, 5, [])
        self.assertEqual((d.px, d.py), (5, 5))
        self.assertFalse(d.pos_known)
        self.assertIs(d.handle({"op": "pointer"})["known"], False)
        warped = make_daemon((0, 0, 1920, 1080), rel_abs=True)
        warped.op_mousemove_rel(5, 5, [])
        self.assertTrue(warped.pos_known)

    def test_pointer_answers_once_seeded(self):
        os.environ["WDOTOOL_UINPUT_PATH"] = "/nonexistent/wdotool-uinput"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        d = daemon._Daemon()
        d.geom = (0, 0, 1920, 1080)
        d.op_seed_pointer(640, 400, [])
        self.assertEqual(d.handle({"op": "pointer"}),
                         {"ok": True, "x": 640, "y": 400, "known": True})


class TestGeometryFallback(unittest.TestCase):
    """B5: the daemon says when the layout size is only a guess."""

    def test_a_query_does_not_print_the_guess_it_is_refusing(self):
        """The 0.4 retest, on both X11 images: `wdotool getdisplaygeometry`
        with no compositor printed two lines that contradict each other --

            wdotool: cannot query Wayland output geometry (...); assuming 1920x1080
            wdotool: no Wayland session found: ...; not guessing a display size

        The second is the command's, and the documents promise it ("it never
        invents a size"). The first is the daemon's pointer-map fallback,
        which is true of a pointer move and of nothing else, so a caller that
        is only *asking* says `quiet` and gets `fallback` in the reply to
        decide with."""
        d = make_daemon()
        d.geom = None
        err = io.StringIO()
        # no compositor of any kind, whatever is running where this runs
        with mock.patch.object(daemon, "_wayland_bbox",
                               side_effect=OSError("no wayland socket found")):
            with contextlib.redirect_stderr(err):
                resp = d.handle({"op": "geometry", "quiet": True})
            self.assertTrue(resp["fallback"])
            self.assertEqual((resp["w"], resp["h"]),
                             daemon.FALLBACK_GEOMETRY[2:])
            self.assertEqual(resp.get("warnings") or [], [])
            self.assertEqual(err.getvalue(), "")
            # ...and the pointer path, which really does use the guess, says so
            with contextlib.redirect_stderr(err):
                d.handle({"op": "geometry"})
        self.assertIn("assuming 1920x1080", err.getvalue())

    def test_geometry_reports_the_fallback(self):
        d = make_daemon()
        d.geom = None
        with contextlib.redirect_stderr(io.StringIO()):
            resp = d.handle({"op": "geometry"})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["fallback"])
        self.assertEqual((resp["w"], resp["h"]), daemon.FALLBACK_GEOMETRY[2:])
        d.geom = (0, 0, 640, 480)
        resp = d.handle({"op": "geometry"})
        self.assertFalse(resp["fallback"])


class TestThePinIsSentWithTheRequest(unittest.TestCase):
    """The client half of the same finding: WDOTOOL_XKB_GROUP is read where
    the user set it -- in the environment of the *command* -- because the
    daemon keeps the environment it was spawned with and outlives it."""

    class Recorder(daemon.DaemonClient):
        def __init__(self):
            self.sent = []

        def _rpc(self, **req):
            self.sent.append(req)
            return {"ok": True}

    def sent_for(self, group):
        c = self.Recorder()
        with env(WDOTOOL_XKB_GROUP=group):
            c.type_text("a", 12)
            c.key("a", "press", 12, False)
        return c.sent

    def test_a_set_variable_travels_with_type_and_key(self):
        self.assertEqual([r.get("xkb_group") for r in self.sent_for("2")],
                         ["2", "2"])

    def test_an_unset_one_leaves_the_request_as_it_was(self):
        """An older daemon must see exactly what an older client sent."""
        for req in self.sent_for(None):
            self.assertNotIn("xkb_group", req)

    def test_the_pointer_ops_do_not_carry_it(self):
        c = self.Recorder()
        with env(WDOTOOL_XKB_GROUP="2"):
            c.mousemove_abs(1, 2)
            c.button(1, True)
        for req in c.sent:
            self.assertNotIn("xkb_group", req)


class TestTransientScope(unittest.TestCase):
    """B11: leaving the launcher's transient systemd scope."""

    APP = ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
           "app-gnome-hotkey.sh-4711.scope")

    def test_app_scope_gets_a_sibling_cgroup(self):
        self.assertEqual(
            daemon.transient_scope_target(self.APP, 1000),
            "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/"
            "wdotool-daemon")

    def test_login_session_scope_is_left_alone(self):
        line = ("0::/user.slice/user-1000.slice/session-3.scope")
        self.assertIsNone(daemon.transient_scope_target(line, 1000))

    def test_service_unit_is_left_alone(self):
        line = ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                "org.gnome.SettingsDaemon.MediaKeys.service")
        self.assertIsNone(daemon.transient_scope_target(line, 1000))

    def test_root_is_left_alone(self):
        self.assertIsNone(daemon.transient_scope_target(self.APP, 0))

    def test_foreign_uid_or_garbage(self):
        self.assertIsNone(daemon.transient_scope_target(self.APP, 1001))
        self.assertIsNone(daemon.transient_scope_target("", 1000))
        self.assertIsNone(daemon.transient_scope_target("1:name=systemd:/x", 1000))


class TestSpawnHygiene(unittest.TestCase):
    """B10: what a daemon inherits from the command that spawned it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wdotool-spawn-")
        # Registered here, before anything is spawned, and in this order:
        # cleanups run last-in-first-out, so the daemons go first and the
        # directory they listen in goes second. tearDown cannot do this --
        # unittest runs it *before* the cleanups -- and a daemon that
        # outlives its own socket directory is unreachable, which used to
        # mean immortal (tests/test_daemon_lifetime.py).
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(stop_daemons_under, self.tmp.name)
        self.backup = {k: os.environ.get(k)
                       for k in ("XDG_RUNTIME_DIR", "WDOTOOL_UINPUT_PATH",
                                 "WDOTOOL_FAKE_UINPUT", "WAYLAND_DISPLAY",
                                 "SWAYSOCK", "I3SOCK",
                                 "DBUS_SESSION_BUS_ADDRESS", "WDOTOOL_SPAWN_MARKER")}
        os.environ["XDG_RUNTIME_DIR"] = self.tmp.name
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/bus"
        os.environ["WDOTOOL_SPAWN_MARKER"] = "kept"
        for k in ("WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_clean_env_keeps_only_what_the_daemon_needs(self):
        env = daemon.clean_env()
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(env.get("XDG_RUNTIME_DIR"), self.tmp.name)
        self.assertEqual(env.get("WDOTOOL_SPAWN_MARKER"), "kept")
        self.assertTrue(env.get("PATH"))
        self.assertEqual(daemon.clean_env({})["PATH"], daemon._DEFAULT_PATH)

    def test_clean_env_keeps_the_compositor_ipc_socket(self):
        """SWAYSOCK/I3SOCK were dropped with everything else, but the daemon
        needs them for itself: _rel_absolute() asks find_sway_socket() whether
        this is sway/i3 and warps everywhere else (B1). The runtime-dir scan
        behind that question never finds i3's socket, which lives under /tmp
        -- which is exactly where this one is."""
        sock = tempfile.NamedTemporaryFile(prefix="wdotool-i3sock-")
        self.addCleanup(sock.close)
        self.assertNotEqual(os.path.dirname(sock.name),
                            os.environ["XDG_RUNTIME_DIR"])
        os.environ["I3SOCK"] = os.environ["SWAYSOCK"] = sock.name
        env = daemon.clean_env()
        self.assertEqual(env.get("SWAYSOCK"), sock.name)
        self.assertEqual(env.get("I3SOCK"), sock.name)
        # what it is kept for: the spawned daemon's own view of the session
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(session.find_sway_socket(), sock.name)
        del env["SWAYSOCK"], env["I3SOCK"]
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(session.find_sway_socket())

    def test_spawned_daemon_drops_fds_cwd_and_env(self):
        # An fd the client had open: stands in for the session D-Bus socket
        # the daemon used to keep ESTABLISHED for its whole life.
        marker = os.open(os.path.join(self.tmp.name, "inherited"),
                         os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(_close_quietly, marker)
        client = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(client.close)
        pid = client._rpc(op="ping")["pid"]

        fds = sorted(int(n) for n in os.listdir("/proc/%d/fd" % pid))
        targets = []
        for fd in fds:
            try:
                targets.append(os.readlink("/proc/%d/fd/%d" % (pid, fd)))
            except OSError:
                targets.append("")
        self.assertNotIn(os.path.join(self.tmp.name, "inherited"), targets)
        self.assertEqual(os.readlink("/proc/%d/cwd" % pid), "/")
        with open("/proc/%d/environ" % pid, "rb") as f:
            env = dict(kv.split("=", 1) for kv in
                       f.read().decode("utf-8", "replace").split("\0") if "=" in kv)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(env.get("WDOTOOL_SPAWN_MARKER"), "kept")
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read().decode("utf-8", "replace").split("\0")
        self.assertIn("__daemon", cmdline)


class TestProtocol(unittest.TestCase):
    """Full client<->daemon protocol against a really spawned (forked) daemon."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="wdotool-test-")
        # Before the spawn, and in this order (cleanups run
        # last-in-first-out): stop the daemons, then remove the directory
        # they listen in. Class cleanups also run when setUpClass itself
        # raises half way through, which tearDownClass does not -- and the
        # spawn is below, so that was a daemon nothing would ever stop.
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(stop_daemons_under, cls.tmp.name)
        cls.env_backup = {
            k: os.environ.get(k)
            for k in ("XDG_RUNTIME_DIR", "WDOTOOL_UINPUT_PATH", "WDOTOOL_FAKE_UINPUT",
                      "WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK",
                      daemon.NO_KEYSTATE_ENV)
        }
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp.name
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"
        # The spawned daemon must not read the *runner's* keyboards: this is
        # the documented override, and it makes the degraded path the one
        # thing the wire tests below can assert on deterministically.
        os.environ[daemon.NO_KEYSTATE_ENV] = "1"
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("SWAYSOCK", None)
        os.environ.pop("I3SOCK", None)

        # leave a stale socket behind to exercise cleanup
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(daemon.socket_path())
        stale.close()

        # spawn under a fully permissive umask: the daemon must still bind an
        # owner-only (0600) socket
        old_umask = os.umask(0o000)
        try:
            cls.client = daemon.DaemonClient.connect_or_spawn()
        finally:
            os.umask(old_umask)
        cls.pid = cls.client._rpc(op="ping")["pid"]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        for k, v in cls.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_geometry_fallback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(self.client.geometry(), (1920, 1080))

    def test_geometry_full_carries_origin(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(self.client.geometry_full(), (0, 0, 1920, 1080))

    def test_socket_mode_is_0600(self):
        # owner-only regardless of the spawning client's umask
        mode = os.stat(daemon.socket_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_malformed_json_values_keep_connection(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        self.addCleanup(sock.close)
        rfile = sock.makefile("r")
        for req in (b"5\n",
                    b'{"op": "mousemove_abs", "x": null, "y": null}\n',
                    b'{"op": "click", "btn": 1, "repeat": 99999999999}\n'):
            sock.sendall(req)
            self.assertFalse(json.loads(rfile.readline())["ok"])
        sock.sendall(b'{"op": "ping"}\n')
        self.assertEqual(json.loads(rfile.readline())["pid"], self.pid)

    def test_pointer_tracking_roundtrip(self):
        """The third element is `known` (fix 38): a warp establishes the
        position, and a delta applied to a position that was already known
        keeps it known."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.client.mousemove_abs(300, 400)
            self.assertEqual(self.client.pointer(), (300, 400, True))
            self.client.mousemove_rel(-100, 50)
            self.assertEqual(self.client.pointer(), (200, 450, True))
            self.client.mousemove_abs(99999, 99999)
            self.assertEqual(self.client.pointer(), (1919, 1079, True))
            # and the wire carries it under that name, not just the client
            self.assertIs(self.client._rpc(op="pointer")["known"], True)

    def test_clear_modifiers_over_the_wire(self):
        # No readable keyboard here (a container has none, and the runner may
        # not read what there is), so nothing is reported and nothing about
        # the behaviour changes -- but what the daemon itself holds still
        # comes back, and goes back down.
        client = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(client.close)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(client.clear_modifiers(), [])
            client.key("ctrl", "down", 0, False)
            self.assertEqual(client.clear_modifiers(),
                             [keymap.KEY_LEFTCTRL])
            client.restore_modifiers([keymap.KEY_LEFTCTRL])
            client.key("ctrl", "up", 0, False)
        self.assertEqual(stderr.getvalue(), "")
        client.restore_modifiers([])                    # no-op, no round trip

    def test_clearmods_rides_on_the_injection_request(self):
        client = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(client.close)
        with contextlib.redirect_stderr(io.StringIO()):
            client.type_text("a", 0, clearmods=True)
            client.click(1, 1, 0, clearmods=True)
            client.button(1, True, clearmods=True)
            client.button(1, False, clearmods=True)
            client.mousemove_abs(4, 4, clearmods=True)
            client.mousemove_rel(1, 1, clearmods=True)

    def test_restore_modifiers_rejects_a_non_modifier(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        self.addCleanup(sock.close)
        rfile = sock.makefile("r")
        sock.sendall(b'{"op": "restore_modifiers", "held": [30]}\n')
        resp = json.loads(rfile.readline())
        self.assertFalse(resp["ok"])
        self.assertIn("not a modifier keycode", resp["error"])

    def test_key_and_type(self):
        self.client.key("ctrl+shift+t", "press", 0, False)
        self.client.key("shift", "down", 0, False)
        self.client.key("shift", "up", 0, False)
        self.client.type_text("Hello, world!\n", 0)

    def test_key_warning_printed_to_stderr(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.client.key("ctrl+bogus", "press", 0, False)
        self.assertEqual(stderr.getvalue(),
                         "(symbol) No such key name 'bogus'. Ignoring it.\n" * 2)

    def test_invalid_key_sequence_is_cmderror(self):
        with self.assertRaises(CmdError) as cm:
            self.client.key("ctrl-x", "press", 0, False)
        self.assertTrue(str(cm.exception).startswith("Error: Invalid key sequence"))

    def test_click_and_buttons(self):
        self.client.click(1, 1, 0)
        self.client.click(3, 2, 0)
        self.client.button(2, True)
        self.client.button(2, False)
        with self.assertRaises(CmdError):
            self.client.button(77, True)

    def test_garbage_line_gets_error_response(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        sock.sendall(b"this is not json\n")
        resp = json.loads(sock.makefile("r").readline())
        self.assertFalse(resp["ok"])
        sock.close()

    def test_unknown_op(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        sock.sendall(b'{"op": "frobnicate"}\n')
        resp = json.loads(sock.makefile("r").readline())
        self.assertFalse(resp["ok"])
        self.assertIn("frobnicate", resp["error"])
        sock.close()

    def test_second_client_reaches_same_daemon(self):
        c2 = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(c2.close)
        self.assertEqual(c2._rpc(op="ping")["pid"], self.pid)

    def test_concurrent_clients(self):
        clients = [daemon.DaemonClient.connect_or_spawn() for _ in range(4)]
        for c in clients:
            self.addCleanup(c.close)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for n, c in enumerate(clients):
                c.mousemove_abs(n, n)
            for c in clients:
                self.assertEqual(len(c.pointer()), 3)


class TestThePointerIsUnknownUntilSomethingMovesIt(unittest.TestCase):
    """F3.3, fix 38: `known` over the wire, on a daemon nothing has moved.

    Its own runtime directory and its own daemon, because the answer is a
    property of a *fresh* daemon and TestProtocol's is shared by twenty
    tests, several of which warp the pointer before this one would run."""

    def client(self):
        tmp = tempfile.TemporaryDirectory(prefix="wdotool-ptr-")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(stop_daemons_under, tmp.name)
        e = env(XDG_RUNTIME_DIR=tmp.name, WDOTOOL_UINPUT_PATH="/dev/null",
                WDOTOOL_FAKE_UINPUT="1", WAYLAND_DISPLAY=None, SWAYSOCK=None,
                I3SOCK=None, **{daemon.NO_KEYSTATE_ENV: "1"})
        e.__enter__()
        self.addCleanup(e.__exit__, None, None, None)
        c = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(c.close)
        return c

    def test_a_fresh_daemon_says_it_does_not_know(self):
        """/dev/uinput opens (the fake one here, as on any root session), so
        the daemon does not refuse -- it answers the tablet's own untouched
        axis state, 0,0, and flags it. The flag is the whole point: 0,0 is a
        real place on the screen and was reported as if the pointer were
        there."""
        c = self.client()
        resp = c._rpc(op="pointer")
        self.assertEqual((resp["x"], resp["y"]), (0, 0))
        self.assertIs(resp["known"], False)
        self.assertEqual(c.pointer(), (0, 0, False))

    def test_and_says_it_does_once_it_has_been_moved(self):
        c = self.client()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            c.mousemove_abs(10, 10)
        self.assertEqual(c.pointer(), (10, 10, True))


if __name__ == "__main__":
    unittest.main()
