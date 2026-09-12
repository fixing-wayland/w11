#!/usr/bin/env python3
"""wdotool's generic wlroots window backend, on the wire.

`WlrBackend` is the capability floor: the backend a wlroots compositor with
no i3-ipc socket falls back to, speaking
zwlr_foreign_toplevel_management_unstable_v1 and nothing else. It had no
test of its own -- only test_wire_hardening, which proves that a compositor
that goes silent during the *constructor* produces one line instead of a
traceback, and never gets as far as a window.

So everything below the constructor was unproven: that arrival order really
is what window ids are made of, that a `closed` toplevel leaves the listing,
that the four state names map onto a protocol with only all-or-nothing
maximize, that `activate` refuses without a seat rather than sending a null
one, and that every capability this backend does not have says so as an
`unsupported` CmdError rather than crashing. Each of those is a wire fact,
so the peer here is a real socket speaking the real bytes.
"""

import io
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common import session
from w11common.errors import CmdError
from support import env
from test_wwmctl_x11 import FakeXServer
from wdotool import backend, backend_wlr, cli, x11_mini, xid_match
from wdotool.backend_wlr import BASE_ID, WlrBackend
from wdotool.ctx import Context
from wxprop import core as wxcore

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

MAXIMIZED, MINIMIZED, ACTIVATED, FULLSCREEN = 0, 1, 2, 3

# handle requests, by the opcode the protocol gives them
SET_MAXIMIZED, UNSET_MAXIMIZED = 0, 1
SET_MINIMIZED, UNSET_MINIMIZED = 2, 3
ACTIVATE, CLOSE = 4, 5
SET_FULLSCREEN, UNSET_FULLSCREEN = 8, 9


def top(title, app_id, *states):
    return {"title": title, "app_id": app_id, "states": states}


class ToplevelCompositor(wl_fake.Server):
    """A wlroots compositor with zwlr_foreign_toplevel_management_v1.

    Announces `toplevels` -- (title, app_id, states) -- the moment the
    manager is bound, each with its own server-side object id out of the
    0xFF000000 range a real compositor allocates from, and records every
    request made on one. `outputs` are (width, height) mode events; without
    a wl_seat the manager still works but `activate` has no seat to pass."""

    MANAGER = "zwlr_foreign_toplevel_manager_v1"
    PREFIX = "wdotool-wlr-"

    HANDLE_BASE = 0xFF000000

    def __init__(self, toplevels=(), manager_version=3, with_seat=True,
                 outputs=((1920, 1080),)):
        self.toplevels = [dict(t) for t in toplevels]
        self.with_seat = with_seat
        self.outputs = list(outputs)
        self.requests = []       # (toplevel index, opcode, body)
        self._handles = {}       # object id -> toplevel index
        super().__init__(manager_version)

    def advertise(self):
        return (([("wl_seat", 7)] if self.with_seat else [])
                + [("wl_output", 4)] * len(self.outputs))

    def new_state(self):
        return {"mgr": None}

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_output":
            w, h = self.outputs[self.names[name][1]]
            # mode(flags=current, width, height, refresh), then done
            self._send(conn, new_id, 1, struct.pack("<Iiii", 1, w, h, 60000))
            self._send(conn, new_id, 2)
        elif iface == self.MANAGER:
            state["mgr"] = new_id
            self._announce(conn, new_id)

    def _announce(self, conn, mgr):
        for i, t in enumerate(self.toplevels):
            oid = self.HANDLE_BASE + i
            self._handles[oid] = i
            self._send(conn, mgr, 0, struct.pack("<I", oid))      # toplevel
            self._send(conn, oid, 0, wl_fake.wstr(t["title"]))    # title
            self._send(conn, oid, 1, wl_fake.wstr(t["app_id"]))   # app_id
            self._send(conn, oid, 4, self._states(t["states"]))   # state
            self._send(conn, oid, 5)                              # done

    @staticmethod
    def _states(states):
        arr = struct.pack("<%dI" % len(states), *states)
        return struct.pack("<I", len(arr)) + arr + b"\0" * wl_fake.pad(len(arr))

    #: request opcode -> (the capability group it belongs to, the state bit, set or clear). `close` has no
    #: state bit -- its answer is the `closed` event -- so it is handled on its own below.
    EFFECT = {
        SET_MAXIMIZED: ("maximize", MAXIMIZED, True),
        UNSET_MAXIMIZED: ("maximize", MAXIMIZED, False),
        SET_MINIMIZED: ("minimize", MINIMIZED, True),
        UNSET_MINIMIZED: ("minimize", MINIMIZED, False),
        ACTIVATE: ("activate", ACTIVATED, True),
        SET_FULLSCREEN: ("fullscreen", FULLSCREEN, True),
        UNSET_FULLSCREEN: ("fullscreen", FULLSCREEN, False),
    }

    #: Which of those groups this compositor actually acts on. All of them by default: sway and labwc, where
    #: `wwmctl -c xtermwin` really closes the window and `-b add,fullscreen` really fullscreens it
    #: [M recon2/labwc.md §3]. The two subclasses below are the compositors that do less.
    HONOURS = frozenset(("close", "activate", "maximize", "minimize", "fullscreen"))

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid not in self._handles:
            return
        i = self._handles[oid]
        self.requests.append((i, opcode, body))
        self._apply(i, opcode)

    def _apply(self, i, opcode):
        """What the compositor does about the request, which is the only reply this protocol has: there is no
        ack, no error and no return value on any handle request, so a client can only look at the state it
        gets back afterwards."""
        if opcode == CLOSE:
            if "close" in self.HONOURS:
                self.closed(i)
            return
        eff = self.EFFECT.get(opcode)
        if eff is None:
            return
        group, bit, on = eff
        if group not in self.HONOURS:
            return
        states = set(self.toplevels[i]["states"])
        states.add(bit) if on else states.discard(bit)
        self.toplevels[i]["states"] = tuple(sorted(states))
        self.restate(i, *self.toplevels[i]["states"])

    # -- what a compositor does between two of the client's roundtrips
    def closed(self, index):
        """`closed` on one handle: the window went away."""
        self._to_all(self.HANDLE_BASE + index, 6)

    def restate(self, index, *states):
        """A new `state` event, then `done` -- a window that changed."""
        self._to_all(self.HANDLE_BASE + index, 4, self._states(states))
        self._to_all(self.HANDLE_BASE + index, 5)

    def _to_all(self, oid, opcode, body=b""):
        with self._lock:
            conns = list(self._clients)
        for c in conns:
            self._send(c, oid, opcode, body)

    # -- readers used by the tests
    def of(self, index):
        return [(op, body) for i, op, body in self.requests if i == index]

    def opcodes(self, index):
        return [op for op, _b in self.of(index)]


