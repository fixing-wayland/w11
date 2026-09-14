#!/usr/bin/env python3
"""wdotool's Wayfire backend, over Wayfire's own JSON IPC.

Everything here is a wire fact, so the peer is a real unix socket speaking the real bytes: `FakeWayfire`
replays the captures recorded off a live Wayfire 0.10.0 [M recon2/wayfire.md §1.2] and records every method
with the data it was handed. That last part is the point of most of this file -- Wayfire's argument names are
not consistent (`view_id` with an underscore in wm-actions, `view-id` and `output-id` with a hyphen in
vswitch), and a misspelled one is not an error the compositor refuses in a way a user would understand: it is
`Missing "output-id"` out of a plugin nobody asked for.

The two things this backend exists to fix, both measured against the wlr floor that Wayfire lands on without
it [M recon2/wayfire.md §2.3, §2.4]: the floor reported the *output* rectangle as every window's
geometry and had no pid, no desktops and no move; and it printed `XTerm.XTerm` where wmctrl on the same
session printed `xterm.XTerm`, because the wlr `app_id` is the WM_CLASS class and the instance is only on the
X plane. U23 below is the second one, end to end, against a real X server."""

import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

from w11common.errors import CmdError
from support import FakeWayfire, fixture_json
from hacks.window import backend_wayfire, x11_mini
from hacks.window.backend_wayfire import GATE_METHOD, WayfireBackend, _WayfireIPC
from wwmctl import cli as wwmctl_cli
from hacks.window import wmctl as wwmctl_core

from test_wwmctl_x11 import FakeXServer

OK = {"result": "ok"}


class _Subscribe(dict):
    """The subscribe's `{"result": "ok"}`, carrying the events that follow it on the same connection."""

    def __init__(self, events):
        super().__init__(OK)
        self.events = list(events)


class WatchingWayfire(FakeWayfire):
    """FakeWayfire that writes a subscribe's events in the same `sendall` as its reply.

    Wayfire turns the connection a `window-rules/events/watch` arrives on into the event stream, so the reply
    and the events that follow it share one socket and the reply is always first. Writing the events from a
    thread of the test's own cannot promise that: `FakeWayfire._serve` appends to `calls` *before* it answers
    and `UnixServer` appends the connection before the handler has read anything, so there is no fact a
    pusher can wait for that means "the ok is on the wire". One write from the serving thread has the
    ordering for free, and nothing here sleeps for it."""

    def frame(self, obj):
        wire = FakeWayfire.frame(obj)
        for event in getattr(obj, "events", ()):
            wire += FakeWayfire.frame(event)
        return wire

#: The gate's refusal, byte for byte. A Wayfire whose `plugins` line has `ipc` and not `ipc-rules` answers
#: `{"methods": ["list-methods"]}` and `No such method found!` to everything else (measured here against
#: wayfire 0.10.0 started with `plugins = ipc`), which is a sentence about a config line and has to read like
#: one rather than surfacing one command at a time.  The rung came in on 2026-09-09: the sentence had the
#: route in it all along (edit wayfire.ini) and now names which rung of AGENTS.md's ladder that is, so a
#: reader who meets this one and the wlr floor's does not get two shapes of answer for two gaps.
GATE_REFUSAL = ("wayfire backend: this Wayfire's IPC has no window-rules/list-views: the window half is "
                "Wayfire's own IPC (AGENTS.md route 2) and it is one config line away -- `plugins = ipc "
                "ipc-rules` in wayfire.ini on Wayfire 0.9 or newer -- at the cost of restarting Wayfire "
                "to load it")

#: Every method this backend can *write* through. FakeWayfire replays recordings, and the recordings are all
#: reads, so the writes need an `{"result": "ok"}` of their own -- which is what the live socket answers to
#: every one of them (measured here: focus-view, configure-view, the five wm-actions, both vswitch methods
#: and the grid pair each came back `{"result": "ok"}`).
ACTS = ("window-rules/focus-view", "window-rules/close-view", "window-rules/configure-view",
        "wm-actions/set-minimized", "wm-actions/set-fullscreen", "wm-actions/set-sticky",
        "wm-actions/set-always-on-top", "wm-actions/send-to-back",
        "vswitch/set-workspace", "vswitch/send-view", "grid/slot_c", "grid/restore")

#: the recorded view row (foot, `capbox`, 100,100 696x494 on HEADLESS-1) and the recorded two-head layout
VIEW = fixture_json("wayfire", "window-rules_list-views.json")[0]
OUTPUTS = fixture_json("wayfire", "window-rules_list-outputs.json")
METHODS = fixture_json("wayfire", "list-methods.json")["methods"]

#: HEADLESS-1 is 1280x720 at the origin, HEADLESS-2 is 1920x1080 at +1280+0, both with a 3x3 viewport grid
#: sitting on cell (0, 0) [M recon2/wayfire.md §1.2]
HEAD1, HEAD2 = 1, 9


