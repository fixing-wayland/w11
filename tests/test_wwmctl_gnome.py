#!/usr/bin/env python3
"""wwmctl on GNOME: the views()/workspaces()/x_info() route over the mock
w11 bridge (tests/test_backend_gnome.py's MockBridge on the
in-process mock bus), with the X plane faked the way test_wwmctl_cli does.

Covers: -l/-lp/-lG/-lx column parity with X ids for XWayland windows and
bridge ids for native ones, X-plane enrichment through the bridge's
DISPLAY/XAUTHORITY, -d from ListWorkspaces, -m with and without Xwayland,
every action's bridge call (-a/-c/-R/-t/-e/-b/-s/-k/-n/-o/-g), -N/-I/-T on
X vs native windows, -i with either id, :SELECT:/:ACTIVE:, the exit codes
and error strings of the bridge-less / bridge-gone paths, and the end-to-end
detection wiring (backend_detect over the mock bus)."""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from w11common import session
from test_backend_gnome import (CALC, EDITOR, WORK_AREA, XTERM,
                                XTERM_XID, MockBridge, _Base)
from wdotool import backend_detect, backend_gnome
from wdotool.backend_gnome import IFACE, OBJECT_PATH, GnomeBackend
from wwmctl import cli, core

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

WM_CHECK = 0x200001
XAUTH = "/run/user/1000/.mutter-Xwaylandauth.AB12CD"


class FakeX11:
    """The x11_mini surface wwmctl.core uses, shaped like Mutter's
    Xwayland: a check window named "GNOME Shell" without WM_CLASS/_NET_WM_PID
    (real wmctrl -m prints N/A for both there), _NET_SHOWING_DESKTOP on the
    root, and the xterm's client rectangle one titlebar below its frame."""

    TITLEBAR = 37  # Mutter's SSD bar: the client sits one bar below the frame

    def __init__(self, showing=0, viewport=()):
        self.calls = []
        self.atoms = {}
        self.states = {}                 # win -> {atom} (_NET_WM_STATE)
        #: one entry per _NET_WM_STATE property read, so a test can say how
        #: many atoms the fallback read back -- a pair sent as one message
        #: reads two before and two after, a single atom one and one
        self.state_reads = []
        self.wm_honours_state = True     # Mutter is the EWMH WM on X
        self.showing = showing
        self.viewport = list(viewport)   # _NET_DESKTOP_VIEWPORT, as published
        self.bridge = None  # the harness' bridge: frames to derive from

    def root(self):
        return 0x1C5

    def get_wm_class(self, win):
        return ("xterm", "XTerm") if win == XTERM_XID else ("", "")

    def get_client_machine(self, win):
        return "vmhost" if win == XTERM_XID else ""

    def get_geometry(self, win):
        # the xterm's client rectangle: the bridge's frame minus the SSD bar
        # (100,117 640x443 for the fixture's frame at 100,80 640x480)
        if self.bridge is None or win != XTERM_XID:
            return (100, 117, 640, 443)
        d = self.bridge.find(XTERM)
        return (d["x"], d["y"] + self.TITLEBAR, d["width"],
                d["height"] - self.TITLEBAR)

    def get_pid(self, win):
        return 1201 if win == XTERM_XID else 0

    def get_prop_ints(self, win, name):
        if (win, name) == (0x1C5, "_NET_SUPPORTING_WM_CHECK"):
            return [WM_CHECK]
        if (win, name) == (0x1C5, "_NET_SHOWING_DESKTOP"):
            return [self.showing]
        if (win, name) == (0x1C5, "_NET_DESKTOP_VIEWPORT"):
            return list(self.viewport)
        if name == "_NET_WM_STATE":
            self.state_reads.append(win)
            return sorted(self.states.get(win, ()))
        return []

    def get_prop_string(self, win, name):
        if (win, name) == (WM_CHECK, "_NET_WM_NAME"):
            return "GNOME Shell"
        return ""

    def set_name(self, win, name, icon, long_, utf8=False):
        self.calls.append(("set_name", win, name, icon, long_, utf8))

    def atom(self, name, only_if_exists=False):
        return self.atoms.setdefault(name, 0x180 + len(self.atoms))

    def send_root_message(self, win, type_name, data):
        """Mutter's side of an EWMH request: _NET_SHOWING_DESKTOP really
        changes the mode and the root property that reports it.

        _NET_WM_STATE carries two atoms (data.l[1] and data.l[2]). A message
        with one of them is decided on that atom's own flag, as every EWMH
        window manager decides it. A message carrying BOTH maximize atoms is
        Mutter's special case: window-x11.c collects them into a single
        `directions` bitmask and makes one decision off the horizontal flag
        (`action == ADD || (action == TOGGLE &&
        !...is_maximized_horizontally (...))`), so a toggle of the pair on a
        window maximized horizontally only clears BOTH axes rather than
        swapping them. This is a GNOME fixture and models GNOME; the pair
        only ever reaches _x_set_state under a backend that names it
        (maximize_pair_state -- KWin and sway answer None and send one atom
        per message). data[2] used to be ignored here, which let a fallback
        that sent the pair as two messages look identical to one that sent
        it as one."""
        self.calls.append(("client_message", win, type_name, tuple(data)))
        if type_name == "_NET_SHOWING_DESKTOP":
            self.showing = int(data[0])
        if type_name == "_NET_WM_STATE" and self.wm_honours_state:
            # what the caller now reads back to tell "sent" from "applied"
            action = data[0]
            atoms = [a for a in (data[1], data[2]) if a]
            have = self.states.setdefault(win, set())
            horz = self.atoms.get("_NET_WM_STATE_MAXIMIZED_HORZ")
            ref = horz if horz in atoms else atoms[0]
            on = (action == 1) or (action == 2 and ref not in have)
            for atom in atoms:
                if on:
                    have.add(atom)
                else:
                    have.discard(atom)

    def close(self):
        pass


