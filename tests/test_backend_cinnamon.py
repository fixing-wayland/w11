"""U24: the Cinnamon window backend, against a mock org.Cinnamon that only understands the JS it is sent.

Cinnamon's `org.Cinnamon.Eval(s) -> (b, s)` runs `JSON.stringify(eval(code))` with no gate of any kind
[M recon2/cinnamon.md §2.2], so wdotool/backend_cinnamon.py drives muffin with one JS program per operation
and the programs are wdotool/cinnamon_js.py. That makes the *text of the scripts* the interface, and this
file treats it as one:

* `MockCinnamon` PARSES what arrives. It knows the two list programs by their exact text and the per-window
  programs by the shape of their wrapper, and it applies the ones it understands to a little model of a
  muffin session built from the records the recon measured live (the xterm and the foot in §2.3, their
  geometry and the four workspaces in §4). A script it cannot parse is recorded in `unknown` and every test
  asserts that list is empty -- which is the landmine: the backend cannot quietly grow a second way of
  asking for something, and it cannot send a script this file has not read.
* `ScriptText` pins the text itself. That is where the id policy, the pid fallback and the X id live: the
  mock plays muffin's side of the mapping, and these assertions are what say the program really asks muffin
  for `get_stable_sequence()` and not `get_id()`.
* `NoStringsInScripts` is the injection half. Titles and WM_CLASS come *out* of these programs; ids and
  coordinates go *in* and are integers. A window titled `'); evil(); //` must never turn up inside a script,
  and no builder may accept a string where a number belongs -- `eval()` on the other end will run whatever
  arrives.

The mock's model also reproduces the one asynchrony the prototype hit: a native Wayland window takes a
resize when it acks the configure, so the same command chain read the old size back and the new one two
seconds later [M cinnamon.md §4]. `resize_lag` is that, in list replies.
"""

import contextlib
import io
import json
import os
import re
import sys
import threading
import unittest
from unittest import mock as umock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common.dbus_mini import ERR, Bus, DBusError
from w11common.errors import CmdError
import test_dbus_mini as tdm
from test_xkbmap import _FakeService
from wdotool import backend, backend_cinnamon, backend_gnome, cinnamon_js as js
from wdotool.backend_cinnamon import CinnamonBackend
from wdotool.ctx import NoSessionError
from wwmctl import core

os.environ["W11_PASSTHROUGH"] = "never"

NAME = backend_cinnamon.BUS_NAME
PATH = backend_cinnamon.OBJECT_PATH
IFACE = backend_cinnamon.IFACE

# ------------------------------------------------------------------ the model
#
# One record per Meta.Window, with the fields the JS reads. The two here are
# the two the recon measured on a live 6.4.13 [M cinnamon.md §2.3]:
#   X11 client (xterm):   {"id":3070932382,"x":16777228,"seq":3,"pid":79691,"ct":1}
#   Wayland client (foot):{"id":2959920136,"x":0,"seq":1,"pid":-1,"cpid":86275,"ct":0}
# `id` is muffin's ~3e9 counter and `seq` its stable sequence; `pid` is -1 for a
# native window and `cpid` is where its pid really is. Geometry is §4's.


def win(seq, **over):
    d = dict(meta_id=3070932382, seq=seq, xwindow=0, title="", wm_class="",
             instance="", pid=-1, client_pid=0, client_type=0, window_type="NORMAL",
             x=0, y=0, w=0, h=0, workspace=0, focus=False, minimized=False,
             maximized=0, fullscreen=False, above=False, shaded=False,
             skip_taskbar=False, client_decorated=False, on_all=False)
    d.update(over)
    return d


XTERM = win(3, meta_id=3070932382, xwindow=16777228, title="yans@w11: ~",
            wm_class="XTerm", instance="xterm", pid=79691, client_type=1,
            x=400, y=337, w=484, h=316, focus=False)
FOOT = win(1, meta_id=2959920136, xwindow=0, title="yans@w11: ~/code",
           wm_class="foot", instance="foot", pid=-1, client_pid=86275, client_type=0,
           x=51, y=21, w=696, h=494, focus=True, client_decorated=True)


