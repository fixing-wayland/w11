#!/usr/bin/env python3
"""wxprop on GNOME: plane resolution over the mock w11 bridge
(tests/test_backend_gnome.py's MockBridge) with the X plane faked as an
in-memory x11_mini stand-in.

Covers: the synthesized property set of native (bridge) windows -- WM_CLASS
from the app id, the states and window types a views() backend knows --,
XWayland windows going to the X plane with the bridge's DISPLAY/XAUTHORITY
(by X id or by bridge id), the no-Xwayland degradation, -root as the merged
X-root + bridge set (and the pure bridge set without Xwayland, with no
connection attempted), -spy on a native window / the native root / the
merged root over the bridge's events, click-to-select over SelectWindow,
-name over both planes, -set/-remove on X vs native windows, and the
bridge-less error lines."""

import io
import os
import queue
import struct
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from w11common.dbus_mini import Bus
from test_backend_gnome import (CALC, DESKTOP, EDITOR, XTERM, XTERM_XID,
                                MockBridge, _Base)
from test_wxprop_cli import _CapStdout
from hacks.window import backend_detect, backend_gnome
from hacks.window.backend_gnome import IFACE, OBJECT_PATH, GnomeBackend
from hacks.window.x11_mini import X11Error
from wxprop import cli
from hacks.property import core

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

XROOT = 0x1C5
WM_CHECK = 0x200001
XAUTH = "/run/user/1000/.mutter-Xwaylandauth.AB12CD"


def _u32(*vals):
    return struct.pack("<%dI" % len(vals), *vals)


def mutter_x_props():
    """What Mutter puts on the X root and what xterm carries, in server
    order: {window: [(name, type, format, wire), ...]}."""
    return {
        XROOT: [
            ("_NET_SUPPORTING_WM_CHECK", "WINDOW", 32, _u32(WM_CHECK)),
            ("_NET_NUMBER_OF_DESKTOPS", "CARDINAL", 32, _u32(1)),
            ("_NET_DESKTOP_NAMES", "UTF8_STRING", 8, b"Workspace 1\0"),
            ("_NET_CURRENT_DESKTOP", "CARDINAL", 32, _u32(0)),
            ("_NET_WORKAREA", "CARDINAL", 32, _u32(0, 32, 1920, 1048)),
            ("_NET_SHOWING_DESKTOP", "CARDINAL", 32, _u32(0)),
            ("_NET_CLIENT_LIST", "WINDOW", 32, _u32(XTERM_XID)),
            ("_NET_CLIENT_LIST_STACKING", "WINDOW", 32, _u32(XTERM_XID)),
            ("_NET_ACTIVE_WINDOW", "WINDOW", 32, _u32(XTERM_XID)),
        ],
        WM_CHECK: [
            ("_NET_WM_NAME", "UTF8_STRING", 8, b"GNOME Shell"),
            ("_NET_SUPPORTING_WM_CHECK", "WINDOW", 32, _u32(WM_CHECK)),
        ],
        XTERM_XID: [
            ("_NET_WM_STATE", "ATOM", 32, b""),
            ("WM_CLASS", "STRING", 8, b"xterm\0XTerm\0"),
            ("_NET_WM_PID", "CARDINAL", 32, _u32(1201)),
            ("WM_CLIENT_MACHINE", "STRING", 8, b"vm"),
            ("_NET_WM_NAME", "UTF8_STRING", 8, b"test@vm: ~"),
            ("WM_NAME", "STRING", 8, b"test@vm: ~"),
        ],
    }