def box(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def view(vid, *, title="capbox", app_id="foot", pid=76872, geo=(100, 100, 696, 494),
         output=HEAD1, focus=1, **kw):
    """The recorded row with fields replaced; `app-id` and `last-focus-timestamp` are not identifiers."""
    row = dict(VIEW)
    row.update({"id": vid, "title": title, "app-id": app_id, "pid": pid,
                "geometry": box(*geo), "base-geometry": box(*geo), "bbox": box(*geo),
                "output-id": output, "last-focus-timestamp": focus})
    row.update(kw)
    return row


def outputs(*heads, current=(0, 0)):
    """The recorded outputs, keeping `heads` (ids), with the first one's viewport moved to `current`."""
    rows = [dict(o) for o in OUTPUTS if o["id"] in (heads or (HEAD1, HEAD2))]
    rows[0]["workspace"] = dict(rows[0]["workspace"], x=current[0], y=current[1])
    return rows


class Case(unittest.TestCase):
    """A backend talking to a FakeWayfire that answers the recorded reads and an `ok` to every write."""

    def backend(self, views=(), outs=None, methods=None, mode="ok", **answers):
        table = {m: OK for m in ACTS}
        table["window-rules/list-views"] = [dict(v) for v in views]
        table["window-rules/list-outputs"] = [dict(o) for o in (outs if outs is not None else OUTPUTS)]
        if methods is not None:
            table["list-methods"] = {"methods": list(methods)}
        table.update(answers)
        self.srv = WatchingWayfire(mode=mode, answers=table)
        self.addCleanup(self.srv.close)
        b = WayfireBackend(sockpath=self.srv.path)
        self.addCleanup(b.ipc.close)
        return b

    def calls(self, method=None):
        """[(method, data)] the double was asked for, or the data of one method's calls."""
        if method is None:
            return list(self.srv.calls)
        return [d for m, d in self.srv.calls if m == method]

    def sent(self):
        return [m for m, _d in self.srv.calls]


class Gate(Case):
    """The api gate: `window-rules/list-views` or nothing."""

    def test_a_wayfire_without_ipc_rules_is_refused_by_name(self):
        srv = FakeWayfire(mode="nomethod")
        self.addCleanup(srv.close)
        with self.assertRaises(CmdError) as cm:
            WayfireBackend(sockpath=srv.path)
        self.assertEqual(str(cm.exception), GATE_REFUSAL)
        # and it stopped there: no window method was tried against a socket that cannot answer one
        self.assertEqual([m for m, _d in srv.calls], ["list-methods"])

    def test_the_gate_reads_the_method_table_and_not_a_version(self):
        """`plugins = ipc` alone: the whole table is `["list-methods"]` (measured here). Wayfire 0.8 answers
        a longer one that still has no `window-rules/list-views` -- the view methods lived in stipc there
        [R recon2/wayfire.md §3.3] -- so the table, not the version, is the test."""
        for table in (["list-methods"], [m for m in METHODS if not m.startswith("window-rules/")]):
            with self.assertRaises(CmdError) as cm:
                self.backend(methods=table)
            self.assertEqual(str(cm.exception), GATE_REFUSAL)

    def test_the_recorded_table_passes_the_gate(self):
        b = self.backend()
        self.assertEqual(b.name, "wayfire")
        self.assertIn(GATE_METHOD, b.methods)
        self.assertEqual(len(b.methods), 60)   # the recorded 0.10.0 table

    def test_a_wedged_compositor_is_not_blamed_on_wayfire_ini(self):
        """The gate is a sentence about a config line, so only the shape that means "that plugin is not
        loaded" may reach it. A socket that accepts and never answers is the compositor being wedged inside
        its own event loop, and telling that user to add `ipc-rules` sends them to edit a file that is
        already right -- under `WDOTOOL_BACKEND=wayfire` it is the only line they would see."""
        old = backend_wayfire.IPC_TIMEOUT
        backend_wayfire.IPC_TIMEOUT = 0.4
        self.addCleanup(setattr, backend_wayfire, "IPC_TIMEOUT", old)
        srv = FakeWayfire(mode="silent")
        self.addCleanup(srv.close)
        with self.assertRaises(CmdError) as cm:
            WayfireBackend(sockpath=srv.path)
        self.assertEqual(str(cm.exception),
                         "wayfire backend: no answer from the compositor within 0.4s "
                         "(it is not responding)")
        self.assertNotIn("ipc-rules", str(cm.exception))
        # and it did ask: the silence is on `list-methods`, not on a socket nobody spoke to
        self.assertEqual([m for m, _d in srv.calls], ["list-methods"])

    def test_no_socket_at_all_names_the_variable(self):
        rt = tempfile.mkdtemp(prefix="wf-empty-")
        self.addCleanup(shutil.rmtree, rt, ignore_errors=True)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": rt}), \
                mock.patch.object(backend_wayfire.session, "find_wayfire_socket", lambda: None):
            with self.assertRaises(CmdError) as cm:
                WayfireBackend()
        self.assertIn("$WAYFIRE_SOCKET", str(cm.exception))


class Wire(Case):
    """The framing and the three failure shapes."""

    def test_the_framing_is_a_native_int32_length_and_a_json_body(self):
        """The recorded ping, byte for byte: `$\\x00\\x00\\x00` and then the 0x24-byte body
        [M recon2/wayfire.md §1.2]."""
        self.assertEqual(_WayfireIPC.frame({"method": "stipc/ping", "data": {}}),
                         b'$\x00\x00\x00{"method": "stipc/ping", "data": {}}')

    def test_a_request_goes_out_framed_and_comes_back_parsed(self):
        b = self.backend()
        self.assertEqual(b.ipc.call("wayfire/configuration")["api-version"], 20250822)
        self.assertEqual(self.calls()[-1], ("wayfire/configuration", {}))

    def test_a_no_such_method_reply_is_one_line_naming_the_method(self):
        b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.ipc.call("stipc/ping")
        self.assertEqual(str(cm.exception), "wayfire: No such method found! (stipc/ping)")

    def test_a_handler_error_is_one_line_and_is_not_repeated(self):
        b = self.backend(views=[view(3)])
        self.srv.mode = "handler-error"
        with self.assertRaises(CmdError) as cm:
            b.list()
        line = str(cm.exception)
        self.assertEqual(line, "wayfire: " + FakeWayfire.HANDLER_ERROR)
        self.assertNotIn("\n", line)
        # the handler shape already names the method inside the sentence
        self.assertEqual(line.count("window-rules/view-info"), 1)

    def test_a_socket_that_accepts_and_says_nothing_is_one_line(self):
        old = backend_wayfire.IPC_TIMEOUT
        backend_wayfire.IPC_TIMEOUT = 0.4
        self.addCleanup(setattr, backend_wayfire, "IPC_TIMEOUT", old)
        b = self.backend(views=[view(3)])
        self.srv.mode = "silent"
        t0 = time.monotonic()
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(str(cm.exception),
                         "wayfire backend: no answer from the compositor within 0.4s "
                         "(it is not responding)")

    def test_the_timeout_is_a_constructor_argument_and_is_the_number_reported(self):
        """requests-batch-6.md, from batch 10: `xkbmap.WayfireLayouts` runs inside `fetch()` while the
        daemon holds its lock, so it may not inherit a command's 10-second deadline -- every other reader in
        xkbmap.py bounds a wedged desktop at 2.0 s.  Two claims here, and both were red before the knob
        landed: a silent Wayfire refuses within the caller's own deadline, and the sentence names that
        deadline rather than IPC_TIMEOUT (a reader that waited 0.3 s and said it waited ten would send
        someone looking for a ten-second stall that never happened)."""
        self.backend(views=[view(3)])          # the gate, on the default deadline
        self.srv.mode = "silent"
        ipc = _WayfireIPC(self.srv.path, timeout=0.3)
        self.addCleanup(ipc.close)
        self.assertEqual(ipc.timeout, 0.3)
        t0 = time.monotonic()
        with self.assertRaises(CmdError) as cm:
            ipc.call("window-rules/list-views")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3, "the reader waited past its own deadline")
        self.assertEqual(str(cm.exception),
                         "wayfire backend: no answer from the compositor within 0.3s "
                         "(it is not responding)")

    # The one caller that passes the knob is pinned where its behaviour can be driven, not by a grep of
    # this module's source: tests/test_xkbmap.py:TestTheActiveGroupFromWayfire
    # test_a_wedged_wayfire_is_bounded_by_this_modules_deadline_not_the_backends puts a silent Wayfire
    # under `WayfireLayouts.group` with WAYFIRE_TIMEOUT at 0.3 s and asserts it came back inside 3 s.

    def test_a_compositor_that_goes_away_is_one_line(self):
        """`gone` closes the connection after the first reply, so the gate is answered and the first window
        command reads EOF -- the shape of a session that ends mid-chain."""
        b = self.backend(views=[view(3)], mode="gone")
        with self.assertRaises(CmdError) as cm:
            b.list()
        line = str(cm.exception)
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("wayfire backend: "), line)


