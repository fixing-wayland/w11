#!/usr/bin/env python3
"""U28: the COSMIC window backend, on the wire.

cosmic-comp publishes no `zwlr_foreign_toplevel_manager_v1` at all, so before this backend existed every
window command on a COSMIC session answered `no Wayland session found ... the compositor does not offer
wlr-foreign-toplevel` -- rc 2, naming a protocol COSMIC is never going to have [M recon2/cosmic.md §2, §3].
What it does have is `ext_foreign_toplevel_list_v1` for arrival and identity and the two `zcosmic_toplevel_*`
protocols for state and control, and all of it was driven by hand against a live cosmic-comp first
(`recon2/cosmic/cosmic_probe.py`, `cosmic_act.py`, output in `toplevels.txt`) before a line of the backend
was written.

Everything below runs against `wl_fake.CosmicCompositor`, which replays that session: the three toplevels
with their 32-character identifiers, the manager's `capabilities [1,2,3,4,6]` (no 5, no 7), the two
workspaces named `1` and `2` with coordinates `[1]` and `[2]`, and the recorded answers to the three
requests that were actually sent -- `set_maximized` -> `state [0]`, `unset_maximized` -> `[]`, `activate` ->
`[2]`. It deliberately advertises no wlr manager and no virtual pointer, because those absences are half of
what makes a COSMIC session what it is.
"""

import io
import os
import struct
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py. This line is what covers `python3 tests/<file>.py`, and it goes before the
# first tool import so that no module can read the variable's absence on the way in.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common import session
from w11common.errors import CmdError
from support import env, sh_block
from test_backend_wlr import FakeXPlane, net_wm_state
from wdotool import window_cmds
from wdotool.backend import ID_BASE, mint_id
from wdotool.backend_cosmic import ST_ACTIVATED, STATE_WAIT, CosmicBackend

#: the head the nested cosmic-comp published [M recon2/cosmic.md §3: `outputs: WINIT-0 1920x1080+0+0`]
OUT_W, OUT_H = 1920, 1080

#: the recorded manager capability array, and the two entries it does NOT carry
CAPS = wl_fake.COSMIC_CAPABILITIES
CAP_FULLSCREEN, CAP_STICKY = 5, 7


#: a toplevel that opens after the listing did: the identifier is the same 32 base62 characters
#: cosmic-comp mints [M recon2/cosmic.md §4], with a word in it so a failure names which window it is.
LATE = ("LateW1nd0wQe8NowEoh7IP065Bbw8xd6", "latecomer", "foot")


class Cosmic(wl_fake.CosmicCompositor):
    """The shared COSMIC fake with its output answering a mode event.

    The recorded registry has `wl_output` in it and the shared fake binds it without saying anything, which
    is right for a registry replay. The rectangle a *geometry-less* listing falls back to is this backend's
    business, so the mode that names it is sent here.

    Every byte this fake writes goes out under one lock. `Server._send` is a bare `sendall`
    [tests/wl_fake.py:258] and two of the fakes below write from a thread that is not the serving one --
    SlowRefresh from a `threading.Timer`, `open_late` from the test's own -- so without it two messages
    could interleave inside one frame and come back as a `struct.error` under load. wl_fake.py belongs to
    another batch this wave, so the lock lives here."""

    PREFIX = "wdotool-cosmic-"

    def __init__(self, *a, **kw):
        self.wire = threading.Lock()
        super().__init__(*a, **kw)

    def _send(self, conn, oid, opcode, body=b""):
        with self.wire:
            super()._send(conn, oid, opcode, body)

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_output":
            # mode(flags=current, width, height, refresh), then done
            self._send(conn, new_id, 1, struct.pack("<Iiii", 1, OUT_W, OUT_H, 60000))
            self._send(conn, new_id, 2)
            return
        super().on_bind(conn, state, name, iface, version, new_id)


class NoSeat(Cosmic):
    """A COSMIC session with no `wl_seat`. `activate` takes one and the protocol does not allow a null."""

    def advertise(self):
        return [g for g in super().advertise() if g[0] != "wl_seat"]


class NoWorkspaceProtocol(Cosmic):
    """A COSMIC session with no `ext_workspace_manager_v1`. Not a shape cosmic-comp has ever had -- it is
    in all 53 of the recorded globals -- but the backend must not assume a global it did not find."""

    def advertise(self):
        return [g for g in super().advertise() if g[0] != wl_fake.WS_MANAGER]


class Sandboxed(Cosmic):
    """What a sandboxed client sees: cosmic-comp builds the toplevel list, the info and the manager behind
    `client_not_sandboxed` [R recon2/cosmic/state.rs:647, 748, 749], so a client the filter rejects never
    gets the globals announced at all. The session is otherwise the recorded one."""

    def advertise(self):
        gone = (wl_fake.EXT_TOPLEVEL_LIST, wl_fake.COSMIC_INFO, wl_fake.COSMIC_MGR)
        return [g for g in super().advertise() if g[0] not in gone]


class NoManager(Cosmic):
    """The half-sandboxed shape: the read side is announced, the write side is not, which is exactly what
    the filter would do if only `toplevel_management_state` grew a stricter one."""

    def advertise(self):
        return [g for g in super().advertise() if g[0] != wl_fake.COSMIC_MGR]


class ExtWorkspaceEnter(Cosmic):
    """cosmic-comp's own shape for a v3 client: `ext_workspace_enter` (opcode 10), one workspace per window.

    The shared fake sends the deprecated `workspace_enter` (opcode 6) with the first workspace for every
    toplevel, which is what a v<3 client -- one holding `zcosmic_workspace_handle_v1`s -- would get.
    cosmic-comp branches on the bound version and sends the ext pair to a v3 client, because that is the only
    handle such a client holds [R recon2/cosmic/tlinfo.rs:639-640, `raw_ext_workspace_handles` ->
    `instance.ext_workspace_enter`]. A backend that read only opcode 6 would pass every other test in this
    file and report no desktop at all on a real COSMIC.

    `WHERE` is one workspace index per toplevel row; an index in `LEAVE` also gets the matching
    `ext_workspace_leave` straight after, which is what a window being dragged off a workspace looks like."""

    WHERE = (0, 1, 1)
    LEAVE = ()

    EXT_ENTER, EXT_LEAVE = 10, 11

    def _send_cosmic_state(self, conn, rec):
        ids, self.ws_ids = self.ws_ids, []   # no deprecated workspace_enter from the base
        try:
            super()._send_cosmic_state(conn, rec)
            i = list(self.tops).index(rec.ext)
            oid = ids[self.WHERE[i]]
            self._send(conn, rec.cosmic, self.EXT_ENTER, struct.pack("<I", oid))
            if i in self.LEAVE:
                self._send(conn, rec.cosmic, self.EXT_LEAVE, struct.pack("<I", oid))
            self._send(conn, rec.cosmic, 1)   # done
        finally:
            self.ws_ids = ids


class SharedState(Cosmic):
    """A COSMIC whose window state outlives the client that asked for it.

    The shared fake builds a fresh `_CosmicTop` per connection, which is right for the one-client transcript
    replay it was written for and wrong for every pair the smoke actually runs: `wdotool windowactivate` and
    `wdotool getactivewindow` are two processes and therefore two connections, and on a real compositor the
    state belongs to the window and not to whoever is watching. One list object per `identifier`, shared by
    every connection's record, is that."""

    def __init__(self, *a, **kw):
        self.kept = {}     # identifier -> the one states list every connection's record points at
        super().__init__(*a, **kw)

    def _announce(self, conn, list_oid):
        super()._announce(conn, list_oid)
        for rec in self.tops.values():
            rec.states = self.kept.setdefault(rec.identifier, rec.states)


