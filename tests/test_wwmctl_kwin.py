#!/usr/bin/env python3
"""wwmctl on Plasma: the KWin backend driven through the CLI.

tests/test_backend_kwin.py proves the backend against the fake KWin on the
in-process mock bus; this file is the tool on top of it, the way
tests/test_wwmctl_gnome.py is the tool on top of the mock bridge. What is
here and nowhere else is the Plasma-shaped answer to the questions the tool
asks: which id a window is listed under when half the session is XWayland,
which uuid an X id resolves to when two windows of one client are identical,
what `-b remove,maximized_vert,maximized_horz` turns into on a compositor
that has no name for the pair, and what the tool says when the X ids cannot
be worked out at all.

Measured on the rig (resolute-kde, Plasma 6.6 / KWin 6.6.6, package route):
`wwmctl -l`, `-d` and the maximize pair all behave as pinned below; the five
plasmashell surfaces that session lists with desktop -1 are the reason the
fixture carries a DESKTOP row of its own."""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import wl_fake` resolve only with the tests directory
# itself on sys.path: running this file by path puts it there for free,
# `python3 -m unittest tests/<file>.py` does not (tests/test_passthrough.py
# fails over a file that imports one of them without this line).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

from test_backend_kwin import UU, WID, XTERM_XID, _Base, _FakeX
from wdotool import backend, backend_detect, backend_kwin
from wdotool import cli as wdotool_cli
from wwmctl import cli, core

#: the twin xterm the tie tests add: a second window of the same client, same
#: class, same title, same rectangle. Only its place in windowList() (ix) and
#: its place in _NET_CLIENT_LIST differ, which is the whole of what tells the
#: two apart on Plasma 6 (KWin 6 dropped every Q_PROPERTY from x11window.h,
#: so no window carries its own X id there).
TWIN_UU = "8d0e0000-0000-4000-8000-00000000d00d"
XID_A, XID_B = 0x1000001, 0x1000003

#: the two ids Xwayland actually gave that pair on the arch-kde golden (Plasma
#: 6.7.5 / KWin 6.7.5, 2026-09-11): `xprop -root _NET_CLIENT_LIST` printed
#: `0xe00012, 0x1000012` and `wwmctl -lpx` listed the two `fwtwin` xterms under
#: exactly those two, one per window. Two X clients get resource-id bases that
#: are 0x00200000 apart there, which is why these are not consecutive.
XID_LIVE_A, XID_LIVE_B = 0x00E00012, 0x01000012

#: the xterm as Xwayland reports it: the client rect sits inside KWin's frame
XTERM_CLIENT = {"pid": 1201, "inst": "xterm", "cls": "XTerm",
                "name": "test@kde: ~", "geo": (101, 105, 638, 454)}


class KwinCliBase(_Base):
    """A fake KWin per test, a KwinBackend on it, and `wm()` to drive the CLI
    over the pair with the X plane faked."""

    def setUp(self):
        self.b = self.backend()          # _Base: fake KWin + KwinBackend
        self.ops = []
        orig = self.b._script

        def spy(op, timeout=None, **kw):
            self.ops.append((op, kw))
            return orig(op, timeout=timeout, **kw)

        self.b._script = spy

    def twin(self, **over):
        """Add a second xterm identical to the fixture's, and return its row."""
        row = dict(self.kwin.find(UU["xterm"]))
        row.update(u=TWIN_UU, so=4, ix=4)
        row.update(over)
        self.kwin.windows.append(row)
        return row

    def xclients(self, *rows):
        """Hand the backend an X plane listing `rows` in _NET_CLIENT_LIST
        order (which is the order that decides a tie)."""
        self.b._x = _FakeX(list(rows))
        return self.b._x

    def wm(self, argv, x11=None):
        """Run the CLI once. `x11` is what wwmctl's own X connection answers
        (the EWMH fallback plane), which is separate from the backend's."""
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(core, "_detect_backend", lambda: self.b),
            mock.patch.object(core, "_x11_connect",
                              lambda display=None, xauthority=None: x11),
            mock.patch.object(core, "hostname", lambda: "testhost"),
            mock.patch.object(sys, "argv", ["wmctrl"]),
        ]
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(list(argv))
        finally:
            for p in patches:
                p.stop()
        return rc, out.getvalue(), err.getvalue()

    def ids(self, out):
        return [int(line.split()[0], 16) for line in out.splitlines()]

    def wdo(self, argv):
        """Run wdotool's CLI once over the same fake KWin.

        `wwmctl -l` and `wdotool search` print two different id spaces on
        Plasma 6 -- the listing prints the X id of an XWayland window, search
        prints the id minted from the KWin uuid for every window there is --
        and vm/live-smoke.d/kde.sh's F4.1 block chains one into the other, so
        the pair has to be exercised against one desktop."""
        out, err = io.StringIO(), io.StringIO()
        backend_ = self.b
        # No sys.argv patch, unlike wm() above: wdotool's cli reads sys.argv only when
        # main() is called with none (wdotool/cli.py:323, and _prog_name at :496), and the
        # argv here carries its own argv[0].
        with mock.patch.object(backend_detect, "detect", lambda: backend_):
            with redirect_stdout(out), redirect_stderr(err):
                rc = wdotool_cli.main(["wdotool"] + list(argv))
        return rc, out.getvalue(), err.getvalue()


class ListingTests(KwinCliBase):
    def test_l_prints_the_xterm_under_its_x_id_and_natives_under_kwin_ids(self):
        """The mixed session: three native Plasma windows and one XWayland
        xterm. A native window has no X id at all, so it is listed under the
        id minted from its KWin uuid -- always >= 0x40000000, which is above
        every id an X server hands out (32 bits with the top bits set is not
        a resource id Xwayland can produce), so the two spaces cannot
        collide in a script that reads the column."""
        self.xclients(dict(XTERM_CLIENT, xid=XTERM_XID))
        rc, out, err = self.wm(["-l"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "0x5b4e28ba  0 testhost Desktop",
            "0x6c9a1f00  0 testhost untitled - Kate",
            "0x7fa85f64  1 testhost test@kde: ~",
            "0x00400005  0 testhost test@kde: ~",
        ])
        ids = self.ids(out)
        self.assertEqual(ids[-1], XTERM_XID)
        for wid in ids[:-1]:
            self.assertGreaterEqual(wid, 0x40000000)
        self.assertEqual(ids[:-1],
                         [WID["desktop"], WID["kate"], WID["konsole"]])

    def test_d_rows_come_from_kwin_desktops_with_their_work_areas(self):
        """`wmctrl -d`: one row per KWin virtual desktop, the current one
        starred, the geometry from workspace.virtualScreenSize and the work
        area from clientArea(WorkArea) -- the fake's 0,32 1920x1048, i.e. a
        1920x1080 screen with a 32px Plasma panel at the top. VP is `0,0` on
        every row: real KWin publishes `_NET_DESKTOP_VIEWPORT` as one pair per
        desktop and real wmctrl prints `VP: 0,0` against each of them
        (docs/WWMCTL.md:397), and there is no X plane in this rig to read it
        from, so the clone prints the origin it knows rather than an absence
        [M goal2/recon/gaps.md 3a, Xvfb :77, 2026-09-11]."""
        rc, out, err = self.wm(["-d"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "0  * DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  Desktop 1",
            "1  - DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  Desktop 2",
        ])

    def test_plasma_5_takes_the_xid_from_the_payload_not_from_a_match(self):
        """5.27's X11Window still carries the `windowId` Q_PROPERTY, so the
        id is in the script's own answer and nothing is matched against
        _NET_CLIENT_LIST at all -- the whole cascade this file's IdentityTests
        exercise is a Plasma 6 problem. (5.27 does still consult the X plane,
        but only to correct a lower-cased WM_CLASS and an empty caption, and
        only when Xwayland is already running.)"""
        self.b = self.backend(plasma=5)
        matched = []
        with mock.patch.object(backend_kwin, "_match_xids",
                               side_effect=lambda *a, **k: matched.append(a)
                               or {}):
            rc, out, err = self.wm(["-l"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.ids(out)[-1], XTERM_XID)
        self.assertEqual(matched, [])
        # ...and on 6 the same listing does go through the matcher
        self.b = self.backend(plasma=6)
        self.xclients(dict(XTERM_CLIENT, xid=XTERM_XID))
        with mock.patch.object(backend_kwin, "_match_xids",
                               side_effect=lambda *a, **k: matched.append(a)
                               or {}):
            self.wm(["-l"])
        self.assertEqual(len(matched), 1)


class IdentityTests(KwinCliBase):
    def test_an_x_id_addresses_its_own_window_of_an_identical_pair(self):
        """Two xterms of one client, same class, same title, same rectangle:
        `-i -r <xid>` has to reach the one that owns that id. The second X
        client is the later of the two in _NET_CLIENT_LIST, so it belongs to
        the later of the two in windowList() (Workspace::propagateWindows
        keeps the order) -- the twin, ix=4."""
        twin = self.twin()
        self.xclients(dict(XTERM_CLIENT, xid=XID_A),
                      dict(XTERM_CLIENT, xid=XID_B))
        rc, out, err = self.wm(["-l"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.ids(out)[-2:], [XID_A, XID_B])
        rc, out, err = self.wm(
            ["-i", "-r", str(XID_B), "-e", "0,10,20,300,200"])
        self.assertEqual((rc, out, err), (0, "", ""))
        moved = [(d["u"], d["x"], d["y"], d["w"], d["h"])
                 for d in self.kwin.windows
                 if (d["x"], d["y"]) == (10, 20)]
        self.assertEqual(moved, [(twin["u"], 10, 20, 300, 200)])
        # ...and the fixture's own xterm was not touched
        self.assertEqual(
            (self.kwin.find(UU["xterm"])["x"],
             self.kwin.find(UU["xterm"])["y"]), (100, 80))

    def test_an_unbreakable_tie_says_so_and_lists_both_under_kwin_ids(self):
        """No `ix` in the payload (an older script) and nothing else left to
        separate the pair: handing one of the two X ids out at random is the
        answer that moves the wrong window, so both keep xid 0, are listed
        under their KWin ids, and the run says what it could not do."""
        self.twin()
        for row in self.kwin.windows:
            row.pop("ix", None)
        self.xclients(dict(XTERM_CLIENT, xid=XID_A),
                      dict(XTERM_CLIENT, xid=XID_B))
        rc, out, err = self.wm(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            err,
            "wwmctl: 2 XWayland window(s) could not be told apart from each "
            "other in the X client list; their X ids are left unset\n")
        ids = self.ids(out)
        self.assertNotIn(XID_A, ids)
        self.assertNotIn(XID_B, ids)
        for wid in ids:
            self.assertGreaterEqual(wid, 0x40000000)


class SearchTests(KwinCliBase):
    """What `vm/live-smoke.d/kde.sh`'s F4.1 pair counts and then moves.

    The live check starts two `xterm -T fwtwin` on Xwayland, counts
    `wdotool search --name '^fwtwin$'` and feeds the second id to
    `wwmctl -i -r <id> -e`. Both halves are here over the fake KWin, because
    both went red on arch-kde and fedora44-kde (CI 2026-09-11) with a count
    of 0 -- and the guest says why: the shell inside a bare `xterm` rewrites
    the title `-T` set, so the windows were there under `test@kde-b6:~` and
    the real `xdotool search --name fwtwin` found none of them either
    [M arch-kde golden, Plasma 6.7.5, 2026-09-11]. The count is a fact about
    the window LIST and not about the X ids: search prints the minted id of
    every window KWin lists, whether or not the X plane could be joined to
    it, which is what tells that failure apart from a broken join."""

    def twins(self):
        """The fixture xterm and a second one, both titled `fwtwin`, both on
        the X plane under ids of their own."""
        self.kwin.find(UU["xterm"])["t"] = "fwtwin"
        twin = self.twin(t="fwtwin")
        self.xclients(dict(XTERM_CLIENT, name="fwtwin", xid=XID_LIVE_A),
                      dict(XTERM_CLIENT, name="fwtwin", xid=XID_LIVE_B))
        return twin

    def test_two_same_title_xterms_are_two_ids_to_search(self):
        """Two windows with the same title are two windows to the tools: two
        lines, two different ids, in KWin's own stacking order."""
        twin = self.twins()
        rc, out, err = self.wdo(["search", "--name", "^fwtwin$"])
        self.assertEqual((rc, err), (0, ""))
        found = [int(line) for line in out.split()]
        self.assertEqual(found, [WID["xterm"], backend_kwin._wid(twin["u"])])
        # ...and the same desktop lists them under the two X ids the golden's
        # Xwayland gave the real pair, one per window
        rc, out, err = self.wm(["-l"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.ids(out)[-2:], [XID_LIVE_A, XID_LIVE_B])

    def test_the_second_id_search_printed_moves_the_second_window(self):
        """F4.1 end to end: the id that came out of `search` addresses one
        window through `wwmctl -i -r <id> -e`, and it is the second of the
        pair -- the first keeps the rectangle it had. The numbers are
        kde.sh's own (`0,700,120,600,400`)."""
        twin = self.twins()
        rc, out, _err = self.wdo(["search", "--name", "^fwtwin$"])
        self.assertEqual(rc, 0)
        second = int(out.split()[1])
        rc, out, err = self.wm(["-i", "-r", str(second), "-e",
                                "0,700,120,600,400"])
        self.assertEqual((rc, out, err), (0, "", ""))
        moved = [(d["u"], d["x"], d["y"], d["w"], d["h"])
                 for d in self.kwin.windows if d["t"] == "fwtwin"]
        self.assertEqual(moved, [(UU["xterm"], 100, 80, 640, 480),
                                 (twin["u"], 700, 120, 600, 400)])

    def test_a_window_with_no_x_id_is_still_found_by_its_title(self):
        """The count does not depend on the X join: with no X plane at all
        (Xwayland not started, or its client list unreadable) the two windows
        are still two windows and still carry ids that address them. This is
        what tells a 0 apart from a join that went wrong -- a 0 means KWin
        did not list the windows."""
        twin = self.twins()
        self.b._x = None
        self.b._x11 = lambda: None
        rc, out, err = self.wdo(["search", "--name", "^fwtwin$"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([int(line) for line in out.split()],
                         [WID["xterm"], backend_kwin._wid(twin["u"])])


class StateTests(KwinCliBase):
    def test_the_maximize_pair_is_two_state_ops_and_no_client_message(self):
        """`wwmctl -b remove,maximized_vert,maximized_horz`.

        GNOME names the pair ("MAXIMIZED") because Mutter unmaximizes to the
        window's *current* rect and a second single-axis call that overtakes
        the first configure corrupts the saved rectangle. KWin settles each
        axis before it answers, so there is nothing to fold: two set_state
        calls, in the order given. And the EWMH ClientMessage fallback must
        not fire at all -- KWin accepted both, and a fallback that sends the
        pair over the X plane as well would toggle them back."""
        self.assertIsNone(self.b.maximize_pair_state())
        x11 = _FakeXRoot()
        self.xclients(dict(XTERM_CLIENT, xid=XTERM_XID))
        rc, out, err = self.wm(
            ["-i", "-r", str(XTERM_XID), "-b",
             "remove,maximized_vert,maximized_horz"], x11=x11)
        self.assertEqual((rc, out, err), (0, "", ""))
        states = [(kw["state"], kw["action"]) for op, kw in self.ops
                  if op == "state"]
        self.assertEqual(states,
                         [("MAXIMIZED_VERT", 0), ("MAXIMIZED_HORZ", 0)])
        self.assertEqual([c for c in x11.calls if c[0] == "client_message"],
                         [])

    def test_a_single_axis_is_one_state_op(self):
        self.xclients(dict(XTERM_CLIENT, xid=XTERM_XID))
        rc, _o, err = self.wm(
            ["-i", "-r", str(XTERM_XID), "-b", "add,maximized_vert"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([(kw["state"], kw["action"]) for op, kw in self.ops
                          if op == "state"],
                         [("MAXIMIZED_VERT", 1)])

    def test_the_pair_is_grouped_only_where_the_backend_names_it(self):
        """The grouping rule itself, over the same two names: with a backend
        that names the pair they are one call, with KWin's they are two."""
        self.assertEqual(
            backend.state_steps(self.b,
                                ["maximized_vert", "maximized_horz"]),
            [("MAXIMIZED_VERT", ["MAXIMIZED_VERT"]),
             ("MAXIMIZED_HORZ", ["MAXIMIZED_HORZ"])])

        class Pairing:
            def maximize_pair_state(self):
                return "MAXIMIZED"

        self.assertEqual(
            backend.state_steps(Pairing(),
                                ["maximized_vert", "maximized_horz"]),
            [("MAXIMIZED", ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"])])


class _FakeXRoot:
    """The EWMH plane wwmctl falls back to: enough for the fallback to be
    *possible*, so that a test asserting it did not happen means something."""

    def __init__(self):
        self.calls = []
        self.atoms = {}
        self.wm_honours_state = True

    def root(self):
        return 0x1C5

    def atom(self, name, only_if_exists=False):
        return self.atoms.setdefault(name, 0x180 + len(self.atoms))

    def get_prop_ints(self, win, name):
        self.calls.append(("get_prop_ints", win, name))
        return []

    def get_prop_string(self, win, name):
        return ""

    def get_wm_class(self, win):
        return ("", "")

    def get_geometry(self, win):
        return (0, 0, 1920, 1080)

    def get_pid(self, win):
        return 0

    def send_root_message(self, win, type_name, data):
        self.calls.append(("client_message", win, type_name, tuple(data)))

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