class Windows(Case):
    """view -> Window."""

    def test_a_view_becomes_a_window(self):
        b = self.backend(views=[view(7, activated=True)])
        (w,) = b.list()
        self.assertEqual((w.id, w.title, w.class_, w.pid), (7, "capbox", "foot", 76872))
        self.assertEqual((w.x, w.y, w.w, w.h), (100, 100, 696, 494))
        self.assertTrue(w.focused and w.visible)
        self.assertEqual(w.desktop, 0)
        # Wayfire publishes no WM_CLASS instance on a view; `search --classname` falls back to class_
        self.assertEqual(w.instance, "")

    def test_the_geometry_is_the_windows_and_not_the_outputs(self):
        """The one number the wlr floor gets wrong: it answered `Geometry: 1280x720` -- the output -- for a
        window Wayfire's own IPC put at `100,100 696x494` [M recon2/wayfire.md §2.3]."""
        b = self.backend(views=[view(7)])
        (w,) = b.list()
        self.assertNotEqual((w.w, w.h), (1280, 720))
        self.assertEqual((w.w, w.h), (696, 494))

    def test_only_mapped_toplevels_are_listed(self):
        b = self.backend(views=[view(1), view(2, mapped=False),
                                view(3, role="desktop-environment"), view(4, role="unmanaged")])
        self.assertEqual([w.id for w in b.list()], [1])

    def test_a_minimized_view_is_listed_and_is_not_visible(self):
        """`mapped` stays true on a minimized view (measured here: set-minimized left `mapped` 1 and
        `minimized` 1), so visibility is the two fields together and `is_mapped` is the minimized one."""
        b = self.backend(views=[view(3, minimized=True)])
        (w,) = b.list()
        self.assertTrue(w.visible is False)
        self.assertFalse(b.is_mapped(3))

    def test_the_listing_is_bottom_to_top_by_last_focus(self):
        """`list-views` answers in view-id order whatever has focus (measured here: focusing either of two
        views left the order alone), so the stacking order backend.hit_test() reads has to be built from
        `last-focus-timestamp` -- most recently focused last."""
        b = self.backend(views=[view(3, focus=640526884177738),
                                view(5, focus=640542289476725),
                                view(4, focus=640500000000000)])
        self.assertEqual([w.id for w in b.list()], [4, 3, 5])

    def test_an_unknown_id_is_one_line_and_sends_nothing(self):
        b = self.backend(views=[view(3)])
        for op in (b.activate, b.close, b.lower, b.minimize):
            with self.assertRaises(CmdError) as cm:
                op(999)
            self.assertEqual(str(cm.exception), "window 999 not found")
        self.assertEqual([m for m in self.sent() if m in ACTS], [])