class FakeXConn:
    """In-memory stand-in for wdotool.x11_mini.X11Conn: properties per
    window, an atom table, a QueryTree shape (root -> frame -> client), and
    an event queue for -spy (an exception object in the queue is raised, so
    a test can end a -spy loop with KeyboardInterrupt)."""

    def __init__(self, props=None):
        self.props = props if props is not None else mutter_x_props()
        self.tree = {XROOT: [WM_CHECK, 0x600001], 0x600001: [XTERM_XID]}
        self.calls = []
        self.events = queue.Queue()
        self._atoms, self._names = {}, {}
        for i, n in enumerate(core._PREDEFINED_ATOMS, start=1):
            self._atoms[n] = i
            self._names[i] = n
        self._next = 300
        for entries in self.props.values():  # a server knows its own atoms
            for n, t, _f, _d in entries:
                self.atom(n)
                self.atom(t)

    def root(self):
        return XROOT

    def atom(self, name, only_if_exists=False):
        a = self._atoms.get(name)
        if a:
            return a
        if only_if_exists:
            return 0
        a = self._next
        self._next += 1
        self._atoms[name] = a
        self._names[a] = name
        return a

    def get_atom_name(self, a):
        return self._names.get(a)

    def _entries(self, win):
        if win not in self.props:
            raise X11Error(3, 20, 0, win)
        return self.props[win]

    def read_property(self, win, name):
        self.calls.append(("read", win, name))
        for n, t, f, d in self._entries(win):
            if n == name:
                return t, f, d
        return None

    def list_properties(self, win):
        self.calls.append(("list", win))
        return [self.atom(n) for n, _t, _f, _d in self._entries(win)]

    def query_tree(self, win):
        return list(self.tree.get(win, []))

    def delete_property(self, win, name):
        # like the real one: False only when the atom does not exist
        self.calls.append(("delete", win, name))
        if not self.atom(name, only_if_exists=True):
            return False
        entries = self._entries(win)
        entries[:] = [e for e in entries if e[0] != name]
        return True

    def change_property(self, win, name, type_name, fmt, data):
        self.calls.append(("change", win, name, type_name, fmt, data))
        entries = self._entries(win)
        entries[:] = [e for e in entries if e[0] != name]
        entries.append((name, type_name, fmt, data))

    def select_input(self, win, mask):
        self.calls.append(("select_input", win, mask))

    def next_event(self, timeout=None):
        try:
            ev = self.events.get(timeout=timeout if timeout is not None
                                 else 30)
        except queue.Empty:
            return None
        if isinstance(ev, BaseException):
            raise ev
        return ev

    def close(self):
        pass


class GnomeXpropBase(_Base):
    def setUp(self):
        self.bridge = MockBridge(self.mock, select_id=EDITOR,
                                 select_delay=0.05)
        self.backend = GnomeBackend(settle=0.05)
        self.x_calls = []
        self.xconn = FakeXConn()

    def tearDown(self):
        self.backend.bus.close()
        self.bridge.close()

    def calls(self, member):
        return [a for m, a in self.bridge.calls if m == member]

    def _patches(self, x="auto", xwayland=None, detect=None, lc="C"):
        if x == "auto":
            x = self.xconn
        self.x = x

        def connect(display, xauthority=None):
            self.x_calls.append((display, xauthority))
            return x

        ps = [
            mock.patch.dict(os.environ, {"WXPROP_ARGV0": "xprop",
                                         "LC_ALL": lc}),
            mock.patch.object(core, "_detect_backend",
                              detect or (lambda: self.backend)),
            mock.patch.object(core, "_x11_connect", connect),
            mock.patch.object(core, "hostname", lambda: "testhost"),
        ]
        os.environ.pop("WXPROP_NO_X", None)
        if xwayland is not None:
            ps.append(mock.patch.object(core, "_xwayland_running",
                                        lambda: xwayland))
        return ps

    def run_cli(self, *args, x="auto", xwayland=None, detect=None, lc="C"):
        out, err = _CapStdout(), io.StringIO()
        ps = self._patches(x, xwayland, detect, lc) + [
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(sys, "stderr", err)]
        for p in ps:
            p.start()
        try:
            code = cli.main(list(args))
        finally:
            for p in reversed(ps):
                p.stop()
        return code, out.buffer.getvalue(), err.getvalue()