class SlowRefresh(SharedState):
    """cosmic-comp's real timing: nothing comes back with `get_cosmic_toplevel`.

    The request arm creates the handle and sends not one event
    [R recon2/cosmic/src-cosmic-comp/src/wayland/protocols/toplevel_info.rs:186-215]; `state`, `geometry`
    and the workspace enters are sent by `ToplevelInfoState::refresh`, which the event loop calls after
    dispatching clients -- and `fn refresh` (src/lib.rs:340-367) SKIPS a refresh that would follow the last
    one by under 150 ms, arming a timer instead. A `wl_display.sync` is answered inside the dispatch, so a
    roundtrip comes back long before any of it. Measured on the fedora44-cosmic golden (cosmic-comp
    1.8.0-1.fc44, `rpm -q` in the transcript) on 2026-09-11: the roundtrip after the
    `get_cosmic_toplevel`s returned in 0.1 ms with every state array still absent, and every one of them
    landed 152.1 ms later -- 153.2, 151.9 and 152.8 ms on three re-runs (the probe that reads that off the
    wire and its output: `goal2/recon/cosmic-state-probe.py`, `goal2/recon/cosmic-state-probe.txt`).

    The shared fake answers instantly, which is the one thing about it a live cosmic-comp does not do. DELAY
    is that gap; the four checks CI measured red on both COSMIC flavors are what a client reads without it.
    """

    #: seconds, the measured 152.1 ms rounded down to something a test can wait out twice inside STATE_WAIT
    DELAY = 0.15

    def __init__(self, *a, **kw):
        self.timers = []
        super().__init__(*a, **kw)

    def _defer(self, fn, *a):
        t = threading.Timer(self.DELAY, fn, args=a)
        t.daemon = True
        self.timers.append(t)
        t.start()

    def _send_cosmic_state(self, conn, rec):
        self._defer(super()._send_cosmic_state, conn, rec)

    def close(self):
        for t in self.timers:
            t.cancel()
        super().close()


class NeverRefresh(Cosmic):
    """A session whose `state` never arrives at all -- the empty handle cosmic-comp hands a client whose
    `ext_foreign_toplevel_handle_v1` it could not resolve [R protocols/toplevel_info.rs:213-219]. Nothing
    may hang on it: the listing is still a listing, with the states it never heard about left empty."""

    def _send_cosmic_state(self, conn, rec):
        pass


class LateWindow(SlowRefresh):
    """A session that answers for a window opened LATER and for none of the ones it started with.

    Both halves are real: the empty handle above is the one cosmic-comp could not resolve, and a toplevel
    announced after the client attached is every `foot` the smoke starts. The pair is here because the
    give-up `_await_state` records is per HANDLE -- a client that armed one deadline per process would
    list the new window with no state at all for the rest of that process's life, which on a
    `wdotool search --sync` loop is the whole loop."""

    def _send_cosmic_state(self, conn, rec):
        if rec.identifier == LATE[0]:
            super()._send_cosmic_state(conn, rec)     # SlowRefresh's timer: DELAY late

    def _announce(self, conn, list_oid):
        self.conn = conn
        super()._announce(conn, list_oid)

    def open_late(self, row, states=()):
        """Announce one more toplevel on the live connection, the way a window opening does.

        `states` is the array its (deferred) `state` event will carry, put in `kept` before the record is
        built so SharedState hands the record that list."""
        self.kept[row[0]] = list(states)
        rows, self.rows = self.rows, (row,)
        try:
            self._announce(self.conn, self.list_oid)
        finally:
            self.rows = rows


#: ext_workspace_manager_v1 / group / handle opcodes, off the XML wdotool/ext_workspace.py documents. The
#: shared WorkspaceServer keeps its own copies private, and TwoGroupCosmic sends a shape it has no
#: parameter for, so they are written out here the way ExtWorkspaceEnter writes out its two.
_WSM_EV_GROUP, _WSM_EV_WORKSPACE, _WSM_EV_DONE = 0, 1, 2
_WSG_EV_CAPABILITIES, _WSG_EV_WORKSPACE_ENTER = 0, 3
_WS_EV_NAME, _WS_EV_COORDINATES, _WS_EV_STATE, _WS_EV_CAPABILITIES = 1, 2, 3, 4
_WS_REQ_ACTIVATE = 1
_WSM_REQ_COMMIT = 0
#: ext_workspace_handle_v1.state and .workspace_capabilities
_WS_ACTIVE, _WS_CAP_ACTIVATE = 1, 1


class TwoGroupCosmic(Cosmic):
    """One `ext_workspace_group_handle_v1` PER OUTPUT, which is what a COSMIC session with two heads is.

    Measured on the fedora44-cosmic golden (cosmic-comp 1.8.0-1.fc44, `vmctl start --heads 2`,
    2026-09-11): groups
    4278190084 and 4278190087, the first carrying workspaces `1` (coordinates [1]) and `2` ([2]) and the
    second `1` ([1]), with BOTH ones active -- one current workspace per head, both with capability 1.

    The shared `WorkspaceServer` publishes ONE group, which is labwc's and Budgie's shape, so the second is
    built here. `activate` is answered the way cosmic-comp answers it: the workspace's own group switches to
    it and the other group is left alone [R handlers/workspace.rs `commit_requests`: `shell.activate(&output,
    idx, ...)` on the output that owns the handle], and the new `state` events go out on the commit, because
    that is the request that applies the batch.
    """

    #: (group index, name, coordinates, active) in announcement order
    ROWS = ((0, "1", (1,), True), (0, "2", (2,), False), (1, "1", (1,), True))

    def __init__(self, *a, **kw):
        self.groups = []          # group oid per group index
        self.rows = ()            # set in _ws_announce -- mutable copies of ROWS
        super().__init__(*a, **kw)

    def _ws_announce(self, conn, mgr_id):
        self.ws_mgr = mgr_id
        self.rows = [list(r) for r in self.ROWS]
        oid = 0xFF000000
        self.groups = []
        for _g in sorted({r[0] for r in self.rows}):
            self.groups.append(oid)
            self._send(conn, mgr_id, _WSM_EV_GROUP, struct.pack("<I", oid))
            self._send(conn, oid, _WSG_EV_CAPABILITIES, struct.pack("<I", 0))
            oid += 1
        self.ws_ids = []
        for group, name, coords, active in self.rows:
            self.ws_ids.append(oid)
            self._send(conn, mgr_id, _WSM_EV_WORKSPACE, struct.pack("<I", oid))
            self._send(conn, self.groups[group], _WSG_EV_WORKSPACE_ENTER, struct.pack("<I", oid))
            self._send(conn, oid, _WS_EV_NAME, wl_fake.wstr(name))
            arr = struct.pack("<%dI" % len(coords), *coords)
            self._send(conn, oid, _WS_EV_COORDINATES,
                       struct.pack("<I", len(arr)) + arr + b"\0" * wl_fake.pad(len(arr)))
            self._send(conn, oid, _WS_EV_STATE, struct.pack("<I", _WS_ACTIVE if active else 0))
            self._send(conn, oid, _WS_EV_CAPABILITIES, struct.pack("<I", _WS_CAP_ACTIVATE))
            oid += 1
        self._send(conn, mgr_id, _WSM_EV_DONE)

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid in self.ws_ids and opcode == _WS_REQ_ACTIVATE:
            self.ws_calls.append(("activate", self.ws_ids.index(oid)))
            self.pending = self.ws_ids.index(oid)
            return
        if oid == self.ws_mgr and opcode == _WSM_REQ_COMMIT:
            self.ws_calls.append(("commit", None))
            want = getattr(self, "pending", None)
            self.pending = None
            if want is not None:
                group = self.rows[want][0]
                for i, row in enumerate(self.rows):
                    if row[0] == group:
                        row[3] = i == want
                for ws_oid, row in zip(self.ws_ids, self.rows):
                    self._send(conn, ws_oid, _WS_EV_STATE,
                               struct.pack("<I", _WS_ACTIVE if row[3] else 0))
                self._send(conn, self.ws_mgr, _WSM_EV_DONE)
            return
        super().on_request(conn, state, oid, opcode, body, fds)