class Marshalling(Case):
    """What goes on the wire for each operation, argument names included."""

    def setUp(self):
        self.b = self.backend(views=[view(3)])

    def test_move_and_resize_are_one_configure_view_each(self):
        """`configure-view` takes the whole box, so a move keeps the size and a resize keeps the origin. The
        size that comes back is the client's: foot asked for 500x400 settled at 498x390
        [M recon2/wayfire.md §3.3], as it does on sway."""
        self.b.move_window(3, 40, 50)
        self.b.resize(3, 500, 400)
        self.assertEqual(self.calls("window-rules/configure-view"), [
            {"id": 3, "geometry": {"x": 40, "y": 50, "width": 696, "height": 494}},
            {"id": 3, "geometry": {"x": 100, "y": 100, "width": 500, "height": 400}},
        ])

    def test_activate_focus_and_close(self):
        self.b.activate(3)
        self.b.focus(3)
        self.b.close(3)
        self.assertEqual(self.calls("window-rules/focus-view"), [{"id": 3}, {"id": 3}])
        self.assertEqual(self.calls("window-rules/close-view"), [{"id": 3}])

    def test_minimize_map_and_unmap_are_set_minimized(self):
        self.b.minimize(3)
        self.b.map(3)
        self.b.unmap(3)
        self.assertEqual(self.calls("wm-actions/set-minimized"),
                         [{"view_id": 3, "state": True}, {"view_id": 3, "state": False},
                          {"view_id": 3, "state": True}])

    def test_lower_is_send_to_back(self):
        """A real lower, which the sway backend has never had: sway's IPC has no such command at all."""
        self.b.lower(3)
        self.assertEqual(self.calls("wm-actions/send-to-back"), [{"view_id": 3, "state": True}])

    def test_raise_is_a_focus_and_never_pins_the_window(self):
        """`set-always-on-top` is a state, not a raise: using it for `windowraise` would leave the window
        above everything for the rest of the session."""
        self.b.raise_(3)
        self.assertEqual(self.calls("window-rules/focus-view"), [{"id": 3}])
        self.assertEqual(self.calls("wm-actions/set-always-on-top"), [])

    def test_wm_actions_spell_the_view_with_an_underscore(self):
        """`view_id` here and `view-id` in vswitch, both measured: the wrong one is answered
        `Missing "view-id"` by a plugin the user never named."""
        self.b.minimize(3)
        (data,) = self.calls("wm-actions/set-minimized")
        self.assertIn("view_id", data)
        self.assertNotIn("view-id", data)