class ReadOnlyToplevels(ToplevelCompositor):
    """river 0.4 on the wire: every handle request is accepted and nothing at all happens.

    Read in river 0.4.8's `Window.zig`: the handle is created (line 437) and only ever pushed at --
    `setTitle`/`setAppId` (438-440, 1226-1245), `setActivated` (795), `destroy` (1201) -- and no
    `request_close`/`request_activate`/`request_maximize`/`request_minimize`/`request_fullscreen` listener
    is registered anywhere in river/*.zig. Measured against that reading with `recon2/river/tops.py`:
    `windowclose` twice, five seconds apart, left `foot` running and still listed; `windowactivate` left
    ACTIVATED on the other window; `windowminimize` and `windowstate --add FULLSCREEN` left the state array
    exactly as it was. All four returned rc 0 [M recon2/river.md §2a]."""

    HONOURS = frozenset()


class ClassicToplevels(ToplevelCompositor):
    """river-classic 0.3.17: close and fullscreen really happen, minimize and maximize do not.

    The same measurement run, same two commands: `windowclose` closed the window (`tops.py` -> `count 0`)
    and `windowstate --add FULLSCREEN` gave `states=['ACTIVATED','FULLSCREEN']`, while `windowminimize` and
    `windowstate --add MAXIMIZED_VERT` changed nothing -- dynamic tiling has neither [M river.md §2a]."""

    HONOURS = frozenset(("close", "fullscreen"))


class CloseByMinimizing(ToplevelCompositor):
    """A compositor whose client answers `close` with something other than going away.

    Nothing here is river: it is the case the close check has to leave alone. A window may take a moment to
    die, or refuse outright and only change its own state, so `close` reports silence and not "the window is
    still there" -- otherwise every unsaved-changes dialog would produce a warning that says the compositor
    ignored the request."""

    HONOURS = frozenset()

    def _apply(self, i, opcode):
        if opcode == CLOSE:
            self.restate(i, MINIMIZED)


class WlrTest(unittest.TestCase):
    """One compositor per test, with the environment pointed at it."""

    TOPLEVELS = (top("Alpha One", "alpha", ACTIVATED),
                 top("Beta Two", "beta"),
                 top("Hidden", "gamma", MINIMIZED))
    comp_kw: dict = {}

    def setUp(self):
        # `list()` reads the X plane now (the route-5 rectangle), so "there is no X plane" has to be SAID
        # rather than left to the box the suite runs on: this guest has an Xwayland of its own whenever a
        # sway or labwc session is up, and the process table is what `_x11` asks. `FakeXPlane.x_server()`
        # patches this back to True for the classes that do want one.
        patch = mock.patch.object(session, "xwayland_running", lambda uid=None: False)
        patch.start()
        self.addCleanup(patch.stop)

    def compositor(self, cls=ToplevelCompositor, **kw):
        opts = dict(self.comp_kw, **kw)
        opts.setdefault("toplevels", self.TOPLEVELS)
        comp = cls(**opts)
        self.addCleanup(comp.close)
        ctx = env(XDG_RUNTIME_DIR=comp.dir,
                  WAYLAND_DISPLAY=os.path.basename(comp.path),
                  SWAYSOCK=None, I3SOCK=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return comp

    def backend(self, cls=ToplevelCompositor, **kw):
        comp = self.compositor(cls, **kw)
        b = WlrBackend()
        self.addCleanup(b.c.close)
        # views() opens an X connection and the backend outlives one test but not the suite: an fd left to
        # the collector prints a ResourceWarning into whatever stderr a later test is capturing.
        self.addCleanup(lambda: b._x.close() if b._x not in (None, "unset") else None)
        return comp, b


class Listing(WlrTest):
    def test_ids_are_arrival_order_from_the_base(self):
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual([w.id for w in wins],
                         [BASE_ID, BASE_ID + 1, BASE_ID + 2])
        self.assertEqual([w.title for w in wins],
                         ["Alpha One", "Beta Two", "Hidden"])
        self.assertEqual([w.class_ for w in wins],
                         ["alpha", "beta", "gamma"])

    def test_the_states_that_this_protocol_does_carry(self):
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual([w.focused for w in wins], [True, False, False])
        self.assertEqual([w.visible for w in wins], [True, True, False])
        # no pids and no workspaces exist in this protocol: it says so in
        # the listing rather than guessing
        self.assertEqual([w.pid for w in wins], [0, 0, 0])
        self.assertEqual([w.desktop for w in wins], [-1, -1, -1])

    def test_geometry_is_the_output_box_because_none_is_carried(self):
        """With no X plane to ask, every row is the floor -- and the listing records that it reported a
        rectangle nobody told it, which is the flag a caller reads to say so (backend_cosmic's twin)."""
        _comp, b = self.backend()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()],
                         [(0, 0, 1920, 1080)] * 3)
        self.assertTrue(b.geometry_is_floor)

    def test_the_widest_mode_wins_over_several_outputs(self):
        _comp, b = self.backend(outputs=((1280, 720), (1920, 1200)))
        self.assertEqual(b.display_size(), (1920, 1200))

    def test_no_mode_event_is_a_clean_refusal(self):
        _comp, b = self.backend(outputs=())
        with self.assertRaises(CmdError) as cm:
            b.display_size()
        self.assertIn("no wl_output mode seen", str(cm.exception))

    def test_a_closed_toplevel_leaves_the_listing(self):
        comp, b = self.backend()
        comp.closed(1)
        self.assertEqual([w.title for w in b.list()], ["Alpha One", "Hidden"])
        # the survivors keep the ids they had: arrival order counts the
        # closed window too, so a window the user already found by id does
        # not silently become a different one when a neighbour goes away
        self.assertEqual([w.id for w in b.list()], [BASE_ID, BASE_ID + 2])

    def test_addressing_a_closed_window_is_one_line(self):
        comp, b = self.backend()
        comp.closed(2)
        b.list()
        with self.assertRaises(CmdError) as cm:
            b.close(BASE_ID + 2)
        self.assertEqual(str(cm.exception), "window %d not found" % (BASE_ID + 2))