class GnomeCliBase(_Base):
    """A mock bridge per test and a GnomeBackend on it; `run` drives the
    CLI with the X plane faked and records what _x11_connect was asked."""

    def setUp(self):
        self.bridge = MockBridge(self.mock, select_id=EDITOR,
                                 select_delay=0.05)
        self.backend = GnomeBackend(settle=0.05)
        self.x_calls = []

    def tearDown(self):
        self.backend.bus.close()
        self.bridge.close()

    def calls(self, member):
        return [a for m, a in self.bridge.calls if m == member]

    def wm(self, argv, x11="auto", xwayland=None, detect=None):
        """x11: FakeX11 to hand out, None for "unreachable", "auto" for a
        FakeX11 when an X window is listed; xwayland: what
        session.xwayland_running() reports (None: leave it to the box)."""
        if x11 == "auto":
            x11 = FakeX11() if any(d["xid"] for d in self.bridge.windows) \
                else None
        if x11 is not None:
            x11.bridge = self.bridge
        self.x11 = x11

        def connect(display=None, xauthority=None):
            self.x_calls.append((display, xauthority))
            return x11

        patches = [
            mock.patch.object(core, "_detect_backend",
                              detect or (lambda: self.backend)),
            mock.patch.object(core, "_x11_connect", connect),
            mock.patch.object(core, "hostname", lambda: "testhost"),
            mock.patch.object(sys, "argv", ["wmctrl"]),
        ]
        if xwayland is not None:
            patches.append(mock.patch.object(session, "xwayland_running",
                                             lambda uid=None: xwayland))
        out, err = io.StringIO(), io.StringIO()
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(list(argv))
        finally:
            for p in patches:
                p.stop()
        # One run of the tool is one process: by the time the next one
        # starts, the clients have answered the configures this one caused.
        # Two calls inside a single run do not get that (MockBridge.settle).
        self.bridge.settle()
        return rc, out.getvalue(), err.getvalue()