NATIVE_EDITOR_DUMP = (
    b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN\n"
    b"_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n"
    b"_NET_WM_DESKTOP(CARDINAL) = 0\n"
    b"_NET_WM_PID(CARDINAL) = 1300\n"
    b'WM_CLIENT_MACHINE(STRING) = "testhost"\n'
    b'WM_CLASS(STRING) = "org.gnome.TextEditor", "org.gnome.TextEditor"\n'
    b'_NET_WM_NAME(UTF8_STRING) = "Untitled Document 1 - Text Editor"\n'
    b'WM_NAME(STRING) = "Untitled Document 1 - Text Editor"\n'
    b"WM_STATE(WM_STATE):\n\t\twindow state: Iconic\n\t\ticon window: 0x0\n")


class NativePlaneTests(GnomeXpropBase):
    def test_native_window_full_dump(self):
        code, out, err = self.run_cli("-id", "%d" % EDITOR)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, NATIVE_EDITOR_DUMP)
        self.assertEqual(self.x_calls, [])  # never touched the X plane

    def test_native_title_outside_latin1_is_typed_utf8(self):
        """wxprop-1: type STRING *means* ISO 8859-1, and xprop decodes a
        STRING as latin-1 before re-encoding it for the locale. UTF-8 bytes
        typed STRING therefore printed as mojibake for every character
        above U+00FF. A title that does not fit latin-1 is UTF8_STRING."""
        self.bridge.find(CALC)["title"] = "\u00e9 \u2713 \u65e5\u672c"
        code, out, err = self.run_cli("-id", "%d" % CALC, "WM_NAME",
                                      "_NET_WM_NAME", lc="C.UTF-8")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (
            'WM_NAME(UTF8_STRING) = "\u00e9 \u2713 \u65e5\u672c"\n'
            '_NET_WM_NAME(UTF8_STRING) = "\u00e9 \u2713 \u65e5\u672c"\n'
        ).encode("utf-8"))

    def test_native_latin1_title_stays_a_string(self):
        """... and one that does fit stays STRING, like the WM_NAME an
        XWayland twin really carries; xprop's STRING-to-locale rule then
        prints it as UTF-8 under a UTF-8 locale."""
        self.bridge.find(CALC)["title"] = "caf\u00e9"
        code, out, err = self.run_cli("-id", "%d" % CALC, "WM_NAME",
                                      lc="C.UTF-8")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         'WM_NAME(STRING) = "caf\u00e9"\n'.encode("utf-8"))

    def test_focused_transient_and_wm_state(self):
        """wxprop-11: the bridge reports transient_for, focus and
        visibility; the native synthesis dropped all three. Mutter puts
        _NET_WM_STATE_FOCUSED last in its own set_net_wm_state order, and
        writes WM_STATE on every X11 window it manages."""
        self.bridge.find(CALC).update(transient_for=EDITOR, focused=True)
        code, out, err = self.run_cli("-id", "%d" % CALC, "_NET_WM_STATE",
                                      "WM_TRANSIENT_FOR", "WM_STATE")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_FOCUSED\n"
            b"WM_TRANSIENT_FOR(WINDOW): window id # 0x400002\n"
            b"WM_STATE(WM_STATE):\n\t\twindow state: Normal\n"
            b"\t\ticon window: 0x0\n"))
        # an unfocused window with no parent carries neither
        code, out, _e = self.run_cli("-id", "%d" % DESKTOP,
                                     "WM_TRANSIENT_FOR")
        self.assertEqual(out, b"WM_TRANSIENT_FOR:  not found.\n")

    def test_desktop_window_type_and_skip_taskbar(self):
        code, out, _e = self.run_cli("-id", "%d" % DESKTOP, "_NET_WM_STATE",
                                     "_NET_WM_WINDOW_TYPE", "WM_CLASS")
        self.assertEqual(code, 0)
        self.assertEqual(out, (
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_SKIP_TASKBAR\n"
            b"_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DESKTOP\n"
            b'WM_CLASS(STRING) = "Gjs", "Gjs"\n'))

    def test_rich_states_in_mutter_order(self):
        self.bridge.find(CALC).update(maximized_h=True, maximized_v=True,
                                      above=True, urgent=True, fullscreen=True,
                                      on_all_workspaces=True, workspace=-1)
        code, out, _e = self.run_cli("-id", "%d" % CALC, "_NET_WM_STATE",
                                     "_NET_WM_DESKTOP")
        self.assertEqual(code, 0)
        self.assertEqual(out, (
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_HORZ, "
            b"_NET_WM_STATE_MAXIMIZED_VERT, _NET_WM_STATE_FULLSCREEN, "
            b"_NET_WM_STATE_ABOVE, _NET_WM_STATE_DEMANDS_ATTENTION, "
            b"_NET_WM_STATE_STICKY\n"
            b"_NET_WM_DESKTOP(CARDINAL) = 4294967295\n"))

    def test_dialog_type(self):
        self.bridge.find(CALC)["window_type"] = "MODAL_DIALOG"
        code, out, _e = self.run_cli("-id", "%d" % CALC, "_NET_WM_WINDOW_TYPE")
        self.assertEqual(out, b"_NET_WM_WINDOW_TYPE(ATOM) = "
                              b"_NET_WM_WINDOW_TYPE_DIALOG\n")

    def test_set_and_remove_on_native_fail_cleanly(self):
        code, _o, err = self.run_cli("-id", "%d" % CALC, "-set", "WM_NAME", "x")
        self.assertEqual(code, 1)
        self.assertIn("-set cannot work on a native Wayland window", err)
        code, _o, err = self.run_cli("-id", "%d" % CALC, "-remove", "WM_NAME")
        self.assertEqual(code, 1)
        self.assertIn("-remove cannot work on a native Wayland window", err)