class Actions(WlrTest):
    def test_close_minimize_map_unmap(self):
        comp, b = self.backend()
        b.close(BASE_ID)
        b.minimize(BASE_ID + 1)
        b.map(BASE_ID + 1)
        b.unmap(BASE_ID + 2)
        self.assertEqual(comp.opcodes(0), [CLOSE])
        self.assertEqual(comp.opcodes(1), [SET_MINIMIZED, UNSET_MINIMIZED])
        self.assertEqual(comp.opcodes(2), [SET_MINIMIZED])

    def test_activate_passes_the_seat_it_bound(self):
        comp, b = self.backend()
        b.activate(BASE_ID + 1)
        (op, body), = comp.of(1)
        self.assertEqual(op, ACTIVATE)
        (seat,) = struct.unpack("<I", body)
        self.assertEqual(seat, b.seat)
        self.assertIn(("wl_seat", 2), comp.binds)

    def test_activate_without_a_seat_refuses_rather_than_sending_null(self):
        comp, b = self.backend(with_seat=False)
        self.assertIsNone(b.seat)
        with self.assertRaises(CmdError) as cm:
            b.activate(BASE_ID)
        self.assertIn("compositor offers no wl_seat", str(cm.exception))
        self.assertEqual(comp.requests, [])


class States(WlrTest):
    def test_fullscreen_add_and_remove(self):
        comp, b = self.backend()
        b.set_state(BASE_ID, "FULLSCREEN", 1)
        b.set_state(BASE_ID, "FULLSCREEN", 0)
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN, UNSET_FULLSCREEN])
        # set_fullscreen takes an output, and a null one means "wherever"
        self.assertEqual(comp.of(0)[0][1], struct.pack("<I", 0))

    def test_fullscreen_toggle_reads_the_state_the_compositor_sent(self):
        comp, b = self.backend()
        b.set_state(BASE_ID, "FULLSCREEN", 2)
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN])
        comp.restate(0, ACTIVATED, FULLSCREEN)
        b.set_state(BASE_ID, "FULLSCREEN", 2)
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN, UNSET_FULLSCREEN])

    def test_fullscreen_needs_version_two(self):
        _comp, b = self.backend(manager_version=1)
        with self.assertRaises(CmdError) as cm:
            b.set_state(BASE_ID, "FULLSCREEN", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("windowstate FULLSCREEN", str(cm.exception))

    def test_both_maximize_axes_are_the_one_maximize_this_protocol_has(self):
        comp, b = self.backend()
        b.set_state(BASE_ID + 1, "MAXIMIZED_VERT", 1)
        b.set_state(BASE_ID + 1, "MAXIMIZED_HORZ", 1)
        b.set_state(BASE_ID + 1, "MAXIMIZED_VERT", 0)
        self.assertEqual(comp.opcodes(1),
                         [SET_MAXIMIZED, SET_MAXIMIZED, UNSET_MAXIMIZED])

    def test_hidden_is_minimize(self):
        comp, b = self.backend()
        b.set_state(BASE_ID, "HIDDEN", 1)
        b.set_state(BASE_ID + 2, "HIDDEN", 2)   # already minimized: toggle off
        self.assertEqual(comp.opcodes(0), [SET_MINIMIZED])
        self.assertEqual(comp.opcodes(2), [UNSET_MINIMIZED])

    def test_a_state_this_protocol_has_no_word_for(self):
        """SHADED is a state wmctrl and xdotool both take, so the refusal owes a route and not a full stop:
        the handle's state array has four members and a fifth is the compositor's to add."""
        _comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_state(BASE_ID, "SHADED", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(str(cm.exception),
                         "windowstate SHADED is not supported by the wlr backend: "
                         "zwlr_foreign_toplevel_management_v1 carries maximized, minimized, activated and "
                         "fullscreen and no other state; not yet here, and the route is a patched "
                         "compositor (AGENTS.md route 6), one state bit and one request each")

    def test_a_version_gap_is_told_apart_from_a_missing_feature(self):
        """`set_fullscreen` is version 2 of the protocol this manager speaks at 1: nobody has to write it,
        a newer compositor build already has it, and the refusal says which of the two it is."""
        _comp, b = self.backend()
        # the fake advertises the manager at 3 and lives in tests/wl_fake.py, which another batch owns, so
        # the negotiated version is moved here: `mgr_ver` is the field the constructor's bind wrote.
        b.mgr_ver = 1
        with self.assertRaises(CmdError) as cm:
            b.set_state(BASE_ID, "FULLSCREEN", 1)
        msg = str(cm.exception)
        self.assertIn("is version 1, whose handles have no set_fullscreen", msg)
        self.assertIn("AGENTS.md route 1", msg)
        self.assertIn("not yet here", msg)


class VerifyAfterAct(WlrTest):
    """U09. A compositor that accepts every request and does nothing is worse than one that fails.

    `zwlr_foreign_toplevel_handle_v1` has no reply for `close`, `activate`, `set_minimized` or
    `set_fullscreen` -- no ack, no error, no return value -- so "did it happen" can only be read off the
    state the compositor sends afterwards. river 0.4 sends nothing back and `wdotool windowclose` exited 0
    with the window still on screen [M recon2/river.md §2a]; there is no version bit to gate on, because
    river advertises manager v3 exactly like sway, so the check is this dynamic one."""

    def acting(self, fn, *a):
        """Run one backend call and return whatever it warned on stderr."""
        err = io.StringIO()
        with redirect_stderr(err):
            fn(*a)
        return err.getvalue()

    def test_a_read_only_compositor_is_reported_and_does_not_raise(self):
        comp, b = self.backend(cls=ReadOnlyToplevels)
        for call, args in ((b.close, (BASE_ID,)), (b.activate, (BASE_ID + 1,)),
                           (b.minimize, (BASE_ID + 1,))):
            said = self.acting(call, *args)
            self.assertIn("river 0.4", said, call.__name__)
        # every request really went out: the backend reports the silence, it does not stop asking
        self.assertEqual(comp.opcodes(0), [CLOSE])
        self.assertEqual(comp.opcodes(1), [ACTIVATE, SET_MINIMIZED])

    def test_the_cause_named_is_the_one_that_can_produce_this_silence(self):
        """One sentence for every request was a false attribution in both directions: `windowminimize` on a
        headless sway 1.11 forced to this backend waits the whole timeout and warns, correctly -- sway has no
        minimized state (measured on this guest, 2026-09-08) -- and river's read-only handle has nothing to
        do with it, while a fullscreen that goes nowhere has nothing to do with minimize."""
        _comp, b = self.backend(cls=ReadOnlyToplevels)
        mini = self.acting(b.minimize, BASE_ID + 1)
        self.assertIn("sway and river-classic have no minimized state", mini)
        # and, because minimize is one xdotool has always had, what would close it -- on both compositors
        # the sentence names, not just the one whose gap is closed by a backend we already ship: sway's
        # scratchpad is route 2, and river, which has no IPC to stash a window with, is route 6
        self.assertIn("sway's scratchpad", mini)
        self.assertIn("AGENTS.md route 2", mini)
        self.assertIn("which is river (route 6)", mini)
        with redirect_stderr(io.StringIO()):
            full = b.set_state(BASE_ID, "FULLSCREEN", 1)
        self.assertNotIn("minimiz", full, "fullscreen has no business naming minimize")
        self.assertIn("river 0.4's wlr-foreign-toplevel is read-only", full)

    def test_close_says_the_client_may_be_the_reason_and_never_blames_a_compositor_alone(self):
        """`close` is the one request whose silence has a third cause: the client. A window that answers with
        an unsaved-changes dialog and changes nothing on its own handle is not the compositor ignoring
        anything, and labwc and sway were both measured closing windows [M labwc.md §3]."""
        _comp, b = self.backend(cls=ReadOnlyToplevels)
        said = self.acting(b.close, BASE_ID)
        self.assertIn("the window did not close within 0.5 s", said)
        self.assertIn("the client may be asking to save", said)
        self.assertNotIn("accepted close", said)

    def test_set_state_returns_the_reason_instead_of_warning(self):
        """The `WindowBackend.set_state` contract: a one-line reason for "accepted and not applied", which
        wdotool prints and succeeds on, and wwmctl answers by trying its EWMH route."""
        comp, b = self.backend(cls=ReadOnlyToplevels)
        err = io.StringIO()
        with redirect_stderr(err):
            why = b.set_state(BASE_ID, "FULLSCREEN", 1)
        self.assertEqual(err.getvalue(), "", "set_state has somewhere to return this")
        self.assertEqual(why, "the compositor accepted set_fullscreen and did not apply it "
                              "(river 0.4's wlr-foreign-toplevel is read-only)")
        self.assertEqual(comp.opcodes(0), [SET_FULLSCREEN])

    def test_the_reason_names_the_request_that_was_ignored(self):
        """One window that starts maximized and one that does not, so both directions have something to
        verify: asking for the state a window is already in has nothing to wait for and says nothing."""
        _comp, b = self.backend(cls=ReadOnlyToplevels,
                                toplevels=(top("Big", "alpha", MAXIMIZED), top("Small", "beta")))
        with redirect_stderr(io.StringIO()):
            self.assertIn("unset_maximized", b.set_state(BASE_ID, "MAXIMIZED_VERT", 0))
            self.assertIn("set_minimized", b.set_state(BASE_ID + 1, "HIDDEN", 1))
            self.assertIsNone(b.set_state(BASE_ID + 1, "MAXIMIZED_VERT", 0),
                              "already unmaximized: there is nothing for the compositor to apply")

    def test_a_compositor_that_acts_says_nothing_at_all(self):
        """The default fake is sway/labwc: `wwmctl -c xtermwin` closed the window there and
        `-b add,fullscreen` fullscreened it [M labwc.md §3], so nothing may be printed and set_state
        returns None."""
        _comp, b = self.backend()
        for call, args in ((b.close, (BASE_ID + 2,)), (b.activate, (BASE_ID + 1,)),
                           (b.minimize, (BASE_ID + 1,)), (b.map, (BASE_ID + 1,))):
            self.assertEqual(self.acting(call, *args), "", call.__name__)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(b.set_state(BASE_ID, "FULLSCREEN", 1))
            self.assertIsNone(b.set_state(BASE_ID, "MAXIMIZED_VERT", 1))
            self.assertIsNone(b.set_state(BASE_ID, "HIDDEN", 1))
        self.assertEqual(err.getvalue(), "")

    def test_river_classic_is_told_apart_from_river(self):
        """The two lines of the measured table, on one compositor: close and fullscreen are silent because
        they happen, minimize warns because river-classic has none [M river.md §2a]."""
        _comp, b = self.backend(cls=ClassicToplevels)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(b.set_state(BASE_ID, "FULLSCREEN", 1))
        self.assertEqual(err.getvalue(), "")
        self.assertIn("have no minimized state", self.acting(b.minimize, BASE_ID + 1))
        self.assertEqual(self.acting(b.close, BASE_ID + 1), "")

    def test_close_accepts_any_answer_at_all_not_just_going_away(self):
        """A client may refuse to close and only change its own state; that is an answer, and a warning
        there would fire on every unsaved-changes dialog."""
        _comp, b = self.backend(cls=CloseByMinimizing)
        self.assertEqual(self.acting(b.close, BASE_ID), "")

    def test_the_wait_is_bounded_and_short(self):
        """A silent compositor must cost VERIFY_TIMEOUT once, not a hang and not a retry loop."""
        _comp, b = self.backend(cls=ReadOnlyToplevels)
        start = time.monotonic()
        with redirect_stderr(io.StringIO()):
            b.minimize(BASE_ID)
        spent = time.monotonic() - start
        self.assertGreaterEqual(spent, backend_wlr.VERIFY_TIMEOUT)
        self.assertLess(spent, backend_wlr.VERIFY_TIMEOUT * 3)


class FakeXPlane:
    """A FakeXServer on a private socket dir, and `session` pointed at it.

    A mixin and not a TestCase, so that the classes that need an X plane can be siblings rather than
    subclasses of each other -- inheriting a TestCase would re-run its tests. tests/test_backend_cosmic.py
    imports this one rather than keeping a second copy of the same twenty lines; `DISPLAY_NUM` is what keeps
    two files from racing for one display number when the suite runs them in one process."""

    DISPLAY_NUM = 7
    SOCK_PREFIX = "wlr-x11-"
    #: (xid, instance, class, _NET_WM_NAME, pid, (x, y, w, h))
    CLIENTS = ((0x40000C, "xterm", "XTerm", "xtermwin", 4242, (0, 0, 484, 316)),)

    def x_server(self, clients=None):
        """A FakeXServer publishing `clients` in _NET_CLIENT_LIST, and the session pointed at it."""
        clients = self.CLIENTS if clients is None else clients
        d = tempfile.mkdtemp(prefix=self.SOCK_PREFIX)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        old = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = d
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", old)
        srv = FakeXServer(d, num=self.DISPLAY_NUM)
        self.addCleanup(srv.stop)
        srv.set_prop(FakeXServer.ROOTS[0], "_NET_CLIENT_LIST", "WINDOW", 32,
                     struct.pack("<%dI" % len(clients), *[c[0] for c in clients]))
        for xid, inst, cls, name, pid, geo in clients:
            srv.set_prop(xid, "WM_CLASS", "STRING", 8,
                         inst.encode() + b"\0" + cls.encode() + b"\0")
            srv.set_prop(xid, "_NET_WM_NAME", "UTF8_STRING", 8, name.encode())
            srv.set_prop(xid, "_NET_WM_PID", "CARDINAL", 32, struct.pack("<I", pid))
            srv.geometry[xid] = (0, 0, geo[2], geo[3])
            srv.translate[xid] = (geo[0], geo[1])
        for name, value in (("xwayland_running", lambda uid=None: True),
                            ("find_x_display", lambda uid=None: ":%d" % self.DISPLAY_NUM),
                            ("find_xauthority", lambda uid=None: None)):
            patch = mock.patch.object(session, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return srv


def net_wm_state(view) -> list:
    """The `_NET_WM_STATE` atom names `wxprop -id <native id>` prints for this View.

    The real path and not a paraphrase of it: `_node_from_view` is what wxprop builds out of a views()
    backend's row, and `NativeViewTarget` is the target `resolve_id` hands a window with no X id
    [wxprop/core.py:850]. Two of the flags this backend fills in are only readable here -- `visible` becomes
    `_NET_WM_STATE_HIDDEN` inside `_props`, and nothing else in the tree reads it."""
    node = wxcore._node_from_view(view)
    target = wxcore.NativeViewTarget(None, wxcore.NativeAtoms(), node, view.window)
    _type, _size, wire = target.fetch(b"_NET_WM_STATE")
    return [target.atom_name(a) for a in struct.unpack("<%dI" % (len(wire) // 4), wire)]


class XWaylandIds(FakeXPlane, WlrTest):
    """U10. The XWayland windows of a wlr session, under their real X ids.

    On labwc an xterm whose real X id is `0x40000c` was listed as `0x000f4240` with a *synthesized*
    `WM_CLASS "xterm","xterm"` where `xprop` said `"xterm","XTerm"`, pid 0 and the output rectangle for
    geometry -- and the same on Budgie, Xfce-Wayland, Wayfire and Hyprland. A same-compositor control (one
    headless sway, native backend vs `WDOTOOL_BACKEND=wlr`) turned pid 85920 into 0 and id 5 into 1000000,
    which is what proves it is this backend and not the compositor [M recon2/labwc.md §4].

    The peer is the real fake X server the wwmctl tests use, over a real socket."""

    XTERM = 0x40000C
    TOPLEVELS = (top("xtermwin", "xterm"), top("footwin", "foot"))

    def test_an_xwayland_window_carries_its_real_id_class_pid_and_geometry(self):
        self.x_server()
        _comp, b = self.backend()
        views = b.views()
        self.assertIsNotNone(views, "an X plane is up: views() must fold it in")
        xt, ft = views
        self.assertEqual(xt.xid, self.XTERM)
        self.assertEqual((xt.instance, xt.cls), ("xterm", "XTerm"))
        self.assertEqual(xt.client_type, "x11")
        self.assertEqual(xt.window.pid, 4242)
        self.assertEqual((xt.window.x, xt.window.y, xt.window.w, xt.window.h),
                         (0, 0, 484, 316))
        # the native toplevel is untouched: no X client agrees with `foot`, so it keeps the floor
        self.assertEqual((ft.xid, ft.app_id, ft.window.pid), (0, "foot", 0))
        self.assertEqual((ft.window.w, ft.window.h), (1920, 1080))

    def test_the_ids_are_the_ones_the_x_tools_print(self):
        """The point of the whole exercise: `wwmctl -l` on labwc printed 0x000f4240 for a window `xprop`
        knows as 0x40000c, so the window id a script carries between the two tools was wrong."""
        self.x_server()
        _comp, b = self.backend()
        self.assertEqual([v.xid for v in b.views()], [self.XTERM, 0])
        self.assertNotIn(BASE_ID, [v.xid for v in b.views()])

    def test_two_windows_that_cannot_be_told_apart_keep_xid_zero(self):
        """The KWin rule, with two of its four keys missing: no pid on a toplevel and no geometry, so two
        xterms with the same title tie and a coin flip would hand one of them the other's id."""
        self.x_server(clients=((0x40000C, "xterm", "XTerm", "same", 11, (0, 0, 100, 100)),
                               (0x40000D, "xterm", "XTerm", "same", 12, (0, 0, 100, 100))))
        _comp, b = self.backend(toplevels=(top("same", "xterm"), top("same", "xterm")))
        err = io.StringIO()
        with redirect_stderr(err):
            views = b.views()
        self.assertEqual([v.xid for v in views], [0, 0])
        self.assertIn("could not be told apart", err.getvalue())

    def test_nothing_is_opened_when_no_xwayland_is_running(self):
        """Connecting to an X display is what *starts* Xwayland on sway and Wayfire, so views() has to ask
        the process table first and answer None rather than spawn a server."""
        self.x_server()
        patch = mock.patch.object(session, "xwayland_running", lambda uid=None: False)
        patch.start()
        self.addCleanup(patch.stop)
        _comp, b = self.backend()
        self.assertIsNone(b.views())
        self.assertEqual(b._x, None)

    def test_an_x_plane_that_fails_mid_read_is_closed_and_not_just_dropped(self):
        """views() falls back to the floor listing on any X failure, and the connection it gives up on has to
        go with it: wdotool's daemon and a wwmctl loop both outlive one call, and a dropped reference is a
        leaked fd until the collector gets to it."""
        self.x_server()
        _comp, b = self.backend()
        self.assertIsNotNone(b.views())
        x = b._x
        with mock.patch.object(type(b), "_x_clients", staticmethod(lambda _x: 1 / 0)):
            self.assertIsNone(b.views())
        self.assertIsNone(b._x)
        self.assertIsNone(x._sock, "the X connection was closed, not merely forgotten")

    def test_a_class_that_does_not_agree_is_never_paired(self):
        """`match_xids`'s "a pair must agree on something" rule: an X client whose WM_CLASS matches no
        app_id gets nobody's id, and a native toplevel never claims to be an X11 client."""
        self.x_server(clients=((0x40000C, "gedit", "Gedit", "xtermwin", 9, (0, 0, 10, 10)),))
        _comp, b = self.backend()
        self.assertEqual([(v.xid, v.client_type) for v in b.views()],
                         [(0, "wayland"), (0, "wayland")])


class XPlaneGeometry(FakeXPlane, WlrTest):
    """Route 5: an XWayland window's rectangle is the X server's, a native one's is still the floor.

    `zwlr_foreign_toplevel_management_v1` carries no rectangle, so every window of the wlr floor -- labwc,
    river, Budgie, Xfce-on-Wayland, LXQt-on-Wayland -- was reported at 0,0 with the widest output's mode.
    Measured on the resolute-labwc golden 2026-09-12, one xterm and one foot on a 1920x1080 head:

        $ wdotool getwindowgeometry 1000000      # the xterm
        Window 1000000
          Position: 0,0 (screen: 0)
          Geometry: 1920x1080
        $ xdotool getwindowgeometry 0x40000c     # the same window, the oracle, the same session
        Window 4194316
          Position: 718,395 (screen: 0)
          Geometry: 484x316

    X is the oracle and the X plane is the lowest rung in reach (AGENTS.md route 5), so the xterm's row now
    answers 718,395 484x316 and the foot's stays the floor with `geometry_is_floor` set. The native half is
    NOT YET at rung 1: no foreign-toplevel protocol carries a rectangle yet.

    The fixture is that measurement: the client list carries the xterm at its measured rect and pid, and the
    two toplevels are the two windows that were on that head."""

    XTERM = 0x40000C
    CLIENTS = ((0x40000C, "xterm", "XTerm", "xtermwin", 2045, (718, 395, 484, 316)),)
    TOPLEVELS = (top("xtermwin", "xterm"), top("footwin", "foot"))
    XRECT = (718, 395, 484, 316)
    FLOOR = (0, 0, 1920, 1080)
    #: Two xterms the join cannot tell apart: same class, same default title, and `match_xids` hands out
    #: no id on a tie -- so both keep the floor, and nothing may be said about it on stderr.
    TIED = ((0x40000C, "xterm", "XTerm", "same", 11, (1, 2, 3, 4)),
            (0x40000D, "xterm", "XTerm", "same", 12, (5, 6, 7, 8)))

    def test_an_xwayland_window_is_listed_at_the_x_servers_rectangle(self):
        self.x_server()
        _comp, b = self.backend()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()], [self.XRECT, self.FLOOR])

    def test_find_answers_the_x_rectangle_which_is_what_getwindowgeometry_reads(self):
        """`cmd_getwindowgeometry` goes through `find()` and never through `views()`, so a rectangle folded
        into views() alone would have left the xdotool clone printing the floor."""
        self.x_server()
        _comp, b = self.backend()
        w = b.find(BASE_ID)
        self.assertEqual((w.x, w.y, w.w, w.h), self.XRECT)

    def test_getwindowgeometry_prints_the_x_rectangle_and_not_the_floor(self):
        """The user-visible line, byte for byte, through the real command: the three lines above are what
        the golden printed for the oracle and what it printed for us differed in all four numbers."""
        self.x_server()
        _comp, b = self.backend()
        ctx = Context()
        ctx._backend = b
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.run_chain(ctx, "wdotool", ["getwindowgeometry", str(BASE_ID)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(),
                         "Window %d\n  Position: 718,395 (screen: 0)\n  Geometry: 484x316\n" % BASE_ID)

    def test_a_native_toplevel_keeps_the_floor_and_the_listing_says_so(self):
        """The half route 5 does not reach. The foot window has no X client to ask, so its row is the
        output box -- and `geometry_is_floor` is how a caller tells that rectangle from a measured one."""
        self.x_server()
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual((wins[1].x, wins[1].y, wins[1].w, wins[1].h), self.FLOOR)
        self.assertTrue(b.geometry_is_floor)

    def test_a_listing_whose_windows_all_have_a_rectangle_does_not_claim_the_floor(self):
        self.x_server()
        _comp, b = self.backend(toplevels=(top("xtermwin", "xterm"),))
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()], [self.XRECT])
        self.assertFalse(b.geometry_is_floor)

    def test_an_x_client_that_pairs_with_nobody_hands_out_no_rectangle(self):
        """`match_xids`' rule, from the geometry side: a client whose WM_CLASS agrees with no toplevel is
        not this window's rectangle, and a native row that took it would report where some other client
        happens to sit."""
        self.x_server(clients=((0x40000C, "gedit", "Gedit", "xtermwin", 9, (11, 22, 33, 44)),))
        _comp, b = self.backend()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()], [self.FLOOR, self.FLOOR])

    def test_no_x_plane_at_all_leaves_every_row_on_the_floor(self):
        """sway and Wayfire spawn their Xwayland on demand; asking for one would start it, so a session
        without one keeps the floor listing rather than gaining a server."""
        self.x_server()
        patch = mock.patch.object(session, "xwayland_running", lambda uid=None: False)
        patch.start()
        self.addCleanup(patch.stop)
        _comp, b = self.backend()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()], [self.FLOOR, self.FLOOR])
        self.assertTrue(b.geometry_is_floor)
        self.assertIsNone(b._x)

    def test_an_x_read_that_fails_leaves_a_floor_listing_and_not_an_exception(self):
        """`list()` runs under every window command there is; an X server that goes away mid-read is a
        listing with no rectangles in it, not a traceback out of `wdotool search`."""
        self.x_server()
        _comp, b = self.backend()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()], [self.XRECT, self.FLOOR])
        with mock.patch.object(type(b), "_x_clients", staticmethod(lambda _x: 1 / 0)):
            wins = b.list()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in wins], [self.FLOOR, self.FLOOR])
        self.assertIsNone(b._x, "the connection that failed was dropped, not kept for the next call")

    def test_the_join_is_read_once_per_listing_and_not_once_per_reader(self):
        """`views()` reads the ids out of the listing `list()` has just joined, so the pairing is computed
        on the listing object and reused. Without the memo one `wwmctl -l` would ask the X server for
        `_NET_CLIENT_LIST` and five properties per client twice: measured in-process against a headless
        labwc on this guest 2026-09-12, `list()` costs 0.02 ms with no X plane, 0.10 ms with one X client
        and 0.53 ms with eight."""
        self.x_server()
        _comp, b = self.backend()
        calls = []
        real = type(b)._x_clients

        def counted(x):
            calls.append(x)
            return real(x)

        with mock.patch.object(type(b), "_x_clients", staticmethod(counted)):
            views = b.views()
        self.assertEqual([v.xid for v in views], [self.XTERM, 0])
        self.assertEqual(len(calls), 1)

    def test_two_windows_that_tie_are_warned_about_once_by_the_reader_that_owes_the_line(self):
        """`wwmctl -l` prints the X id column, so a 0 in it is what wants explaining and `views()` is where
        the sentence belongs -- once for the listing, though `list()` and `views()` both ran the join."""
        self.x_server(clients=self.TIED)
        _comp, b = self.backend(toplevels=(top("same", "xterm"), top("same", "xterm")))
        err = io.StringIO()
        with redirect_stderr(err):
            b.views()
        self.assertEqual(err.getvalue().count("could not be told apart"), 1)

    def test_a_tie_says_nothing_on_stderr_under_a_wdotool_command(self):
        """The join runs under `list()` now, and `list()` runs under every window command there is -- so a
        warning inside the join is a line the original never printed. The pinned xdotool 4.20260303.1 writes
        nothing at all on `search` or `getwindowgeometry`, whatever the X client list looks like, and a
        `--sync` loop would have printed ours once per poll. Two xterms under one title is the shape that
        ties, and it is the ordinary shape on this floor."""
        self.x_server(clients=self.TIED)
        _comp, b = self.backend(toplevels=(top("same", "xterm"), top("same", "xterm")))
        ctx = Context()
        ctx._backend = b
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            cli.run_chain(ctx, "wdotool", ["search", "--class", "xterm"])
            cli.run_chain(ctx, "wdotool", ["getwindowgeometry", str(BASE_ID)])
        self.assertEqual(err.getvalue(), "")
        # and the tie is still a tie: neither window took the other's rectangle
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()], [self.FLOOR, self.FLOOR])

    def test_the_wlr_floor_and_the_kwin_path_print_one_sentence_for_a_tie(self):
        """The line moved from the matcher to `views()`, so there are two printers of it now and they must
        print the same bytes: `wwmctl -l` on labwc and `wwmctl -l` on KWin say the same thing about the same
        tie. The literal below is what shipped before this batch (`xid_match.match_xids`, unchanged)."""
        line = ("wdotool: 2 XWayland window(s) could not be told apart from each other in the X client "
                "list; their X ids are left unset\n")
        # The prefix is whatever main() last named the process (wwmctl's CLI tests leave it at
        # "wwmctl" when the whole suite runs in one interpreter, run 2026-09-12); pin it for this test.
        from wdotool import backend as backend_mod
        self.addCleanup(backend_mod.set_program, backend_mod.program())
        backend_mod.set_program("wdotool")
        self.x_server(clients=self.TIED)
        _comp, b = self.backend(toplevels=(top("same", "xterm"), top("same", "xterm")))
        floor = io.StringIO()
        with redirect_stderr(floor):
            views = b.views()
        self.assertEqual(floor.getvalue(), line)
        self.assertEqual([v.xid for v in views], [0, 0], "a tie hands out no id, which is what the line says")
        raw = [{"u": "1", "c": "xterm", "n": "", "t": "same"}, {"u": "2", "c": "xterm", "n": "", "t": "same"}]
        clients = [{"xid": x, "pid": p, "inst": "xterm", "cls": "XTerm", "name": "same", "geo": (1, 2, 3, 4)}
                   for x, p in ((0x40000C, 11), (0x40000D, 12))]
        kwin = io.StringIO()
        with redirect_stderr(kwin):
            self.assertEqual(xid_match.match_xids(raw, clients, None), {})
        self.assertEqual(kwin.getvalue(), line)

    def test_the_pointer_is_hit_tested_against_the_x_rectangle_and_misses_outside_it(self):
        """What the floor cost beyond `getwindowgeometry`: `hit_test` is `getmouselocation`'s `window:`
        field, and a row claiming the whole output is a hit on every pixel of the head
        [vm/live-smoke.d/labwc.sh's note]. With the X rectangle the xterm is under 730,400 and nothing is
        under 10,10 -- which is the truth, and the answer the floor could never give.

        Only the XWayland half moves: a native toplevel still claims the whole head, so a session whose
        windows are all native hit-tests exactly as it did. That is the rung-1 gap, not a fixable one
        here."""
        self.x_server()
        _comp, b = self.backend(toplevels=(top("xtermwin", "xterm"),))
        wins = b.list()
        self.assertEqual(backend.hit_test(wins, 730, 400), BASE_ID)
        self.assertEqual(backend.hit_test(wins, 10, 10), 0)