class ActiveOnTheSecondHead(TwoGroupCosmic):
    """Two groups whose desktop numbers the grouped and the flat order disagree about.

    Same shape as the golden's -- a group per output, an active workspace in each -- with the first head
    carrying three workspaces instead of two, so the active one there (`3`, coordinates [3]) sorts BEHIND
    the other head's `1` (coordinates [1]) in `WorkspaceClient._live()`'s flat order. Grouped it is desktop
    2; flat it would be desktop 1, and `wmctrl -s 1` would move the wrong head."""

    ROWS = ((0, "1", (1,), False), (0, "2", (2,), False), (0, "3", (3,), True), (1, "1", (1,), True))


class CosmicTest(unittest.TestCase):
    def setUp(self):
        # `xid_match` warns on a tie it cannot break, which is XWaylandIds's business and noise in the
        # other thirty tests: capture stderr here and let the tests that care read it back. Geometry asserts
        # this buffer is EMPTY, which is the point -- the geometry fallback used to print on every listing.
        self.err = io.StringIO()
        ctx = redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def compositor(self, cls=Cosmic, **kw):
        comp = cls(**kw)
        self.addCleanup(comp.close)
        ctx = env(XDG_RUNTIME_DIR=comp.dir,
                  WAYLAND_DISPLAY=os.path.basename(comp.path),
                  SWAYSOCK=None, I3SOCK=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return comp

    def backend(self, cls=Cosmic, **kw):
        comp = self.compositor(cls, **kw)
        return comp, self.connect()

    def connect(self):
        b = CosmicBackend()
        self.addCleanup(b.c.close)
        self.addCleanup(lambda: b._x.close() if b._x not in (None, "unset") else None)
        return b

    def wid(self, b, title):
        for w in b.list():
            if w.title == title:
                return w.id
        raise AssertionError("no window titled %r" % title)


class Listing(CosmicTest):
    def test_a_session_that_announces_neither_toplevel_global_says_what_would_change_that(self):
        """The constructor's own precondition. `no Wayland session found ... the compositor does not offer
        wlr-foreign-toplevel` is what every COSMIC session answered before this backend existed
        (vm/live-smoke.d/cosmic.sh:14); this one keeps that head and stops being a full stop, because the
        commonest way to meet it is to be the sandboxed client rather than to be on a compositor without
        the protocol -- cosmic-comp builds both behind `client_not_sandboxed`
        [R recon2/cosmic/state.rs:647, 748]."""
        self.compositor(Sandboxed)
        with self.assertRaises(CmdError) as cm:
            CosmicBackend()
        said = str(cm.exception)
        self.assertTrue(said.startswith("cosmic backend: compositor does not offer "
                                        "ext_foreign_toplevel_list_v1 and zcosmic_toplevel_info_v1"), said)
        self.assertIn("hides them from a sandboxed client", said)
        self.assertIn("AGENTS.md route 1", said)
        self.assertIn("(route 2)", said, "and the compositor that really has neither gets its own rung")

    def test_ids_are_minted_from_the_identifier(self):
        """`ext_foreign_toplevel_handle_v1.identifier` is 32 base62 characters and is the only handle the
        protocol carries -- no pid, no X id, no number [M recon2/cosmic.md §4]. So the id is minted from it:
        `ID_BASE | 30 bits of blake2b`, i.e. at or ABOVE 0x40000000, which keeps it out of the range
        Xwayland gives its own clients ((client << 21) | serial, far below 2^30) and 32-bit clean for
        everything downstream."""
        _comp, b = self.backend()
        wins = b.list()
        want = [mint_id(ident) for ident, _t, _a in wl_fake.COSMIC_TOPLEVELS]
        self.assertEqual([w.id for w in wins], want)
        for w in wins:
            self.assertGreaterEqual(w.id, ID_BASE)

    def test_the_ids_are_the_same_in_a_second_process(self):
        """The property the wlr floor's arrival-order ids do not have: there, a window's id changed when a
        neighbour closed [M recon2/hyprland.md §3]. Two backends over one compositor stand in for two
        processes reading one session."""
        _comp, b1 = self.backend()
        b2 = self.connect()
        self.assertEqual([w.id for w in b1.list()], [w.id for w in b2.list()])

    def test_title_class_and_the_pid_that_does_not_exist(self):
        _comp, b = self.backend()
        wins = b.list()
        self.assertEqual([w.title for w in wins], ["cosmicterm", "typedtest", "cosmicxterm"])
        self.assertEqual([w.class_ for w in wins], ["foot", "foot", "XTerm"])
        self.assertEqual([w.pid for w in wins], [0, 0, 0],
                         "neither toplevel protocol carries a pid")

    def test_kill_says_there_is_no_pid(self):
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        with self.assertRaises(CmdError) as cm:
            b.kill(wid)
        self.assertEqual(str(cm.exception), "no pid for window %d" % wid)

    def test_a_closed_toplevel_leaves_the_listing(self):
        comp, b = self.backend()
        wid = self.wid(b, "typedtest")
        b.close(wid)
        self.assertEqual([w.title for w in b.list()], ["cosmicterm", "cosmicxterm"])
        self.assertEqual(comp.calls[-1][0], "close")
        with self.assertRaises(CmdError) as cm:
            b.close(wid)
        self.assertEqual(str(cm.exception), "window %d not found" % wid)

    def test_the_compositor_without_the_protocols_says_which_ones(self):
        """A labwc registry has neither, and the refusal has to name what is missing rather than repeat the
        wlr backend's sentence."""
        self.compositor(cls=wl_fake.RegistryServer, fixture="labwc")
        with self.assertRaises(CmdError) as cm:
            CosmicBackend()
        self.assertIn("ext_foreign_toplevel_list_v1", str(cm.exception))
        self.assertIn("zcosmic_toplevel_info_v1", str(cm.exception))

    def test_no_socket_at_all(self):
        with env(WAYLAND_DISPLAY="/nonexistent/wdotool-no-wayland",
                 XDG_RUNTIME_DIR="/nonexistent/wdotool-no-runtime",
                 SWAYSOCK=None, I3SOCK=None):
            with self.assertRaises(CmdError) as cm:
                CosmicBackend()
        self.assertIn("no Wayland socket found", str(cm.exception))


class TheStateArray(CosmicTest):
    def test_the_recorded_transcript_replays_step_by_step(self):
        """`cosmic_act.py`'s three requests and the three answers it got: before `{'state': []}`, after
        set_maximized `[0]`, after unset_maximized `[]`, after activate `[2]` [M cosmic.md §4].

        Read back off the backend and not off the compositor's own bookkeeping: what is under test is that
        this client re-reads the `state` array the compositor sends after each request, which is the only
        answer either protocol has. `Window` has no maximize field, so the two maximize steps are asked for
        as *toggles*: the second one can only come out as `unset_maximized` if the backend read `[0]` back
        off the wire. The states a listing does show -- ACTIVATED here, MINIMIZED next door -- are read from
        the backend, and the flags a listing cannot show are ViewFlags's half."""
        comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        self.assertEqual([w.focused for w in b.list()], [False, False, False])
        b.set_state(wid, "MAXIMIZED_VERT", 2)
        b.set_state(wid, "MAXIMIZED_VERT", 2)
        b.activate(wid)
        self.assertEqual([(w.title, w.focused) for w in b.list()],
                         [("cosmicterm", True), ("typedtest", False), ("cosmicxterm", False)])
        self.assertEqual([c[0] for c in comp.calls],
                         ["set_maximized", "unset_maximized", "activate"])

    def test_minimized_is_not_visible(self):
        _comp, b = self.backend()
        wid = self.wid(b, "typedtest")
        b.minimize(wid)
        self.assertEqual([(w.title, w.visible) for w in b.list()],
                         [("cosmicterm", True), ("typedtest", False), ("cosmicxterm", True)])
        b.map(wid)
        self.assertTrue(all(w.visible for w in b.list()))

    def test_both_maximize_axes_are_the_one_maximize_this_protocol_has(self):
        comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        b.set_state(wid, "MAXIMIZED_VERT", 1)
        b.set_state(wid, "MAXIMIZED_HORZ", 1)
        b.set_state(wid, "MAXIMIZED_VERT", 0)
        self.assertEqual([c[0] for c in comp.calls],
                         ["set_maximized", "set_maximized", "unset_maximized"])

    def test_activate_passes_the_seat_and_refuses_without_one(self):
        comp, b = self.backend()
        b.activate(self.wid(b, "cosmicterm"))
        name, ids = comp.calls[-1]
        self.assertEqual(name, "activate")
        self.assertEqual(ids[1], b.seat, "activate(toplevel, seat)")

        comp2, b2 = self.backend(cls=NoSeat)
        self.assertIsNone(b2.seat)
        with self.assertRaises(CmdError) as cm:
            b2.activate(self.wid(b2, "cosmicterm"))
        self.assertIn("offers no wl_seat", str(cm.exception))
        self.assertEqual(comp2.calls, [], "no null seat may go on the wire")


class Capabilities(CosmicTest):
    def test_fullscreen_is_gated_on_the_advertised_array(self):
        """Decision C5.15. `set_fullscreen` did work on the live compositor even though 5 is not in
        `[1,2,3,4,6]`, but a client that ignores the capability array is a client that will be wrong the
        first time the array is right -- so the array decides, and the refusal names the capability, then
        what would close the gap: the array is built in cosmic-comp itself, so route 6 is the lowest rung
        that reaches it."""
        comp, b = self.backend()
        self.assertNotIn(CAP_FULLSCREEN, CAPS)
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "FULLSCREEN", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(str(cm.exception),
                         "windowstate FULLSCREEN is not supported by the cosmic backend: "
                         "cosmic-comp does not advertise the fullscreen capability; not yet here, and "
                         "the route is a patched cosmic-comp (AGENTS.md route 6), which is where that "
                         "array is built [R recon2/cosmic/state.rs:752-756]")
        self.assertEqual(comp.calls, [])

    def test_a_missing_manager_names_the_sandbox_filter_and_not_just_the_absence(self):
        """The write side gone is not a compositor without the protocol: cosmic-comp has it and hides it
        from a client `client_not_sandboxed` rejects [R recon2/cosmic/state.rs:749]. So the refusal keeps
        its old head -- `offers no <iface>; cannot <op>` -- and owes the rest: run unsandboxed and the
        protocol that is already there answers. Rung 1, because no code of ours is missing."""
        _comp, b = self.backend(cls=NoManager)
        with self.assertRaises(CmdError) as cm:
            b.close(self.wid(b, "cosmicterm"))
        said = str(cm.exception)
        self.assertTrue(said.startswith("cosmic backend: compositor offers no zcosmic_toplevel_manager_v1; "
                                        "cannot windowclose"), said)
        self.assertIn("not yet here", said)
        self.assertIn("AGENTS.md route 1", said)
        self.assertIn("sandbox filter", said)

    def test_a_compositor_that_does_advertise_it_gets_the_request(self):
        comp, b = self.backend(capabilities=CAPS + (CAP_FULLSCREEN,))
        wid = self.wid(b, "cosmicterm")
        self.assertIsNone(b.set_state(wid, "FULLSCREEN", 1))
        name, ids = comp.calls[-1]
        self.assertEqual(name, "set_fullscreen")
        self.assertEqual(ids[1], 0, "a null output means wherever the window is")
        b.set_state(wid, "FULLSCREEN", 2)
        self.assertEqual(comp.calls[-1][0], "unset_fullscreen",
                         "the toggle reads the state the compositor sent back")

    def test_sticky_is_gated_the_same_way(self):
        _comp, b = self.backend()
        self.assertNotIn(CAP_STICKY, CAPS)
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "STICKY", 1)
        self.assertIn("sticky capability", str(cm.exception))

    def test_close_is_gated_too(self):
        comp, b = self.backend(capabilities=())
        with self.assertRaises(CmdError) as cm:
            b.close(self.wid(b, "cosmicterm"))
        self.assertIn("close capability", str(cm.exception))
        self.assertEqual(comp.calls, [])

    def test_move_resize_raise_and_lower_name_the_protocol(self):
        """There is no request behind any of them: the manager has `set_rectangle`, a minimise-animation
        hint, and nothing else [M cosmic.md §5.2]."""
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")
        for call, args, op in ((b.move_window, (wid, 1, 2), "windowmove"),
                               (b.resize, (wid, 3, 4), "windowsize"),
                               (b.raise_, (wid,), "windowraise"),
                               (b.lower, (wid,), "windowlower")):
            with self.assertRaises(CmdError) as cm:
                call(*args)
            self.assertTrue(getattr(cm.exception, "unsupported", False), op)
            # vm/live-smoke.d/cosmic.sh:160 matches the prefix alone, so the sentence after the colon is
            # free to say what AGENTS.md asks a refusal to say: the lack, then NOT YET and the route
            # not `assertEqual` against the format string this line is built from, which would restate it:
            # the command names itself, and the two halves of the sentence are asserted below
            self.assertTrue(str(cm.exception).startswith(op + " is not supported by the cosmic backend: "),
                            str(cm.exception))
            self.assertIn("is not supported by the cosmic backend: the COSMIC toplevel protocol has "
                          "no move, resize, raise or lower", str(cm.exception))
            self.assertIn("AGENTS.md route 6", str(cm.exception))

    def test_a_state_this_protocol_has_no_word_for(self):
        """SHADED is one wmctrl and xdotool both take, so the refusal owes it a route and not a full stop:
        the handle's state array has five members and a sixth is cosmic-comp's to add (route 6)."""
        _comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "SHADED", 1)
        self.assertEqual(str(cm.exception),
                         "windowstate SHADED is not supported by the cosmic backend: the COSMIC toplevel "
                         "protocol carries maximized, minimized, activated, fullscreen and sticky and no "
                         "other state; not yet here, and the route is a patched cosmic-comp "
                         "(AGENTS.md route 6), one state member and one request each")

    def test_a_version_gap_is_told_apart_from_a_missing_feature(self):
        """`set_sticky` is version 3 of a protocol this session speaks at 2: nothing has to be written for
        it, a newer cosmic-comp already has it, and the refusal has to say so rather than read like the
        capability refusal above."""
        _comp, b = self.backend(capabilities=CAPS + (CAP_STICKY,))
        # the fake in tests/wl_fake.py advertises the manager at 4 and belongs to another batch, so the
        # negotiated version is moved here instead: `mgr_ver` is the field the bind above wrote.
        b.mgr_ver = 2
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "STICKY", 1)
        msg = str(cm.exception)
        self.assertIn("zcosmic_toplevel_manager_v1 is version 2 and set_sticky arrived in version 3", msg)
        self.assertIn("AGENTS.md route 1", msg)
        self.assertIn("not yet here", msg)