class States(Case):
    """set_state, and the maximize pair."""

    def setUp(self):
        self.b = self.backend(views=[view(3)])

    def test_the_four_states_wayfire_takes(self):
        for state, method in (("FULLSCREEN", "wm-actions/set-fullscreen"),
                              ("STICKY", "wm-actions/set-sticky"),
                              ("HIDDEN", "wm-actions/set-minimized"),
                              ("ABOVE", "wm-actions/set-always-on-top")):
            self.assertIsNone(self.b.set_state(3, state, 1))
            self.assertEqual(self.calls(method)[-1], {"view_id": 3, "state": True})
            self.b.set_state(3, state, 0)
            self.assertEqual(self.calls(method)[-1], {"view_id": 3, "state": False})

    def test_a_toggle_reads_the_view_back(self):
        b = self.backend(views=[view(3, fullscreen=True, sticky=False)])
        b.set_state(3, "FULLSCREEN", 2)
        b.set_state(3, "STICKY", 2)
        self.assertEqual(self.calls("wm-actions/set-fullscreen"), [{"view_id": 3, "state": False}])
        self.assertEqual(self.calls("wm-actions/set-sticky"), [{"view_id": 3, "state": True}])

    def test_above_refuses_a_toggle_rather_than_guessing(self):
        """Wayfire takes always-on-top and never reports it back: the view record's `layer` stayed
        `workspace` on both sides of a set-always-on-top (measured here), so there is nothing to toggle
        from."""
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(3, "ABOVE", 2)
        self.assertIn("does not report the always-on-top state back", str(cm.exception))
        self.assertIn("AGENTS.md route 6", str(cm.exception))
        self.assertIn("not done yet", str(cm.exception))
        self.assertEqual(self.calls("wm-actions/set-always-on-top"), [])

    def test_the_maximize_pair_is_one_grid_call(self):
        self.assertEqual(self.b.maximize_pair_state(), "MAXIMIZED")
        self.b.set_state(3, "MAXIMIZED", 1)
        self.b.set_state(3, "MAXIMIZED", 0)
        self.assertEqual(self.calls("grid/slot_c"), [{"view_id": 3}])
        self.assertEqual(self.calls("grid/restore"), [{"view_id": 3}])

    def test_a_maximize_toggle_reads_the_tiled_edges(self):
        """`tiled-edges` is 15 when all four edges are tiled: measured on this box, `grid/slot_c` -> 15,
        `slot_l` -> 7, `slot_t` -> 13, `grid/restore` -> 0."""
        b = self.backend(views=[view(3, **{"tiled-edges": 15})])
        b.set_state(3, "MAXIMIZED", 2)
        self.assertEqual(self.calls("grid/restore"), [{"view_id": 3}])
        self.assertEqual(self.calls("grid/slot_c"), [])

    def test_one_maximize_axis_alone_is_refused(self):
        """The grid plugin's slots are halves of the screen, not axes -- `slot_t` is the top half
        (tiled-edges 13), which is not "maximized vertically" -- so a lone axis has no expression here."""
        for state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            with self.assertRaises(CmdError) as cm:
                self.b.set_state(3, state, 1)
            self.assertIn("both axes at once", str(cm.exception))
            # the gap half: a lone axis IS reachable over this same IPC, so the refusal names that route
            # and its price rather than stopping at the grid plugin's shape
            self.assertIn("window-rules/configure-view", str(cm.exception))
            self.assertIn("AGENTS.md route 2", str(cm.exception))
        self.assertEqual(self.calls("grid/slot_c"), [])
        self.assertEqual(self.calls("window-rules/configure-view"), [],
                         "naming the route is not taking it")

    def test_maximizing_without_the_grid_plugin_names_it(self):
        b = self.backend(views=[view(3)], methods=[m for m in METHODS if not m.startswith("grid/")])
        with self.assertRaises(CmdError) as cm:
            b.set_state(3, "MAXIMIZED", 1)
        self.assertEqual(str(cm.exception),
                         "wayfire: maximizing needs Wayfire's `grid` plugin "
                         "(add it to the `plugins` line in wayfire.ini)")

    def test_a_state_wayfire_has_no_word_for_says_what_would_close_it(self):
        """SHADED is a state wmctrl takes, so the refusal is a gap and owes its rung: every state this
        backend does set is a `wm-actions` method, which makes a plugin the place a sixth would go."""
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(3, "SHADED", 1)
        self.assertEqual(str(cm.exception),
                         "windowstate SHADED is not supported by the wayfire backend: no Wayfire plugin "
                         "has a method for it; not yet here, and the route is a patched Wayfire plugin "
                         "(AGENTS.md route 6), which is where `wm-actions` keeps the others")