class XPlanePid(FakeXPlane, WlrTest):
    """The other half of the same route-5 join: `_NET_WM_PID`, and the two commands that were dead without it.

    `zwlr_foreign_toplevel_management_v1` carries no pid, so `list()` handed out 0 for every window and
    `wdotool getwindowpid` on an XWayland window answered `window 1000000 has no pid associated with it`
    while the X plane in the same process knew the number: on the resolute-labwc golden, 2026-09-12,
    `wwmctl -lGpx` -- which reads `views()`, where the pid was already folded -- printed
    `0x0040000c -1 2045 718 395 484 316` for that very window [M goal2/requests-batch-12.md 4]. X answers
    it, so we owe it, and the fixture below is that row: pid 2045 at 718,395 484x316.

    The native half stays at 0 and keeps xdotool's own sentence, which is what the original prints for an X
    window with no `_NET_WM_PID`. NOT YET, rung 1: a foreign-toplevel protocol that carries the pid."""

    XTERM = 0x40000C
    CLIENTS = ((0x40000C, "xterm", "XTerm", "xtermwin", 2045, (718, 395, 484, 316)),)
    TOPLEVELS = (top("xtermwin", "xterm"), top("footwin", "foot"))

    def test_the_listing_carries_the_x_servers_pid_for_the_xwayland_row(self):
        """`list()` and not `views()`: every xdotool command reads the listing, and `getwindowpid` reads
        `find()`, which is `list()`."""
        self.x_server()
        _comp, b = self.backend()
        self.assertEqual([w.pid for w in b.list()], [2045, 0])
        self.assertEqual(b.find(BASE_ID).pid, 2045)

    def test_getwindowpid_prints_the_number_where_it_used_to_refuse(self):
        self.x_server()
        _comp, b = self.backend()
        ctx = Context()
        ctx._backend = b
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.run_chain(ctx, "wdotool", ["getwindowpid", str(BASE_ID)])
        self.assertEqual((rc, out.getvalue()), (0, "2045\n"))

    def test_a_native_toplevel_still_refuses_in_the_originals_own_words(self):
        """The half route 5 does not reach: no X server has heard of the foot window. The sentence is
        xdotool's byte for byte -- the original prints it for an X window with no `_NET_WM_PID`, so parity
        and honesty are the same string here."""
        self.x_server()
        _comp, b = self.backend()
        ctx = Context()
        ctx._backend = b
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.run_chain(ctx, "wdotool", ["getwindowpid", str(BASE_ID + 1)])
        self.assertEqual(rc, 1)
        self.assertEqual(err.getvalue(),
                         "window %d has no pid associated with it.\n" % (BASE_ID + 1))

    def test_windowkill_reaches_the_x_clients_pid(self):
        """`WindowBackend.kill` sends SIGKILL to `find(wid).pid`, so the fold is what turns `windowkill`
        from `no pid for window 1000000` into a signal. The kill itself is faked -- 2045 is the golden's
        pid and not this box's -- and what is asserted is the number the command reached for."""
        self.x_server()
        _comp, b = self.backend()
        with mock.patch("os.kill") as killed:
            b.kill(BASE_ID)
        self.assertEqual(killed.call_args[0][0], 2045)

    def test_windowkill_on_a_native_toplevel_is_still_refused_and_signals_nobody(self):
        self.x_server()
        _comp, b = self.backend()
        with mock.patch("os.kill") as killed:
            with self.assertRaises(CmdError):
                b.kill(BASE_ID + 1)
        self.assertEqual(killed.call_count, 0, "a refusal must not have signalled anything first")

    def test_an_x_client_that_pairs_with_nobody_hands_out_no_pid(self):
        """The same rule the rectangle follows: a client whose WM_CLASS agrees with no toplevel is not this
        window's pid, and a row that took it would name a process that is not the window's."""
        self.x_server(clients=((0x40000C, "gedit", "Gedit", "xtermwin", 9, (11, 22, 33, 44)),))
        _comp, b = self.backend()
        self.assertEqual([w.pid for w in b.list()], [0, 0])