class MockCinnamon(_FakeService):
    """org.Cinnamon on the mock bus, answering Eval by reading the script.

    It is deliberately not a table of canned replies: the operations change the model, so a `windowmove`
    followed by a `getwindowgeometry` answers what the move did, and a test cannot pass by both halves
    agreeing on a fiction."""

    #: the per-window wrapper, exactly as cinnamon_js._window builds it. The
    #: regex IS the pin on that shape: a backend that stopped looking windows
    #: up by stable sequence would stop being understood here.
    WRAPPER = re.compile(
        r"^\(function\(\)\{const w=global\.display\.list_windows\(0\)"
        r"\.find\(function\(w\)\{return w\.get_stable_sequence\(\)==(\d+);\}\);"
        r"if\(!w\)return 'NOWIN';(.*);return 'ok';\}\)\(\)$", re.S)

    MOVE = re.compile(r"^w\.move_frame\(true,(-?\d+),(-?\d+)\)$")
    RESIZE = re.compile(r"^const r=w\.get_frame_rect\(\);"
                        r"w\.move_resize_frame\(true,r\.x,r\.y,(\d+),(\d+)\)$")
    MOVE_RESIZE = re.compile(r"^w\.move_resize_frame\(true,(-?\d+),(-?\d+),(\d+),(\d+)\)$")
    SET_WS = re.compile(r"^w\.change_workspace_by_index\((\d+),false\)$")
    ACTIVATE_WS = re.compile(r"^global\.workspace_manager\.get_workspace_by_index\((\d+)\)"
                             r"\.activate\(global\.get_current_time\(\)\);1$")

    def __init__(self, address, windows=None, workspaces=4, active=0,
                 screen=(800, 600), pointer=(120, 240)):
        self.windows = [dict(w) for w in (windows if windows is not None
                                          else [XTERM, FOOT])]
        self.n_workspaces = workspaces
        self.active = active
        self.screen = screen
        self.pointer = pointer
        self.work_area = (0, 0, 1920, 1040)     # measured on the X11 session [§3.1]
        self.scripts = []          # every script that arrived, in order
        self.unknown = []          # ...and the ones this mock could not read
        self.resize_lag = 0        # list replies before a resize becomes visible
        self._pending = []         # (window, w, h, replies left)
        self.fail_next = None      # answer (false, <text>) once
        self.lock = threading.Lock()
        _FakeService.__init__(self, address, NAME)

    # -- the model

    def by_seq(self, seq):
        for w in self.windows:
            if w["seq"] == seq:
                return w
        return None

    def _record(self, w):
        """One entry of the list program's reply: muffin's side of the field mapping in cinnamon_js.LIST."""
        return {"id": w["seq"], "xid": w["xwindow"], "title": w["title"],
                "cls": w["wm_class"], "inst": w["instance"],
                "pid": w["pid"] if w["pid"] > 0 else w["client_pid"],
                "ct": w["client_type"], "wt": w["window_type"],
                "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"],
                "ws": -1 if w["on_all"] else w["workspace"], "act": self.active,
                "foc": w["focus"], "min": w["minimized"], "max": w["maximized"],
                "full": w["fullscreen"], "above": w["above"], "shaded": w["shaded"],
                "skipt": w["skip_taskbar"], "dec": not w["client_decorated"]}

    def _list_reply(self):
        for entry in list(self._pending):
            entry[3] -= 1
            if entry[3] <= 0:
                entry[0]["w"], entry[0]["h"] = entry[1], entry[2]
                self._pending.remove(entry)
        return json.dumps([self._record(w) for w in self.windows])

    # -- the interpreter

    def _value(self, script):
        """What `eval(script)` would produce, or KeyError for a script this mock cannot read."""
        if script == js.LIST:
            return self._list_reply()
        if script == js.WORKSPACES:
            return json.dumps([{"i": i, "name": "Workspace %d" % (i + 1),
                                "act": i == self.active, "wa": list(self.work_area)}
                               for i in range(self.n_workspaces)])
        if script == js.GET_DESKTOP:
            return self.active
        if script == js.NUM_DESKTOPS:
            return self.n_workspaces
        if script == js.DISPLAY_SIZE:
            return json.dumps(list(self.screen))
        if script == js.POINTER:
            return json.dumps([self.pointer[0], self.pointer[1], 0])
        m = self.ACTIVATE_WS.match(script)
        if m:
            self.active = int(m.group(1))
            return 1
        m = self.WRAPPER.match(script)
        if m is None:
            raise KeyError(script)
        w = self.by_seq(int(m.group(1)))
        if w is None:
            return js.NOWIN
        self._apply(w, m.group(2))
        return "ok"

    def _apply(self, w, body):
        """The window operations, by their exact JS body."""
        simple = {
            "w.activate(global.get_current_time())": lambda: self._focus(w),
            "w.focus(global.get_current_time())": lambda: self._focus(w),
            "w.delete(global.get_current_time())": lambda: self.windows.remove(w),
            "w.kill()": lambda: self.windows.remove(w),
            "w.minimize()": lambda: w.update(minimized=True),
            "w.unminimize()": lambda: w.update(minimized=False),
            "w.raise()": lambda: self._restack(w, -1),
            "w.lower()": lambda: self._restack(w, 0),
        }
        if body in simple:
            simple[body]()
            return
        for name, (add, remove) in js.STATES.items():
            if body == add:
                return self._state(w, name, True)
            if body == remove:
                return self._state(w, name, False)
        m = self.MOVE.match(body)
        if m:
            w["x"], w["y"] = int(m.group(1)), int(m.group(2))
            return None
        m = self.RESIZE.match(body)
        if m:
            return self._resize(w, int(m.group(1)), int(m.group(2)))
        m = self.MOVE_RESIZE.match(body)
        if m:
            w["x"], w["y"] = int(m.group(1)), int(m.group(2))
            return self._resize(w, int(m.group(3)), int(m.group(4)))
        m = self.SET_WS.match(body)
        if m:
            w["workspace"] = int(m.group(1))
            return None
        raise KeyError(body)

    def _resize(self, w, width, height):
        if self.resize_lag <= 0:
            w["w"], w["h"] = width, height
        else:
            # what a native Wayland client does: the new size lands when it
            # acks the configure, several reads later [M cinnamon.md §4]
            self._pending.append([w, width, height, self.resize_lag])

    def _focus(self, w):
        for other in self.windows:
            other["focus"] = other is w
        self._restack(w, -1)

    def _restack(self, w, where):
        self.windows.remove(w)
        self.windows.insert(len(self.windows) if where == -1 else 0, w)

    def _state(self, w, name, add):
        if name == "MAXIMIZED":
            w["maximized"] = 3 if add else 0
        elif name == "MAXIMIZED_HORZ":
            w["maximized"] = (w["maximized"] | 1) if add else (w["maximized"] & ~1)
        elif name == "MAXIMIZED_VERT":
            w["maximized"] = (w["maximized"] | 2) if add else (w["maximized"] & ~2)
        elif name == "FULLSCREEN":
            w["fullscreen"] = add
        elif name == "ABOVE":
            w["above"] = add
        elif name == "STICKY":
            w["on_all"] = add
        elif name == "SHADED":
            w["shaded"] = add
        elif name == "HIDDEN":
            w["minimized"] = add

    # -- the wire

    def dispatch(self, m):
        if m.path != PATH or m.interface != IFACE or m.member != "Eval":
            return _FakeService.dispatch(self, m)
        (script,) = m.args()
        with self.lock:
            self.scripts.append(script)
            if self.fail_next is not None:
                text, self.fail_next = self.fail_next, None
                return "bs", (False, text)
            try:
                value = self._value(script)
            except KeyError:
                self.unknown.append(script)
                # what Cinnamon really answers for a program that throws
                return "bs", (False, "TypeError: MockCinnamon cannot read this script")
        return "bs", (True, json.dumps(value))


