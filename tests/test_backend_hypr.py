#!/usr/bin/env python3
"""U19: the Hyprland window backend against the recorded IPC (`support.FakeHypr`).

Everything the double replays is a recording off a live Hyprland 0.53.3 in a QEMU VM
[M recon2/hyprland.md §2, fixtures under tests/fixtures/hypr/]: four clients, one of them an XWayland
xmessage with pid 11920 at 300,200 size 400x300; two monitors, the second a scale-2.0 HEADLESS at +1920+0;
two workspaces; `us,de` keyboards; the five event lines a foot window's opening wrote.

What is being proved here is the three things the generic wlroots floor got wrong on that session and the
refusals that replace the two it got wrong by lying [M recon2/hyprland.md §3]:

* ids shifted under the caller (`0x000f4241` became `0x000f4240` when another window closed) -- here they are
  minted from `address` and are the same in two backends built minutes apart;
* geometry was the whole output where `hyprctl -j clients` had the real rectangle;
* `windowminimize` was accepted and did nothing -- here it is a refusal that names the reason.

The dispatch strings are asserted byte for byte against the double's request log, because they are the whole
contract with the compositor and every one of them was measured `ok` and verified in `hyprctl clients`.
"""

import copy
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from w11common import session
from w11common.errors import CmdError
from test_wwmctl_x11 import FakeXServer
from wdotool import backend as backend_mod
from wdotool import backend_hypr as hypr_mod
from wdotool import x11_mini
from wdotool.backend_hypr import HyprBackend
from wdotool.ctx import SoftCmdError
from wdotool.hypr_ipc import HyprIPC

#: the four addresses of the recorded session, in the order the fixture lists them
ADDRS = ("0x59daae6de8f0", "0x59daae69e360", "0x59daaec05920", "0x59daae933ac0")
XMSG = ADDRS[3]           # the XWayland xmessage, pid 11920
FOOT_FLOAT = ADDRS[0]     # floating, at 100,120 size 800x600
FOOT_TILED = ADDRS[1]     # tiled, at 22,22 size 931x1036


class BatchHypr(support.FakeHypr):
    """`support.FakeHypr` that also speaks `[[BATCH]]`, which is what `hyprctl --batch` sends.

    The request is the prefix and then the requests separated by `;`, and the reply is one answer per
    request joined by three newlines -- `ok\n\n\nok` for two dispatches that both took, and
    `ok\n\n\nInvalid dispatcher` when the second verb does not exist (measured over a raw socket, see
    `SEP`).  `refuse_verbs` refuses the named dispatch verbs and nothing else, so a batch can be half
    accepted, which is the only way the relayed sentence below is worth asserting."""

    PREFIX = "[[BATCH]]"
    #: Measured over a raw socket on the arch-hypr golden (Hyprland 0.56.2, 2026-09-11): two dispatches
    #: that took answered `b'ok\n\n\nok'`, and one whose second verb does not exist answered
    #: `b'ok\n\n\nInvalid dispatcher'` -- the whole batch runs and every answer comes back, three
    #: newlines between them.
    SEP = b"\n\n\n"

    def __init__(self, mode="ok", refuse_verbs=(), **kw):
        self.refuse_verbs = tuple(refuse_verbs)
        self.batches = []
        super().__init__(mode, **kw)

    def reply_for(self, req: str) -> bytes:
        if req.startswith(self.PREFIX):
            parts = req[len(self.PREFIX):].split(";")
            self.batches.append(parts)
            return self.SEP.join(self.reply_for(p) for p in parts)
        verb = req.split(" ", 1)[0]
        if verb == "dispatch" and req.split(" ")[1:2] and req.split(" ")[1] in self.refuse_verbs:
            return self.INVALID.encode()
        return super().reply_for(req)


class Base(unittest.TestCase):
    """One FakeHypr per test, and a backend speaking to it."""

    DOUBLE = BatchHypr

    def hypr(self, mode="ok", **kw) -> support.FakeHypr:
        srv = self.DOUBLE(mode, **kw)
        self.addCleanup(srv.close)
        return srv

    def backend(self, srv=None, mode="ok", **kw) -> HyprBackend:
        srv = srv if srv is not None else self.hypr(mode, **kw)
        self.srv = srv
        b = HyprBackend(ipc=HyprIPC(srv.path))
        # views() opens an X connection and the backend lives for the process, so nothing in the tool closes
        # it; a test that makes forty of them would otherwise leave forty for the garbage collector to
        # complain about at whatever moment it gets round to them
        self.addCleanup(self._close_x, b)
        return b

    @staticmethod
    def _close_x(b: HyprBackend):
        x = getattr(b, "_x", None)
        if x not in (None, "unset"):
            try:
                x.close()
            except OSError:
                pass

    def wid(self, addr: str, b: HyprBackend) -> int:
        return backend_mod.mint_id(addr)

    def dispatches(self) -> list:
        """Every non-query request the double saw, in order."""
        return [r for r in self.srv.requests if not r.startswith("j/")]


