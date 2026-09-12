#!/usr/bin/env python3
"""U11b: `ext_workspace.WorkspaceClient`'s own ordering, against a compositor with a group per output.

`wdotool/ext_workspace.py` had one sort key, `(coordinates, arrival)`, and it is right for every session
that publishes ONE `ext_workspace_group_handle_v1` -- labwc, Budgie, Xfce-on-Wayland, LXQt-on-Wayland. It is
wrong for cosmic-comp, which publishes one group PER OUTPUT: measured on the fedora44-cosmic golden with two
heads on 2026-09-11, groups 4278190084 (workspaces `1` coordinates [1] and `2` [2]) and 4278190087 (`1`
[1]), both `1`s active, one current workspace per head. Flat, those three sort into `1, 1, 2`, so desktop 1
was the OTHER head's already-active workspace: `wdotool set_desktop 1` activated something that was already
active and `get_desktop` answered 0, which is the fedora44-cosmic CI failure [M goal2/recon/flavors.md 5,
goal2/ci/rig-fedora44-cosmic.log, goal2/requests-batch-5.md].

Batch 5 landed the fix inside `backend_cosmic.py::_ws_rows` because this file was not its to edit; batch 17
moved it here (`_rows()`), where labwc and Budgie inherit it for free, and `backend_cosmic` went back to
`handles()/active_index()/activate()`. This file is the client half of that: the COSMIC-shaped fixture is
here so that the ordering is proved where it lives, and `tests/test_backend_cosmic.py::GroupedWorkspaces`
keeps proving the backend end of the same thing.

The peer is a real compositor on a real socket (`wl_fake.WorkspaceCompositor`, the labwc/Budgie shape,
subclassed here for the second group), so an ordering claim is a claim about the bytes that arrived.
"""

import os
import struct
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
from w11common.wayland_mini import WlConn
from wdotool import ext_workspace

#: The three event opcodes this fixture sends by hand, spelled the way `wl_fake` spells them -- the module
#: keeps them private because nothing outside it announced a workspace until now.
_WSM_EV_GROUP, _WSM_EV_WORKSPACE, _WSM_EV_DONE = 0, 1, 2
_WSG_EV_CAPABILITIES, _WSG_EV_WORKSPACE_ENTER = 0, 3
_WS_EV_NAME, _WS_EV_COORDINATES, _WS_EV_STATE, _WS_EV_CAPABILITIES = 1, 2, 3, 4


class TwoGroups(wl_fake.WorkspaceCompositor):
    """One `ext_workspace_group_handle_v1` per output: the golden's two heads, announced in its order.

    `ROWS` is `(group index, name, coordinates, active)` in the order the manager announced the workspaces,
    which is the order the arrival counter runs in. The shared `WorkspaceServer` publishes one group and
    that is labwc's shape, so the second group is built here -- the same way
    `tests/test_backend_cosmic.py::TwoGroupCosmic` builds it for the backend end."""

    PREFIX = "wdotool-ws-2g-"

    #: the golden's three rows: group 0 carries `1` and `2`, group 1 carries `1`
    ROWS = ((0, "1", (1,), True), (0, "2", (2,), False), (1, "1", (1,), True))

    def __init__(self, rows=None, **kw):
        self.rows = tuple(self.ROWS if rows is None else rows)
        self.groups = []
        super().__init__(**kw)

    def _ws_announce(self, conn, mgr_id):
        self.ws_mgr = mgr_id
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
            self._send(conn, oid, _WS_EV_STATE,
                       struct.pack("<I", ext_workspace.STATE_ACTIVE if active else 0))
            self._send(conn, oid, _WS_EV_CAPABILITIES,
                       struct.pack("<I", ext_workspace.CAP_ACTIVATE))
            oid += 1
        self._send(conn, mgr_id, _WSM_EV_DONE)


class ClientTest(unittest.TestCase):
    def client(self, comp):
        """A `WorkspaceClient` bound over a live connection to `comp`, its events already read."""
        self.addCleanup(comp.close)
        conn = WlConn(comp.path)
        self.addCleanup(conn.close)
        conn.get_registry()
        ws = ext_workspace.WorkspaceClient.bind(conn)
        self.assertIsNotNone(ws, "the fake advertises ext_workspace_manager_v1")
        conn.roundtrip()
        return ws