class XPlaneTests(GnomeXpropBase):
    def test_x_window_by_x_id_uses_bridge_display_and_cookie(self):
        code, out, err = self.run_cli("-id", "0x400005", "WM_CLASS",
                                      "_NET_WM_PID")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n'
                              b"_NET_WM_PID(CARDINAL) = 1201\n")
        self.assertEqual(self.x_calls, [(":0", XAUTH)])
        self.assertIn(("read", XTERM_XID, "WM_CLASS"), self.xconn.calls)

    def test_x_window_by_bridge_id_redirects_to_its_x_id(self):
        code, out, _e = self.run_cli("-id", "%d" % XTERM, "WM_CLIENT_MACHINE")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLIENT_MACHINE(STRING) = "vm"\n')
        self.assertEqual(self.xconn.calls[-1], ("read", XTERM_XID, "WM_CLIENT_MACHINE"))

    def test_x_window_with_x_unreachable_is_synthesized_from_the_bridge(self):
        code, out, _e = self.run_cli("-id", "0x400005", "WM_CLASS", x=None)
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')
        self.assertEqual(self.x_calls, [(":0", XAUTH)])

    def test_explicit_display_skips_the_bridge_cookie(self):
        code, out, _e = self.run_cli("-display", ":5", "-id", "0x400005",
                                     "WM_CLASS")
        self.assertEqual(code, 0)
        self.assertEqual(self.x_calls, [(":5", None)])

    def test_unknown_id_goes_to_x_only_when_xwayland_is_up(self):
        code, _o, err = self.run_cli("-id", "0x12345", xwayland=True)
        self.assertEqual(code, 1)
        self.assertIn("X Error of failed request:  BadWindow", err)
        self.assertEqual(self.x_calls, [(":0", XAUTH)])
        self.x_calls.clear()
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        code, _o, err = self.run_cli("-id", "0x12345", xwayland=False)
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: window id # 0x12345 does not "
                              "exists!\n")
        self.assertEqual(self.x_calls, [])  # no Xwayland spawned for a typo

    def test_set_and_remove_on_x_window(self):
        code, out, err = self.run_cli("-id", "0x400005", "-set", "WM_NAME",
                                      "renamed")
        self.assertEqual((code, out, err), (0, b"", ""))
        self.assertIn(("change", XTERM_XID, "WM_NAME", "STRING", 8, b"renamed"),
                      self.xconn.calls)
        code, _o, err = self.run_cli("-id", "0x400005", "-remove", "WM_CLASS")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(("delete", XTERM_XID, "WM_CLASS"), self.xconn.calls)
        code, out, _e = self.run_cli("-id", "0x400005", "WM_CLASS")
        self.assertEqual(out, b"WM_CLASS:  not found.\n")