class Ids(Base):
    """Minted from `address`, and stable -- the regression the wlr floor could not fix."""

    def test_every_id_is_the_mint_of_its_address(self):
        b = self.backend()
        got = {w.title + "@%d,%d" % (w.x, w.y): w.id for w in b.list()}
        want = {"foot@100,120": backend_mod.mint_id(FOOT_FLOAT),
                "foot@22,22": backend_mod.mint_id(FOOT_TILED),
                "Screen Layout Editor@967,22": backend_mod.mint_id(ADDRS[2]),
                "xmessage@300,200": backend_mod.mint_id(XMSG)}
        self.assertEqual(got, want)
        for wid in got.values():
            self.assertEqual(wid & 0xC0000000, backend_mod.ID_BASE,
                             "a minted id must sit outside Xwayland's (client << 21) | serial range")

    def test_two_backends_over_one_session_agree(self):
        """The measured defect: two foots listed as 0x000f4240/0x000f4241 and the survivor of a close became
        0x000f4240, so an id a script was holding named another window [M recon2/hyprland.md §3]. A hash of
        the address cannot do that -- and it is what makes `wdotool search` usable from a second process."""
        srv = self.hypr()
        first = {w.title: w.id for w in HyprBackend(ipc=HyprIPC(srv.path)).list()}
        second = {w.title: w.id for w in HyprBackend(ipc=HyprIPC(srv.path)).list()}
        self.assertEqual(first, second)
        # and the id does not move when the window in front of it goes away
        clients = [c for c in json.loads(json.dumps(srv.payloads["clients"]))
                   if c["address"] != FOOT_FLOAT]
        srv.payloads["clients"] = clients
        b = self.backend(srv)
        after = {w.title: w.id for w in b.list()}
        self.assertEqual(after["xmessage"], first["xmessage"])

    def test_a_thirty_bit_collision_is_re_minted(self):
        """Two live windows whose addresses hash to one id: whoever comes second is re-minted rather than
        dropped, so every window keeps an id of its own (backend.mint_map's rule)."""
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        for r in rows:
            r["address"] = FOOT_FLOAT      # every window at the same handle is the extreme case
        srv.payloads["clients"] = rows
        with mock.patch.object(backend_mod, "mint_id", lambda key, salt=0: 0x40000000 | salt):
            ids = [w.id for w in self.backend(srv).list()]
        self.assertEqual(ids, [0x40000000, 0x40000000, 0x40000000, 0x40000000])
        rows[1]["address"] = FOOT_TILED
        rows[2]["address"] = ADDRS[2]
        rows[3]["address"] = XMSG
        srv.payloads["clients"] = rows
        with mock.patch.object(backend_mod, "mint_id", lambda key, salt=0: 0x40000000 | salt):
            ids = [w.id for w in self.backend(srv).list()]
        self.assertEqual(sorted(ids), [0x40000000, 0x40000001, 0x40000002, 0x40000003])


class Listing(Base):
    """Everything `hyprctl -j clients` publishes and the protocol does not."""

    def rows(self):
        return {w.title + "@%d,%d" % (w.x, w.y): w for w in self.backend().list()}

    def test_geometry_pid_class_and_instance_are_the_recorded_ones(self):
        """The wlr floor answered `Position: 0,0  Geometry: 1920x1080` for a foot the compositor placed at
        22,61 sized 1876x997 [M recon2/hyprland.md §3]."""
        w = self.rows()["foot@100,120"]
        self.assertEqual((w.x, w.y, w.w, w.h), (100, 120, 800, 600))
        self.assertEqual((w.pid, w.class_, w.instance), (10211, "foot", "foot"))
        x = self.rows()["xmessage@300,200"]
        self.assertEqual((x.x, x.y, x.w, x.h, x.pid), (300, 200, 400, 300, 11920))
        self.assertEqual((x.class_, x.instance), ("Xmessage", "Xmessage"))

    def test_the_active_window_is_the_focused_one(self):
        rows = self.rows()
        self.assertEqual([k for k, w in rows.items() if w.focused], ["xmessage@300,200"])

    def test_the_list_is_bottom_to_top_so_hit_test_finds_the_front_window(self):
        """Hyprland publishes no stacking order; `focusHistoryID` is the nearest thing (0 has focus). list()
        is that reversed, because backend.hit_test() reads the last hit as the topmost."""
        wins = self.backend().list()
        self.assertEqual([w.title for w in wins],
                         ["foot", "foot", "Screen Layout Editor", "xmessage"])
        # 150,200 is inside BOTH foots (the floating one covers 100,120..900,720, the tiled one
        # 22,22..953,1058) and inside neither the xmessage nor the editor -- so the answer is decided by the
        # order alone, and the focused-window shortcut in hit_test() cannot reach it. Ascending
        # focusHistoryID would put the floating foot last and hand the click to the staler window.
        self.assertEqual(backend_mod.hit_test(wins, 150, 200), backend_mod.mint_id(FOOT_TILED))

    def test_an_unmapped_client_is_not_listed(self):
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["mapped"] = False
        srv.payloads["clients"] = rows
        self.assertEqual(len(self.backend(srv).list()), 3)

    def test_a_window_on_an_offscreen_workspace_is_not_visible(self):
        """`visible` is "on a workspace some monitor is showing", which is what --onlyvisible means and what
        the wlr floor could not answer at all."""
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["workspace"] = {"id": 7, "name": "7"}
        srv.payloads["clients"] = rows
        wins = {w.title + str(w.desktop): w for w in self.backend(srv).list()}
        self.assertFalse(wins["foot6"].visible)
        self.assertTrue(wins["foot0"].visible)

    def test_a_hidden_client_is_not_visible(self):
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["hidden"] = True
        srv.payloads["clients"] = rows
        self.assertFalse([w for w in self.backend(srv).list() if w.x == 100][0].visible)


