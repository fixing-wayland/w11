#!/usr/bin/env python3
"""U11: the wlr floor's desktops, over `ext_workspace_manager_v1`.

`wwmctl -d` answered `get_desktop is not supported by the wlr backend` on labwc while labwc's own panel was
showing three workspaces, and on Budgie and Xfce-on-Wayland while theirs showed four. All three publish
`ext_workspace_manager_v1` v1 and were read by hand with a 25-line `wayland_mini` client [M recon2/labwc.md
§6a: one group, `one` with state 1, `two`, `three`, each caps=1; budgie.md: `Workspace 1..4`;
xfce-wayland.md: four the same way]. sway 1.11 and Wayfire 0.10 publish no workspace protocol at all, so the
refusal has to survive exactly where it is right today [M labwc.md §2, wayfire.md §1.1].

The peer is a real compositor on a real socket: `wl_fake.WorkspaceServer` replays the recorded event order
(group, then each workspace's name, coordinates where they exist, state and capabilities, then the manager's
`done`) and records every request, so "set_desktop 2 sent activate on the third handle and then commit" is a
statement about bytes and not about a mock's call log. `set_active()` is what lets a test tell an accepted
activate from an ignored one: the compositor moves the bit and re-sends the states, the way a real one does,
and the backend has to read it back rather than remember what it asked for.
"""

import os
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py. This line is what covers `python3 tests/<file>.py`, and it goes before the
# first tool import so that no module can read the variable's absence on the way in.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common.errors import CmdError
from support import env
from test_backend_wlr import ToplevelCompositor, top
from hacks.window.backend_wlr import WlrBackend

#: Xfce 4.20 on Wayland runs labwc with four workspaces named the labwc way [M recon2/xfce-wayland.md].
XFCE_WORKSPACES = wl_fake.BUDGIE_WORKSPACES


class WorkspaceToplevels(wl_fake.WorkspaceServer, ToplevelCompositor):
    """labwc's whole shape: the wlr toplevel manager *and* the workspace manager on one socket.

    Not `wl_fake.WorkspaceCompositor`, which has no toplevel manager and would therefore never get as far as
    constructing the backend under test."""

    PREFIX = "wdotool-wlr-ws-"

    def __init__(self, workspaces=wl_fake.LABWC_WORKSPACES, **kw):
        self.workspaces = tuple(workspaces)
        super().__init__(**kw)