class Geometry(CosmicTest):
    def test_a_geometry_event_fills_the_rectangle(self):
        _comp, b = self.backend(geometry=(40, 50, 640, 480))
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b.list()],
                         [(40, 50, 640, 480)] * 3)

    def test_without_one_the_output_rectangle_is_reported_and_nothing_is_printed(self):
        """No `geometry` event ever arrived in the nested rig -- not in 4 s after `get_cosmic_toplevel` and
        not after a maximize -- because cosmic-comp sends it only alongside `output_enter` or on a change
        [M cosmic.md §4, R toplevel_info.rs]. So the fallback is the normal case on the only COSMIC ever
        measured, and `list()` is what every window command runs: a line on stderr there is a line on every
        `wdotool search`, every `getactivewindow` and every `wwmctl -l`. The flag is how a caller that wants
        to say so finds out, the way the wlr floor reports the same rectangle silently."""
        _comp, b = self.backend()
        self.assertFalse(b.geometry_is_floor, "nothing has been listed yet")
        first = b.list()
        b.list()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in first], [(0, 0, OUT_W, OUT_H)] * 3)
        self.assertTrue(b.geometry_is_floor)
        self.assertEqual(self.err.getvalue(), "")

    def test_a_geometry_event_leaves_the_flag_alone(self):
        _comp, b = self.backend(geometry=(40, 50, 640, 480))
        b.list()
        self.assertFalse(b.geometry_is_floor)

    def test_display_size_is_the_output_mode(self):
        _comp, b = self.backend()
        self.assertEqual(b.display_size(), (OUT_W, OUT_H))