class Desktops(Base):
    """Hyprland workspace ids are 1-based and dense; wmctrl desktops are 0-based."""

    def test_workspace_one_is_desktop_zero(self):
        self.assertEqual({w.desktop for w in self.backend().list()}, {0})
        b = self.backend()
        self.assertEqual(b.get_desktop(), 0)
        self.assertEqual(b.num_desktops(), 2)

    def test_a_special_workspace_is_on_no_numbered_desktop(self):
        """Special (scratchpad) workspaces carry negative ids and belong to no desktop number at all -- -1,
        the answer a named sway workspace gets."""
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["workspace"] = {"id": -98, "name": "special:magic"}
        srv.payloads["clients"] = rows
        self.assertEqual([w.desktop for w in self.backend(srv).list() if w.x == 100], [-1])

    def test_the_workspace_records_carry_the_monitor_work_area(self):
        """`reserved` is the monitor's layer-shell gap, which is what _NET_WORKAREA means; the second head is
        scale 2.0, so its 1920x1080 of pixels is 960x540 of layout."""
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[0]["reserved"] = [0, 30, 0, 10]
        srv.payloads["monitors"] = mons
        got = {ws.index: (ws.name, ws.active, ws.work_area) for ws in self.backend(srv).workspaces()}
        self.assertEqual(got[0], ("1", True, (0, 30, 1920, 1040)))
        self.assertEqual(got[1], ("2", False, (1920, 0, 960, 540)))

    def test_exactly_one_workspace_is_current_even_with_two_heads(self):
        """`active` is "this is the current desktop", not "this is on some screen". The recorded session shows
        workspace 1 on Virtual-1 and 2 on HEADLESS-2 -- both visible, and `j/activeworkspace` names 1. wwmctl
        prints `*` on every active row and reads the first as the current desktop (wwmctl/core.py), so the
        visible-set reading would mark two desktops current where wmctrl marks one; sway's backend fills the
        flag from the node's `focused` for exactly this reason (wdotool/backend_sway.py)."""
        b = self.backend()
        wss = b.workspaces()
        self.assertEqual([ws.index for ws in wss if ws.active], [b.get_desktop()])
        # and the flag follows the focus rather than the second head
        srv = self.hypr()
        srv.payloads["activeworkspace"] = dict(srv.payloads["activeworkspace"], id=2, name="2")
        self.assertEqual([ws.index for ws in self.backend(srv).workspaces() if ws.active], [1])

    def test_a_rotated_head_has_its_work_area_the_tall_way_round(self):
        """`width`/`height` are the MODE's pixels: `--rotate left` left the mode at 1920x1080 and reported
        `transform: 3` [M recon2/hyprland.md §4], and the work area of that head is 1080x1920 minus reserved.
        The wlr floor and wxrandr both read the enum this way, and `wwmctl -d`'s WA column is this."""
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[0]["transform"] = 3
        mons[0]["reserved"] = [0, 30, 0, 10]
        srv.payloads["monitors"] = mons
        got = {ws.index: ws.work_area for ws in self.backend(srv).workspaces()}
        self.assertEqual(got[0], (0, 30, 1080, 1880))