class CinnamonCase(unittest.TestCase):
    """One mock bus for the file; a fresh org.Cinnamon per test."""

    @classmethod
    def setUpClass(cls):
        cls.mock = tdm.MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def windows(self):
        return [XTERM, FOOT]

    def setUp(self):
        self.svc = MockCinnamon(self.mock.address, windows=self.windows())
        self.addCleanup(self.svc.close, self.mock)
        self.buses = []
        self.addCleanup(self._close_buses)

    def _close_buses(self):
        for b in self.buses:
            b.close()

    def tearDown(self):
        self.assertEqual(self.svc.unknown, [],
                         "the backend sent a script MockCinnamon could not read")

    def backend(self, settle=0.0, **kw):
        b = Bus(self.mock.address)
        self.buses.append(b)
        return CinnamonBackend(bus=b, names=b.list_names(), settle=settle, **kw)


# ---------------------------------------------------------------- the scripts

class ScriptText(unittest.TestCase):
    """The programs themselves. Every claim here is one muffin makes true or false, and there is nowhere
    else in the tree that it can be stated."""

    def test_the_id_is_the_stable_sequence(self):
        """`get_id()` is a ~3e9 counter (two windows measured 3070932382 and 2959920136, two apart);
        `get_stable_sequence()` is small and dense, which is the shape every other backend's ids have
        [M recon2/cinnamon.md §2.3]."""
        self.assertIn("id:w.get_stable_sequence()", js.LIST)
        self.assertNotIn("get_id()", js.LIST)
        self.assertIn("w.get_stable_sequence()==", js.action(7, "activate"))

    def test_the_pid_falls_back_to_the_client_pid(self):
        """`get_pid()` reads _NET_WM_PID and answers -1 for a native Wayland window; `get_client_pid()` is
        where its pid is [M cinnamon.md §2.3]."""
        self.assertIn("pid:(w.get_pid()>0?w.get_pid():(w.get_client_pid?w.get_client_pid():0))",
                      js.LIST)

    def test_the_x_id_comes_straight_out_of_the_window(self):
        """`get_xwindow()` is the X id for an X client and 0 for a native one, so views() needs no matching
        against _NET_CLIENT_LIST -- the thing the KWin backend does need [M cinnamon.md §2.3]."""
        self.assertIn("xid:w.get_xwindow()", js.LIST)
        self.assertIn("ct:w.get_client_type()", js.LIST)

    def test_the_list_is_muffins_and_is_stacking_ordered(self):
        """`meta_display_list_all_windows` does not exist in muffin: it has `list_windows(flags)`
        [M cinnamon.md §2.3]. hit_test() wants bottom-to-top."""
        self.assertIn("global.display.sort_windows_by_stacking(", js.LIST)
        self.assertIn("global.display.list_windows(0)", js.LIST)
        self.assertNotIn("list_all_windows", js.LIST)

    def test_the_window_type_arrives_by_its_enum_name(self):
        self.assertIn("const TYPE={};for(const k in M.WindowType)TYPE[M.WindowType[k]]=k;", js.LIST)
        self.assertIn("wt:TYPE[w.get_window_type()]||\"NORMAL\"", js.LIST)

    def test_the_per_window_wrapper_is_one_program(self):
        self.assertEqual(
            js.action(3, "activate"),
            "(function(){const w=global.display.list_windows(0)"
            ".find(function(w){return w.get_stable_sequence()==3;});"
            "if(!w)return 'NOWIN';w.activate(global.get_current_time());return 'ok';})()")

    def test_move_and_resize_are_the_frame_rectangle(self):
        self.assertEqual(
            js.move(3, 100, 120),
            "(function(){const w=global.display.list_windows(0)"
            ".find(function(w){return w.get_stable_sequence()==3;});"
            "if(!w)return 'NOWIN';w.move_frame(true,100,120);return 'ok';})()")
        self.assertIn("const r=w.get_frame_rect();w.move_resize_frame(true,r.x,r.y,500,400)",
                      js.resize(3, 500, 400))

    def test_the_maximize_pair_is_one_flag_and_the_axes_are_the_other_two(self):
        """`MaximizeFlags.BOTH`: `get_maximized()` answered 3 afterwards [M cinnamon.md §4]."""
        self.assertIn("w.maximize(imports.gi.Meta.MaximizeFlags.BOTH)", js.state(3, "MAXIMIZED", True))
        self.assertIn("w.unmaximize(imports.gi.Meta.MaximizeFlags.BOTH)", js.state(3, "MAXIMIZED", False))
        self.assertIn("MaximizeFlags.VERTICAL", js.state(3, "MAXIMIZED_VERT", True))
        self.assertIn("MaximizeFlags.HORIZONTAL", js.state(3, "MAXIMIZED_HORZ", True))

    def test_shading_is_here_because_muffin_kept_it(self):
        """mutter dropped shading and KWin 6 removed it; muffin has `shade`/`unshade`/`is_shaded`
        [M cinnamon.md §2.3]. The GNOME backend calls the same name a gap, in as many words."""
        self.assertIn("w.shade(global.get_current_time())", js.state(3, "SHADED", True))
        self.assertIn("SHADED", backend_gnome._GAP_REASONS)
        self.assertNotIn("SHADED", backend_cinnamon._GAP_REASONS)