class Workspaces(CosmicTest):
    def test_the_two_recorded_workspaces_in_coordinate_order(self):
        """`ws {'name': '1', 'coords': [1], 'caps': 1}` and `'2'` with `[2]` [M cosmic.md §4]."""
        _comp, b = self.backend()
        self.assertEqual(b.num_desktops(), 2)
        self.assertEqual([w.name for w in b.workspaces()], ["1", "2"])

    def test_a_window_reports_the_workspace_it_entered(self):
        """`workspace_enter` names a workspace by object id, and the id is one of the handles the
        ext-workspace client already holds -- which is how the desktop column gets a number at all on a
        protocol family where the wlr floor leaves it -1."""
        _comp, b = self.backend()
        self.assertEqual([w.desktop for w in b.list()], [0, 0, 0])

    def test_the_workspace_a_window_entered_is_the_one_it_reports(self):
        """Not index 0 for everything: the oid the enter names is looked up in the same coordinate-ordered
        list `wwmctl -d` counts, so a window on the second workspace has to come back as desktop 1."""
        _comp, b = self.backend(cls=ExtWorkspaceEnter)
        self.assertEqual([w.desktop for w in b.list()], [0, 1, 1])

    def test_the_ext_spelling_of_the_enter_is_the_one_cosmic_comp_sends(self):
        """v3 renamed the pair. A client that binds `zcosmic_toplevel_info_v1` v3 holds no
        `zcosmic_workspace_handle_v1` at all, so cosmic-comp sends it `ext_workspace_enter` (opcode 10) and
        never the deprecated opcode 6 [R tlinfo.rs:639-640]. Both are read, and this is the one that runs on
        a real session."""
        comp, b = self.backend(cls=ExtWorkspaceEnter)
        self.assertEqual(b.info_ver, 3)
        self.assertEqual([w.desktop for w in b.list()][1], 1)
        self.assertEqual(comp.ws_ids[1], b._ws_handles()[1],
                         "the handle the enter named is the ext-workspace client's own")

    def test_a_window_that_left_its_workspace_has_none(self):
        class Leaves(ExtWorkspaceEnter):
            LEAVE = (1,)

        _comp, b = self.backend(cls=Leaves)
        self.assertEqual([w.desktop for w in b.list()], [0, -1, 1])

    def test_an_empty_workspace_manager_is_not_a_missing_one(self):
        """The protocol is there and says there are no workspaces: that is an answer (-1, 0), not a
        capability gap, and a window with no `workspace_enter` has no desktop of its own."""
        _comp, b = self.backend(workspaces=())
        self.assertEqual([w.desktop for w in b.list()], [-1, -1, -1])
        self.assertEqual((b.get_desktop(), b.num_desktops()), (-1, 0))

    def test_no_workspace_protocol_at_all_is_the_capability_refusal(self):
        _comp, b = self.backend(cls=NoWorkspaceProtocol)
        self.assertIsNone(b.ws)
        self.assertIsNone(b.workspaces())
        for call in (b.get_desktop, b.num_desktops):
            with self.assertRaises(CmdError) as cm:
                call()
            self.assertTrue(getattr(cm.exception, "unsupported", False))
        with self.assertRaises(CmdError) as cm:
            b.set_window_desktop(self.wid(b, "cosmicterm"), 0)
        self.assertTrue(getattr(cm.exception, "unsupported", False))

    def test_set_desktop_is_activate_then_commit(self):
        comp, b = self.backend()
        b.set_desktop(1)
        self.assertEqual(comp.ws_calls, [("activate", 1), ("commit", None)])
        comp.set_active(1)
        self.assertEqual(b.get_desktop(), 1)

    def test_moving_a_window_uses_the_request_that_works(self):
        """cosmic-comp's handler for the deprecated `move_to_workspace` is an empty arm, while
        `MoveToExtWorkspace` resolves the handle and moves the window [R recon2/cosmic/tlmgmt.rs:251-263];
        its capability array still says 6 and not 8 [R state.rs:752-756]. So the gate is 6 and the request
        is opcode 13."""
        comp, b = self.backend()
        b.set_window_desktop(self.wid(b, "cosmicterm"), 1)
        name, ids = comp.calls[-1]
        self.assertEqual(name, "move_to_ext_workspace")
        self.assertEqual(ids[1], comp.ws_ids[1], "the second workspace's own handle")
        self.assertEqual(len(ids), 3, "toplevel, workspace, output")

    def test_a_workspace_that_is_not_there(self):
        comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_window_desktop(self.wid(b, "cosmicterm"), 9)
        self.assertIn("no workspace 9", str(cm.exception))
        self.assertEqual(comp.calls, [])


class CosmicXPlane(FakeXPlane):
    """The shared X rig on its own display number, so this file and test_backend_wlr.py never race for one.

    `views()` and its matcher are one piece of code shared by the two backends (`backend_wlr.XPlaneViews`),
    so the rig that feeds it is shared too."""

    DISPLAY_NUM = 4
    SOCK_PREFIX = "cosmic-x11-"
    CLIENTS = ((0x600012, "xterm", "XTerm", "cosmicxterm", 77, (0, 0, 484, 316)),)