class Dispatches(Base):
    """Every mutating string, byte for byte. Each was measured `ok` and verified in `hyprctl clients`
    [M recon2/hyprland.md §2]."""

    def setUp(self):
        self.b = self.backend()
        self.float_id = backend_mod.mint_id(FOOT_FLOAT)
        self.tiled_id = backend_mod.mint_id(FOOT_TILED)
        self.srv.requests.clear()

    def test_move_and_resize_address_the_window_by_address(self):
        self.b.move_window(self.float_id, 100, 120)
        self.b.resize(self.float_id, 640, 480)
        self.assertEqual(self.dispatches(), [
            "dispatch movewindowpixel exact 100 120,address:0x59daae6de8f0",
            "[[BATCH]]dispatch resizewindowpixel exact 640 480,address:0x59daae6de8f0;"
            "dispatch movewindowpixel exact 100 120,address:0x59daae6de8f0",
        ])

    def test_resize_re_issues_the_origin_after_the_size_in_one_batch(self):
        """Hyprland 0.56.2 resizes a floating window about its CENTRE, so the size MOVES it: measured with
        `hyprctl` alone, 100,100 800x600 sent `resizewindowpixel exact 400 300` landed at 300,250 400x300
        (both centred on 500,400), which is why the smoke's move-then-size came out 660,340 800x600
        [M goal2/recon/flavors.md 3a, and CI runs 34372382621 / 103360601210].  `xdotool windowsize` never
        moves the origin, so the origin `j/clients` already carries goes back out after the resize, in the
        same batch -- one round trip, nothing of Hyprland's between the two dispatches."""
        self.b.resize(self.float_id, 640, 480)
        sent = self.dispatches()
        self.assertEqual(len(sent), 1, sent)
        self.assertTrue(sent[0].startswith("[[BATCH]]"), sent[0])
        first, second = sent[0][len("[[BATCH]]"):].split(";")
        self.assertEqual(first, "dispatch resizewindowpixel exact 640 480,address:0x59daae6de8f0")
        # 100,120 is the fixture's `at` for this window, so the second dispatch is the PRE-RESIZE origin
        # and not a number this test made up
        self.assertEqual(second, "dispatch movewindowpixel exact 100 120,address:0x59daae6de8f0")

    def test_a_row_without_an_origin_resizes_alone_rather_than_moving_to_the_corner(self):
        """A `j/clients` row with no readable `at` gets the resize on its own.

        Every row measured here carries a two-number `at` (the recorded fixture and the live arch-hypr
        golden), so this is the shape of a row nobody has seen -- and the answer to not knowing where the
        window is is to leave it there. `movewindowpixel exact 0 0` is the one thing this method exists to
        prevent, and sending it off a default would be doing it deliberately."""
        rows = copy.deepcopy(support.fixture_json("hypr", "clients.json"))
        for row in rows:
            if row.get("address") == FOOT_FLOAT:
                del row["at"]
        srv = self.hypr(payloads={"clients": rows})
        b = self.backend(srv)
        srv.requests.clear()
        b.resize(backend_mod.mint_id(FOOT_FLOAT), 640, 480)
        self.assertEqual(self.dispatches(),
                         ["dispatch resizewindowpixel exact 640 480,address:0x59daae6de8f0"])

    def test_a_batch_hyprland_did_not_take_whole_is_its_own_words(self):
        """The reply to a batch is one answer per request, joined by three newlines (`ok\n\n\nok` for two
        dispatches that took), so a refusal anywhere in it makes one of those answers something else. It is
        relayed whole and in Hyprland's name, the way a single dispatch is -- half a batch that took is not
        a success to report."""
        srv = self.hypr(refuse_verbs=("movewindowpixel",))
        b = self.backend(srv)
        with self.assertRaises(CmdError) as cm:
            b.resize(backend_mod.mint_id(FOOT_FLOAT), 640, 480)
        self.assertEqual(str(cm.exception), "hypr: %s" % BatchHypr.INVALID)
        self.assertEqual(len(srv.batches), 1)

    def test_a_tiled_window_is_refused_before_anything_is_sent(self):
        """The refusal comes first, so `--sync` does not spin on a resize that will never happen -- and the
        batch above never reaches a window Hyprland would re-lay out anyway."""
        with self.assertRaises(SoftCmdError):
            self.b.resize(self.tiled_id, 640, 480)
        self.assertEqual(self.dispatches(), [])

    def test_activate_close_and_workspaces(self):
        self.b.activate(self.float_id)
        self.b.close(self.float_id)
        self.b.set_desktop(1)
        self.b.set_window_desktop(self.float_id, 1)
        self.assertEqual(self.dispatches(), [
            "dispatch focuswindow address:0x59daae6de8f0",
            "dispatch closewindow address:0x59daae6de8f0",
            "dispatch workspace 2",
            "dispatch movetoworkspacesilent 2,address:0x59daae6de8f0",
        ])

    def test_raising_a_floating_window_is_a_focus(self):
        """A focused floating window is drawn in front of the other floating ones, which is the whole of what
        Hyprland offers here and what the plan asks for ("focus, as sway does for floating")."""
        self.b.raise_(self.float_id)
        self.assertEqual(self.dispatches(), ["dispatch focuswindow address:0x59daae6de8f0"])

    def test_fullscreen_focuses_first_and_toggles_once(self):
        """`dispatch fullscreen` takes no address -- it acts on whatever has focus -- so the window is focused
        first; and it is a toggle, so a window that is already fullscreen is left alone rather than being
        turned off by an --add that arrived twice."""
        self.b.set_state(self.float_id, "FULLSCREEN", 1)
        self.assertEqual(self.dispatches(), [
            "dispatch focuswindow address:0x59daae6de8f0",
            "dispatch fullscreen 0",
        ])
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["fullscreen"] = 2
        srv.payloads["clients"] = rows
        b = self.backend(srv)
        srv.requests.clear()
        b.set_state(self.float_id, "FULLSCREEN", 1)
        self.assertEqual(self.dispatches(), [])
        b.set_state(self.float_id, "FULLSCREEN", 0)
        self.assertEqual(self.dispatches(), [
            "dispatch focuswindow address:0x59daae6de8f0",
            "dispatch fullscreen 0",
        ])

    def test_the_maximize_pair_is_one_call_and_a_lone_axis_is_refused(self):
        """`dispatch fullscreen 1` takes both axes at once and there is no per-axis dispatcher, so
        maximize_pair_state folds the pair -- and a single axis is refused rather than half-applied."""
        self.assertEqual(self.b.maximize_pair_state(), "MAXIMIZED")
        self.assertEqual(backend_mod.state_steps(self.b, ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"]),
                         [("MAXIMIZED", ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"])])
        self.b.set_state(self.float_id, "MAXIMIZED", 1)
        self.assertEqual(self.dispatches(),
                         ["dispatch focuswindow address:0x59daae6de8f0", "dispatch fullscreen 1"])
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(self.float_id, "MAXIMIZED_VERT", 1)
        self.assertEqual(str(cm.exception),
                         "windowstate MAXIMIZED_VERT is not supported by the hypr backend "
                         "(Hyprland maximizes both axes at once; ask for both); not yet here, and the "
                         "route is a patched Hyprland (AGENTS.md route 6), one dispatcher each")

    def test_going_from_maximized_to_fullscreen_clears_the_standing_one(self):
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["fullscreen"] = 1
        srv.payloads["clients"] = rows
        b = self.backend(srv)
        srv.requests.clear()
        b.set_state(self.float_id, "FULLSCREEN", 1)
        self.assertEqual(self.dispatches(), [
            "dispatch focuswindow address:0x59daae6de8f0",
            "dispatch fullscreen 1",     # clears the maximize
            "dispatch fullscreen 0",     # then the fullscreen
        ])

    def test_sticky_is_pin_and_only_for_floating_windows(self):
        self.b.set_state(self.float_id, "STICKY", 1)
        self.assertEqual(self.dispatches(), ["dispatch pin address:0x59daae6de8f0"])
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(self.tiled_id, "STICKY", 1)
        self.assertEqual(str(cm.exception),
                         "hypr: only floating windows can be pinned (setfloating it first)")

    def test_a_tiled_move_or_resize_is_the_soft_refusal_and_sends_nothing(self):
        """A tiled window ignores the move until it is floated -- the dispatcher answers `ok` and the window
        stays put [M recon2/arch.md, Hyprland IPC]. Refused up front, so `--sync` does not spin."""
        for call, word in ((lambda: self.b.move_window(self.tiled_id, 1, 2), "position"),
                           (lambda: self.b.resize(self.tiled_id, 3, 4), "size")):
            with self.assertRaises(SoftCmdError) as cm:
                call()
            self.assertIn("setfloating it first", str(cm.exception))
            self.assertIn(word, str(cm.exception))
        self.assertEqual(self.dispatches(), [])

    def test_a_refused_dispatch_is_hyprlands_own_words(self):
        b = self.backend(mode="refuse")
        with self.assertRaises(CmdError) as cm:
            b.activate(backend_mod.mint_id(FOOT_FLOAT))
        self.assertEqual(str(cm.exception), "hypr: Invalid dispatcher")


class Refusals(Base):
    """What the wlr floor did silently, said out loud."""

    def test_minimize_names_the_reason(self):
        """Measured: `windowminimize` on the wlr floor was accepted and `hyprctl` still said
        `mapped: 1 hidden: 0` [M recon2/hyprland.md §3]."""
        b = self.backend()
        wid = backend_mod.mint_id(FOOT_FLOAT)
        for op, call in (("windowminimize", b.minimize), ("windowunmap", b.unmap)):
            with self.assertRaises(CmdError) as cm:
                call(wid)
            # vm/live-smoke.d/hypr.sh:169 greps the parenthesis contiguous with the prefix, which is why
            # the route goes after the closing bracket and not inside it
            self.assertEqual(str(cm.exception),
                             "%s is not supported by the hypr backend (Hyprland has no minimize); not yet "
                             "here, and the route is Hyprland's own IPC (AGENTS.md route 2), a special "
                             "workspace to stash the window in, at the cost of the bookkeeping that brings "
                             "it back and a listing that keeps showing it while it is stashed" % op)
            self.assertTrue(cm.exception.unsupported)
        self.assertEqual(self.dispatches(), [])

    def test_lower_names_what_j_clients_does_not_carry(self):
        """The reason is only what was measured: no row of the recorded `j/clients` orders the windows front
        to back [M recon2/hyprland.md §2]. It does not claim there is no dispatcher -- `alterzorder
        top,address:0x...` was run on a floating window and answered `ok` [M requests-batch-5.md, "Read by
        batch 20", item 8] -- so the dispatcher is the route the refusal names, and the cost it names is
        sending it blind, not a run nobody has done."""
        b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.lower(backend_mod.mint_id(FOOT_FLOAT))
        self.assertEqual(str(cm.exception),
                         "windowlower is not supported by the hypr backend "
                         "(Hyprland publishes no stacking order in j/clients); not yet here, and the "
                         "route is `alterzorder bottom` over the IPC we already speak (AGENTS.md route 2), "
                         "measured to answer ok on a floating window; the cost is sending it unverified, "
                         "because j/clients publishes no order to read a lower back from")
        self.assertEqual(self.dispatches(), [])

    def test_raising_a_tiled_window_warns_and_sends_nothing(self):
        """sway's shape and sway's rung (wdotool/backend_sway.py, `raise_`): a tiled window sits in a layout with
        no z to alter, so `alterzorder` -- the route the floating half names -- is not the route here; the
        restack has to come from the compositor's own code. Warning, and nothing sent."""
        b = self.backend()
        with mock.patch.object(hypr_mod, "warn") as w:
            b.raise_(backend_mod.mint_id(FOOT_TILED))
        self.assertEqual(w.call_args[0][0],
                         "windowraise: tiled Hyprland windows have no stacking order; not yet, and the "
                         "route is a patched Hyprland (AGENTS.md route 6), a restack its layout has no "
                         "word for today; ignoring")
        self.assertEqual(self.dispatches(), [])

    def test_an_unknown_window_is_not_found_before_anything_is_sent(self):
        b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.activate(0x40000000)
        self.assertEqual(str(cm.exception), "window 1073741824 not found")
        self.assertEqual(self.dispatches(), [])


class Pointer(Base):
    def test_the_cursor_position_comes_from_cursorpos(self):
        """`hyprctl cursorpos` answered `640, 360` exactly after a `mousemove 640 360`, twice, 0 px error
        [M recon2/hyprland.md §3]."""
        self.assertEqual(self.backend().pointer(), (640, 360))
        self.assertIn("j/cursorpos", self.srv.requests)

    def test_a_compositor_that_will_not_say_gets_the_caller_its_fallback(self):
        self.assertIsNone(self.backend(mode="badjson").pointer())

    def test_display_size_is_the_bounding_box_with_the_scale_divided_out(self):
        """The recorded second head is 1920x1080 at scale 2.0 sitting at +1920+0, so the layout is
        1920 + 960 wide."""
        self.assertEqual(self.backend().display_size(), (2880, 1080))

    def test_a_rotated_head_is_measured_the_tall_way_round(self):
        """The mode stays 1920x1080 and `transform` becomes 3 -- the value `hyprctl monitors` reported after
        `--rotate left` [M recon2/hyprland.md §4] -- so the layout box is 1080 wide by 1920 high. Without the
        swap `wdotool getdisplaygeometry` and the mousemove clamp are ninety degrees out on every rotated
        Hyprland head, and they disagree with wxrandr's own reading of the same j/monitors row."""
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[0]["transform"] = 3
        mons[1]["disabled"] = True
        srv.payloads["monitors"] = mons
        self.assertEqual(self.backend(srv).display_size(), (1080, 1920))

    def test_a_disabled_monitor_is_out_of_the_box(self):
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[1]["disabled"] = True
        srv.payloads["monitors"] = mons
        self.assertEqual(self.backend(srv).display_size(), (1920, 1080))


class Events(Base):
    """The `.socket2.sock` line stream, in sway's vocabulary."""

    def backend_with_events(self, ev):
        """A backend whose event socket is the event double's.

        `support.FakeHypr` and `support.FakeHyprEvents` pick their own temp directories, so the two are not
        siblings the way Hyprland's are and `events_path` cannot derive one from the other. A two-line
        subclass rather than a patch of HyprIPC's property: nothing this test does then survives into the
        next one, which a class-level patch and its restore could not promise."""
        srv = self.hypr()
        self.srv = srv

        class _IPC(HyprIPC):
            events_path = property(lambda self: ev.path)

        return HyprBackend(ipc=_IPC(srv.path))

    def test_the_five_recorded_lines_become_sways_words(self):
        ev = support.FakeHyprEvents()
        self.addCleanup(ev.close)
        b = self.backend_with_events(ev)
        got = []
        for wid, change in b.events(timeout=2.0):
            got.append((wid, change))
            if len(got) == 3:
                break
        self.assertEqual(got, [
            (backend_mod.mint_id(FOOT_TILED), "title"),
            (backend_mod.mint_id(FOOT_TILED), "new"),
            (backend_mod.mint_id(FOOT_TILED), "focus"),
        ])

    def test_the_event_addresses_carry_no_0x_and_still_mint_the_listing_ids(self):
        """`activewindowv2>>59daae69e360` -- the stream spells the address without the prefix the listing
        uses, and an id that did not agree with list()'s would make every event about a window nobody can
        name."""
        ev = support.FakeHyprEvents(lines=("activewindowv2>>59daae69e360",))
        self.addCleanup(ev.close)
        b = self.backend_with_events(ev)
        wid, change = next(iter(b.events(timeout=2.0)))
        self.assertEqual(change, "focus")
        self.assertIn(wid, [w.id for w in b.list()])

    def test_a_workspace_event_folds_in_only_when_asked(self):
        ev = support.FakeHyprEvents(lines=("workspacev2>>2,2", "closewindow>>59daae69e360"))
        self.addCleanup(ev.close)
        b = self.backend_with_events(ev)
        self.assertEqual(list(b.events(timeout=2.0)),
                         [(backend_mod.mint_id(FOOT_TILED), "close")])
        ev2 = support.FakeHyprEvents(lines=("workspacev2>>2,2",))
        self.addCleanup(ev2.close)
        b2 = self.backend_with_events(ev2)
        self.assertEqual(list(b2.events(timeout=2.0, workspaces=True)), [(0, "workspace")])

    def test_select_window_waits_for_the_next_focus(self):
        ev = support.FakeHyprEvents(lines=("windowtitlev2>>59daae69e360,foot",
                                           "activewindowv2>>59daae933ac0"))
        self.addCleanup(ev.close)
        b = self.backend_with_events(ev)
        self.assertEqual(b.select_window(), backend_mod.mint_id(XMSG))
        self.assertEqual(b.select_window_hint, "focus the target window to select it")


class FailureModes(Base):
    """Six moods of the double, one line each and never a traceback."""

    def ask(self, mode):
        b = self.backend(mode=mode)
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertNotIn("Traceback", str(cm.exception))
        return str(cm.exception)

    def test_a_compositor_that_closed_the_socket(self):
        self.assertEqual(self.ask("gone"),
                         "hypr backend: the compositor closed the IPC socket without answering `j/clients`")

    def test_an_answer_that_is_not_json(self):
        self.assertTrue(self.ask("badjson").startswith(
            "hypr backend: the compositor's answer to j/clients is not JSON ("))

    def test_a_reply_truncated_mid_object(self):
        self.assertTrue(self.ask("short").startswith(
            "hypr backend: the compositor's answer to j/clients is not JSON ("))

    def test_a_wedged_compositor_ends_at_the_deadline(self):
        """The kernel accepts for a compositor stuck in its own event loop, so only our own deadline ends
        this. Driven at 0.4 s rather than the shipped 10."""
        srv = self.hypr("wedged")
        self.srv = srv
        b = HyprBackend(ipc=HyprIPC(srv.path, timeout=0.4))
        start = time.monotonic()
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(str(cm.exception),
                         "hypr backend: no answer from the compositor within 0.4s (it is not responding)")

    def test_a_socket_that_is_not_there(self):
        tmp = tempfile.mkdtemp(prefix="hypr-gone-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ipc = HyprIPC(os.path.join(tmp, ".socket.sock"))
        with self.assertRaises(CmdError) as cm:
            ipc.json("clients")
        self.assertTrue(str(cm.exception).startswith("hypr backend: cannot connect to "))

    def test_no_socket_at_all_is_one_sentence(self):
        tmp = tempfile.mkdtemp(prefix="hypr-none-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = tmp
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)
        with support.env(HYPRLAND_INSTANCE_SIGNATURE=None, XDG_RUNTIME_DIR=tmp):
            with self.assertRaises(CmdError) as cm:
                HyprBackend()
        self.assertEqual(str(cm.exception),
                         "hypr backend: no Hyprland IPC socket found ($HYPRLAND_INSTANCE_SIGNATURE unset "
                         "and no hypr/*/.socket.sock in any runtime dir)")

    def test_one_connection_per_request_which_is_the_protocol(self):
        """The reply is delimited by EOF and nothing else, so a client that kept the connection would block
        against a real Hyprland for ever. Three requests, three connections."""
        srv = self.hypr()
        self.srv = srv
        b = HyprBackend(ipc=HyprIPC(srv.path))
        b.list()
        self.assertEqual(len(srv.requests), 3)
        self.assertEqual(len(srv.conns), 3)


class Views(Base):
    """U19's last claim: the XWayland row is matched to a real X client on pid and class.

    A real `FakeXServer` and a real `X11Conn`, not a stub of the matcher: `hyprctl` publishes `xwayland: true`,
    the pid and the exact rectangle but no X id at all, and the whole point is that the join through
    `_NET_CLIENT_LIST` is what produces `0x00400020` and `xmessage.Xmessage`
    [M recon2/hyprland.md §3: wwmctl -lpx printed the synthetic 0x000f4243 where wmctrl printed 0x00400020]."""

    XID = 0x00400020

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hypr-x11-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        old = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", old)
        self.x = FakeXServer(self.dir, num=71)
        self.addCleanup(self.x.stop)
        p = mock.patch.dict(os.environ, {"DISPLAY": ":71",
                                         "XAUTHORITY": os.path.join(self.dir, "no-such-authority")})
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(session, "xwayland_running", lambda uid=None: True)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(session, "find_x_display", lambda uid=None: ":71")
        p3.start()
        self.addCleanup(p3.stop)
        p4 = mock.patch.object(session, "find_xauthority", lambda uid=None: None)
        p4.start()
        self.addCleanup(p4.stop)

    def plant(self, xid=XID, inst="xmessage", cls="Xmessage", pid=11920, geo=(300, 200, 400, 300)):
        root = FakeXServer.ROOTS[0]
        self.x.set_prop(root, "_NET_CLIENT_LIST", "WINDOW", 32, struct.pack("<I", xid))
        self.x.set_prop(xid, "WM_CLASS", "STRING", 8, (inst + "\0" + cls + "\0").encode())
        self.x.set_prop(xid, "_NET_WM_PID", "CARDINAL", 32, struct.pack("<I", pid))
        self.x.set_prop(xid, "_NET_WM_NAME", "UTF8_STRING", 8, b"xmessage")
        self.x.geometry[xid] = geo
        self.x.translate[xid] = geo[:2]

    def views(self, srv=None):
        return {v.window.title: v for v in self.backend(srv).views()}

    def test_the_xwayland_row_gets_its_real_x_id_and_wm_class_pair(self):
        self.plant()
        v = self.views()["xmessage"]
        self.assertEqual(v.xid, self.XID)
        self.assertEqual((v.instance, v.cls), ("xmessage", "Xmessage"))
        self.assertEqual(v.client_type, "x11")
        self.assertEqual(v.app_id, "")

    def test_native_toplevels_keep_xid_zero_and_their_app_id(self):
        self.plant()
        v = self.views()["Screen Layout Editor"]
        self.assertEqual((v.xid, v.client_type, v.app_id), (0, "wayland", "__main__.py"))

    def test_a_client_that_contradicts_the_row_is_not_matched(self):
        """pid and WM_CLASS are filters: an X client of another pid AND another class agrees with nothing, so
        the xmessage keeps xid 0 -- an unknown id beats a wrong one (wdotool/xid_match.py)."""
        self.plant(pid=999, inst="xterm", cls="XTerm")
        v = self.views()["xmessage"]
        self.assertEqual(v.xid, 0)
        self.assertEqual((v.instance, v.cls), ("Xmessage", "Xmessage"))

    def test_nothing_is_opened_when_no_xwayland_runs(self):
        """Connecting to the socket would START Xwayland on a compositor that spawns it on demand."""
        self.plant()
        with mock.patch.object(session, "xwayland_running", lambda uid=None: False):
            with mock.patch.object(x11_mini, "X11Conn",
                                   lambda *a, **k: self.fail("the X socket was opened")):
                self.assertEqual(self.views()["xmessage"].xid, 0)

    def test_the_view_flags_come_from_the_client_row(self):
        self.plant()
        srv = self.hypr()
        rows = json.loads(json.dumps(srv.payloads["clients"]))
        rows[0]["fullscreen"] = 2
        rows[1]["fullscreen"] = 1
        rows[3]["pinned"] = True
        srv.payloads["clients"] = rows
        views = {v.window.id: v for v in self.backend(srv).views()}
        self.assertTrue(views[backend_mod.mint_id(FOOT_FLOAT)].fullscreen)
        self.assertTrue(views[backend_mod.mint_id(FOOT_TILED)].maximized_h)
        self.assertTrue(views[backend_mod.mint_id(FOOT_TILED)].maximized_v)
        self.assertFalse(views[backend_mod.mint_id(FOOT_TILED)].floating)
        self.assertTrue(views[backend_mod.mint_id(XMSG)].sticky)
        self.assertEqual(views[backend_mod.mint_id(XMSG)].ws_name, "1")
        # every recorded row sits on monitor 0, and 0 is a monitor: `int(r.get("monitor") or -1)` would
        # report the common case as "unknown"
        self.assertEqual({v.monitor for v in views.values()}, {0})
        rows[3]["monitor"] = 1
        srv.payloads["clients"] = rows
        by_id = {v.window.id: v for v in self.backend(srv).views()}
        self.assertEqual(by_id[backend_mod.mint_id(XMSG)].monitor, 1)


class TheWiring(Base):
    """The backend is what a Hyprland session detects onto, and it says what it is."""

    def test_the_name_and_the_window_manager_name(self):
        b = self.backend()
        self.assertEqual(b.name, "hypr")
        self.assertEqual(b.wm_name, "Hyprland")

    def test_detect_reaches_it_from_the_socket_alone(self):
        """Detection's hypr arm sits above the bus checks because a Hyprland session owns neither
        org.kde.KWin nor org.gnome.Shell [M recon2/hyprland.md §2, `busctl --user list`]."""
        from wdotool import backend_detect
        srv = self.hypr()
        self.srv = srv
        backend_detect.reset()
        self.addCleanup(backend_detect.reset)
        with mock.patch.object(session, "find_sway_socket", lambda: None), \
                mock.patch.object(session, "find_hypr_socket", lambda: srv.path):
            b = backend_detect.detect()
        self.assertIsInstance(b, HyprBackend)
        self.assertEqual(b.sockpath, srv.path)

    def test_the_forced_spelling_builds_it_too(self):
        from wdotool import backend_detect
        srv = self.hypr()
        self.srv = srv
        backend_detect.reset()
        self.addCleanup(backend_detect.reset)
        with support.env(WDOTOOL_BACKEND="hypr"), \
                mock.patch.object(session, "find_hypr_socket", lambda: srv.path):
            self.assertIsInstance(backend_detect.detect(), HyprBackend)

    def test_the_events_socket_is_the_sibling_of_the_request_one(self):
        ipc = HyprIPC("/run/user/1000/hypr/sig/.socket.sock")
        self.assertEqual(ipc.events_path, "/run/user/1000/hypr/sig/.socket2.sock")



if __name__ == "__main__":
    unittest.main()