class WorkspaceTest(unittest.TestCase):
    def backend(self, workspaces=wl_fake.LABWC_WORKSPACES, cls=WorkspaceToplevels):
        comp = cls(workspaces=workspaces, toplevels=(top("a term", "foot"),))
        self.addCleanup(comp.close)
        ctx = env(XDG_RUNTIME_DIR=comp.dir,
                  WAYLAND_DISPLAY=os.path.basename(comp.path),
                  SWAYSOCK=None, I3SOCK=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        b = WlrBackend()
        self.addCleanup(b.c.close)
        return comp, b


class Labwc(WorkspaceTest):
    def test_the_three_configured_desktops_are_read(self):
        _comp, b = self.backend()
        self.assertEqual(b.num_desktops(), 3)
        self.assertEqual(b.get_desktop(), 0)
        self.assertEqual([w.name for w in b.workspaces()], ["one", "two", "three"])
        self.assertEqual([w.active for w in b.workspaces()], [True, False, False])

    def test_set_desktop_is_activate_on_that_handle_then_commit(self):
        """ext-workspace is double-buffered: `activate` alone changes nothing and the manager's `commit` is
        what applies the batch, which is what moved the active bit when it was driven by hand
        [M recon2/labwc.md §6a]."""
        comp, b = self.backend()
        b.set_desktop(2)
        self.assertEqual(comp.ws_calls, [("activate", 2), ("commit", None)])

    def test_the_active_bit_is_read_back_and_not_remembered(self):
        comp, b = self.backend()
        b.set_desktop(2)
        comp.set_active(2)
        self.assertEqual(b.get_desktop(), 2)
        self.assertEqual([w.active for w in b.workspaces()], [False, False, True])

    def test_a_desktop_that_is_not_there(self):
        comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_desktop(7)
        self.assertIn("cannot activate workspace 7", str(cm.exception))
        self.assertEqual(comp.ws_calls, [], "nothing may go on the wire for a workspace that does not exist")

    def test_a_workspace_that_does_not_advertise_activate_is_not_asked(self):
        """`workspace_capabilities` is the protocol's own statement about what may be requested; sending
        `activate` to a workspace that never advertised it is asking for a protocol error."""
        rows = tuple((name, state, 0, coords) for name, state, _caps, coords
                     in wl_fake.LABWC_WORKSPACES)
        comp, b = self.backend(workspaces=rows)
        with self.assertRaises(CmdError):
            b.set_desktop(1)
        self.assertEqual(comp.ws_calls, [])

    def test_the_work_area_is_not_invented(self):
        """The protocol carries no geometry, and a workspace's group may cover several outputs, so `wwmctl
        -d` prints the zero rectangle rather than the size of one screen."""
        _comp, b = self.backend()
        self.assertEqual([w.work_area for w in b.workspaces()], [(0, 0, 0, 0)] * 3)


class Budgie(WorkspaceTest):
    def test_the_four_workspaces_are_named_as_budgie_names_them(self):
        """Budgie 10.10 over labwc with `<desktops number="4"/>`: `Workspace 1` active, `Workspace 2..4`
        [M recon2/budgie.md]. Xfce 4.20 on Wayland publishes the same four [M xfce-wayland.md]."""
        _comp, b = self.backend(workspaces=wl_fake.BUDGIE_WORKSPACES)
        self.assertEqual(b.num_desktops(), 4)
        self.assertEqual([w.name for w in b.workspaces()],
                         ["Workspace 1", "Workspace 2", "Workspace 3", "Workspace 4"])
        self.assertEqual(b.get_desktop(), 0)

    def test_xfce_on_wayland_is_the_same_labwc_underneath(self):
        _comp, b = self.backend(workspaces=XFCE_WORKSPACES)
        self.assertEqual(b.num_desktops(), 4)


class Cosmic(WorkspaceTest):
    """COSMIC is the shape that makes ordering a rule instead of an accident: its workspaces carry
    `coordinates` (`[1]`, `[2]`) and labwc's carry none at all [M recon2/cosmic.md §4, labwc.md §6a]."""

    def test_coordinates_decide_the_order_arrival_does_not(self):
        rows = tuple(reversed(wl_fake.COSMIC_WORKSPACES))
        _comp, b = self.backend(workspaces=rows)
        self.assertEqual([w.name for w in b.workspaces()], ["1", "2"],
                         "announced 2 then 1; coordinates [1] and [2] put them back")

    def test_without_coordinates_arrival_order_stands(self):
        """labwc sends the `coordinates` event not at all, and its panel shows the announcement order, so
        the one comparison is `(coordinates, arrival)` and not two code paths."""
        rows = (("three", 0, 1, None), ("one", 1, 1, None), ("two", 0, 1, None))
        _comp, b = self.backend(workspaces=rows)
        self.assertEqual([w.name for w in b.workspaces()], ["three", "one", "two"])
        self.assertEqual(b.get_desktop(), 1)


class TheHandleAccessors(WorkspaceTest):
    """`handles()` and `active_handles()`, added 2026-09-09 for COSMIC (requests-batch-8.md item 1).

    The public `backend.Workspace` carries no protocol object -- it is what `wwmctl -d` prints -- and
    `hacks/window/backend_cosmic.py` needs the `ext_workspace_handle_v1` object ids: `workspace_enter` names a
    workspace by oid and `move_to_ext_workspace` takes one.  Before this it reached through to `_live()`.

    The claim under test is that the two accessors run the SAME ordering rule `workspace_list()` runs, which
    is the whole reason a second accessor is safe: `handles()[n]` is the handle of the desktop
    `workspace_list()[n]` describes.  COSMIC's reversed announcement is the fixture that can tell the two
    orders apart -- with arrival order the pairing would be inverted and every desktop number wrong."""

    def test_the_handles_are_in_the_same_order_the_workspace_rows_are(self):
        rows = tuple(reversed(wl_fake.COSMIC_WORKSPACES))
        comp, b = self.backend(workspaces=rows)
        self.assertEqual([w.name for w in b.workspaces()], ["1", "2"])
        # `comp.ws_ids` is the compositor's own object id per workspace in the order IT announced them
        # ("2" first, then "1"), so the coordinate rule has to hand the handles back REVERSED against
        # announcement.  An arrival-order `handles()` would give desktop 0 the handle of "2".
        self.assertEqual(b.ws.handles(), list(reversed(comp.ws_ids)))
        self.assertEqual(len(set(comp.ws_ids)), 2, "two live workspaces, two distinct handles")

    def test_active_handles_is_the_active_row_and_not_an_index(self):
        """`active_index()` answers a position and loses which handle it was; a window's `workspace_enter`
        names the handle, so visibility is a set membership over oids."""
        comp, b = self.backend(workspaces=wl_fake.LABWC_WORKSPACES)
        # labwc announces "one" active, and the oracle is the compositor's id for it, not a position
        self.assertEqual(b.ws.active_handles(), {comp.ws_ids[0]})
        self.assertEqual(b.ws.active_index(), 0)
        # ...and it is read back rather than remembered: the compositor moves the bit and re-sends the
        # states, the way `test_the_active_bit_is_read_back_and_not_remembered` drives it
        comp.set_active(2)
        b.num_desktops()                                  # the pump
        self.assertEqual(b.ws.active_handles(), {comp.ws_ids[2]})
        # and the two really are different numbers: an index that happened to equal an oid would make
        # the lines above pass on either
        self.assertNotEqual(list(b.ws.active_handles())[0], b.ws.active_index())

    def test_a_removed_workspace_is_in_neither(self):
        """The compositor sends `ext_workspace_handle_v1.removed` and both accessors drop that oid -- a
        handle the compositor has destroyed must never reach a `move_to_ext_workspace`.  The one removed
        here is labwc's ACTIVE workspace, so `active_handles()` is tested by more than an absence."""
        comp, b = self.backend(workspaces=wl_fake.LABWC_WORKSPACES)
        self.assertEqual(b.ws.handles(), list(comp.ws_ids))
        gone = comp.ws_ids[0]
        self.assertIn(gone, b.ws.active_handles())
        comp.remove(0)
        b.num_desktops()                                  # the pump
        self.assertEqual(b.ws.handles(), list(comp.ws_ids[1:]))
        self.assertNotIn(gone, b.ws.handles())
        self.assertEqual(b.ws.active_handles(), set())


class NoWorkspaceProtocol(WorkspaceTest):
    def test_sway_and_wayfire_keep_the_refusal_they_have(self):
        """sway 1.11 has an IPC socket instead and Wayfire 0.10 has nothing; on both, the wlr floor's
        desktop refusal is the honest answer and must not become a made-up single desktop."""
        comp = ToplevelCompositor(toplevels=(top("a term", "foot"),))
        self.addCleanup(comp.close)
        with env(XDG_RUNTIME_DIR=comp.dir,
                 WAYLAND_DISPLAY=os.path.basename(comp.path),
                 SWAYSOCK=None, I3SOCK=None):
            b = WlrBackend()
            self.addCleanup(b.c.close)
            for call in (b.get_desktop, b.num_desktops):
                with self.assertRaises(CmdError) as cm:
                    call()
                self.assertTrue(getattr(cm.exception, "unsupported", False))
            with self.assertRaises(CmdError) as cm:
                b.set_desktop(1)
            self.assertEqual(str(cm.exception),
                             "set_desktop is not supported by the wlr backend: this compositor publishes "
                             "no ext_workspace_manager_v1; not yet here, and the routes are that protocol "
                             "where the compositor grows it (AGENTS.md route 1) or the compositor's own "
                             "IPC where it has one (route 2), which is a backend per compositor")
            self.assertIsNone(b.workspaces())

    def test_a_window_still_reports_no_desktop_of_its_own(self):
        """Neither foreign-toplevel protocol carries a workspace association, so the `-1` in the listing's
        desktop column stays even where the session's desktops are known [M labwc.md §6a]."""
        _comp, b = self.backend()
        self.assertEqual([w.desktop for w in b.list()], [-1])
        self.assertEqual(b.window_desktop(b.list()[0].id), -1)


if __name__ == "__main__":
    unittest.main()