class ViewFlags(CosmicXPlane, CosmicTest):
    """The state bits the X join must not drop.

    `views()` replaces the listing wxprop reads, and wxprop's fallback for a backend without one was
    `{"visible": w.visible}`: a row handed over with the flags at their defaults loses
    `_NET_WM_STATE_HIDDEN` on a minimized window -- which the wlr floor used to print -- and never gains the
    fullscreen, maximize or sticky bits this protocol's five-entry state array does carry."""

    def flagged(self, **kw):
        """The views of a session where cosmicterm is minimized, typedtest fullscreen and sticky.

        Driven through the manager rather than planted: the states come back off the compositor's own
        `state` array, which is the only place this backend reads them from."""
        self.x_server()
        _comp, b = self.backend(capabilities=CAPS + (CAP_FULLSCREEN, CAP_STICKY), **kw)
        b.set_state(self.wid(b, "cosmicterm"), "HIDDEN", 1)
        b.set_state(self.wid(b, "typedtest"), "FULLSCREEN", 1)
        b.set_state(self.wid(b, "typedtest"), "STICKY", 1)
        return {v.window.title: v for v in b.views()}

    def test_the_minimized_window_comes_back_minimized(self):
        v = self.flagged()["cosmicterm"]
        self.assertTrue(v.minimized)
        self.assertFalse(v.window.visible)

    def test_fullscreen_and_sticky_reach_the_view(self):
        v = self.flagged()["typedtest"]
        self.assertEqual((v.fullscreen, v.sticky, v.minimized), (True, True, False))

    def test_the_xwayland_row_carries_them_too(self):
        """The row that goes through the X join keeps the Wayland state: the two halves are one View."""
        self.x_server()
        _comp, b = self.backend()
        b.set_state(self.wid(b, "cosmicxterm"), "MAXIMIZED_VERT", 1)
        v = {r.window.title: r for r in b.views()}["cosmicxterm"]
        self.assertEqual(v.xid, 0x600012)
        self.assertEqual((v.maximized_h, v.maximized_v), (True, True))

    def test_wxprop_prints_the_states_the_protocol_carries(self):
        """The contract that made this a bug: `_node_from_view` reads `minimized`/`hidden` for `visible`,
        `fullscreen` for `fullscreen_mode` and `sticky` for STICKY, and `_NET_WM_STATE_HIDDEN` comes off that
        node [wxprop/core.py:171-176, 545]."""
        views = self.flagged()
        self.assertEqual(net_wm_state(views["cosmicterm"]), ["_NET_WM_STATE_HIDDEN"])
        self.assertEqual(net_wm_state(views["typedtest"]),
                         ["_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_STICKY"])
        self.assertEqual(net_wm_state(views["cosmicxterm"]), [],
                         "and nothing is invented for a window with an empty state array")


class XWaylandIds(CosmicXPlane, CosmicTest):
    """The weakest matcher in the tree, and deliberately so (decision C5.16): a COSMIC toplevel has neither
    a pid nor -- usually -- a geometry, so `WM_CLASS` and the title are all there is [M cosmic.md §5.4]."""

    XTERM = 0x600012

    def test_the_xterm_gets_its_real_id_and_wm_class(self):
        """cosmic-comp runs its own Xwayland and `wxprop -id 0x600012` was byte-identical to `xprop` there
        [M cosmic.md §3]; what was missing is the join from the toplevel list to that id."""
        self.x_server()
        _comp, b = self.backend()
        views = b.views()
        by_title = {v.window.title: v for v in views}
        xt = by_title["cosmicxterm"]
        self.assertEqual(xt.xid, self.XTERM)
        self.assertEqual((xt.instance, xt.cls), ("xterm", "XTerm"))
        self.assertEqual(xt.client_type, "x11")
        self.assertEqual(xt.window.pid, 77)
        self.assertEqual((xt.window.x, xt.window.y, xt.window.w, xt.window.h), (0, 0, 484, 316))
        self.assertEqual([v.xid for v in views if v.window.title != "cosmicxterm"], [0, 0])

    def test_two_identical_windows_keep_xid_zero(self):
        self.x_server([(0x600012, "foot", "foot", "cosmicterm", 1, (0, 0, 10, 10)),
                       (0x600013, "foot", "foot", "cosmicterm", 2, (0, 0, 10, 10))])
        rows = (("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "cosmicterm", "foot"),
                ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "cosmicterm", "foot"))
        _comp, b = self.backend(toplevels=rows)
        views = b.views()
        self.assertEqual([v.xid for v in views], [0, 0])
        self.assertIn("could not be told apart", self.err.getvalue())

    def test_nothing_is_opened_when_no_xwayland_runs(self):
        self.x_server()
        patch = mock.patch.object(session, "xwayland_running", lambda uid=None: False)
        patch.start()
        self.addCleanup(patch.stop)
        _comp, b = self.backend()
        self.assertIsNone(b.views())