class Plugins(Case):
    """The operations whose plugin the user has to have asked for.

    Measured on this box against wayfire 0.10.0: `plugins = ipc ipc-rules` answers 23 methods -- every
    `window-rules/*` read and write plus `wayfire/*` and `input/*` -- and no `grid/*`, `vswitch/*` or
    `wm-actions/*`. `wm-actions` is not in the stock plugin list at all
    (`/usr/share/wayfire/metadata/core.xml` line 10), so minimize on a default Wayfire is a missing plugin
    rather than a missing feature, and the difference is the whole message."""

    #: what a Wayfire with only the IPC plugins answers, measured
    IPC_ONLY = [m for m in METHODS
                if m.startswith(("window-rules/", "wayfire/", "input/")) or m == "list-methods"]

    def setUp(self):
        self.b = self.backend(views=[view(3)], methods=self.IPC_ONLY)

    def test_the_reads_and_the_window_rules_writes_work_without_an_extra_plugin(self):
        """The gate passes, the whole listing side is live, and the three writes that live in `ipc-rules`
        itself go out in full: this is a real Wayfire configuration and it must not be refused wholesale.
        Each write is checked by the bytes it produced, not by not raising -- `_need()` refuses without
        sending, so an over-eager gate here would look exactly like a silent no-op."""
        self.assertEqual([w.id for w in self.b.list()], [3])
        self.assertEqual(self.b.pointer(), (640, 360))
        self.assertEqual(self.b.get_desktop(), 0)
        self.b.move_window(3, 40, 50)
        self.b.activate(3)
        self.b.close(3)
        self.assertEqual(self.calls("window-rules/configure-view"),
                         [{"id": 3, "geometry": {"x": 40, "y": 50, "width": 696, "height": 494}}])
        self.assertEqual(self.calls("window-rules/focus-view"), [{"id": 3}])
        self.assertEqual(self.calls("window-rules/close-view"), [{"id": 3}])

    def test_each_missing_plugin_is_named(self):
        cases = ((lambda: self.b.minimize(3), "minimizing", "wm-actions"),
                 (lambda: self.b.lower(3), "lowering a window", "wm-actions"),
                 (lambda: self.b.set_state(3, "FULLSCREEN", 1), "windowstate FULLSCREEN", "wm-actions"),
                 (lambda: self.b.set_state(3, "MAXIMIZED", 1), "maximizing", "grid"),
                 (lambda: self.b.set_desktop(1), "switching desktops", "vswitch"),
                 (lambda: self.b.set_window_desktop(3, 1),
                  "moving a window to another desktop", "vswitch"))
        for op, what, plugin in cases:
            with self.subTest(what):
                with self.assertRaises(CmdError) as cm:
                    op()
                self.assertEqual(str(cm.exception),
                                 "wayfire: %s needs Wayfire's `%s` plugin "
                                 "(add it to the `plugins` line in wayfire.ini)" % (what, plugin))
        self.assertEqual([m for m in self.sent() if m in ACTS], [])


class Desktops(Case):
    """The 3x3 viewport grid, both ways."""

    def test_the_grid_flattens_row_major(self):
        """`index = y * grid_width + x` on the view's own output. Geometry is relative to the viewport in
        front, so a view two screens to the right and one down is on cell (2, 1) = 5."""
        b = self.backend(views=[view(1, geo=(100, 50, 696, 494)),
                                view(2, geo=(2560 + 100, 720 + 50, 696, 494)),
                                view(3, geo=(1280 + 100, 50, 696, 494))],
                         outs=outputs(HEAD1))
        self.assertEqual([w.desktop for w in b.list()], [0, 5, 1])
        self.assertEqual(b.num_desktops(), 9)

    def test_a_view_behind_the_current_viewport_reads_negative_and_still_lands(self):
        """Measured on this box: with the viewport at (1, 0), a view sitting on (0, 0) of a 1280-wide output
        read back `x: -882`. Floor division is what carries it back to cell 0."""
        b = self.backend(views=[view(5, geo=(-882, 202, 484, 316))],
                         outs=outputs(HEAD1, current=(1, 0)))
        (w,) = b.list()
        self.assertEqual(w.desktop, 0)
        self.assertEqual(b.get_desktop(), 1)
        self.assertFalse(w.visible)          # it is on the viewport behind the current one

    def test_the_outputs_own_origin_is_subtracted_first(self):
        """The second head sits at +1280+0 and is 1920 wide, so a view at x 3130 is on ITS first viewport --
        1900 px into the head, not 3180 px into the layout. Reading the layout coordinate as an offset into
        the head puts the window one viewport too far right."""
        b = self.backend(views=[view(9, geo=(3130, 100, 100, 100), output=HEAD2)])
        (w,) = b.list()
        self.assertEqual(w.desktop, 0)

    def test_a_sticky_view_is_on_no_numbered_desktop(self):
        b = self.backend(views=[view(3, sticky=True, geo=(2560, 0, 696, 494))])
        (w,) = b.list()
        self.assertEqual(w.desktop, -1)
        self.assertTrue(w.visible)

    def test_set_desktop_spells_output_id_with_a_hyphen(self):
        """`output_id` is answered `Missing "output-id"` [M recon2/wayfire.md §1.2]."""
        b = self.backend(outs=outputs(HEAD1))
        b.set_desktop(4)
        (data,) = self.calls("vswitch/set-workspace")
        self.assertEqual(data, {"x": 1, "y": 1, "output-id": HEAD1})
        self.assertNotIn("output_id", data)

    def test_set_desktop_refuses_a_cell_off_the_grid(self):
        b = self.backend(outs=outputs(HEAD1))
        with self.assertRaises(CmdError) as cm:
            b.set_desktop(9)
        self.assertIn("viewport grid is 3x3", str(cm.exception))
        self.assertEqual(self.calls("vswitch/set-workspace"), [])

    def test_send_view_spells_the_view_with_a_hyphen(self):
        """The opposite spelling to wm-actions, and measured: `view_id` is answered `Missing "view-id"`."""
        b = self.backend(views=[view(3)], outs=outputs(HEAD1))
        b.set_window_desktop(3, 7)
        (data,) = self.calls("vswitch/send-view")
        self.assertEqual(data, {"view-id": 3, "x": 1, "y": 2})

    def test_get_desktop_is_the_current_cell(self):
        b = self.backend(outs=outputs(HEAD1, current=(2, 1)))
        self.assertEqual(b.get_desktop(), 5)

    def test_workspaces_are_the_grid_cells_in_layout_coordinates(self):
        """`workarea` is output-relative where `geometry` is absolute -- the recorded second head sits at
        `x: 1280` with `workarea.x: 0` -- so the work area a desktop row prints has to add the origin back."""
        b = self.backend(outs=outputs(HEAD2, current=(1, 0)))
        rows = b.workspaces()
        self.assertEqual(len(rows), 9)
        self.assertEqual([r.index for r in rows], list(range(9)))
        self.assertEqual([r.index for r in rows if r.active], [1])
        self.assertEqual(rows[0].work_area, (1280, 0, 1920, 1080))

    def test_display_size_is_the_bounding_box_of_every_head(self):
        b = self.backend()
        self.assertEqual(b.display_size(), (3200, 1080))