class NoStringsInScripts(unittest.TestCase):
    """The injection surface, and the whole of it: `eval()` runs what arrives."""

    HOSTILE = "'); imports.gi.GLib.spawn_command_line_async('touch /tmp/pwned'); ('"

    def test_no_builder_takes_a_string_where_a_number_belongs(self):
        for call in (lambda v: js.action(v, "activate"),
                     lambda v: js.move(v, 0, 0),
                     lambda v: js.resize(v, 1, 1),
                     lambda v: js.move_resize(v, 0, 0, 1, 1),
                     lambda v: js.set_workspace(v, 0),
                     lambda v: js.set_desktop(v),
                     lambda v: js.state(v, "FULLSCREEN", True)):
            with self.assertRaises((ValueError, TypeError)):
                call(self.HOSTILE)

    def test_the_coordinates_are_numbers_too(self):
        with self.assertRaises((ValueError, TypeError)):
            js.move(1, self.HOSTILE, 0)
        with self.assertRaises((ValueError, TypeError)):
            js.resize(1, 1, self.HOSTILE)

    def test_an_unknown_state_name_is_not_a_program(self):
        with self.assertRaises(KeyError):
            js.state(1, "'); evil(); ('", True)
        with self.assertRaises(KeyError):
            js.action(1, "evil")


class TitlesNeverGoBackIn(CinnamonCase):
    """The other half: the strings really do come back, and really do not go out again."""

    HOSTILE = "'); imports.gi.GLib.spawn_command_line_async('touch /tmp/pwned'); ('"

    def windows(self):
        return [dict(XTERM, title=self.HOSTILE, wm_class=self.HOSTILE,
                     instance=self.HOSTILE), FOOT]

    def test_a_hostile_title_survives_the_round_trip_and_enters_no_script(self):
        b = self.backend()
        wins = b.list()
        self.assertEqual(wins[0].title, self.HOSTILE)
        b.activate(wins[0].id)
        b.move_window(wins[0].id, 10, 20)
        b.set_state(wins[0].id, "FULLSCREEN", 1)
        b.views()
        self.assertTrue(self.svc.scripts)
        for script in self.svc.scripts:
            self.assertNotIn("spawn_command_line_async", script)
            self.assertNotIn("pwned", script)


# ---------------------------------------------------------------- listing