class GroupPerOutput(ClientTest):
    """The measured interleaving, and the key that undoes it."""

    def test_the_desktops_run_group_by_group_and_not_by_coordinate(self):
        """`1, 2, 1` -- the first head's two workspaces, then the second head's -- where the flat key gives
        `1, 1, 2`. These are the golden's own three rows [M goal2/requests-batch-5.md]."""
        comp = TwoGroups()
        ws = self.client(comp)
        self.assertEqual([w.name for w in ws.workspace_list()], ["1", "2", "1"])
        self.assertEqual(ws.handles(), list(comp.ws_ids))
        self.assertEqual(ws.count(), 3)

    def test_the_flat_key_really_does_interleave_these_rows(self):
        """The guard on the sentence above: `_live()` is still the flat `(coordinates, arrival)` order, and
        on this fixture it is a DIFFERENT list -- so the test above is not agreeing with itself."""
        comp = TwoGroups()
        ws = self.client(comp)
        self.assertEqual([r.oid for r in ws._live()],
                         [comp.ws_ids[0], comp.ws_ids[2], comp.ws_ids[1]])
        self.assertNotEqual([r.oid for r in ws._live()], ws.handles())

    def test_desktop_1_is_the_first_heads_second_workspace(self):
        """The number `wmctrl -s 1` names. Flat, index 1 was the second head's `1`, which was already
        active: the activate moved nothing, and that was the CI FAIL."""
        comp = TwoGroups()
        ws = self.client(comp)
        self.assertTrue(ws.activate(1))
        ws.c.roundtrip()                    # the requests are on the wire; let the peer read them
        comp_calls = comp.ws_calls
        self.assertEqual(comp_calls, [("activate", 1), ("commit", None)],
                         "the second row announced, then the manager's commit")

    def test_the_current_desktop_is_the_first_active_row_of_the_grouped_order(self):
        """Both heads say their own workspace is active -- the golden had two `1`s with the active bit --
        and `wwmctl -d` prints the first of them as current, the way backend_hypr does for Hyprland's
        per-monitor workspaces."""
        comp = TwoGroups()
        ws = self.client(comp)
        self.assertEqual(ws.active_index(), 0)
        self.assertEqual(ws.active_handles(), {comp.ws_ids[0], comp.ws_ids[2]})
        self.assertEqual([w.active for w in ws.workspace_list()], [True, False, True])

    def test_the_group_key_is_the_groups_earliest_arrival_and_not_the_first_row_met(self):
        """The one ordering the coordinates can hide: here the SECOND group's rows carry the lower
        coordinates, so a key that took each group's first row in coordinate order would read group 1's
        arrival off its `[1]` row and put that group first. The earliest arrival per group is what decides,
        so the announcement order of the groups survives."""
        rows = ((0, "a", (9,), True), (1, "b", (1,), False), (0, "c", (2,), False))
        comp = TwoGroups(rows=rows)
        ws = self.client(comp)
        self.assertEqual([w.name for w in ws.workspace_list()], ["c", "a", "b"],
                         "group 0 first (it arrived first), its own rows by coordinate")


class OneGroupIsUnchanged(ClientTest):
    """labwc, Budgie, Xfce-on-Wayland and LXQt-on-Wayland publish a single group: the new key is a constant
    there and the order is exactly what it was before batch 17 [M recon2/labwc.md 6a]."""

    def test_labwcs_three_named_desktops_keep_their_announcement_order(self):
        comp = wl_fake.WorkspaceCompositor(workspaces=wl_fake.LABWC_WORKSPACES)
        ws = self.client(comp)
        self.assertEqual([w.name for w in ws.workspace_list()], ["one", "two", "three"])
        self.assertEqual([w.active for w in ws.workspace_list()], [True, False, False])
        self.assertEqual(ws.handles(), list(comp.ws_ids))
        self.assertEqual([r.oid for r in ws._live()], ws.handles(),
                         "with one group the two orders are the same list")

    def test_cosmics_coordinates_still_decide_inside_a_group(self):
        """COSMIC's `[1]`/`[2]` announced the other way round: the coordinate, not the arrival, is what
        numbers them -- the half of the old key that was never wrong."""
        comp = wl_fake.WorkspaceCompositor(
            workspaces=(("2", 0, 1, (2,)), ("1", 1, 1, (1,))))
        ws = self.client(comp)
        self.assertEqual([w.name for w in ws.workspace_list()], ["1", "2"])
        self.assertEqual(ws.active_index(), 0)


class RemovedRows(ClientTest):
    """A workspace the compositor destroyed leaves the numbering, and takes its group's key with it when it
    was the group's only row."""

    def test_a_removed_workspace_leaves_the_grouped_order(self):
        comp = TwoGroups()
        ws = self.client(comp)
        comp.remove(1)                      # the first head's `2`
        ws.c.roundtrip()
        self.assertEqual(ws.handles(), [comp.ws_ids[0], comp.ws_ids[2]])
        self.assertEqual([w.name for w in ws.workspace_list()], ["1", "1"])
        self.assertEqual(ws.count(), 2)


if __name__ == "__main__":
    unittest.main()