class ListingTests(GnomeCliBase):
    def test_l_ids_planes_and_columns(self):
        rc, out, err = self.wm(["-l"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        # stacking order bottom->top, X ids for XWayland, bridge ids native
        self.assertEqual(out.splitlines(), [
            "0x003ffffd  0 testhost Desktop",
            "0x00400002  0 testhost Untitled Document 1 - Text Editor",
            "0x00400003  1 testhost Calculator",
            "0x00400005  0 testhost test@vm: ~",
        ])
        # one listing round trip, then XInfo for the X plane (an XWayland
        # window is listed) -- which this run leaves unreachable
        self.assertEqual([m for m, _ in self.bridge.calls],
                         ["ListWorkspaces", "ListWindows", "XInfo"])

    def test_lpGx_without_x_plane(self):
        rc, out, _e = self.wm(["-lpGx"], x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0x003ffffd  0 900    0    0    1920 1080 Gjs.Gjs               "
            "testhost Desktop",
            "0x00400002  0 1300   300  200  800  600  "
            "org.gnome.TextEditor.org.gnome.TextEditor  "
            "testhost Untitled Document 1 - Text Editor",
            "0x00400003  1 1400   500  300  400  500  "
            "org.gnome.Calculator.org.gnome.Calculator  testhost Calculator",
            # the bridge's WM_CLASS pair stands in when X is unreachable
            "0x00400005  0 1201   100  80   640  480  xterm.XTerm           "
            "testhost test@vm: ~",
        ])

    def test_l_x_enrichment_uses_the_bridge_display_and_cookie(self):
        rc, out, _e = self.wm(["-lGx"])
        self.assertEqual(rc, 0)
        # one X connection, opened with what XInfo said
        self.assertEqual(self.x_calls, [(":0", XAUTH)])
        lines = out.splitlines()
        # machine column: WM_CLIENT_MACHINE from X for the xterm, hostname
        # for native windows; right-aligned to the LONGEST of the two
        self.assertEqual(lines[-1], "0x00400005  0 100  117  640  443  "
                                    "xterm.XTerm             vmhost test@vm: ~")
        self.assertEqual(lines[0], "0x003ffffd  0 0    0    1920 1080 "
                                   "Gjs.Gjs               testhost Desktop")

    def test_l_machine_column_does_not_reflow_when_a_window_is_raised(self):
        """wwmctl-3: wmctrl 1.07 sizes the machine column from the LAST
        row, which is stable only because its list is in creation order.
        Ours is stacking order, so the last row changes whenever a window
        is raised; the column is sized from the longest instead and the
        listing is identical in either order."""
        first = self.wm(["-l"])[1]
        # raise a native (long machine) above the xterm (short machine):
        # the last row's machine changes, the column must not
        self.bridge.m_Raise(None, CALC)
        second = self.wm(["-l"])[1]
        self.assertEqual(sorted(first.splitlines()),
                         sorted(second.splitlines()))
        for line in first.splitlines():
            self.assertIn(line, second.splitlines())
        # ... and the short machine really is padded out to the long one
        self.assertIn("   vmhost test@vm: ~", first)

    def test_an_id_shared_by_two_windows_is_reported_once(self):
        """wwmctl-8: bridge ids and X11 ids share one integer space --
        Mutter seeds meta_display_generate_window_id() from g_random_int(),
        so a native window CAN carry the id of an XWayland window. `-i`
        resolves the X one and the other becomes unreachable by id; say so
        rather than acting on a window the caller did not mean."""
        self.bridge.find(CALC)["id"] = XTERM_XID
        rc, _o, err = self.wm(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "wwmctl: two windows share the id 0x%08x; "
                              "-i uses the X11 one\n" % XTERM_XID)
        # ... and the X window is the one -i acts on
        rc, _o, _e = self.wm(["-i", "-a", "0x%x" % XTERM_XID])
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("Activate"), [(XTERM,)])

    def test_no_x_windows_means_no_x_connection(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        rc, out, _e = self.wm(["-lx"], x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertEqual(self.x_calls, [])  # Xwayland is never spawned
        self.assertEqual(len(out.splitlines()), 3)

    def test_x_info_falls_back_to_the_session_scan(self):
        self.bridge.xinfo = ("", "")
        with mock.patch.object(session, "find_x_display", lambda uid=None: ":7"), \
                mock.patch.object(session, "find_xauthority",
                                  lambda uid=None: "/rt/.mutter-Xwaylandauth.Z"):
            rc, _o, _e = self.wm(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.x_calls, [(":7", "/rt/.mutter-Xwaylandauth.Z")])

    def test_sticky_window_prints_desktop_minus_one(self):
        self.bridge.find(CALC).update(on_all_workspaces=True, workspace=-1)
        rc, out, _e = self.wm(["-l"], x11=None)
        self.assertIn("0x00400003 -1 testhost Calculator", out.splitlines())


class DesktopTests(GnomeCliBase):
    def test_d_from_list_workspaces(self):
        rc, out, err = self.wm(["-d"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "0  * DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  Workspace 1",
            "1  - DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  Workspace 2",
            "2  - DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  Workspace 3",
        ])
        self.assertIn("DisplaySize", [m for m, _ in self.bridge.calls])
        self.assertIn("ListWorkspaces", [m for m, _ in self.bridge.calls])

    def test_d_viewports_are_the_x_servers(self):
        # wmctrl prints _NET_DESKTOP_VIEWPORT[2i],[2i+1] per desktop and N/A
        # when the array is too short: a WM that publishes one pair per
        # desktop (KWin does) gets `VP: 0,0` on every row -- ours said N/A.
        x = FakeX11(viewport=[0, 0, 0, 0, 0, 0])
        rc, out, err = self.wm(["-d"], x11=x, xwayland=True)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([ln.split("VP: ")[1].split()[0]
                          for ln in out.splitlines()], ["0,0"] * 3)
        # a single pair is the CURRENT desktop's and no other row's -- row 0
        # gets it only when row 0 is the current one, which a plain [2i] rule
        # gets wrong [M 2026-09-11, Xvfb :81, the pinned wmctrl 1.07 over a
        # hand-built EWMH root: `[7,9]` with 2 desktops prints `7,9`/`N/A`
        # for cur=0 and `N/A`/`7,9` for cur=1]
        x = FakeX11(viewport=[7, 9])
        rc, out, _e = self.wm(["-d"], x11=x, xwayland=True)
        self.assertEqual([ln.split("VP: ")[1].split()[0]
                          for ln in out.splitlines()], ["7,9", "N/A", "N/A"])
        self.bridge.active_ws = 1
        self.bridge._refresh_active()
        self.addCleanup(self.bridge._refresh_active)
        self.addCleanup(setattr, self.bridge, "active_ws", 0)
        x = FakeX11(viewport=[7, 9])
        rc, out, _e = self.wm(["-d"], x11=x, xwayland=True)
        self.assertEqual([ln.split("VP: ")[1].split()[0]
                          for ln in out.splitlines()], ["N/A", "7,9", "N/A"])
        # a longer-but-short array is indexed all the same, and its N/A stays
        # past the end rather than moving to the current row [same reading:
        # `[7,9,1,2]` over 3 desktops prints `7,9`/`1,2`/`N/A` for every cur]
        x = FakeX11(viewport=[7, 9, 1, 2])
        rc, out, _e = self.wm(["-d"], x11=x, xwayland=True)
        self.assertEqual([ln.split("VP: ")[1].split()[0]
                          for ln in out.splitlines()], ["7,9", "1,2", "N/A"])
        self.bridge.active_ws = 0
        self.bridge._refresh_active()
        # no X plane at all: every desktop's origin, which is what a WM that
        # publishes viewports prints and what real wmctrl prints through xw11,
        # whose root publishes one `0,0` pair per desktop [M
        # goal2/recon/gaps.md 3a, Xvfb :77, 2026-09-11]
        self.assertEqual(len(self.x_calls), 4)   # one per run above
        self.x_calls = []
        rc, out, _e = self.wm(["-d"], x11=None, xwayland=False)
        self.assertEqual([ln.split("VP: ")[1].split()[0]
                          for ln in out.splitlines()], ["0,0", "0,0", "0,0"])
        self.assertEqual(self.x_calls, [])

    def test_d_nameless_workspace_prints_its_index(self):
        orig = self.bridge.m_ListWorkspaces

        def nameless(m):
            import json
            sig, (raw,) = orig(m)
            rows = json.loads(raw)
            rows[1]["name"] = ""
            return sig, (json.dumps(rows),)
        self.bridge.m_ListWorkspaces = nameless
        rc, out, _e = self.wm(["-d"], x11=None)
        self.assertEqual(out.splitlines()[1],
                         "1  - DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  1")

    def test_s(self):
        rc, _o, err = self.wm(["-s", "2"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetActiveWorkspace"), [(2,)])
        rc, _o, err = self.wm(["-s", "9"], x11=None)
        self.assertEqual((rc, err), (1, "workspace 9 not found\n"))
        rc, _o, err = self.wm(["-s", "-1"], x11=None)
        self.assertEqual((rc, err), (1, "Invalid desktop ID.\n"))

    def test_k_goes_through_show_desktop(self):
        rc, _o, err = self.wm(["-k", "on"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(["-k", "off"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("ShowDesktop"), [(True,), (False,)])
        rc, _o, err = self.wm(["-k", "maybe"], x11=None)
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("ShowDesktop"), [(True,), (False,)])

    def test_k_on_twice_still_restores_the_desktop(self):
        """wwmctl-2: `-k on` is a latch, not a stack. The bridge's scan
        skips already-minimized windows, so a second `on` must not rescan:
        it would replace the restore set with an empty one and leave every
        later `-k off` a no-op."""
        def minimized():
            return {d["id"] for d in self.bridge.windows if d["minimized"]}

        before = minimized()
        self.assertEqual(before, {EDITOR})       # the fixture's editor
        self.assertEqual(self.wm(["-k", "on"], x11=None)[0], 0)
        on1 = minimized()
        self.assertIn(XTERM, on1)                # something really went away
        self.assertEqual(self.wm(["-k", "on"], x11=None)[0], 0)
        self.assertEqual(minimized(), on1)       # no-op, restore set intact
        self.assertEqual(self.wm(["-k", "off"], x11=None)[0], 0)
        self.assertEqual(minimized(), before)    # everything came back
        self.assertEqual(self.calls("ShowDesktop"),
                         [(True,), (True,), (False,)])

    def test_k_prefers_the_x_plane(self):
        """wwmctl-6: Mutter's real show-desktop mode is reachable with the
        _NET_SHOWING_DESKTOP ClientMessage real wmctrl sends, and on GNOME
        it works: every window is hidden, `-k off` brings them all back
        untouched, and `-m` (the same property) finally agrees. The
        bridge's minimize-everything stand-in is the fallback, for a
        session with no X plane."""
        x = FakeX11(showing=0)
        rc, _o, err = self.wm(["-k", "on"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([c for c in x.calls if c[0] == "client_message"],
                         [("client_message", x.root(),
                           "_NET_SHOWING_DESKTOP", (1, 0, 0, 0, 0))])
        self.assertEqual(self.calls("ShowDesktop"), [])   # not the stand-in
        self.assertEqual(x.showing, 1)
        rc, _o, err = self.wm(["-k", "off"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(x.showing, 0)
        self.assertEqual(self.calls("ShowDesktop"), [])

    def test_k_falls_back_to_the_bridge_when_the_wm_does_not_answer(self):
        class DeafX11(FakeX11):
            def send_root_message(self, win, type_name, data):
                self.calls.append(("client_message", win, type_name,
                                   tuple(data)))   # ... and nothing happens

        with mock.patch.object(core.time, "sleep"):
            rc, _o, err = self.wm(["-k", "on"], x11=DeafX11(showing=0))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("ShowDesktop"), [(True,)])

    def test_k_toggle_reads_the_x_root_state(self):
        """wwmctl-5: 1.07+git's `-k toggle` flips whatever
        _NET_SHOWING_DESKTOP says; with no X plane to ask, an absent
        property reads as off, like the oracle's (NULL-dereferencing)
        default."""
        x = FakeX11(showing=0)
        rc, _o, err = self.wm(["-k", "toggle"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(x.showing, 1)
        rc, _o, err = self.wm(["-k", "toggle"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(x.showing, 0)
        # no X plane: "off" is the reading, so toggle asks the bridge for on
        rc, _o, err = self.wm(["-k", "toggle"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("ShowDesktop"), [(True,)])

    def test_j_prints_the_active_workspace(self):
        rc, out, err = self.wm(["-j"], x11=None)
        self.assertEqual((rc, out, err), (0, "0 \n", ""))
        self.bridge.m_SetActiveWorkspace(None, 2)
        rc, out, _e = self.wm(["-j"], x11=None)
        self.assertEqual(out, "2 \n")

    def test_Y_z_E_reach_the_bridge(self):
        rc, _o, err = self.wm(["-Y", "Calculator"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Minimize"), [(CALC,)])
        rc, _o, err = self.wm(["-z", "Calculator"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Lower"), [(CALC,)])
        rc, out, err = self.wm(["-E", "Calculator"], x11=None)
        self.assertEqual((rc, out, err), (0, "Calculator\n", ""))

    def test_n_dynamic_workspaces_warns_and_succeeds(self):
        rc, _o, err = self.wm(["-n", "5"], x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("SetNWorkspaces"), [(5,)])
        self.assertTrue(err.startswith("wwmctl: dynamic workspaces are enabled"))
        self.assertTrue(err.rstrip().endswith("; ignoring"))
        self.assertEqual(err.count("\n"), 1)

    def test_o_and_g_still_warn(self):
        for argv in (["-o", "0,0"], ["-g", "1920,1080"]):
            rc, _o, err = self.wm(argv, x11=None)
            self.assertEqual(rc, 0)
            self.assertIn("ignoring", err)
        self.assertNotIn("ShowDesktop", [m for m, _ in self.bridge.calls])


class WmInfoTests(GnomeCliBase):
    EXPECT = ("Name: GNOME Shell\nClass: N/A\nPID: N/A\n"
              'Window manager\'s "showing the desktop" mode: %s\n')

    def test_m_from_the_x_plane_when_xwayland_is_up(self):
        rc, out, _e = self.wm(["-m"], x11=FakeX11(showing=1))
        self.assertEqual((rc, out), (0, self.EXPECT % "ON"))
        self.assertEqual(self.x_calls, [(":0", XAUTH)])

    def test_m_from_the_bridge_when_xwayland_is_down(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        rc, out, _e = self.wm(["-m"], x11=FakeX11(), xwayland=False)
        self.assertEqual((rc, out), (0, self.EXPECT % "N/A"))
        self.assertEqual(self.x_calls, [])  # nothing spawned Xwayland

    def test_m_xwayland_up_without_x_windows_still_asks_x(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        rc, out, _e = self.wm(["-m"], x11=FakeX11(), xwayland=True)
        self.assertEqual((rc, out), (0, self.EXPECT % "OFF"))
        self.assertEqual(self.x_calls, [(":0", XAUTH)])


class ActionTests(GnomeCliBase):
    def test_a_by_title_and_by_either_id(self):
        rc, _o, err = self.wm(["-a", "calcu"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, _e = self.wm(["-i", "-a", "0x400005"], x11=None)  # X id
        self.assertEqual(rc, 0)
        rc, _o, _e = self.wm(["-i", "-a", "%d" % XTERM], x11=None)  # bridge id
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("Activate"), [(CALC,), (XTERM,), (XTERM,)])
        rc, _o, err = self.wm(["-i", "-a", "0x999"], x11=None)
        # exit 1 like wmctrl, and a line saying which id: real wmctrl asks
        # the X server about an -i id and Xlib prints BadWindow
        self.assertEqual(rc, 1)
        self.assertIn("no window with id 0x00000999", err)

    def test_x_matches_class_pairs(self):
        rc, _o, _e = self.wm(["-x", "-a", "TextEditor"], x11=None)
        self.assertEqual(self.calls("Activate"), [(EDITOR,)])
        rc, _o, _e = self.wm(["-x", "-F", "-a", "xterm.XTerm"], x11=None)
        self.assertEqual(self.calls("Activate"), [(EDITOR,), (XTERM,)])

    def test_c_and_active_magic(self):
        rc, _o, _e = self.wm(["-c", ":ACTIVE:"], x11=None)
        self.assertEqual((rc, self.calls("Close")), (0, [(XTERM,)]))
        self.assertNotIn(XTERM, [d["id"] for d in self.bridge.windows])

    def test_select_magic_asks_for_a_click(self):
        # The GNOME bridge's picker answers on the next button press, so the
        # hint says click -- "focus the target window" was true only of the
        # focus-change wait sway is still stuck with.
        rc, _o, err = self.wm(["-a", ":SELECT:"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("click the target window to select it", err)
        self.assertNotIn("focus the target window", err)
        self.assertEqual(self.calls("SelectWindow"), [(0,)])
        self.assertEqual(self.calls("Activate"), [(EDITOR,)])

    def test_R_moves_to_the_current_desktop_then_activates(self):
        self.bridge.active_ws = 2
        self.bridge._refresh_active()
        with mock.patch.object(core.time, "sleep") as sl:
            rc, _o, _e = self.wm(["-R", "Calculator"], x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("MoveToWorkspace"), [(CALC, 2)])
        self.assertEqual(self.calls("Activate"), [(CALC,)])
        sl.assert_called()  # the non-sway grace period

    def test_t(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-t", "2"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(["-r", "Calculator", "-t", "-1"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("MoveToWorkspace"), [(CALC, 2), (CALC, 0)])
        rc, _o, err = self.wm(["-r", "Calculator", "-t", "7"], x11=None)
        self.assertEqual((rc, err), (1, "workspace 7 not found\n"))

    def test_e_client_rectangle_with_the_frame_extents(self):
        # gravity 0 (the window's own: NorthWest): the frame's top-left at
        # X,Y; W,H are the CLIENT size, so the frame is one SSD bar taller
        # (the X plane says the client sits 37 px below the frame)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "0,10,20,300,200"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 237)])
        self.assertEqual(self.calls("Move"), [(XTERM, 10, 20)])
        # -1 keeps the current value: the frame stays at y=20 and 300 wide
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "0,50,-1,-1,-1"])
        self.assertEqual(self.calls("Move")[-1], (XTERM, 50, 20))
        self.assertEqual(len(self.calls("Resize")), 1)
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "0,-1,-1,-1,700"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 737))
        self.assertEqual(len(self.calls("Move")), 2)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "1,2"])
        self.assertEqual(rc, 1)
        self.assertIn("gravity,X,Y,width,height", err)

    def test_e_gravity_places_the_named_frame_point(self):
        # frame 100,80 640x480 over client 100,117 640x443: extents
        # 0,37,0,0.  SouthEast: the frame's bottom-right corner lands on
        # the requested client rectangle's, (X+W, Y+H)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,1000,900,300,200"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 237))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 1000, 863))
        # Center: the frame centred on the requested rectangle's centre
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "5,500,400,300,200"])
        self.assertEqual(self.calls("Move")[-1], (XTERM, 500, 382))
        # Static: the client itself at X,Y, the frame one bar higher; the
        # -1s keep the client size
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "10,200,300,-1,-1"])
        self.assertEqual(self.calls("Move")[-1], (XTERM, 200, 263))
        self.assertEqual(len(self.calls("Resize")), 2)
        # Static with -1 for the position keeps the frame where it is
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "10,-1,-1,400,100"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 400, 137))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 200, 263))

    def test_e_bare_resize_keeps_the_gravity_point(self):
        # `-e 9,-1,-1,W,H` pins the frame's bottom-right corner and grows
        # up and to the left, as Mutter does for real wmctrl (live: a
        # frame 200,150 500x400 asked for a 300x250 client keeps its
        # 700,550 corner and lands at 400,263 with a 300x287 frame)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,-1,-1,300,250"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 287)])
        self.assertEqual(self.calls("Move"), [(XTERM, 440, 273)])  # 740,560
        # Center keeps the frame's centre (440+150, 273+143 = 590, 416)
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "5,-1,-1,200,150"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 200, 187))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 490, 323))
        # NorthWest pins the top-left, so a bare resize is a resize alone
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "0,-1,-1,300,200"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 237))
        self.assertEqual(len(self.calls("Move")), 2)
        # ... and a narrower window under East keeps its right edge (790)
        # while the unchanged height leaves the vertical centre alone
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "6,-1,-1,150,-1"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 150, 237))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 640, 323))

    def test_e_x_minus_one_with_a_y_keeps_the_left_edge(self):
        """wwmctl-4: only a bare `G,-1,-1,W,H` is anchored on the gravity
        point. Give one coordinate and Mutter keeps the other axis'
        unchanged frame edge -- measured on GNOME 46, where anchoring it
        left `9,-1,200,...` 80 px away from where real wmctrl leaves the
        window. Frame 100,80 640x480 over client 100,117 640x443."""
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,-1,200,300,250"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 287)])
        # x: the frame's left edge stays at 100 (was 440 with the anchor)
        # y: SouthEast puts the frame's bottom on the requested client's,
        #    200 + 250 - 287
        self.assertEqual(self.calls("Move"), [(XTERM, 100, 163)])

    def test_e_y_minus_one_with_an_x_keeps_the_top_edge(self):
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "5,300,-1,300,250"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 287)])
        # Center on x: 300 + 150 - 150; y keeps the frame's top (was 177)
        self.assertEqual(self.calls("Move"), [(XTERM, 300, 80)])

    def test_e_bare_resize_on_gnome_50_keeps_the_corner(self):
        """wwmctl-4, the other half: GNOME 46 anchors a bare
        `-e G,-1,-1,W,H` on the gravity point, GNOME 50 applies no gravity
        to it at all and keeps the top-left corner. Measured against real
        wmctrl on both; the compositor says which it is."""
        self.bridge.shell_version = "50.1"
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,-1,-1,300,250"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 287)])
        self.assertEqual(self.calls("Move"), [])   # the corner does not move

    def test_e_bare_resize_falls_back_when_no_version_is_reported(self):
        """A backend that will not say (sway, a shell that hides the
        property) keeps the older, documented behaviour."""
        self.bridge.shell_version = None
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,-1,-1,300,250"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Move"), [(XTERM, 440, 273)])

    def test_e_without_frame_extents_is_the_frame_rectangle(self):
        # a native window: the frame is the client, so every gravity puts
        # X,Y,W,H on the rectangle -lG prints
        for grav in ("0", "5", "9", "10"):
            rc, _o, err = self.wm(["-r", "Calculator", "-e",
                                   "%s,100,100,320,240" % grav], x11=None)
            self.assertEqual((rc, err), (0, ""), grav)
            self.assertEqual(self.calls("Resize")[-1], (CALC, 320, 240), grav)
            self.assertEqual(self.calls("Move")[-1], (CALC, 100, 100), grav)
        # an XWayland window with the X plane unreachable: the bridge's
        # frame rect is all there is, so it is addressed directly
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,10,20,300,200"],
                              x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 200))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 10, 20))

    def test_e_drops_the_request_when_the_window_will_not_hold_still(self):
        """wwmctl-1: the frame rect (compositor) and the client rect (X
        server) are read a round trip apart. On a window that is moving the
        difference is meaningless -- it used to be silently zeroed, which
        collapsed every gravity to NorthWest and resized the frame to the
        client size. Now nothing is sent and stderr says why."""
        class MovingX11(FakeX11):
            n = 0

            def get_geometry(self, win):
                x, y, w, h = FakeX11.get_geometry(self, win)
                self.n += 1
                return (x, y + 40 * self.n, w, h)

        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,-1,-1,300,250"],
                              x11=MovingX11())
        self.assertEqual(rc, 0)
        self.assertEqual(
            err, "wwmctl: window moved while measuring the frame; ignoring\n")
        self.assertEqual(self.calls("Resize"), [])
        self.assertEqual(self.calls("Move"), [])

    def test_e_measures_again_until_the_window_settles(self):
        """The same sampling accepts a window that moved once and then
        stopped: two consecutive agreeing pairs are enough."""
        class SettlingX11(FakeX11):
            n = 0

            def get_geometry(self, win):
                x, y, w, h = FakeX11.get_geometry(self, win)
                self.n += 1
                return (x, y + (40 if self.n == 1 else 0), w, h)

        rc, _o, err = self.wm(["-r", "test@vm", "-e", "0,10,20,300,200"],
                              x11=SettlingX11())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 237)])
        self.assertEqual(self.calls("Move"), [(XTERM, 10, 20)])

    def test_b_states_mutter_cannot_set_go_through_the_x_server(self):
        """wwmctl-6: the bridge answers "not applied" for below,
        skip_taskbar, skip_pager, shaded and modal -- Mutter exports no
        Wayland setter. For an XWayland window Mutter is still the EWMH
        window manager, and the root ClientMessage real wmctrl sends does
        work (oracle-proven live). The compositor stays the first choice
        for everything else: `hidden` really minimizes through the bridge,
        where the X route is a no-op."""
        x = FakeX11()
        rc, _o, err = self.wm(["-r", "test@vm", "-b", "add,below"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetState"), [])
        self.assertEqual(
            [c for c in x.calls if c[0] == "client_message"],
            [("client_message", XTERM_XID, "_NET_WM_STATE",
              (1, x.atom("_NET_WM_STATE_BELOW"), 0, 0, 0))])
        # two properties at once, and remove/toggle carry their action
        x = FakeX11()
        rc, _o, err = self.wm(["-r", "test@vm", "-b",
                               "toggle,skip_taskbar,skip_pager"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([(c[3][0], c[2]) for c in x.calls
                          if c[0] == "client_message"],
                         [(2, "_NET_WM_STATE"), (2, "_NET_WM_STATE")])
        # a state the compositor CAN set still goes through the bridge
        x = FakeX11()
        rc, _o, err = self.wm(["-r", "test@vm", "-b", "add,hidden"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetState"), [(XTERM, "HIDDEN", "add")])
        self.assertEqual([c for c in x.calls if c[0] == "client_message"], [])

    def test_b_on_a_native_window_still_warns(self):
        """A native window has no X twin to ask, so the gap stays a
        warning rather than a ClientMessage into the void."""
        x = FakeX11()
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,below"], x11=x)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)
        self.assertEqual([c for c in x.calls if c[0] == "client_message"], [])

    def test_b_states_through_set_state(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,fullscreen"],
                               x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(
            ["-r", "Calculator", "-b", "toggle,maximized_vert,maximized_horz"],
            x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "remove,hidden"],
                               x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetState"), [
            (CALC, "FULLSCREEN", "add"), (CALC, "MAXIMIZED", "toggle"),
            (CALC, "HIDDEN", "remove")])
        d = self.bridge.find(CALC)
        self.assertTrue(d["fullscreen"] and d["maximized_v"] and d["maximized_h"])

    def test_b_the_maximize_pair_is_one_request(self):
        """wwmctl-4: `-b remove,maximized_vert,maximized_horz` -- wmctrl's
        documented way to unmaximize -- must reach Mutter as ONE call with
        both direction bits, and give the window back the rectangle it had.
        Live on GNOME 46 and 50 the two single-axis calls left a 200,150
        900x600 window at 200,32 900x1048; see core._state_steps."""
        d = self.bridge.find(CALC)
        start = (d["x"], d["y"], d["width"], d["height"])
        rc, _o, err = self.wm(["-r", "Calculator", "-b",
                               "add,maximized_vert,maximized_horz"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((d["x"], d["y"], d["width"], d["height"]), WORK_AREA)
        rc, _o, err = self.wm(["-r", "Calculator", "-b",
                               "remove,maximized_vert,maximized_horz"],
                              x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((d["x"], d["y"], d["width"], d["height"]), start)
        self.assertEqual((d["maximized_h"], d["maximized_v"]), (False, False))
        self.assertEqual(self.calls("SetState"),
                         [(CALC, "MAXIMIZED", "add"),
                          (CALC, "MAXIMIZED", "remove")])

    def test_b_two_single_axis_calls_are_what_corrupted_the_rect(self):
        """Why it is folded, kept as the fake's account of Mutter: with no
        client round trip in between, the second call still reads the
        maximized frame rect and carries half of it into its target -- and
        that rectangle then becomes the restore size, which is why nothing
        short of an explicit resize got the window back afterwards."""
        d = self.bridge.find(CALC)
        start = (d["x"], d["y"], d["width"], d["height"])
        self.bridge.m_SetState(None, CALC, "MAXIMIZED", "add")
        self.bridge.settle()
        self.bridge.m_SetState(None, CALC, "MAXIMIZED_VERT", "remove")
        self.bridge.m_SetState(None, CALC, "MAXIMIZED_HORZ", "remove")
        self.assertEqual((d["maximized_h"], d["maximized_v"]), (False, False))
        self.assertEqual((d["x"], d["y"], d["width"], d["height"]),
                         (start[0], WORK_AREA[1], start[2], WORK_AREA[3]))
        self.assertEqual(self.bridge.saved[CALC][3], WORK_AREA[3])

    def test_b_only_the_two_maximize_axes_together_are_folded(self):
        """The neighbours of the fold: one axis, an axis with something
        else, and the same axis twice all stay one request per property."""
        for arg, sent in (
                ("add,maximized_vert",
                 [(CALC, "MAXIMIZED_VERT", "add")]),
                ("remove,maximized_horz",
                 [(CALC, "MAXIMIZED_HORZ", "remove")]),
                ("add,maximized_vert,fullscreen",
                 [(CALC, "MAXIMIZED_VERT", "add"), (CALC, "FULLSCREEN", "add")]),
                ("add,maximized_horz,maximized_horz",
                 [(CALC, "MAXIMIZED_HORZ", "add")] * 2)):
            self.bridge.calls = []
            rc, _o, err = self.wm(["-r", "Calculator", "-b", arg], x11=None)
            self.assertEqual((rc, err), (0, ""), arg)
            self.assertEqual(self.calls("SetState"), sent, arg)

    def test_b_a_backend_without_a_pair_name_still_sends_two(self):
        """KWin and sway settle each axis before they answer, so there is
        nothing to fold there and the two calls stay two calls."""
        with mock.patch.object(type(self.backend), "maximize_pair_state",
                               lambda self: None):
            rc, _o, err = self.wm(["-r", "Calculator", "-b",
                                   "remove,maximized_vert,maximized_horz"],
                                  x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetState"),
                         [(CALC, "MAXIMIZED_VERT", "remove"),
                          (CALC, "MAXIMIZED_HORZ", "remove")])

    def test_b_single_axis_and_one_axis_of_a_pair_keep_the_rectangle(self):
        """The neighbours that were already right stay right: one axis up
        and down again, and removing the pair when only one axis was set."""
        d = self.bridge.find(CALC)
        start = (d["x"], d["y"], d["width"], d["height"])
        for axis in ("maximized_vert", "maximized_horz"):
            self.wm(["-r", "Calculator", "-b", "add," + axis], x11=None)
            self.assertNotEqual((d["x"], d["y"], d["width"], d["height"]),
                                start, axis)
            self.wm(["-r", "Calculator", "-b", "remove," + axis], x11=None)
            self.assertEqual((d["x"], d["y"], d["width"], d["height"]),
                             start, axis)
            self.wm(["-r", "Calculator", "-b", "add," + axis], x11=None)
            self.wm(["-r", "Calculator", "-b",
                     "remove,maximized_vert,maximized_horz"], x11=None)
            self.assertEqual((d["x"], d["y"], d["width"], d["height"]),
                             start, axis)
            self.assertEqual((d["maximized_h"], d["maximized_v"]),
                             (False, False), axis)

    def test_b_toggle_of_the_pair_follows_the_horizontal_flag(self):
        """A pair that is half set has to go one way or the other. Mutter
        decides that on the horizontal flag when it reads two atoms out of
        one _NET_WM_STATE message -- which is the message real wmctrl sends
        -- so the pair never lands maximized on the other axis alone."""
        d = self.bridge.find(CALC)
        pair = ["-b", "toggle,maximized_vert,maximized_horz"]
        self.wm(["-r", "Calculator", "-b", "add,maximized_horz"], x11=None)
        self.wm(["-r", "Calculator"] + pair, x11=None)
        self.assertEqual((d["maximized_h"], d["maximized_v"]), (False, False))
        self.wm(["-r", "Calculator", "-b", "add,maximized_vert"], x11=None)
        self.wm(["-r", "Calculator"] + pair, x11=None)
        self.assertEqual((d["maximized_h"], d["maximized_v"]), (True, True))

    def _refuse_pair(self, *states):
        """Make the bridge answer "not applied" for these state names (the
        folded pair, by default), which is what drives the -b step onto the
        X fallback."""
        states = frozenset(states or ("MAXIMIZED",))
        orig = self.bridge.m_SetState

        def refuse(m, wid, state, action):
            if state in states:
                return "b", (False,)
            return orig(m, wid, state, action)

        self.bridge.m_SetState = refuse

    def test_b_the_pair_falls_back_to_one_message_with_both_atoms(self):
        """A bridge that will not do the pair leaves an XWayland window with
        the message real wmctrl sends: ONE _NET_WM_STATE ClientMessage
        carrying both atoms, in data.l[1] and data.l[2].

        Two messages are not the same request. wmctrl's own
        `-b remove,maximized_vert,maximized_horz` is one message, and Mutter
        turns its two atoms into a single `directions` bitmask and one
        meta_window_set_unmaximize_flags() call (window-x11.c) -- which is
        precisely what stops the restore size being corrupted (measured on
        GNOME 46 and 50: two calls left a 200,150 900x600 window at 200,32
        900x1048, see core._state_steps). Sending two here rebuilt the bug
        the fold exists to avoid, on the one route that is supposed to be
        byte-for-byte wmctrl."""
        self._refuse_pair()
        x = FakeX11()
        rc, _o, err = self.wm(["-r", "test@vm", "-b",
                               "remove,maximized_vert,maximized_horz"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([c for c in x.calls if c[0] == "client_message"],
                         [("client_message", XTERM_XID, "_NET_WM_STATE",
                           (0, x.atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                            x.atom("_NET_WM_STATE_MAXIMIZED_HORZ"), 0, 0))])
        # and both atoms are read back, not just the first: two before the
        # message and two in the settle loop
        self.assertEqual(x.state_reads, [XTERM_XID] * 4)

    def test_b_a_toggle_of_the_pair_on_x_follows_the_horizontal_atom(self):
        """One message, so Mutter makes one decision for both axes and takes
        it off the horizontal flag: a window maximized horizontally only
        ends with BOTH atoms gone, never with the axes swapped. Two separate
        toggles would have cleared HORZ and set VERT."""
        self._refuse_pair()
        x = FakeX11()
        horz = x.atom("_NET_WM_STATE_MAXIMIZED_HORZ")
        vert = x.atom("_NET_WM_STATE_MAXIMIZED_VERT")
        x.states[XTERM_XID] = {horz}
        rc, _o, err = self.wm(["-r", "test@vm", "-b",
                               "toggle,maximized_vert,maximized_horz"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(x.states[XTERM_XID], set())
        self.assertEqual([c[3] for c in x.calls if c[0] == "client_message"],
                         [(2, vert, horz, 0, 0)])

    def test_b_a_single_axis_toggle_on_x_reads_its_own_atom(self):
        """The other half of the rule, and the reason _x_set_state cannot
        just always read the horizontal flag: `-b toggle,maximized_vert` is
        ONE atom in data.l[1] and 0 in data.l[2], and Mutter decides it on
        maximized_vertically. On a window that is maximized horizontally
        only, that toggle ADDS the vertical atom -- the window ends up
        maximized on both axes, not unmaximized -- and the read-back has to
        expect exactly that or the caller is told the state "did not appear"
        on a window manager that did what it was asked."""
        self._refuse_pair("MAXIMIZED_VERT")
        x = FakeX11()
        horz = x.atom("_NET_WM_STATE_MAXIMIZED_HORZ")
        vert = x.atom("_NET_WM_STATE_MAXIMIZED_VERT")
        x.states[XTERM_XID] = {horz}
        rc, _o, err = self.wm(["-r", "test@vm", "-b", "toggle,maximized_vert"],
                              x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(x.states[XTERM_XID], {horz, vert})
        self.assertEqual([c[3] for c in x.calls if c[0] == "client_message"],
                         [(2, vert, 0, 0, 0)])
        # one atom asked for, so one atom read before and one after
        self.assertEqual(x.state_reads, [XTERM_XID] * 2)

    def test_b_gaps_warn_and_succeed(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,shaded,below"],
                               x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(err.count("; ignoring"), 2)
        self.assertIn("SHADED", err)
        self.assertIn("BELOW", err)
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,skip_taskbar"],
                               x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_N_I_T_on_an_x_window_go_to_the_x_plane(self):
        for mode, icon, long_ in (("N", False, True), ("I", True, False),
                                  ("T", True, True)):
            x11 = FakeX11()
            with mock.patch.dict(os.environ, {"LC_ALL": "C", "LC_CTYPE": "C",
                                              "LANG": "C"}):
                rc, _o, err = self.wm(["-r", "test@vm", "-%s" % mode, "New"],
                                      x11=x11)
            self.assertEqual((rc, err), (0, ""), mode)
            self.assertEqual(x11.calls,
                             [("set_name", XTERM_XID, "New", icon, long_,
                               False)])
        self.assertEqual(self.x_calls, [(":0", XAUTH)] * 3)

    def test_N_carries_the_utf8_environment(self):
        """wwmctl-7: -u (and any UTF-8 locale) is wmctrl's envir_utf8, and
        window_set_title has no locale copy to write there: it DELETES the
        legacy WM_NAME instead of leaving a lossy STRING behind. The flag
        never left cli, so both properties were always written."""
        with mock.patch.dict(os.environ, {"LC_ALL": "C", "LC_CTYPE": "C",
                                          "LANG": "C"}):
            x11 = FakeX11()
            rc, _o, err = self.wm(["-r", "test@vm", "-N", "New"], x11=x11)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(x11.calls[0][5], False)
            x11 = FakeX11()
            rc, _o, err = self.wm(["-u", "-r", "test@vm", "-N", "New"],
                                  x11=x11)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(x11.calls[0][5], True)
        with mock.patch.dict(os.environ, {"LC_ALL": "en_US.UTF-8"}):
            x11 = FakeX11()
            rc, _o, err = self.wm(["-r", "test@vm", "-T", "New"], x11=x11)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(x11.calls[0][5], True)

    def test_N_on_a_native_window_warns_and_succeeds(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-N", "New"], x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertIn("native window", err)
        self.assertIn("ignoring", err)
        self.assertEqual(self.x11.calls, [])

    def test_N_on_x_window_with_x_unreachable_warns(self):
        rc, _o, err = self.wm(["-r", "test@vm", "-N", "New"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("cannot reach the XWayland server", err)


class ErrorPathTests(_Base):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "_x11_connect",
                               lambda display=None, xauthority=None: None), \
                mock.patch.object(sys, "argv", ["wmctrl"]), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        backend_detect.reset()

    def tearDown(self):
        backend_detect.reset()

    def test_real_detection_over_the_mock_bus(self):
        bridge = MockBridge(self.mock)
        try:
            rc, out, err = self._run(["-l"])
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(len(out.splitlines()), 4)
            self.assertIn("0x00400005  0 ", out)
        finally:
            bridge.close()

    def test_bridge_not_installed_is_one_clear_line(self):
        """Not installed is a state of the DISK, and this test asked the
        runner's own ~/.local/share for it: on a developer box that happens
        to have the bridge installed the message under test was a different
        one entirely. It is a double now, and the name of the test is the
        thing it sets."""
        self.addCleanup(setattr, backend_gnome, "extension_installed",
                        backend_gnome.extension_installed)
        backend_gnome.extension_installed = lambda: ""
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            for argv in (["-l"], ["-d"], ["-m"], ["-a", "x"], ["-s", "1"]):
                backend_detect.reset()
                rc, out, err = self._run(argv)
                self.assertEqual((rc, out), (1, ""), argv)
                self.assertEqual(err.count("\n"), 1, err)
                self.assertIn("gnome/install-bridge.sh", err)
        finally:
            bridge.close()

    def test_bridge_gone_mid_session(self):
        bridge = MockBridge(self.mock)
        b = GnomeBackend(settle=0.05)
        bridge.close()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "_detect_backend", lambda: b), \
                mock.patch.object(sys, "argv", ["wmctrl"]), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["-l"])
        b.bus.close()
        self.assertEqual(rc, 1)
        self.assertIn("bridge vanished", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_k_and_n_without_any_session_still_warn_and_succeed(self):
        # no bridge, no shell: the warn-and-succeed fallbacks need no session
        with mock.patch.object(core, "_detect_backend",
                               mock.Mock(side_effect=core.CmdError("nope"))):
            for argv in (["-k", "on"], ["-n", "3"]):
                rc, _o, err = self._run(argv)
                self.assertEqual(rc, 0, argv)
                self.assertIn("ignoring", err)


class EventsHookTests(_Base):
    def test_workspace_events_are_folded_in_on_request(self):
        import threading
        import time
        from w11common.dbus_mini import Bus
        bridge = MockBridge(self.mock)
        b = GnomeBackend(settle=0.05)
        emitter = Bus(self.mock.address)
        try:
            gen = b.events(timeout=3, workspaces=True)

            def fire():
                time.sleep(0.2)
                emitter.emit_signal(OBJECT_PATH, IFACE, "WorkspaceEvent", "s", ("switch",))
                emitter.emit_signal(OBJECT_PATH, IFACE, "WindowEvent", "ts", (CALC, "title"))
            threading.Thread(target=fire, daemon=True).start()
            self.assertEqual(next(gen), (0, "workspace"))
            self.assertEqual(next(gen), (CALC, "title"))
            gen.close()
        finally:
            emitter.close()
            b.bus.close()
            bridge.close()

    def test_show_desktop_and_set_num_desktops(self):
        bridge = MockBridge(self.mock)
        b = GnomeBackend(settle=0.05)
        try:
            b.show_desktop(True)
            b.show_desktop(False)
            self.assertEqual([a for m, a in bridge.calls if m == "ShowDesktop"],
                             [(True,), (False,)])
            with self.assertRaises(core.CmdError):
                b.set_num_desktops(4)
            self.assertEqual(b.wm_name, "GNOME Shell")
        finally:
            b.bus.close()
            bridge.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