class Listing(CinnamonCase):
    """The reply record -> Window/View mapping. *Which* JS expression fills each field is not this class's
    claim -- the mock computes the fields, so a test here could only agree with it -- and is pinned as
    literal script text in `ScriptText` (`id:w.get_stable_sequence()`, the `get_pid()>0` ternary over
    `get_client_pid()`, `xid:w.get_xwindow()`). These say the backend carries what came back into the field
    the rest of the tree reads."""

    def test_the_seq_field_becomes_the_window_id(self):
        ids = [w.id for w in self.backend().list()]
        self.assertEqual(ids, [3, 1])
        for w in self.backend().list():
            self.assertNotIn(w.id, (3070932382, 2959920136))

    def test_the_pid_field_becomes_the_window_pid_for_both_client_kinds(self):
        by_id = {w.id: w for w in self.backend().list()}
        self.assertEqual(by_id[3].pid, 79691)     # the X client's _NET_WM_PID
        self.assertEqual(by_id[1].pid, 86275)     # foot's, via get_client_pid

    def test_the_xid_field_becomes_the_view_xid_and_decides_the_client_kind(self):
        by_id = {v.window.id: v for v in self.backend().views()}
        self.assertEqual(by_id[3].xid, 16777228)
        self.assertEqual(by_id[3].client_type, "x11")
        self.assertEqual(by_id[1].xid, 0)
        self.assertEqual(by_id[1].client_type, "wayland")
        self.assertEqual(by_id[1].app_id, "foot")
        self.assertEqual(by_id[3].app_id, "")

    def test_the_wm_class_pair_is_both_halves(self):
        v = {v.window.id: v for v in self.backend().views()}[3]
        self.assertEqual((v.instance, v.cls), ("xterm", "XTerm"))

    def test_the_geometry_is_the_frame_rectangle(self):
        w = {w.id: w for w in self.backend().list()}[1]
        self.assertEqual((w.x, w.y, w.w, w.h), (51, 21, 696, 494))

    def test_one_eval_per_listing(self):
        b = self.backend()
        del self.svc.scripts[:]
        b.list()
        self.assertEqual(self.svc.scripts, [js.LIST])

    def test_views_asks_for_the_workspace_names_and_nothing_else(self):
        b = self.backend()
        del self.svc.scripts[:]
        views = b.views()
        self.assertEqual(self.svc.scripts, [js.WORKSPACES, js.LIST])
        self.assertEqual(views[0].ws_name, "Workspace 1")

    def test_a_window_on_all_workspaces_is_sticky_and_visible(self):
        self.svc.windows[0]["on_all"] = True
        self.svc.active = 2
        by_id = {v.window.id: v for v in self.backend().views()}
        self.assertTrue(by_id[3].sticky)
        self.assertEqual(by_id[3].window.desktop, -1)
        self.assertTrue(by_id[3].window.visible)
        self.assertFalse(by_id[1].window.visible)   # on workspace 0, we are on 2

    def test_a_minimized_window_is_not_visible_and_is_still_mapped_nowhere(self):
        self.svc.windows[1]["minimized"] = True
        b = self.backend()
        self.assertFalse({w.id: w for w in b.list()}[1].visible)
        self.assertFalse(b.is_mapped(1))
        self.assertTrue(b.is_mapped(3))

    def test_the_maximize_bits_are_the_meta_enum_way_round(self):
        """HORIZONTAL is bit 0 and VERTICAL is bit 1 (meta/window.h). The only value the recon could measure
        was the pair (3), which cannot tell them apart, so this is the enum's word against a coin flip."""
        self.svc.windows[0]["maximized"] = 1
        v = {v.window.id: v for v in self.backend().views()}[3]
        self.assertTrue(v.maximized_h)
        self.assertFalse(v.maximized_v)

    def test_an_unknown_id_is_one_line(self):
        with self.assertRaises(CmdError) as cm:
            self.backend().find(4242)
        self.assertEqual(str(cm.exception), "window 4242 not found")


class HitTest(CinnamonCase):
    """`window_type` exists for exactly one reason: one rule, on every backend, looks through the desktop
    and dock layers the way a click on an X11 root window does."""

    def windows(self):
        return [win(9, window_type="DESKTOP", x=0, y=0, w=800, h=600, wm_class="Nemo"),
                win(10, window_type="DOCK", x=0, y=0, w=800, h=40, wm_class="Cinnamon"),
                dict(FOOT, x=0, y=0, w=800, h=600)]

    def test_the_desktop_and_dock_layers_are_looked_through(self):
        wins = self.backend().list()
        self.assertEqual([w.window_type for w in wins], ["DESKTOP", "DOCK", "NORMAL"])
        self.assertEqual(backend.hit_test(wins, 10, 10), 1)

    def test_nothing_under_the_pointer_is_zero(self):
        wins = [w for w in self.backend().list() if w.window_type == "NORMAL"]
        self.assertEqual(backend.hit_test(wins, 900, 900), 0)


# ---------------------------------------------------------------- acting

class Acting(CinnamonCase):
    def test_every_action_is_one_eval_the_mock_recognises(self):
        """One script per operation, and its body one the mock's table knows -- the landmine, since an
        unrecognised script is recorded in `svc.unknown` and every `tearDown` asserts that list empty.
        What the operation does to a real muffin is `js.action`'s business, pinned as text in `ScriptText`;
        what this says is that the backend sends exactly one and does not read anything back afterwards."""
        b = self.backend()
        for op, check in (
                (lambda: b.activate(3), lambda: self.svc.by_seq(3)["focus"]),
                (lambda: b.minimize(3), lambda: self.svc.by_seq(3)["minimized"]),
                (lambda: b.map(3), lambda: not self.svc.by_seq(3)["minimized"]),
                (lambda: b.set_window_desktop(3, 2),
                 lambda: self.svc.by_seq(3)["workspace"] == 2)):
            del self.svc.scripts[:]
            op()
            self.assertEqual(len(self.svc.scripts), 1, self.svc.scripts)
            self.assertTrue(check())

    def test_close_and_kill_both_go_through_muffin(self):
        b = self.backend()
        b.close(3)
        self.assertIsNone(self.svc.by_seq(3))
        b.kill(1)
        self.assertIsNone(self.svc.by_seq(1))

    def test_kill_needs_no_pid_of_its_own(self):
        """The default implementation reads the pid and signals it; muffin kills the client itself, which
        works whatever uid this process has."""
        b = self.backend()
        with umock.patch("os.kill", side_effect=AssertionError("os.kill was called")):
            b.kill(1)

    def test_raise_and_lower_restack(self):
        b = self.backend()
        b.raise_(3)
        self.assertEqual([w.id for w in b.list()], [1, 3])
        b.lower(3)
        self.assertEqual([w.id for w in b.list()], [3, 1])

    def test_an_action_on_a_window_that_is_gone_is_one_line(self):
        b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.activate(4242)
        self.assertEqual(str(cm.exception), "window 4242 not found")

    def test_the_desktop_number_round_trips(self):
        b = self.backend()
        self.assertEqual(b.num_desktops(), 4)
        self.assertEqual(b.get_desktop(), 0)
        b.set_desktop(2)
        self.assertEqual(b.get_desktop(), 2)

    def test_the_workspaces_carry_their_names_and_work_areas(self):
        ws = self.backend().workspaces()
        self.assertEqual([w.index for w in ws], [0, 1, 2, 3])
        self.assertEqual(ws[0].name, "Workspace 1")
        self.assertTrue(ws[0].active)
        self.assertEqual(ws[1].work_area, (0, 0, 1920, 1040))

    def test_the_display_size_is_the_layout_box(self):
        self.assertEqual(self.backend().display_size(), (800, 600))

    def test_the_pointer_is_the_compositors_own(self):
        self.assertEqual(self.backend().pointer(), (120, 240))

    def test_there_is_no_picker(self):
        """`global.stage.grab` does not exist in muffin's Clutter [M cinnamon.md §2.3], so the refusal owes
        the route. A reactive Clutter actor is code inside Cinnamon, but this backend already pushes JS in
        over `org.Cinnamon.Eval` with nothing installed, and AGENTS.md line 33 files Eval under rung 2 --
        the lowest rung that does the job is the one the sentence must name, so it is 2 and not the 3 an
        extension would be. Pinned whole: this is the one byte-for-byte copy of the sentence, and the same
        constant is what wxprop prints as its hint."""
        with self.assertRaises(CmdError) as cm:
            self.backend().select_window()
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(str(cm.exception),
                         "selectwindow is not supported by the cinnamon backend: picking a window by "
                         "clicking is not yet done on Cinnamon (AGENTS.md route 2, a reactive Clutter "
                         "actor through org.Cinnamon.Eval); name the window another way")

    def test_x_info_defers_to_the_session_scan(self):
        """muffin has no `get_x11_display()` to ask, and writes `.mutter-Xwaylandauth.*`, which
        w11common/session.py already finds [M cinnamon.md §2.3]."""
        self.assertIsNone(self.backend().x_info())