class TheSmokeIdCheck(unittest.TestCase):
    """`vm/live-smoke.d/cosmic.sh`'s own id predicate, sliced out and run over the ids this backend mints.

    The check read the wrong way round until 2026-09-11: it failed an id `>= 0x40000000`, which is every id
    `backend.mint_id` produces, and both COSMIC flavors went red on it in CI with the sentence "the id
    1081277706 is at or above 0x40000000, where an XWayland id could collide with it" -- 1081277706 is
    0x40730C0A, `ID_BASE | blake2b` exactly [M goal2/recon/flavors.md §5]. The script's own `if` is what
    runs here, not a paraphrase: a copy in this file would go on passing the day the script changes."""

    SMOKE = os.path.join(ROOT, "vm", "live-smoke.d", "cosmic.sh")

    #: the id fedora44-cosmic and arch-cosmic both minted for the smoke's foot window in CI
    CI_ID = 1081277706

    def verdict(self, wid) -> str:
        """`pass`/`fail` for one id, from the script's block with the two reporters stubbed out."""
        block = sh_block(self.SMOKE, '    if [ "$WIN" -ge 1000000 ]', "\n    fi\n")
        script = "\n".join(['fail() { echo "FAIL $*"; }',
                             'pass() { echo "PASS $*"; }',
                             'WIN=$1',
                             block])
        run = subprocess.run(["bash", "-c", script, "idcheck", str(wid)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def test_every_id_this_backend_mints_is_accepted(self):
        for identifier, _title, _app in wl_fake.COSMIC_TOPLEVELS:
            wid = mint_id(identifier)
            self.assertGreaterEqual(wid, ID_BASE)
            said = self.verdict(wid)
            self.assertTrue(said.startswith("PASS"), "%s -> %d: %s" % (identifier, wid, said))

    def test_the_id_ci_failed_on_is_accepted(self):
        """The number is the datum, not a shape: 1081277706 is what the CI run printed for the smoke's foot
        window and what the old `if` failed on, and the claim is that it lies inside the band `mint_id`
        maps identifiers into -- [ID_BASE, ID_BASE + 2**30), read off the mint itself here -- so the
        corrected check has to take it. A CI id in the floor's range or down in Xwayland's would fail this
        line, and would mean detection had taken the wrong branch rather than that the check was wrong."""
        band = range(ID_BASE, ID_BASE + (1 << 30))
        minted = [mint_id(i) for i, _t, _a in wl_fake.COSMIC_TOPLEVELS]
        self.assertTrue(all(w in band for w in minted), minted)
        self.assertIn(self.CI_ID, band, "the id CI printed is not one this mint could have made")
        said = self.verdict(self.CI_ID)
        self.assertTrue(said.startswith("PASS"), said)

    def test_an_id_inside_xwaylands_own_range_fails(self):
        """0x40001e is the X id cosmic-comp's Xwayland gave the smoke's xterm on the golden (measured
        2026-09-11, `wwmctl lists that window under its real X id 0x40001e`). A NATIVE row carrying a
        number like that would mean the id was not minted at all."""
        said = self.verdict(0x40001E)
        self.assertTrue(said.startswith("FAIL"), said)
        self.assertIn("below 0x40000000", said)

    def test_a_wlr_floor_id_fails_with_the_floor_named(self):
        said = self.verdict(1000001)
        self.assertTrue(said.startswith("FAIL"), said)
        self.assertIn("wlr floor", said)


class StackCtx:
    """What `wdotool.window_cmds.cmd_getactivewindow` asks of its ctx: a backend and a window stack.

    The command and not a paraphrase of it: what CI printed on COSMIC was
    `xdo_get_active_window reported an error`, which is this function's own string for a listing in which
    nothing is focused [wdotool/window_cmds.py:250-268]."""

    def __init__(self, backend):
        self._backend = backend
        self.stack = []

    def backend(self):
        return self._backend


def getactivewindow(backend) -> str:
    """`wdotool getactivewindow` over `backend`, its stdout as a string."""
    out = io.StringIO()
    with redirect_stdout(out):
        window_cmds.cmd_getactivewindow(StackCtx(backend), [])
    return out.getvalue()


class DeferredState(CosmicTest):
    """The four COSMIC failures CI measured, which are one failure: nobody waited for `state`.

    `get_cosmic_toplevel` is answered with silence and the arrays come out of cosmic-comp's rate-limited
    refresh up to 150 ms later (SlowRefresh's paragraph). A client that reads whatever a roundtrip brought
    back sees every window unfocused and unmaximized, which is `getactivewindow` erroring and
    `wxprop -id _NET_WM_STATE` empty -- two of the four FAILs on both COSMIC flavors
    [goal2/ci/rig-fedora44-cosmic.log]."""

    def test_getactivewindow_names_the_window_another_process_activated(self):
        """The smoke's pair, in the shape it runs: `wdotool windowactivate --sync` and then
        `wdotool getactivewindow` are two processes, so the second one is a second connection that has to
        learn the state from scratch."""
        comp, b = self.backend(cls=SlowRefresh)
        wid = self.wid(b, "cosmicterm")
        b.activate(wid)
        self.assertEqual(comp.calls[-1][0], "activate")
        b2 = self.connect()
        self.assertEqual(getactivewindow(b2), "%d\n" % wid)

    def test_the_first_listing_of_a_connection_carries_the_state(self):
        """Not just the command above: `focused` is what `wdotool search --onlyvisible`, `wwmctl -l`'s
        `*` and every `--sync` predicate read off the same listing."""
        _comp, b = self.backend(cls=SlowRefresh)
        b.activate(self.wid(b, "typedtest"))
        b2 = self.connect()
        self.assertEqual([(w.title, w.focused) for w in b2.list()],
                         [("cosmicterm", False), ("typedtest", True), ("cosmicxterm", False)])

    def test_a_session_whose_state_never_comes_still_lists_its_windows(self):
        """The wait is bounded by STATE_WAIT and is not an error: a handle cosmic-comp could not resolve
        gets no events at all, and a listing of three windows with nothing known about them is still the
        answer. Bounded means both ends -- it waits the budget out (or the wait is not happening) and it
        does not wait twice the budget (or the deadline is not the one it is written to)."""
        comp = self.compositor(NeverRefresh)
        t0 = time.monotonic()
        b = self.connect()
        spent = time.monotonic() - t0
        self.assertGreaterEqual(spent, STATE_WAIT, "%.3fs: the silent handles were not waited for" % spent)
        self.assertLess(spent, STATE_WAIT * 2, "%.3fs: the deadline is not STATE_WAIT" % spent)
        wins = b.list()
        self.assertEqual([w.title for w in wins], ["cosmicterm", "typedtest", "cosmicxterm"])
        self.assertEqual([w.focused for w in wins], [False, False, False])
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in wins], [(0, 0, OUT_W, OUT_H)] * 3,
                         "and the rectangle falls back to the head's, silently")
        self.assertEqual(comp.calls, [], "nothing was sent to the manager to make that happen")

    def test_the_give_up_is_remembered_so_the_next_listing_does_not_pay_again(self):
        """`_refresh()` runs on every `list()` and every `window*` command. Before `_Top.waited` the
        never-answering session paid the whole budget on each of them: measured over NeverRefresh, three
        consecutive `list()` calls took 0.504 s, 0.501 s and 0.501 s and one `activate()` 0.501 s, so a
        `wdotool search --sync` loop polled at two hertz and every tool that lists twice cost a second.
        The budget belongs to the handle, and this session's handles have spent theirs."""
        _comp, b = self.backend(cls=NeverRefresh)     # the constructor is where it is spent
        wid = self.wid(b, "cosmicterm")
        t0 = time.monotonic()
        for _ in range(3):
            b.list()
        b.activate(wid)
        spent = time.monotonic() - t0
        self.assertLess(spent, STATE_WAIT,
                        "%.3fs for three listings and an activate: the give-up is not remembered" % spent)

    def test_a_window_that_opens_later_is_waited_for_all_the_same(self):
        """The give-up is per handle and not per process. This session answers for nothing it started with
        -- so its three handles are given up on -- and then a window opens, whose `state` comes 150 ms
        after the `get_cosmic_toplevel` like every other one on a live cosmic-comp. The listing that
        follows has to carry it: `wdotool search --sync foot` polls `list()` in one process, and a
        deadline armed once per process would answer `focused: False` about the window it just opened for
        as long as that process lived."""
        comp, b = self.backend(cls=LateWindow)
        self.assertEqual([w.focused for w in b.list()], [False, False, False])
        comp.open_late(LATE, states=[ST_ACTIVATED])
        wins = b.list()
        self.assertEqual([w.title for w in wins],
                         ["cosmicterm", "typedtest", "cosmicxterm", "latecomer"])
        self.assertEqual([w.focused for w in wins], [False, False, False, True],
                         "the new handle got its own wait; the old ones are still silent")
        self.assertEqual(getactivewindow(b), "%d\n" % wins[-1].id)

    def test_the_wait_ends_as_soon_as_the_arrays_are_in(self):
        """It is a wait FOR the events and not a sleep: SlowRefresh answers in 150 ms and the constructor
        must not sit out the whole 500 ms budget after that."""
        _comp = self.compositor(SlowRefresh)
        t0 = time.monotonic()
        b = self.connect()
        spent = time.monotonic() - t0
        self.assertTrue(all(r.have_state for r in b.tops.values()))
        self.assertLess(spent, STATE_WAIT, "%.3fs" % spent)