class PointerAndX(Case):
    """The two things the wlr floor could not answer at all."""

    def test_pointer_reads_wayfires_own_cursor(self):
        """`window-rules/get_cursor_position` -- an underscore in a family of hyphens -- answered
        `{"pos": {"x": 640.0, "y": 360.0}}`, and a mousemove followed by this read came back 0 px out
        [M recon2/wayfire.md §1.2, §2.3]."""
        b = self.backend()
        self.assertEqual(b.pointer(), (640, 360))
        self.assertEqual(self.sent()[-1], "window-rules/get_cursor_position")

    def test_x_info_comes_from_stipc_when_stipc_is_loaded(self):
        b = self.backend(**{"stipc/get_xwayland_display": {"result": "ok", "display": ":1"}})
        info = b.x_info()
        self.assertIsNotNone(info)
        self.assertEqual(info[0], ":1")

    def test_x_info_is_none_without_stipc_and_costs_no_round_trip(self):
        b = self.backend(methods=[m for m in METHODS if not m.startswith("stipc/")])
        before = len(self.srv.calls)
        self.assertIsNone(b.x_info())
        self.assertEqual(len(self.srv.calls), before)


class Events(Case):
    """`window-rules/events/watch`, in sway's vocabulary.

    The subscribe turns its own connection into a stream: Wayfire answers `{"result": "ok"}` and pushes
    everything afterwards down the same socket, so the events here are written into the connection the
    watcher opens rather than answered as replies. The names and their order are what one foot window
    produced on this box: view-set-output, view-title-changed, view-app-id-changed, view-mapped,
    view-focused, view-geometry-changed, view-unmapped [M recon2/wayfire.md §1.2 for the first four]."""

    def watching(self, events, **kw):
        """A backend whose `events/watch` is answered `ok` and then these events, in that order and in one
        write on the watcher's own connection."""
        return self.backend(**dict(kw, **{"window-rules/events/watch": _Subscribe(events)}))

    def test_events_speak_sways_words(self):
        b = self.watching([{"event": "view-mapped", "view": view(7, title="evbox")},
                           {"event": "view-focused", "view": view(7)},
                           {"event": "view-title-changed", "view": view(7)},
                           {"event": "view-geometry-changed", "view": view(7)},
                           {"event": "view-app-id-changed", "view": view(7)},
                           {"event": "view-unmapped", "view": view(7)}])
        got = list(b.events(timeout=3))
        self.assertEqual(got, [(7, "new"), (7, "focus"), (7, "title"), (7, "move"), (7, "close")])

    def test_the_workspace_stream_is_folded_in_on_request(self):
        """No view has id 0, so the desktop events ride the same iterator as (0, "workspace") -- the shape
        the sway, GNOME and KWin backends already give a root-level watcher."""
        b = self.watching([{"event": "wset-workspace-changed", "wset": {"index": 1}},
                           {"event": "view-focused", "view": view(3)}])
        self.assertEqual(list(b.events(timeout=3, workspaces=True)), [(0, "workspace"), (3, "focus")])

    def test_the_workspace_stream_is_left_out_by_default(self):
        b = self.watching([{"event": "wset-workspace-changed", "wset": {"index": 1}},
                           {"event": "view-focused", "view": view(3)}])
        self.assertEqual(list(b.events(timeout=3)), [(3, "focus")])

    def test_select_window_waits_for_the_next_focus(self):
        b = self.watching([{"event": "view-mapped", "view": view(7)},
                           {"event": "view-focused", "view": view(5)}])
        self.assertEqual(b.select_window(), 5)
        self.assertEqual(b.select_window_hint, "focus the target window to select it")

    def test_the_stream_is_its_own_connection(self):
        """A subscription and a command cannot share a socket: every reply the command socket waits for would
        arrive behind an unbounded queue of events."""
        b = self.watching([{"event": "view-focused", "view": view(3)}])
        list(b.events(timeout=2))
        self.assertGreaterEqual(len(self.srv.conns), 2)