class Moving(CinnamonCase):
    def test_a_move_lands_and_is_read_back(self):
        b = self.backend()
        b.move_window(1, 100, 100)
        w = b.find(1)
        self.assertEqual((w.x, w.y), (100, 100))

    def test_a_resize_is_waited_for(self):
        """A native Wayland client takes the new size when it acks the configure: the prototype's chain read
        the old size and had the new one two seconds later [M cinnamon.md §4]. So resize() polls."""
        self.svc.resize_lag = 3
        b = self.backend(settle=2.0)
        b.resize(1, 500, 400)
        self.assertEqual((self.svc.by_seq(1)["w"], self.svc.by_seq(1)["h"]), (500, 400))
        self.assertEqual(b.find(1).w, 500)
        # the polling is what made that true: more than the one apply script
        self.assertGreater(self.svc.scripts.count(js.LIST), 1)

    def test_the_wait_gives_up_rather_than_hanging(self):
        """A client that never takes the size (or a compositor that refuses it) must not wedge the command:
        the poll is bounded and the tool answers with what is there."""
        self.svc.resize_lag = 10 ** 6
        b = self.backend(settle=0.05)
        b.resize(1, 500, 400)
        self.assertEqual(b.find(1).w, 696)

    def test_no_wait_at_all_is_one_script(self):
        """`settle` 0 means no round trip beyond the move itself. The read is the wait's first step, so a
        wait of zero has to skip it too -- otherwise a caller that constructed the backend with `settle=0`,
        and every move of an Xwayland client whose configure is synchronous anyway, pays a second Eval to
        ask a question its own timeout has already answered."""
        b = self.backend(settle=0.0)
        del self.svc.scripts[:]
        b.move_window(1, 10, 10)
        self.assertEqual(len(self.svc.scripts), 1)
        self.assertEqual(self.svc.by_seq(1)["x"], 10)

    def test_a_negative_settle_is_not_an_endless_one(self):
        """The guard is `<= 0`, not `== 0`: a settle read off the environment can be negative, and a
        deadline already in the past would still have cost one read before the loop noticed."""
        b = self.backend(settle=-1.0)
        del self.svc.scripts[:]
        b.resize(1, 500, 400)
        self.assertEqual(len(self.svc.scripts), 1)