class RootTests(GnomeXpropBase):
    def test_root_merges_x_root_with_the_bridge(self):
        code, out, err = self.run_cli("-root")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        # Mutter's own root properties, straight from X, in server order
        self.assertEqual(lines[0], b"_NET_SUPPORTING_WM_CHECK(WINDOW): "
                                   b"window id # 0x200001")
        self.assertIn(b"_NET_WORKAREA(CARDINAL) = 0, 32, 1920, 1048", lines)
        self.assertIn(b"_NET_SHOWING_DESKTOP(CARDINAL) = 0", lines)
        # the window-list properties see every window, by the id the tools
        # print (X id for XWayland, bridge id for native), and the
        # workspace facts come from the workspace manager
        self.assertIn(b"_NET_CLIENT_LIST(WINDOW): window id # 0x3ffffd, "
                      b"0x400002, 0x400003, 0x400005", lines)
        self.assertIn(b"_NET_CLIENT_LIST_STACKING(WINDOW): window id # "
                      b"0x3ffffd, 0x400002, 0x400003, 0x400005", lines)
        self.assertIn(b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x400005", lines)
        self.assertIn(b"_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 3", lines)
        self.assertIn(b"_NET_CURRENT_DESKTOP(CARDINAL) = 0", lines)
        self.assertIn(b'_NET_DESKTOP_NAMES(UTF8_STRING) = "Workspace 1", '
                      b'"Workspace 2", "Workspace 3"', lines)
        self.assertEqual(len(lines), len(mutter_x_props()[XROOT]))
        self.assertEqual(self.x_calls, [(":0", XAUTH)])

    def test_root_single_property_and_active_native_window(self):
        self.bridge._focus(CALC)
        code, out, _e = self.run_cli("-root", "_NET_ACTIVE_WINDOW",
                                     "_NET_SUPPORTING_WM_CHECK")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x400003\n"
                              b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # "
                              b"0x200001\n")

    def test_root_without_xwayland_is_the_bridge_set_and_spawns_nothing(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        code, out, err = self.run_cli("-root", xwayland=False)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(self.x_calls, [])
        self.assertTrue(lines[0].startswith(b"_NET_SUPPORTED(ATOM) = "))
        self.assertIn(b"_NET_CLIENT_LIST(WINDOW): window id # 0x3ffffd, "
                      b"0x400002, 0x400003", lines)
        self.assertIn(b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0", lines)
        self.assertIn(b'_NET_DESKTOP_NAMES(UTF8_STRING) = "Workspace 1", '
                      b'"Workspace 2", "Workspace 3"', lines)
        self.assertIn(b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x0", lines)

    def test_root_with_xwayland_up_but_no_x_windows_uses_x(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        code, out, _e = self.run_cli("-root", "_NET_SUPPORTING_WM_CHECK",
                                     xwayland=True)
        self.assertEqual(code, 0)
        self.assertEqual(out, b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # "
                              b"0x200001\n")
        self.assertEqual(self.x_calls, [(":0", XAUTH)])

    def test_set_on_the_merged_root_goes_to_x(self):
        code, _o, err = self.run_cli("-root", "-set", "WM_NAME", "rooty")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(("change", XROOT, "WM_NAME", "STRING", 8, b"rooty"),
                      self.xconn.calls)


    def test_removing_an_override_from_the_merged_root_is_visible(self):
        """wxprop-2: -set/-remove address the real X root while a read of
        one of the six window-list names is answered from the bridge, so
        the tool could not see its own write. Deleting _NET_CLIENT_LIST
        breaks every EWMH client on the X plane (`wmctrl -l`: "Cannot get
        client list properties") and -root reported silence."""
        code, out, err = self.run_cli("-root", "-remove", "_NET_CLIENT_LIST")
        self.assertEqual((code, out), (0, b""))
        self.assertEqual(err, "xprop:  -remove _NET_CLIENT_LIST writes the "
                              "X root only; XWayland clients read it, the "
                              "compositor does not\n")
        self.assertIn(("delete", XROOT, "_NET_CLIENT_LIST"), self.xconn.calls)

    def test_merged_root_reads_back_what_it_wrote(self):
        """... and once a name has been written this run, reads of it go to
        the X root, so a caller that writes and then reads sees its own
        write rather than the synthesis."""
        ps = self._patches()
        for pp in ps:
            pp.start()
        try:
            target = core.resolve_root(core.Session())
            self.assertIsInstance(target, core.MergedRootTarget)
            synth = target.fetch(b"_NET_CLIENT_LIST")
            self.assertEqual(synth[0], "WINDOW")
            self.assertEqual(len(synth[2]), 16)     # all four windows
            self.assertTrue(target.remove_prop(b"_NET_CLIENT_LIST"))
            self.assertIsNone(target.fetch(b"_NET_CLIENT_LIST"))
            self.assertNotIn(b"_NET_CLIENT_LIST", target.list_names())
            # the five untouched names still come from the compositor
            self.assertEqual(len(target.fetch(b"_NET_CLIENT_LIST_STACKING")[2]),
                             16)
        finally:
            for pp in reversed(ps):
                pp.stop()

    def test_merged_root_synthesizes_a_name_the_x_root_never_had(self):
        """The absence of an override is not damage: Mutter writes
        _NET_CURRENT_DESKTOP only once the workspace changes, so a fresh
        session has none while the compositor knows the answer (live on
        GNOME 46). -root must still print it."""
        props = mutter_x_props()
        props[XROOT] = [e for e in props[XROOT]
                        if e[0] != "_NET_CURRENT_DESKTOP"]
        self.xconn = FakeXConn(props)
        code, out, err = self.run_cli("-root", "_NET_CURRENT_DESKTOP")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b"_NET_CURRENT_DESKTOP(CARDINAL) = 0\n")

    def test_setting_an_override_on_the_merged_root_says_where_it_went(self):
        code, _o, err = self.run_cli("-root", "-f", "_NET_CURRENT_DESKTOP",
                                     "32c", "-set", "_NET_CURRENT_DESKTOP",
                                     "7")
        self.assertEqual(code, 0)
        self.assertEqual(err, "xprop:  -set _NET_CURRENT_DESKTOP writes the "
                              "X root only; XWayland clients read it, the "
                              "compositor does not\n")
        # the X root really took it; -root still answers from the
        # compositor, which is the authority on the workspace
        self.assertIn(("change", XROOT, "_NET_CURRENT_DESKTOP", "CARDINAL",
                       32, struct.pack("<I", 7)), self.xconn.calls)
        code, out, _e = self.run_cli("-root", "_NET_CURRENT_DESKTOP")
        self.assertEqual(out, b"_NET_CURRENT_DESKTOP(CARDINAL) = 0\n")


class SelectionTests(GnomeXpropBase):
    def test_click_select_uses_select_window(self):
        code, out, err = self.run_cli("WM_CLASS")
        self.assertEqual(code, 0)
        self.assertIn("xprop: click the target window to select it\n", err)
        self.assertEqual(out, b'WM_CLASS(STRING) = "org.gnome.TextEditor", '
                              b'"org.gnome.TextEditor"\n')
        self.assertEqual(self.calls("SelectWindow"), [(0,)])

    def test_click_select_landing_on_an_x_window(self):
        self.bridge.select_id = XTERM
        code, out, _e = self.run_cli("WM_CLIENT_MACHINE")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLIENT_MACHINE(STRING) = "vm"\n')

    def test_name_matches_the_x_plane_first_then_native(self):
        code, out, _e = self.run_cli("-name", "test@vm: ~", "WM_CLIENT_MACHINE")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLIENT_MACHINE(STRING) = "vm"\n')
        code, out, _e = self.run_cli("-name", "Calculator", "WM_CLASS")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "org.gnome.Calculator", '
                              b'"org.gnome.Calculator"\n')
        code, out, _e = self.run_cli("-name", "org.gnome.TextEditor", "WM_NAME")
        self.assertEqual(out, b'WM_NAME(STRING) = "Untitled Document 1 - '
                              b'Text Editor"\n')
        code, _o, err = self.run_cli("-name", "nope")
        self.assertEqual((code, err),
                         (1, "xprop: error: No window with name nope exists!\n"))

    def test_name_without_xwayland_never_connects(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        code, out, _e = self.run_cli("-name", "Calculator", "WM_CLASS",
                                     xwayland=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.x_calls, [])


class SpyTests(GnomeXpropBase):
    """-spy loops run the CLI on a thread; the test plays the bridge (signals
    from a Bus of its own) and the X server (the fake's event queue)."""

    def _spy(self, *args, **kw):
        result = {}
        ready = threading.Event()

        def target():
            out, err = _CapStdout(), io.StringIO()
            ps = self._patches(**kw) + [
                mock.patch.object(sys, "stdout", out),
                mock.patch.object(sys, "stderr", err)]
            for p in ps:
                p.start()
            ready.set()
            try:
                result["code"] = cli.main(list(args))
            finally:
                for p in reversed(ps):
                    p.stop()
                result["out"] = out.buffer.getvalue()
                result["err"] = err.getvalue()
        t = threading.Thread(target=target, daemon=True)
        t.start()
        ready.wait(5)
        time.sleep(0.4)  # the initial dump + the event subscription
        return t, result

    def _limit_events(self, seconds=3):
        """The root loops have no natural end: make the bridge stream stop
        after `seconds` of silence so the CLI thread returns."""
        real = self.backend.events

        def limited(timeout=None, workspaces=False):
            return real(timeout=seconds, workspaces=workspaces)
        self.backend.events = limited

    def _emit(self, member, sig, args):
        emitter = Bus(self.mock.address)
        try:
            emitter.emit_signal(OBJECT_PATH, IFACE, member, sig, args)
        finally:
            emitter.close()

    def test_spy_native_window_reprints_on_bridge_events(self):
        t, r = self._spy("-spy", "-id", "%d" % CALC, "_NET_WM_STATE", "WM_NAME")
        self.bridge.find(CALC)["fullscreen"] = True
        self._emit("WindowEvent", "ts", (CALC, "fullscreen_mode"))
        time.sleep(0.3)
        self.bridge.find(CALC)["title"] = "Calc 2"
        self._emit("WindowEvent", "ts", (EDITOR, "title"))  # someone else
        self._emit("WindowEvent", "ts", (CALC, "move"))     # no property
        self._emit("WindowEvent", "ts", (CALC, "title"))
        time.sleep(0.3)
        self._emit("WindowEvent", "ts", (CALC, "close"))
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["out"], (
            b"_NET_WM_STATE(ATOM) = \n"
            b'WM_NAME(STRING) = "Calculator"\n'
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n"
            b'WM_NAME(STRING) = "Calc 2"\n'))

    def test_spy_native_root_follows_workspace_and_focus_events(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        self._limit_events()
        t, r = self._spy("-spy", "-root", "_NET_CURRENT_DESKTOP",
                         "_NET_ACTIVE_WINDOW", xwayland=False)
        self.bridge.active_ws = 1
        self._emit("WorkspaceEvent", "s", ("switch",))
        time.sleep(0.3)
        self.bridge._focus(CALC)
        self._emit("WindowEvent", "ts", (CALC, "focus"))
        t.join(8)  # ends when the (limited) bridge stream goes silent
        self.assertFalse(t.is_alive())
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["out"], (
            b"_NET_CURRENT_DESKTOP(CARDINAL) = 0\n"
            b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"
            b"_NET_CURRENT_DESKTOP(CARDINAL) = 1\n"
            b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x400003\n"))
        self.assertEqual(self.x_calls, [])

    def test_spy_merged_root_mixes_x_and_bridge_events(self):
        self._limit_events()
        t, r = self._spy("-spy", "-root", "_NET_SHOWING_DESKTOP",
                         "_NET_ACTIVE_WINDOW", "_NET_CLIENT_LIST")
        # an X-side change of a Mutter-owned property: reprinted from X
        self.xconn.props[XROOT][5] = ("_NET_SHOWING_DESKTOP", "CARDINAL", 32, _u32(1))
        self.xconn.events.put({"type": "PropertyNotify", "window": XROOT,
                               "atom": self.xconn.atom("_NET_SHOWING_DESKTOP"),
                               "state": 0})
        # an X-side change of an overridden property: NOT reprinted (the
        # X root only knows X windows)
        self.xconn.events.put({"type": "PropertyNotify", "window": XROOT,
                               "atom": self.xconn.atom("_NET_CLIENT_LIST"),
                               "state": 0})
        time.sleep(0.4)
        self.bridge._focus(CALC)
        self._emit("WindowEvent", "ts", (CALC, "focus"))
        time.sleep(0.4)
        self.xconn.events.put(KeyboardInterrupt())
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(r["code"], 130)
        self.assertEqual(r["out"], (
            b"_NET_SHOWING_DESKTOP(CARDINAL) = 0\n"
            b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x400005\n"
            b"_NET_CLIENT_LIST(WINDOW): window id # 0x3ffffd, 0x400002, "
            b"0x400003, 0x400005\n"
            b"_NET_SHOWING_DESKTOP(CARDINAL) = 1\n"
            b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x400003\n"))
        self.assertIn(("select_input", XROOT, core.SPY_EVENT_MASK), self.xconn.calls)


class ErrorPathTests(_Base):
    def setUp(self):
        backend_detect.reset()

    def tearDown(self):
        backend_detect.reset()

    def _run(self, *args, x=None):
        out, err = _CapStdout(), io.StringIO()
        with mock.patch.dict(os.environ, {"WXPROP_ARGV0": "xprop", "LC_ALL": "C"}), \
                mock.patch.object(core, "_x11_connect",
                                  lambda display, xauthority=None: x), \
                mock.patch.object(core, "_xwayland_running", lambda: False), \
                mock.patch.object(core, "hostname", lambda: "testhost"), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            os.environ.pop("WXPROP_NO_X", None)
            code = cli.main(list(args))
        return code, out.buffer.getvalue(), err.getvalue()

    def test_real_detection_over_the_mock_bus(self):
        bridge = MockBridge(self.mock)
        try:
            code, out, err = self._run("-id", "%d" % CALC, "WM_CLASS")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, b'WM_CLASS(STRING) = "org.gnome.Calculator", '
                                  b'"org.gnome.Calculator"\n')
        finally:
            bridge.close()

    def test_bridge_not_installed_is_one_clear_line(self):
        """Whether the bridge is on disk is a fact about the disk, and this
        read the runner's own ~/.local/share for it: on a box with the
        extension installed the line under test was a different line. The
        state the test is named for is now the state it sets."""
        self.addCleanup(setattr, backend_gnome, "extension_installed",
                        backend_gnome.extension_installed)
        backend_gnome.extension_installed = lambda: ""
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            for args in (("WM_CLASS",), ("-id", "4194307", "WM_CLASS"),
                         ("-root",)):
                backend_detect.reset()
                code, out, err = self._run(*args)
                self.assertEqual((code, out), (1, b""), args)
                self.assertEqual(err.count("\n"), 1, err)
                self.assertTrue(err.startswith("xprop: error: "), err)
                self.assertIn("gnome/install-bridge.sh", err)
        finally:
            bridge.close()

    def test_name_without_bridge_keeps_xprop_wording(self):
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            code, _o, err = self._run("-name", "Calculator")
            self.assertEqual((code, err),
                             (1, "xprop: error: No window with name Calculator "
                                 "exists!\n"))
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