class XIds(Case):
    """U23: the X window ids, and the WM_CLASS pair the wlr floor could not print."""

    XTERM = 0x0060000C
    XPID = 55726

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wayfire-x-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        old = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", old)
        patch = mock.patch.dict(os.environ, {"XAUTHORITY": os.path.join(self.dir, "no-authority")})
        patch.start()
        self.addCleanup(patch.stop)
        self.x = FakeXServer(self.dir, num=71)
        self.addCleanup(self.x.stop)
        root = FakeXServer.ROOTS[0]
        self.x.geometry[root] = (0, 0, 1280, 720)
        self.x.set_prop(root, "_NET_CLIENT_LIST", "WINDOW", 32, struct.pack("<I", self.XTERM))
        # the pair wmctrl prints for an xterm, and the pid Wayfire also knows
        self.x.set_prop(self.XTERM, "WM_CLASS", "STRING", 8, b"xterm\0XTerm\0")
        self.x.set_prop(self.XTERM, "_NET_WM_PID", "CARDINAL", 32, struct.pack("<I", self.XPID))
        self.x.geometry[self.XTERM] = (0, 0, 484, 316)
        self.x.translate[self.XTERM] = (0, 0)
        self.running = mock.patch.object(backend_wayfire.session, "xwayland_running",
                                         lambda uid=None: True)
        self.running.start()
        self.addCleanup(self.running.stop)

    #: the two recorded views of the live session: an XWayland xterm (`app-id` XTerm, the WM_CLASS *class*)
    #: and a native foot [M recon2/wayfire.md §2.4, recon2/wayfire/list-views.json]
    def views(self):
        return [view(3, title="wfxterm", app_id="XTerm", pid=self.XPID, geo=(0, 0, 484, 316),
                     focus=640526884177738),
                view(5, title="wfterm", app_id="foot", pid=55725, geo=(100, 100, 696, 494),
                     focus=640542289476725)]

    def wayfire(self, **kw):
        b = self.backend(views=self.views(), outs=outputs(HEAD1),
                         **{"stipc/get_xwayland_display": {"result": "ok", "display": ":71"}}, **kw)
        # the backend keeps its X connection for the life of the process, as the KWin backend does
        self.addCleanup(lambda: b._x not in ("unset", None) and b._x.close())
        return b

    def test_the_xterm_is_matched_on_pid_and_class(self):
        b = self.wayfire()
        views = {v.window.id: v for v in b.views()}
        self.assertEqual(views[3].xid, self.XTERM)
        self.assertEqual((views[3].instance, views[3].cls), ("xterm", "XTerm"))
        self.assertEqual(views[3].app_id, "")
        self.assertEqual(views[3].client_type, "x11")
        # the native one is untouched: no X id, and its app id twice
        self.assertEqual((views[5].xid, views[5].app_id, views[5].client_type),
                         (0, "foot", "wayland"))

    def test_wwmctl_lx_prints_the_wmctrl_class_pair(self):
        """The floor printed `XTerm.XTerm` where `wmctrl -l -x` on the same session printed `xterm.XTerm`,
        and the id was synthetic (`0x000f4240` against the real `0x0040000c`) [M recon2/wayfire.md §2.4]."""
        b = self.wayfire()
        out = self.run_wwmctl(["-l", "-x"], b)
        self.assertEqual(out, [
            "0x0060000c  0 xterm.XTerm           testhost wfxterm",
            "0x00000005  0 foot.foot             testhost wfterm",
        ])
        self.assertNotIn("XTerm.XTerm", "\n".join(out))

    def test_nothing_is_opened_when_no_xwayland_is_running(self):
        """Connecting to ask would start one: Wayfire spawns Xwayland on demand."""
        self.running.stop()
        self.addCleanup(self.running.start)
        with mock.patch.object(backend_wayfire.session, "xwayland_running", lambda uid=None: False):
            b = self.wayfire()
            views = {v.window.id: v for v in b.views()}
        self.assertEqual([v.xid for v in views.values()], [0, 0])
        self.assertEqual(views[3].cls, "XTerm")   # the app id, all Wayfire itself knows

    def run_wwmctl(self, argv, backend):
        """`wwmctl <argv>` in process, on this backend and with no X plane of wwmctl's own -- so the class
        column is what the backend's views() said and nothing else."""
        import io
        from contextlib import redirect_stderr, redirect_stdout
        old = (wwmctl_core._detect_backend, wwmctl_core._x11_connect, wwmctl_core.hostname, sys.argv)
        wwmctl_core._detect_backend = lambda: backend
        wwmctl_core._x11_connect = lambda *a, **kw: None
        wwmctl_core.hostname = lambda: "testhost"
        sys.argv = ["wmctrl"]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = wwmctl_cli.main(list(argv))
        finally:
            (wwmctl_core._detect_backend, wwmctl_core._x11_connect,
             wwmctl_core.hostname, sys.argv) = old
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue().splitlines()


if __name__ == "__main__":
    unittest.main()