class ViewFlags(FakeXPlane, WlrTest):
    """The state bits the join must not drop.

    `views()` replaces the listing wxprop used to read, and wxprop's fallback for a backend without one was
    `{"visible": w.visible}` -- so a minimized native window on labwc printed `_NET_WM_STATE_HIDDEN` before
    this backend had a views() at all. The protocol carries four state bits and the View has a field for
    each; a row handed over with them at their defaults is a regression, not a floor."""

    XTERM = 0x40000C
    TOPLEVELS = (top("xtermwin", "xterm", MINIMIZED),
                 top("footwin", "foot", FULLSCREEN),
                 top("bigwin", "bar", MAXIMIZED))

    def views(self):
        self.x_server()
        _comp, b = self.backend()
        return {v.window.title: v for v in b.views()}

    def test_a_minimized_toplevel_comes_back_minimized(self):
        v = self.views()["xtermwin"]
        self.assertEqual(v.xid, self.XTERM, "and this one is the XWayland row, which had the same defaults")
        self.assertTrue(v.minimized)
        self.assertFalse(v.window.visible)

    def test_a_fullscreen_toplevel_comes_back_fullscreen(self):
        v = self.views()["footwin"]
        self.assertEqual((v.fullscreen, v.minimized), (True, False))

    def test_a_maximized_toplevel_carries_both_axes(self):
        """The protocol has one all-or-nothing maximize and EWMH has two axes, which is the same mapping
        `set_state` makes in the other direction."""
        v = self.views()["bigwin"]
        self.assertEqual((v.maximized_h, v.maximized_v), (True, True))

    def test_wxprop_prints_the_hidden_and_fullscreen_states(self):
        """The contract that made this a bug: `_node_from_view` reads `minimized`/`hidden` for `visible` and
        `fullscreen` for `fullscreen_mode`, and `_NET_WM_STATE_HIDDEN` comes off that node
        [wxprop/core.py:171-176, 545]."""
        views = self.views()
        self.assertIn("_NET_WM_STATE_HIDDEN", net_wm_state(views["xtermwin"]))
        self.assertEqual(net_wm_state(views["footwin"]), ["_NET_WM_STATE_FULLSCREEN"])
        self.assertEqual(net_wm_state(views["bigwin"]),
                         ["_NET_WM_STATE_MAXIMIZED_HORZ", "_NET_WM_STATE_MAXIMIZED_VERT"])

    def test_a_window_with_nothing_set_prints_no_states(self):
        """The other direction: the flags are read off the compositor's state array and not invented."""
        self.x_server()
        _comp, b = self.backend(toplevels=(top("xtermwin", "xterm"),))
        self.assertEqual(net_wm_state(b.views()[0]), [])