class States(CinnamonCase):
    def test_the_pair_is_one_call(self):
        b = self.backend()
        self.assertEqual(b.maximize_pair_state(), "MAXIMIZED")
        del self.svc.scripts[:]
        b.set_state(1, "MAXIMIZED", 1)
        self.assertEqual(len(self.svc.scripts), 1)
        self.assertEqual(self.svc.by_seq(1)["maximized"], 3)

    def test_each_axis_on_its_own_still_works(self):
        b = self.backend()
        b.set_state(1, "MAXIMIZED_HORZ", 1)
        self.assertEqual(self.svc.by_seq(1)["maximized"], 1)
        b.set_state(1, "MAXIMIZED_VERT", 1)
        self.assertEqual(self.svc.by_seq(1)["maximized"], 3)
        b.set_state(1, "MAXIMIZED_HORZ", 0)
        self.assertEqual(self.svc.by_seq(1)["maximized"], 2)

    def test_shaded_is_supported_here_and_is_a_gap_on_gnome(self):
        b = self.backend()
        self.assertIsNone(b.set_state(1, "SHADED", 1))
        self.assertTrue(self.svc.by_seq(1)["shaded"])
        self.assertNotIn("SHADED", b.unsupported_states())
        self.assertIn("SHADED", backend_gnome._GAP_REASONS)

    def test_a_toggle_reads_the_state_first(self):
        b = self.backend()
        b.set_state(1, "FULLSCREEN", 2)
        self.assertTrue(self.svc.by_seq(1)["fullscreen"])
        b.set_state(1, "FULLSCREEN", 2)
        self.assertFalse(self.svc.by_seq(1)["fullscreen"])

    def test_sticky_is_the_workspace_answer(self):
        b = self.backend()
        b.set_state(1, "STICKY", 1)
        self.assertEqual(b.find(1).desktop, -1)

    def test_the_cosmetic_states_warn_and_succeed(self):
        b = self.backend()
        err = []
        with umock.patch.object(backend_cinnamon, "warn", err.append):
            self.assertIsNone(b.set_state(1, "SKIP_TASKBAR", 1))
        self.assertEqual(len(err), 1)
        self.assertIn("Muffin has no setter for it", err[0])
        self.assertIn("SKIP_TASKBAR", b.unsupported_states())

    def test_the_gap_sentences_say_what_would_close_them(self):
        """AGENTS.md: never the compositor's lack alone. Both shapes -- the warn-and-succeed one and the
        refusal -- carry the X plane (where wwmctl really does set these on an XWayland window,
        `unsupported_states()`) and the rung for a native window."""
        b = self.backend()
        err = []
        with umock.patch.object(backend_cinnamon, "warn", err.append):
            b.set_state(1, "MODAL", 1)
        with self.assertRaises(CmdError) as cm:
            b.set_state(1, "BELOW", 1)
        for said in (err[0], str(cm.exception)):
            self.assertIn("not yet here", said)
            self.assertIn("wwmctl sets it on the X plane", said)
            self.assertIn("AGENTS.md route 6", said)
        self.assertTrue(err[0].endswith("ignoring"), "the warn still ends where it did")

    def test_below_is_a_named_gap(self):
        b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_state(1, "BELOW", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("Muffin has no API for it", str(cm.exception))

    def test_the_maximize_pair_is_folded_before_it_gets_here(self):
        """backend.state_steps() is what wwmctl -b uses; naming the pair is what makes it fold."""
        steps = backend.state_steps(self.backend(),
                                    ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"])
        self.assertEqual(steps, [("MAXIMIZED", ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"])])


# ---------------------------------------------------------------- errors

class Errors(CinnamonCase):
    def test_an_eval_that_failed_is_one_line_with_cinnamons_text(self):
        self.svc.fail_next = "TypeError: w.frobnicate is not a function"
        with self.assertRaises(CmdError) as cm:
            self.backend().list()
        self.assertEqual(str(cm.exception),
                         "cinnamon backend: Eval failed: "
                         "TypeError: w.frobnicate is not a function")

    def test_a_bus_without_cinnamon_is_the_no_session_answer(self):
        b = Bus(self.mock.address)
        self.buses.append(b)
        with self.assertRaises(NoSessionError) as cm:
            CinnamonBackend(bus=b, names=["org.freedesktop.DBus", "org.kde.KWin"])
        self.assertEqual(str(cm.exception),
                         "cinnamon backend: org.Cinnamon is not on the session bus "
                         "(no Cinnamon session?)")

    def test_the_name_going_away_mid_session_is_one_line(self):
        """Cinnamon restarting (`RestartCinnamon` is on its own interface) drops the name; every call after
        that is ServiceUnknown, and a user does not need our stack for it."""
        b = self.backend()
        self.svc.close(self.mock)
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertEqual(str(cm.exception), backend_cinnamon._GONE)

    def test_a_reply_that_is_not_json_is_one_line(self):
        b = self.backend()
        with umock.patch.object(b.bus, "call", return_value=(True, "{not json")):
            with self.assertRaises(CmdError) as cm:
                b.list()
        self.assertIn("malformed JSON", str(cm.exception))

    def test_a_program_that_answered_nothing_is_one_line(self):
        b = self.backend()
        with umock.patch.object(b.bus, "call", return_value=(True, "")):
            with self.assertRaises(CmdError) as cm:
                b.get_desktop()
        self.assertIn("answered nothing", str(cm.exception))

    def test_a_no_reply_names_the_shell_and_not_the_wire(self):
        b = self.backend()
        with umock.patch.object(b.bus, "call",
                                side_effect=DBusError(ERR + "NoReply", "timeout")):
            with self.assertRaises(CmdError) as cm:
                b.list()
        self.assertIn("no reply from org.Cinnamon", str(cm.exception))

    def tearDown(self):
        # this class kills the service in one test, so the landmine check runs
        # only while it is alive
        if self.svc.bus.sock is not None:
            CinnamonCase.tearDown(self)


# ---------------------------------------------------------------- events

class Events(CinnamonCase):
    """There is no signal route through Eval [M cinnamon.md §2.2], so `-spy` polls. What matters is that a
    diff of two list replies yields sway's words for the four things it can see.

    The changes are made *during* the poll's own sleep, which is where they happen in a real session -- so
    no thread and no wall-clock waiting decides whether this test passes."""

    def drain(self, backend_obj, steps):
        steps = list(steps)

        def during_the_sleep(_seconds):
            if steps:
                steps.pop(0)()

        with umock.patch("time.sleep", during_the_sleep):
            # timeout 0: the iterator ends at the first poll that sees nothing,
            # which is the poll after the last step
            return list(backend_obj.events(timeout=0.0))

    def test_a_new_window_a_title_change_a_focus_and_a_close(self):
        b = self.backend()
        seen = self.drain(b, [
            lambda: self.svc.windows.append(win(7, wm_class="nemo", x=0, y=0, w=10, h=10)),
            lambda: self.svc.by_seq(7).update(title="Home"),
            lambda: self.svc._focus(self.svc.by_seq(7)),
            lambda: self.svc.windows.remove(self.svc.by_seq(7)),
        ])
        self.assertEqual(seen, [(7, "new"), (7, "title"), (7, "focus"), (7, "close")])

    def test_a_window_that_opens_already_focused_gets_both_words(self):
        """sway sends `new` and `focus` as two events, and wxprop -spy keys on `focus` to reprint
        _NET_WM_STATE_FOCUSED. A window mapped under the pointer is focused in the very poll that first
        sees it, so the rising edge the next poll would look for has already gone by: emit both here or
        never emit the focus at all."""
        b = self.backend()

        def opens_focused():
            self.svc.windows.append(win(8, wm_class="nemo", x=0, y=0, w=10, h=10))
            self.svc._focus(self.svc.by_seq(8))

        seen = self.drain(b, [opens_focused])
        self.assertEqual(seen, [(8, "new"), (8, "focus")])

    def test_a_window_that_opens_unfocused_gets_only_the_one(self):
        """The control: the extra word is the focus flag's, not something every new window now gets."""
        b = self.backend()
        seen = self.drain(b, [lambda: self.svc.windows.append(
            win(9, wm_class="nemo", x=0, y=0, w=10, h=10))])
        self.assertEqual(seen, [(9, "new")])

    def test_a_quiet_session_ends_the_iterator_on_the_timeout(self):
        b = self.backend()
        self.assertEqual(self.drain(b, []), [])

    def test_the_words_are_sways(self):
        """wwmctl and wxprop -spy print these; a backend inventing its own vocabulary would print something
        no consumer of theirs has ever handled."""
        b = self.backend()
        seen = self.drain(b, [lambda: self.svc.by_seq(1).update(title="other")])
        self.assertEqual({c for _wid, c in seen}, {"title"})


class TheDesktopColumnOffThisBackend(CinnamonCase):
    """`wwmctl -l` over a real `CinnamonBackend`: the sticky background windows that were the one red
    check on resolute-cinnamon-wayland (76 pass / 1 fail).

    The number is muffin's own, taken on the `resolute-cinnamon` X11 golden 2026-09-12: `nemo-desktop`
    0x02a00030 carried `_NET_WM_STATE = SKIP_PAGER, SKIP_TASKBAR, STICKY`, `_NET_WM_DESKTOP: not found.`
    and `_WIN_WORKSPACE: not found.`, and real `wmctrl -l` printed `0x02a00030  0 ... Desktop` -- wmctrl
    falls through to 0 for a window carrying neither property. On the WAYLAND session muffin publishes no
    `_NET_WM_DESKTOP` on the X window at all, so the same 0 is owed there.

    This is the backend half of `tests/test_wwmctl_cli.py`'s gate tests: `core.NO_NET_WM_DESKTOP_XWM` is
    keyed on `CinnamonBackend.name`, and nothing else in the tree ties the 0 to the backend it was
    measured on."""

    def rows(self):
        b = self.backend()
        c = core.Core()
        c._backend = b
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(c.list_windows(False, False, False), 0)
        return out.getvalue().splitlines()

    def nemo_desktop(self):
        """The golden's window, in this file's model: an X client (`ct:1`, a real xid) that muffin
        reports on all workspaces, which `cinnamon_js.LIST` turns into `ws:-1`."""
        self.svc.windows[:] = [win(3, meta_id=3070932382, xwindow=0x02A00030, title="Desktop",
                                   wm_class="Nemo-desktop", instance="nemo-desktop", pid=1234,
                                   client_type=1, x=0, y=0, w=1920, h=1080, on_all=True)]

    def test_a_sticky_x_window_prints_the_0_real_wmctrl_printed_for_it(self):
        self.nemo_desktop()
        self.assertEqual(self.rows(), ["0x02a00030  0 %s Desktop" % core.hostname()])

    def test_the_backend_itself_still_reports_minus_one(self):
        """The guard that keeps the test above from being a tautology of the backend: the -1 is what
        `CinnamonBackend.list()` really says (`ws:(w.is_on_all_workspaces()?-1:...)`), and the 0 is
        `Core._desktop_column` translating it into the original's byte."""
        self.nemo_desktop()
        self.assertEqual([w.desktop for w in self.backend().list()], [-1])

    def test_a_native_sticky_row_keeps_the_minus_one(self):
        """foot with `on_all`: the only original that can see a native window reads it through xw11,
        whose shadow publishes `_NET_WM_DESKTOP = 0xFFFFFFFF`, which wmctrl prints as -1. Measured on a
        headless labwc 2026-09-12: `0x00600001 -1 ... gfoot` from the original for the row this column
        prints -1 for. So the 0 is for X-plane rows and not a blanket."""
        self.svc.windows[:] = [dict(FOOT, on_all=True)]
        self.assertEqual(self.rows(), ["0x00000001 -1 %s yans@w11: ~/code" % core.hostname()])


if __name__ == "__main__":
    unittest.main()