class DeferredStateViews(CosmicXPlane, CosmicTest):
    """The other two of the four: what `wxprop -id` prints for a window maximized by another process."""

    def test_wxprop_prints_the_maximize_a_second_process_asked_for(self):
        """CI's `wxprop -id says the window is maximized [got: _NET_WM_STATE(ATOM) = ]` on both flavors:
        `wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz` really did send `set_maximized`, and the
        `wxprop` that followed it read a state array it had not waited for. Measured green on the
        fedora44-cosmic golden after the wait landed, 2026-09-11: `_NET_WM_STATE(ATOM) =
        _NET_WM_STATE_MAXIMIZED_HORZ, _NET_WM_STATE_MAXIMIZED_VERT, _NET_WM_STATE_FOCUSED`."""
        self.x_server()
        _comp, b = self.backend(cls=SlowRefresh)
        b.set_state(self.wid(b, "cosmicterm"), "MAXIMIZED_VERT", 1)
        b2 = self.connect()
        views = {v.window.title: v for v in b2.views()}
        self.assertEqual((views["cosmicterm"].maximized_h, views["cosmicterm"].maximized_v), (True, True))
        self.assertEqual(net_wm_state(views["cosmicterm"]),
                         ["_NET_WM_STATE_MAXIMIZED_HORZ", "_NET_WM_STATE_MAXIMIZED_VERT"])
        self.assertEqual(net_wm_state(views["typedtest"]), [],
                         "and the window nobody maximized says nothing")

    def test_the_geometry_arrives_with_the_state_and_not_the_head_rectangle(self):
        """The same wait is why `wdotool getwindowgeometry` reads a rectangle on COSMIC at all: the
        `geometry` event comes out of the same refresh. Measured on the golden 2026-09-11: `762,201
        696x532` where the floor fallback would have been `0,0 1920x1080`, which is why
        vm/live-smoke.d/cosmic.sh's geometry check is a `want` now and not an `xwant`."""
        self.x_server()
        _comp, b = self.backend(cls=SlowRefresh, geometry=(40, 50, 640, 480))
        b2 = self.connect()
        self.assertEqual([(w.x, w.y, w.w, w.h) for w in b2.list()], [(40, 50, 640, 480)] * 3)
        self.assertFalse(b2.geometry_is_floor, "no window fell back to the output rectangle")


class GroupedWorkspaces(CosmicTest):
    """Desktop numbers on a session with a workspace group per head.

    `set_desktop 1 -> get_desktop 0` was the third COSMIC FAIL on both flavors. With two heads cosmic-comp
    publishes two groups, each with its own active workspace, and ordering the flat list by `(coordinates,
    arrival)` alone interleaves them: desktop 1 was the OTHER head's workspace `1`, already active, so the
    activate moved nothing at all [M goal2/recon/flavors.md §5 and the golden, TwoGroupCosmic's paragraph].
    """

    def flat_order(self, b):
        """What `ext_workspace.WorkspaceClient` numbers by -- the order this backend does NOT use."""
        return [r.oid for r in b.ws._live()]

    def test_the_desktops_run_group_by_group(self):
        _comp, b = self.backend(cls=TwoGroupCosmic)
        self.assertEqual([(w.index, w.name, w.active) for w in b.workspaces()],
                         [(0, "1", True), (1, "2", False), (2, "1", True)])
        self.assertEqual(b.num_desktops(), 3)

    def test_the_flat_order_is_a_different_list_and_is_not_the_one_used(self):
        """The guard on the sentence above: with the coordinates these two groups carry, sorting by them
        alone really does interleave the heads, so the two orders are different lists."""
        comp, b = self.backend(cls=TwoGroupCosmic)
        self.assertEqual(self.flat_order(b), [comp.ws_ids[0], comp.ws_ids[2], comp.ws_ids[1]])
        self.assertEqual(b._ws_handles(), list(comp.ws_ids))

    def test_the_current_desktop_is_the_first_active_row_of_the_GROUPED_order(self):
        """`ActiveOnTheSecondHead` is the fixture the two orders disagree about: grouped, the first active
        row is the first head's `3` at desktop 2; flat, the second head's `1` sorts in at index 1 by its
        coordinates and the answer would be 1. Both are read here, so the test says which order produced
        the number rather than agreeing with either."""
        comp, b = self.backend(cls=ActiveOnTheSecondHead)
        flat = self.flat_order(b)
        self.assertEqual(flat, [comp.ws_ids[0], comp.ws_ids[3], comp.ws_ids[1], comp.ws_ids[2]])
        self.assertEqual(b._ws_handles(), list(comp.ws_ids), "grouped: announcement order, group by group")
        active = {comp.ws_ids[2], comp.ws_ids[3]}
        self.assertEqual(next(i for i, oid in enumerate(flat) if oid in active), 1,
                         "the flat order's answer, spelled out so the two cannot be confused")
        self.assertEqual(b.get_desktop(), 2)
        self.assertEqual([w.active for w in b.workspaces()], [False, False, True, True])

    def test_set_desktop_on_the_fixture_the_orders_disagree_about(self):
        """And the switch reaches the same workspace the number named: desktop 1 is the first head's `2`
        (announcement index 1), where the flat order would have activated the other head's `1` (index 3)
        -- the shape of the CI failure, where the activate went to a workspace that was already active."""
        comp, b = self.backend(cls=ActiveOnTheSecondHead)
        b.set_desktop(1)
        self.assertEqual(comp.ws_calls, [("activate", 1), ("commit", None)])
        self.assertEqual(b.get_desktop(), 1)

    def test_set_desktop_1_switches_the_head_that_owns_desktop_1(self):
        """The measured pair: `wdotool set_desktop 1` then `wdotool get_desktop` answered 1 on the
        fedora44-cosmic golden on 2026-09-11, having answered 0 in CI. `activate` goes to the FIRST group's
        second workspace -- announcement index 1 -- and not to the other head's row, which is what the flat
        order would have activated."""
        comp, b = self.backend(cls=TwoGroupCosmic)
        b.set_desktop(1)
        self.assertEqual(comp.ws_calls, [("activate", 1), ("commit", None)])
        self.assertEqual(b.get_desktop(), 1)
        self.assertEqual([w.active for w in b.workspaces()], [False, True, True],
                         "the other head's workspace is left where it was")

    def test_set_desktop_0_puts_it_back(self):
        """The round trip, on the two-workspace head: what this one pins is the `activate` + `commit` pair
        going out again for the second switch and the state coming back, not the ordering (with these rows
        desktop 0 is the same workspace in either order -- the ordering is the two tests above)."""
        comp, b = self.backend(cls=TwoGroupCosmic)
        b.set_desktop(1)
        b.set_desktop(0)
        self.assertEqual(comp.ws_calls[-2:], [("activate", 0), ("commit", None)])
        self.assertEqual(b.get_desktop(), 0)

    def test_a_desktop_that_is_not_there(self):
        comp, b = self.backend(cls=TwoGroupCosmic)
        for n in (-1, 3):
            with self.assertRaises(CmdError) as cm:
                b.set_desktop(n)
            self.assertIn("cannot activate workspace %d" % n, str(cm.exception))
        self.assertEqual(comp.ws_calls, [], "nothing goes on the wire for a desktop that is not there")

    def test_a_window_on_the_second_group_reports_its_grouped_number(self):
        """The desktop COLUMN reads the same order: `wwmctl -l`'s desktop and `set_desktop` have to agree
        about which workspace is 2, or `wmctrl -s 2` and `wmctrl -l` are talking about different heads."""

        class SecondGroup(TwoGroupCosmic):
            WHERE = (0, 2, 2)      # one ROWS index per toplevel
            EXT_ENTER = 10

            def _send_cosmic_state(self, conn, rec):
                ids, self.ws_ids = self.ws_ids, []
                try:
                    super()._send_cosmic_state(conn, rec)
                    i = list(self.tops).index(rec.ext)
                    self._send(conn, rec.cosmic, self.EXT_ENTER, struct.pack("<I", ids[self.WHERE[i]]))
                    self._send(conn, rec.cosmic, 1)   # done
                finally:
                    self.ws_ids = ids

        _comp, b = self.backend(cls=SecondGroup)
        self.assertEqual([w.desktop for w in b.list()], [0, 2, 2])


if __name__ == "__main__":
    unittest.main()