class Capabilities(WlrTest):
    def test_the_refusals_name_the_protocol_that_has_no_request(self):
        """README note (c) explains these four as a tiling compositor's prerogative. labwc is a *stacking*
        compositor and still cannot move a window, because `zwlr_foreign_toplevel_handle_v1` has
        `set_rectangle` -- a minimise-animation hint -- and nothing else [M recon2/labwc.md §6c]. So the
        reason names the protocol."""
        _comp, b = self.backend()
        for call, args, op in ((b.move_window, (BASE_ID, 1, 2), "windowmove"),
                               (b.resize, (BASE_ID, 3, 4), "windowsize"),
                               (b.raise_, (BASE_ID,), "windowraise"),
                               (b.lower, (BASE_ID,), "windowlower")):
            with self.assertRaises(CmdError) as cm:
                call(*args)
            self.assertTrue(getattr(cm.exception, "unsupported", False), op)
            # the protocol clause first, contiguous with the colon, because vm/live-smoke.d/labwc.sh:135
            # and river.sh:176 pin exactly that much of the line -- plan C2's point on the wlroots family
            # is that the reason names the PROTOCOL and not sway's tiling; the NOT-YET and its two routes
            # follow it (AGENTS.md), so a user reading the refusal is told what would fix it
            # `assertEqual` against `"%s ... %s" % (op, NO_GEOMETRY)` would be the format string restated,
            # so the claims are made one at a time: the command names ITSELF (each of the four does, and
            # a shared refusal that said `windowmove` for all four would be the bug), then the two halves
            # of the sentence below
            self.assertTrue(str(cm.exception).startswith(op + " is not supported by the wlr backend: "),
                            str(cm.exception))
            self.assertIn("is not supported by the wlr backend: "
                          "zwlr_foreign_toplevel_management_v1 carries no geometry", str(cm.exception))
            self.assertIn("AGENTS.md route 5", str(cm.exception))

    def test_there_are_no_desktops(self):
        """This compositor advertises no `ext_workspace_manager_v1`, which is sway 1.11's and Wayfire
        0.10's shape [M recon2/labwc.md §2, wayfire.md §1.1] -- there the refusal stands unchanged.
        tests/test_backend_wlr_workspaces.py is the other half, where the global is there."""
        _comp, b = self.backend()
        self.assertIsNone(b.ws)
        self.assertIsNone(b.workspaces())
        for call in (b.get_desktop, b.num_desktops):
            with self.assertRaises(CmdError) as cm:
                call()
            self.assertTrue(getattr(cm.exception, "unsupported", False))
        with self.assertRaises(CmdError) as cm:
            b.set_desktop(1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))

    def test_a_compositor_without_the_protocol_says_which_one(self):
        self.compositor(manager_version=None)
        with self.assertRaises(CmdError) as cm:
            WlrBackend()
        self.assertIn("zwlr_foreign_toplevel_management_unstable_v1",
                      str(cm.exception))

    def test_no_socket_at_all(self):
        with env(WAYLAND_DISPLAY="/nonexistent/wdotool-no-wayland",
                 XDG_RUNTIME_DIR="/nonexistent/wdotool-no-runtime",
                 SWAYSOCK=None, I3SOCK=None):
            with self.assertRaises(CmdError) as cm:
                WlrBackend()
        self.assertIn("no Wayland socket found", str(cm.exception))

    def test_it_binds_the_manager_at_the_version_it_supports(self):
        """A compositor may offer a version newer than the events this
        client knows how to read; binding it would promise to understand
        them."""
        comp, b = self.backend(manager_version=9)
        self.assertEqual(b.mgr_ver, 3)
        self.assertIn((ToplevelCompositor.MANAGER, 3), comp.binds)


if __name__ == "__main__":
    unittest.main()
