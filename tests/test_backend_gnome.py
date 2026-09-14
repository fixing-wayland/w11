#!/usr/bin/env python3
"""GNOME backend tests against a mock w11 bridge served on
dbus_mini's in-process mock bus (tests/test_dbus_mini.py MockBus): every
GnomeBackend method, the Window/View/Workspace mapping, error mapping, the
pointer hit-test, the (opt-in) Eval auto-load path, backend_detect's order
for each ListNames outcome, and the shipped udev rule / installer /
interface XML. No GNOME, no real bus needed."""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from w11common import dbus_mini, session
from w11common.dbus_mini import Bus, DBusError, Variant
from w11common.errors import CmdError
from support import env
from test_dbus_mini import MockBus
import wl_fake
from hacks.window import backend_detect, backend_gnome
from hacks.window.backend import (View, Window, WindowBackend, Workspace,
                             hit_test, state_steps)
from hacks.window.backend_gnome import (BUS_NAME, EXT_UUID, IFACE,
                                   OBJECT_PATH, SHELL_NAME, GnomeBackend)
from wdotool.ctx import NoSessionError

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

XTERM, EDITOR, CALC, DESKTOP = 4194305, 4194306, 4194307, 4194301
XTERM_XID = 0x400005
# The fake's work area: the 1920x1080 screen minus a top panel. A window
# maximized on an axis gets that axis of this rectangle, as Mutter's does.
WORK_AREA = (0, 32, 1920, 1048)
# state name -> (touches the horizontal axis, touches the vertical one)
MAX_AXES = {"MAXIMIZED_HORZ": (True, False), "MAXIMIZED_VERT": (False, True),
            "MAXIMIZED": (True, True)}


def _rect(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def fixture_windows():
    """Bottom-to-top stacking order, shaped like the bridge's ListWindows:
    DING's DESKTOP window, a minimized native editor, a calculator parked
    on workspace 1, and a focused XWayland xterm on top."""
    base = {
        "wm_class_instance": "", "gtk_app_id": "", "sandboxed_app_id": "",
        "desktop_id": "", "role": "", "client_type": "wayland",
        "window_type": "NORMAL", "focused": False, "minimized": False,
        "hidden": False, "on_all_workspaces": False, "workspace": 0,
        "on_active_workspace": True, "monitor": 0, "fullscreen": False,
        "maximized_h": False, "maximized_v": False, "above": False,
        "urgent": False, "skip_taskbar": False, "transient_for": 0,
        "decorated": True, "xid": 0,
    }

    def win(**kw):
        d = dict(base)
        d.update(kw)
        d.update(_rect(*d.pop("rect")))
        d["buffer_rect"] = _rect(d["x"], d["y"], d["width"], d["height"])
        d["stable_sequence"] = d["id"] & 0xff
        return d

    return [
        win(id=DESKTOP, title="Desktop", wm_class="Gjs", pid=900,
            window_type="DESKTOP", rect=(0, 0, 1920, 1080), skip_taskbar=True),
        win(id=EDITOR, title="Untitled Document 1 - Text Editor", wm_class="",
            gtk_app_id="org.gnome.TextEditor",
            desktop_id="org.gnome.TextEditor.desktop", pid=1300,
            rect=(300, 200, 800, 600), minimized=True, hidden=True),
        win(id=CALC, title="Calculator", wm_class="org.gnome.Calculator",
            gtk_app_id="org.gnome.Calculator", pid=1400, workspace=1,
            on_active_workspace=False, rect=(500, 300, 400, 500)),
        win(id=XTERM, xid=XTERM_XID, title="test@vm: ~", wm_class="XTerm",
            wm_class_instance="xterm", pid=1201, client_type="x11",
            rect=(100, 80, 640, 480), focused=True),
    ]


def _ext_info_variant(v):
    """One value of GetExtensionInfo's a{sv}, typed the way gnome-shell types
    it: numbers are doubles (`state`, `version`), `shell-version` is an array
    of strings, everything else a string."""
    if isinstance(v, (list, tuple)):
        return Variant("as", [str(x) for x in v])
    if isinstance(v, (int, float)):
        return Variant("d", float(v))
    return Variant("s", str(v))


class MockBridge:
    """The extension's D-Bus surface on a Bus of its own (serve_calls),
    owning org.gnome.Shell and/or org.w11.Bridge like the real shell
    connection does. State lives in `windows` (bridge JSON shape) and is
    mutated by the actions; `calls` records (member, args)."""

    # The bridge's own hard cap on a window selection (extension.js
    # SELECT_MAX_MS); a test below checks the two have not drifted apart.
    SELECT_MAX_MS = 30000
    VERSION = 3

    def __init__(self, mock, own_shell=True, own_bridge=True,
                 eval_unsafe=False, select_delay=0.2, select_id=EDITOR,
                 shell_mode="user", ext_info=None, screensaver_active=False,
                 shell_version="46.0", version=None):
        #: the MockBus itself, not just its address: close() has to wait for
        #: the bus to let go of the names this connection owns before the
        #: next bridge asks for them (MockBus.wait_dropped)
        self.mock = mock
        self.bus = Bus(mock.address)
        self.bus.serve_calls = True
        self.windows = fixture_windows()
        #: id -> the rectangle a window is unmaximized back to, and id -> the
        #: one Mutter still believes because its client has not answered the
        #: last configure. See _maximize()/settle().
        self.saved = {}
        self.stale = {}
        self.calls = []
        #: org.freedesktop.DBus.Properties.Get, kept apart from `calls` so
        #: that the "which bridge methods did this cost?" assertions above
        #: stay about the bridge's own interface
        self.prop_gets = []
        self.active_ws = 0
        self.n_ws = 3
        self.eval_unsafe = eval_unsafe
        self.shell_mode = shell_mode
        self.ext_info = ext_info  # None: uuid unknown to the shell
        self.screensaver_active = screensaver_active
        self.select_delay = select_delay
        # What the user does at the picker: "button" (the default), "escape",
        # "timeout" or "disable"; where the press lands is select_at (a point,
        # hit-tested) or, by default, select_id (the window it resolves to).
        self.select_event = "button"
        self.select_id = select_id
        self.select_at = None
        self.grabs = []            # "take"/"release", in order
        self.version = self.VERSION if version is None else version
        self.xinfo = (":0", "/run/user/1000/.mutter-Xwaylandauth.AB12CD")
        self.pointer = (640, 400, 0)
        #: members that answer LATE rather than not at all: a gnome-shell
        #: busy in a JS loop still holds the D-Bus connection open, so the
        #: client's own timeout is the only thing that ends the call
        self.stall = set()
        self.stall_for = 2.0
        #: member -> the exact string to hand back where a JSON document is
        #: expected. A bridge that answers `{}` to ListWindows is not
        #: hypothetical: a shell that lost its window list mid-restart did.
        self.garble = {}
        #: ListMonitors' rows -- one 1920x1080 head at scale 1, as the QEMU
        #: rig reports on all three GNOME goldens; a test that wants two
        #: heads or a fractional scale assigns to this
        self.monitors = [{"index": 0, "x": 0, "y": 0, "width": 1920,
                          "height": 1080, "scale": 1, "primary": True,
                          "connector": "Virtual-1"}]
        #: is gnome-shell's "Keep these display settings?" dialog on screen?
        #: True between a persistent ApplyMonitorsConfig and its answer.
        self.display_change_pending = False
        #: every verdict that reached the bridge, dialog or no dialog
        self.display_change_verdicts = []
        self._show_desktop_wins = []   # ShowDesktop(true)'s restore set
        self.shell_version = shell_version
        if own_shell:
            assert self.bus.request_name(SHELL_NAME) == 1
            assert self.bus.request_name("org.gnome.ScreenSaver") == 1
        if own_bridge:
            assert self.bus.request_name(BUS_NAME) == 1
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        if self.bus.sock is None:
            return
        unique = self.bus.unique_name
        try:
            self.bus.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(3)
        self.bus.close()
        # Closing the socket is not the same event as the bus letting go of
        # org.gnome.Shell and org.w11.Bridge: that happens in the
        # bus's own thread when this connection's recv() returns nothing.
        # Return before it and the next bridge's RequestName is answered 3,
        # not 1 -- which is what made this file fail about one run in ten
        # under load. Wait for the drop instead of hoping to win the race.
        if not self.mock.wait_dropped(unique):
            raise AssertionError(
                "MockBus still holds connection %s five seconds after it "
                "was closed" % unique)

    # -- state helpers

    def find(self, wid):
        for d in self.windows:
            if d["id"] == wid:
                return d
        raise DBusError(IFACE + ".NotFound", "window %d not found" % wid)

    def _focus(self, wid):
        for d in self.windows:
            d["focused"] = d["id"] == wid

    def _refresh_active(self):
        for d in self.windows:
            d["on_active_workspace"] = (d["on_all_workspaces"]
                                        or d["workspace"] == self.active_ws)

    # -- serving

    def _serve(self):
        try:
            for m in self.bus.messages(None):
                if m.type != dbus_mini.METHOD_CALL:
                    continue
                try:
                    sig, out = self._dispatch(m)
                except DBusError as e:
                    self.bus.error_reply(m, e.name, e.message)
                    continue
                if sig is None:
                    continue  # async reply already handled
                self.bus.reply(m, sig, out)
        except Exception:  # noqa: BLE001 -- socket shut down by close()
            pass

    def _dispatch(self, m):
        a = m.args()
        if m.interface == dbus_mini.PROPS_IFACE and m.member == "Get":
            self.prop_gets.append(a)
            if a == (IFACE, "Version"):
                return "v", (Variant("u", self.version),)
            if a == (SHELL_NAME, "Mode"):
                return "v", (Variant("s", self.shell_mode),)
            if a == (SHELL_NAME, "ShellVersion"):
                if self.shell_version is None:
                    raise DBusError(dbus_mini.ERR + "InvalidArgs",
                                    "no such property")
                return "v", (Variant("s", self.shell_version),)
            raise DBusError(dbus_mini.ERR + "InvalidArgs", "no such property")
        if m.interface == "org.gnome.ScreenSaver" and m.member == "GetActive":
            self.calls.append(("GetActive", a))
            return "b", (bool(self.screensaver_active),)
        if m.interface == SHELL_NAME + ".Extensions" and m.member == "GetExtensionInfo":
            self.calls.append(("GetExtensionInfo", a))
            info = dict(self.ext_info or {}) if a[0] == EXT_UUID else {}
            # `shell-version` is an array of strings on the wire (gnome-shell
            # hands the extension's metadata straight through), not the
            # string repr of a Python list: measured with gdbus on
            # stonking-gnome, `{'shell-version': <['45', ..., '50']>}`.
            return "a{sv}", ({k: _ext_info_variant(v) for k, v in info.items()},)
        if m.interface == SHELL_NAME and m.member == "Eval":
            self.calls.append(("Eval", a))
            if not self.eval_unsafe:
                return "bs", (False, "")
            # unsafe mode: pretend loadExtension ran and the bridge came up
            self.bus.request_name(BUS_NAME)
            return "bs", (True, '"ok"')
        if m.interface != IFACE or m.path != OBJECT_PATH:
            raise DBusError(dbus_mini.ERR + "UnknownMethod",
                            "no %s on %s" % (m.member, m.path))
        self.calls.append((m.member, a))
        if m.member in self.stall:
            time.sleep(self.stall_for)
        if m.member in self.garble:
            return "s", (self.garble[m.member],)
        h = getattr(self, "m_" + m.member, None)
        if h is None:
            raise DBusError(dbus_mini.ERR + "UnknownMethod", "no %s" % m.member)
        return h(m, *a)

    # -- bridge methods (return (out signature, out values))

    def m_ListWindows(self, m):
        import json
        return "s", (json.dumps(self.windows),)

    def m_GetWindow(self, m, wid):
        import json
        return "s", (json.dumps(self.find(wid)),)

    def m_Activate(self, m, wid):
        d = self.find(wid)
        d["minimized"] = d["hidden"] = False
        if not d["on_all_workspaces"]:
            self.active_ws = d["workspace"]
        self._refresh_active()
        self.windows.remove(d)
        self.windows.append(d)
        self._focus(wid)
        return "", ()

    def m_Focus(self, m, wid):
        self.find(wid)
        self._focus(wid)
        return "", ()

    def m_Close(self, m, wid):
        self.windows.remove(self.find(wid))
        return "", ()

    m_Kill = m_Close

    def m_Minimize(self, m, wid):
        d = self.find(wid)
        d["minimized"] = d["hidden"] = True
        d["focused"] = False
        return "", ()

    def m_Unminimize(self, m, wid):
        d = self.find(wid)
        d["minimized"] = d["hidden"] = False
        return "", ()

    def m_Raise(self, m, wid):
        d = self.find(wid)
        self.windows.remove(d)
        self.windows.append(d)
        return "", ()

    def m_Lower(self, m, wid):
        d = self.find(wid)
        self.windows.remove(d)
        self.windows.insert(0, d)
        return "", ()

    def m_Move(self, m, wid, x, y):
        d = self.find(wid)
        d["x"], d["y"] = x, y
        return "", ()

    def m_Resize(self, m, wid, w, h):
        d = self.find(wid)
        d["width"], d["height"] = w, h
        return "", ()

    def m_MoveResize(self, m, wid, x, y, w, h):
        d = self.find(wid)
        d.update(x=x, y=y, width=w, height=h)
        return "", ()

    def settle(self, wid=None):
        """The clients answer the configures Mutter sent them: from here on
        it reads the rectangles it asked for. Live this takes one round trip
        of the Wayland connection, which two D-Bus calls in one process
        never leave room for -- _maximize() is what that costs."""
        self.stale = {} if wid is None else \
            {k: v for k, v in self.stale.items() if k != wid}

    def _maximize(self, d, state, want):
        """Mutter's own maximize bookkeeping, measured on GNOME 46 and 50.

        meta_window_set_[un]maximize_flags() builds its target from the
        frame rect it currently believes and replaces only the axes it is
        changing -- from the work area when maximizing, from the saved
        rectangle when unmaximizing. `self.stale` is the rect it still
        believes because the client has not answered the last configure
        yet, so a second single-axis call in the same breath drags the
        maximized half of the geometry into its target. Once neither flag
        is left set, the result becomes the saved rectangle (maybe_save_rect
        in meta-window-wayland.c) -- which is how the damage sticks."""
        horz, vert = MAX_AXES[state]
        h, v = d["maximized_h"], d["maximized_v"]
        if not (horz and h != want) and not (vert and v != want):
            return                       # neither axis would change
        now = (d["x"], d["y"], d["width"], d["height"])
        if want and not h and not v:
            self.saved[d["id"]] = now    # saved on the way up
        x, y, w, ht = self.stale.get(d["id"], now)
        ax, ay, aw, ah = WORK_AREA if want else self.saved.get(d["id"], now)
        if horz:
            x, w, d["maximized_h"] = ax, aw, want
        if vert:
            y, ht, d["maximized_v"] = ay, ah, want
        self.stale.setdefault(d["id"], now)
        d.update(x=x, y=y, width=w, height=ht)
        d["buffer_rect"] = _rect(x, y, w, ht)
        if not d["maximized_h"] and not d["maximized_v"]:
            self.saved[d["id"]] = (x, y, w, ht)

    def m_SetState(self, m, wid, state, action):
        d = self.find(wid)
        if action not in ("add", "remove", "toggle"):
            raise DBusError(IFACE + ".InvalidArgs",
                            "action must be add|remove|toggle, got %s" % action)
        if state in MAX_AXES:
            # a toggle of the pair goes the way the horizontal flag says,
            # which is Mutter's rule for two atoms of one _NET_WM_STATE
            # message (bridge v3; v1/v2 asked for "both are already set")
            cur = d["maximized_v"] if state == "MAXIMIZED_VERT" \
                else d["maximized_h"]
            self._maximize(d, state,
                           {"add": True, "remove": False}.get(action, not cur))
            return "b", (True,)
        key = {"FULLSCREEN": "fullscreen", "HIDDEN": "minimized",
               "ABOVE": "above", "STICKY": "on_all_workspaces",
               "DEMANDS_ATTENTION": "urgent"}.get(state)
        if key is None:
            return "b", (False,)
        want = {"add": True, "remove": False}.get(action, not d[key])
        d[key] = want
        if key == "minimized":
            d["hidden"] = want
        if key == "on_all_workspaces":
            d["workspace"] = -1 if want else self.active_ws
        return "b", (True,)

    def m_MoveToWorkspace(self, m, wid, index):
        d = self.find(wid)
        if index < 0:
            d["on_all_workspaces"], d["workspace"] = True, -1
        elif index >= self.n_ws:
            raise DBusError(IFACE + ".NotFound", "workspace %d not found" % index)
        else:
            d["on_all_workspaces"], d["workspace"] = False, index
        self._refresh_active()
        return "", ()

    def window_under_pointer(self, x, y):
        """Port of the extension's _windowUnderPointer, over the same rows it
        uses (ListWindows', bottom-to-top): the topmost window containing the
        point, DESKTOP/DOCK looked through, hidden windows and other
        workspaces skipped. The last hit wins -- a focused window does NOT win
        over one above it, unlike getmouselocation's tie-break. 0 = the press
        landed on no window."""
        hit = 0
        for d in self.windows:
            if d["window_type"] in ("DESKTOP", "DOCK"):
                continue
            if d["minimized"] or d["hidden"]:
                continue
            if not (d["on_active_workspace"] or d["on_all_workspaces"]):
                continue
            if d["width"] <= 0 or d["height"] <= 0:
                continue
            if (d["x"] <= x < d["x"] + d["width"]
                    and d["y"] <= y < d["y"] + d["height"]):
                hit = d["id"]
        return hit

    def m_SelectWindow(self, m, timeout_ms):
        """Port of the extension's SelectWindow (extension.js): grab, answer
        on the NEXT BUTTON PRESS with the window under the pointer, and let
        Escape / the timeout / a shutdown come back as .Cancelled -- with the
        grab released on every one of those paths, which `grabs` records."""
        cap = min(timeout_ms or self.SELECT_MAX_MS, self.SELECT_MAX_MS)
        # Refused before anything is taken: a second concurrent picker (the
        # first one's handler would eat every event, leaving this one to
        # swallow the user's next click), and a shell that is already modal
        # (the overview, a menu -- pushModal refuses and the extension does
        # not reach past it for a plain stage grab, because the windows'
        # frame rects are not what is on screen then).
        if self.select_event == "busy":
            raise DBusError(IFACE + ".Unsupported",
                            "another window selection is already in progress")
        if self.select_event == "modal":
            raise DBusError(IFACE + ".Unsupported",
                            "the shell would not grant an input grab "
                            "(something else is modal: the overview, a menu, "
                            "a dialog)")
        self.grabs.append("take")
        try:
            time.sleep(self.select_delay)
            reasons = {"escape": "cancelled with Escape",
                       "timeout": "no window picked within %d ms" % cap,
                       "disable": "the bridge extension was disabled"}
            if self.select_event in reasons:
                raise DBusError(IFACE + ".Cancelled", reasons[self.select_event])
            if self.select_at is not None:
                return "t", (self.window_under_pointer(*self.select_at),)
            return "t", (self.select_id,)
        finally:
            self.grabs.append("release")

    def m_GetActiveWorkspace(self, m):
        return "i", (self.active_ws,)

    def m_SetActiveWorkspace(self, m, index):
        if not 0 <= index < self.n_ws:
            raise DBusError(IFACE + ".NotFound", "workspace %d not found" % index)
        self.active_ws = index
        self._refresh_active()
        return "", ()

    def m_GetNWorkspaces(self, m):
        return "i", (self.n_ws,)

    def m_SetNWorkspaces(self, m, count):
        raise DBusError(IFACE + ".Unsupported",
                        "dynamic workspaces are enabled (org.gnome.mutter "
                        "dynamic-workspaces); the workspace count is managed by the shell")

    def m_ListWorkspaces(self, m):
        import json
        out = [{"index": i, "name": "Workspace %d" % (i + 1),
                "active": i == self.active_ws,
                "work_area": _rect(0, 32, 1920, 1048), "viewport": {"x": 0, "y": 0}}
               for i in range(self.n_ws)]
        return "s", (json.dumps(out),)

    def m_ShowDesktop(self, m, show):
        """Faithful port of the extension's _showDesktop (extension.js):
        minimize every normal window on the active workspace and remember
        them, restore that set on `off`. `on` is a LATCH -- a second one
        must not rescan, or the restore set would come back empty (the
        scan skips already-minimized windows) and `off` would restore
        nothing, forever."""
        if not show:
            for wid in self._show_desktop_wins:
                d = next((x for x in self.windows if x["id"] == wid), None)
                if d is not None and d["minimized"]:
                    d["minimized"] = d["hidden"] = False
            self._show_desktop_wins = []
            return "", ()
        if self._show_desktop_wins:
            return "", ()
        done = []
        for d in self.windows:
            if d["minimized"] or d["window_type"] in ("DESKTOP", "DOCK"):
                continue
            if not (d["on_active_workspace"] or d["on_all_workspaces"]):
                continue
            d["minimized"] = d["hidden"] = True
            d["focused"] = False
            done.append(d["id"])
        self._show_desktop_wins = done
        return "", ()

    def m_DisplaySize(self, m):
        return "ii", (1920, 1080)

    def m_GetPointer(self, m):
        return "iiu", self.pointer

    def m_ListMonitors(self, m):
        import json
        return "s", (json.dumps(self.monitors),)

    def m_XInfo(self, m):
        return "ss", self.xinfo

    def m_ConfirmDisplayChange(self, m, keep):
        # What the extension does, measured on GNOME 46.0 and 50.1 (see
        # gnome/README.md "Verified live"): with the dialog up it invokes the
        # Keep or Revert handler and answers true; with nothing pending it
        # still forwards the verdict to Mutter -- harmless there -- and
        # answers false. The dialog is gone either way it was answered.
        found = self.display_change_pending
        self.display_change_pending = False
        self.display_change_verdicts.append(bool(keep))
        return "b", (found,)

    def m_GetVersion(self, m):
        return "u", (self.version,)


@contextlib.contextmanager
def no_bus():
    """Pretend no session bus exists anywhere (the host running the tests
    usually has one under /run/user/<uid>/bus that the scan would find)."""
    orig = session.find_user_bus
    session.find_user_bus = lambda: None
    try:
        yield
    finally:
        session.find_user_bus = orig


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()
        cls.rtdir = tempfile.mkdtemp(prefix="wdotool-gnome-rt-")
        cls._env = env(DBUS_SESSION_BUS_ADDRESS=cls.mock.address,
                       XDG_RUNTIME_DIR=cls.rtdir, WAYLAND_DISPLAY=None,
                       SWAYSOCK=None, I3SOCK=None, WDOTOOL_BACKEND=None,
                       SUDO_UID=None, PKEXEC_UID=None)
        cls._env.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._env.__exit__(None, None, None)
        cls.mock.close()
        shutil.rmtree(cls.rtdir, ignore_errors=True)


class BackendTests(_Base):
    def setUp(self):
        self.bridge = MockBridge(self.mock)
        self.b = GnomeBackend(settle=0.3)

    def window_at(self, x, y):
        """The shared pointer hit-test over this backend's own list() --
        what getmouselocation runs. The backend has no hit-test of its own:
        it fills Window.window_type and backend.hit_test does the rest."""
        return hit_test(self.b.list(), x, y)

    def tearDown(self):
        self.b.bus.close()
        self.bridge.close()

    def calls(self, member):
        return [a for m, a in self.bridge.calls if m == member]

    # -- listing / fields

    def test_list_maps_fields(self):
        wins = self.b.list()
        self.assertEqual([w.id for w in wins], [DESKTOP, EDITOR, CALC, XTERM])
        xterm = wins[-1]
        self.assertEqual(xterm, Window(id=XTERM, title="test@vm: ~", class_="XTerm",
                                       instance="xterm", pid=1201,
                                       x=100, y=80, w=640, h=480,
                                       focused=True, visible=True, desktop=0))
        # B4: native toplevels carry no WM_CLASS instance, so --classname
        # falls back to class_ = the app_id for them.
        self.assertEqual(wins[2].instance, "")
        editor = wins[1]
        self.assertEqual(editor.class_, "org.gnome.TextEditor")  # gtk_app_id fallback
        self.assertFalse(editor.visible)  # minimized
        self.assertTrue(editor.pid == 1300)
        calc = wins[2]
        self.assertFalse(calc.visible)  # parked on workspace 1
        self.assertEqual(calc.desktop, 1)
        self.assertTrue(wins[0].visible)  # the desktop window is a window too
        self.assertEqual(sum(w.focused for w in wins), 1)

    def test_find_uses_getwindow_and_notfound(self):
        w = self.b.find(CALC)
        self.assertEqual((w.id, w.title, w.desktop), (CALC, "Calculator", 1))
        self.assertEqual(self.calls("GetWindow"), [(CALC,)])
        with self.assertRaises(CmdError) as cm:
            self.b.find(999)
        self.assertEqual(str(cm.exception), "window 999 not found")

    def test_is_mapped_is_not_minimized(self):
        self.assertTrue(self.b.is_mapped(XTERM))
        self.assertFalse(self.b.is_mapped(EDITOR))
        self.assertTrue(self.b.is_mapped(CALC))  # other workspace, still mapped

    # -- actions

    def test_activate_switches_workspace_unminimizes_and_settles(self):
        self.b.activate(CALC)
        self.assertEqual(self.calls("Activate"), [(CALC,)])
        self.assertGreaterEqual(len(self.calls("GetWindow")), 1)  # settle poll
        w = self.b.find(CALC)
        self.assertTrue(w.focused and w.visible)
        self.assertEqual(self.b.get_desktop(), 1)
        self.assertEqual(self.b.list()[-1].id, CALC)  # raised on top
        self.b.activate(EDITOR)
        self.assertTrue(self.b.find(EDITOR).visible)

    def test_activate_unknown_window(self):
        with self.assertRaises(CmdError) as cm:
            self.b.activate(42)
        self.assertEqual(str(cm.exception), "window 42 not found")

    def test_focus_does_not_raise(self):
        self.b.focus(DESKTOP)
        self.assertEqual(self.calls("Focus"), [(DESKTOP,)])
        wins = self.b.list()
        self.assertTrue(wins[0].focused)
        self.assertEqual(wins[-1].id, XTERM)  # stacking unchanged

    def test_close_and_kill(self):
        self.b.close(EDITOR)
        self.assertEqual(self.calls("Close"), [(EDITOR,)])
        self.assertNotIn(EDITOR, [w.id for w in self.b.list()])
        self.b.kill(XTERM)  # via the bridge, not os.kill
        self.assertEqual(self.calls("Kill"), [(XTERM,)])
        self.assertNotIn(XTERM, [w.id for w in self.b.list()])

    def test_minimize_map_unmap(self):
        self.b.minimize(XTERM)
        self.assertEqual(self.calls("Minimize"), [(XTERM,)])
        self.assertFalse(self.b.find(XTERM).visible)
        self.b.map(XTERM)
        self.assertEqual(self.calls("Unminimize"), [(XTERM,)])
        self.assertTrue(self.b.find(XTERM).visible)
        self.b.unmap(XTERM)
        self.assertEqual(self.calls("Minimize"), [(XTERM,), (XTERM,)])
        self.assertFalse(self.b.is_mapped(XTERM))

    def test_raise_lower(self):
        self.b.lower(XTERM)
        self.assertEqual(self.b.list()[0].id, XTERM)
        self.b.raise_(XTERM)
        self.assertEqual(self.b.list()[-1].id, XTERM)
        self.assertEqual(self.calls("Lower") + self.calls("Raise"), [(XTERM,), (XTERM,)])

    def test_move_resize(self):
        self.b.move_window(XTERM, 10, 20)
        self.b.resize(XTERM, 700, 500)
        self.assertEqual(self.calls("Move"), [(XTERM, 10, 20)])
        self.assertEqual(self.calls("Resize"), [(XTERM, 700, 500)])
        w = self.b.find(XTERM)
        self.assertEqual((w.x, w.y, w.w, w.h), (10, 20, 700, 500))
        with self.assertRaises(CmdError):
            self.b.move_window(7, 0, 0)

    def test_set_state_bridge_states(self):
        for state, key in (("FULLSCREEN", "fullscreen"), ("MAXIMIZED_HORZ", "maximized_h"),
                           ("MAXIMIZED_VERT", "maximized_v"), ("HIDDEN", "minimized"),
                           ("ABOVE", "above"), ("STICKY", "on_all_workspaces"),
                           ("DEMANDS_ATTENTION", "urgent")):
            self.b.set_state(XTERM, state, 1)
            self.assertTrue(self.bridge.find(XTERM)[key], state)
            self.b.set_state(XTERM, state, 0)
            self.assertFalse(self.bridge.find(XTERM)[key], state)
            self.b.set_state(XTERM, state, 2)
            self.assertTrue(self.bridge.find(XTERM)[key], state)
            self.b.set_state(XTERM, state, 2)
            self.assertFalse(self.bridge.find(XTERM)[key], state)
        self.assertEqual(self.calls("SetState")[:3],
                         [(XTERM, "FULLSCREEN", "add"), (XTERM, "FULLSCREEN", "remove"),
                          (XTERM, "FULLSCREEN", "toggle")])
        self.b.set_state(XTERM, "STICKY", 1)
        self.assertEqual(self.b.find(XTERM).desktop, -1)

    def test_set_state_maximize_pair(self):
        """MAXIMIZED is one Mutter call with both direction bits, and the
        backend that has to use it is the one that names it: the base class
        answers None, so KWin and sway -- which settle each axis before they
        answer -- go on sending the axes separately."""
        self.assertEqual(self.b.maximize_pair_state(), "MAXIMIZED")
        self.assertIsNone(WindowBackend.maximize_pair_state(self.b))
        d = self.bridge.find(XTERM)
        start = (d["x"], d["y"], d["width"], d["height"])
        self.b.set_state(XTERM, "MAXIMIZED", 1)
        self.assertEqual((d["maximized_h"], d["maximized_v"]), (True, True))
        self.assertEqual((d["x"], d["y"], d["width"], d["height"]), WORK_AREA)
        self.b.set_state(XTERM, "MAXIMIZED", 0)
        self.assertEqual((d["maximized_h"], d["maximized_v"]), (False, False))
        self.assertEqual((d["x"], d["y"], d["width"], d["height"]), start)

    def test_state_steps_groups_only_the_maximize_pair(self):
        """backend.state_steps() is where every command that can be handed
        both axes at once has to group them -- `wwmctl -b` today, `wdotool
        windowstate` once it honours more than one option. Nothing else is
        grouped, and a session-less caller gets its names back untouched."""
        pair = ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"]
        self.assertEqual(state_steps(self.b, pair),
                         [("MAXIMIZED", ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"])])
        self.assertEqual(state_steps(self.b, ["maximized_horz", "MAXIMIZED_VERT"]),
                         [("MAXIMIZED", ["MAXIMIZED_HORZ", "MAXIMIZED_VERT"])])
        self.assertEqual(
            state_steps(self.b, ["MAXIMIZED_VERT", "MAXIMIZED_HORZ", "ABOVE"]),
            [("MAXIMIZED", ["MAXIMIZED_VERT", "MAXIMIZED_HORZ"]),
             ("ABOVE", ["ABOVE"])])
        for names in (["MAXIMIZED_VERT"], ["MAXIMIZED_VERT", "FULLSCREEN"],
                      ["MAXIMIZED_VERT", "MAXIMIZED_VERT"],
                      ["MAXIMIZED_VERT", "ABOVE", "MAXIMIZED_HORZ"]):
            self.assertEqual(state_steps(self.b, names),
                             [(n, [n]) for n in names], names)
        # a backend that settles each axis itself, and no backend at all
        self.assertEqual(state_steps(WindowBackend(), pair),
                         [(n, [n]) for n in pair])
        self.assertEqual(state_steps(None, pair), [(n, [n]) for n in pair])

    def test_set_state_cosmetic_warns_and_succeeds(self):
        for state in ("SKIP_TASKBAR", "SKIP_PAGER", "MODAL"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.b.set_state(XTERM, state, 1)
            self.assertIn("windowstate %s" % state, err.getvalue())
            self.assertIn("ignoring", err.getvalue())
        # but the window must exist
        with self.assertRaises(CmdError):
            self.b.set_state(999, "SKIP_TASKBAR", 1)

    def test_set_state_capability_gaps_raise(self):
        for state in ("BELOW", "SHADED", "FOO"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(CmdError) as cm:
                self.b.set_state(XTERM, state, 1)
            self.assertIn("windowstate %s is not supported" % state, str(cm.exception))
            self.assertEqual(err.getvalue(), "")   # an error, not a warning
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(XTERM, "SHADED", 2)
        # review finding 2: shading is observable, so it is a gap, never rc 0
        self.assertIn("does not implement window shading", str(cm.exception))
        with self.assertRaises(CmdError):
            self.b.set_state(XTERM, "FULLSCREEN", 7)

    # -- desktops

    def test_desktops(self):
        self.assertEqual(self.b.num_desktops(), 3)
        self.assertEqual(self.b.get_desktop(), 0)
        self.b.set_desktop(2)
        self.assertEqual(self.b.get_desktop(), 2)
        self.assertFalse(self.b.find(XTERM).visible)  # now on another workspace
        with self.assertRaises(CmdError) as cm:
            self.b.set_desktop(9)
        self.assertEqual(str(cm.exception), "workspace 9 not found")
        self.assertEqual(self.b.window_desktop(CALC), 1)
        self.b.set_window_desktop(CALC, 2)
        self.assertEqual(self.b.window_desktop(CALC), 2)
        self.assertTrue(self.b.find(CALC).visible)
        self.b.set_window_desktop(CALC, -1)
        self.assertEqual(self.b.window_desktop(CALC), -1)
        with self.assertRaises(CmdError):
            self.b.set_window_desktop(CALC, 5)
        self.assertEqual(self.calls("MoveToWorkspace"), [(CALC, 2), (CALC, -1), (CALC, 5)])

    def test_pointer_reports_the_compositors_own(self):
        # B6: Mutter knows where the pointer is whoever moved it, so this is
        # what getmouselocation reports and what seeds the daemon's model.
        self.assertEqual(self.b.pointer(), (640, 400))
        self.bridge.pointer = (2881, 17, 0)
        self.assertEqual(self.b.pointer(), (2881, 17))
        self.assertEqual(self.b.real_pointer(), (2881, 17))  # historical alias
        self.assertEqual(len(self.calls("GetPointer")), 3)

    def test_set_num_desktops_calls_the_bridge(self):
        # B9: the command used to print "managed by the compositor; ignoring"
        # and never call anything. The mock bridge answers Unsupported (its
        # dynamic-workspaces are on), which must be marked as a capability
        # gap rather than a plain failure.
        with self.assertRaises(CmdError) as cm:
            self.b.set_num_desktops(3)
        self.assertEqual(self.calls("SetNWorkspaces"), [(3,)])
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("dynamic workspaces", str(cm.exception))

    def test_invalid_window_id_is_one_line_not_a_marshal_traceback(self):
        # B8: -5 and 2**64 cannot be carried as a D-Bus 't'; ctx rejects them
        # first, and _call is the belt-and-braces guard behind it.
        for bad in (-5, 2 ** 64):
            with self.assertRaises(CmdError) as cm:
                self.b._call("GetWindow", "t", (bad,))
            self.assertIn("invalid argument", str(cm.exception))
            self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_display_size_and_extras(self):
        self.assertEqual(self.b.display_size(), (1920, 1080))
        self.assertEqual(self.b.bridge_version(), MockBridge.VERSION)
        mons = self.b.monitors()
        self.assertEqual((mons[0]["connector"], mons[0]["primary"]), ("Virtual-1", True))

    # -- selectwindow: the window under the pointer at the next press

    def test_select_window_blocks_for_a_button_press(self):
        t0 = time.monotonic()
        self.assertEqual(self.b.select_window(), EDITOR)
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        # timeout_ms 0: the wait is as long as the user takes (the bridge
        # caps it), and the grab is released whatever happens
        self.assertEqual(self.calls("SelectWindow"), [(0,)])
        self.assertEqual(self.bridge.grabs, ["take", "release"])

    def test_select_window_returns_the_window_that_already_has_focus(self):
        # The whole point: xdotool answers with the window under the pointer,
        # so clicking the focused window is a selection like any other. The
        # focus-change picker could never return it -- it waited for a focus
        # event that a click on the focused window does not produce.
        self.assertTrue(self.bridge.find(XTERM)["focused"])
        self.bridge.select_at = (150, 100)          # inside the xterm
        self.assertEqual(self.b.select_window(), XTERM)
        self.assertEqual(self.bridge.grabs, ["take", "release"])
        self.assertEqual([m for m, _ in self.bridge.calls if m == "Focus"], [])

    def test_select_window_picks_the_topmost_not_the_focused(self):
        # The one deliberate difference from getmouselocation's window: a
        # click lands on what is on top of it, focus or no focus. Here the
        # editor is focused and the xterm is stacked above it.
        self.b.map(EDITOR)
        self.b.focus(EDITOR)
        self.assertEqual(self.window_at(350, 250), EDITOR)     # focused wins
        self.bridge.select_at = (350, 250)
        self.assertEqual(self.b.select_window(), XTERM)        # topmost wins

    def test_select_window_hit_test_matches_the_client_side_rule(self):
        # Same answers as the client-side hit-test for the same points: the
        # desktop layer is looked through, other workspaces and hidden
        # windows are not hits.
        for x, y in ((150, 100), (600, 400), (1800, 1000), (0, 0)):
            self.bridge.select_at = (x, y)
            expected = self.window_at(x, y)
            if expected:
                self.assertEqual(self.b.select_window(), expected)
            else:
                with self.assertRaises(CmdError) as cm:
                    self.b.select_window()
                self.assertIn("no window under the pointer", str(cm.exception))

    def test_select_window_cancelled_with_escape(self):
        self.bridge.select_event = "escape"
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertEqual(str(cm.exception), "selectwindow: cancelled with Escape")
        self.assertFalse(getattr(cm.exception, "unsupported", False))
        self.assertEqual(self.bridge.grabs, ["take", "release"])

    def test_select_window_timeout_and_shutdown_are_cancellations(self):
        for event, needle in (("timeout", "no window picked within"),
                              ("disable", "extension was disabled")):
            self.bridge.grabs = []
            self.bridge.select_event = event
            with self.assertRaises(CmdError) as cm:
                self.b.select_window()
            self.assertIn(needle, str(cm.exception))
            self.assertTrue(str(cm.exception).startswith("selectwindow: "))
            self.assertEqual(self.bridge.grabs, ["take", "release"])

    def test_select_window_refused_while_something_else_is_modal(self):
        # F4: with the overview up the extension used to grab anyway and
        # hit-test the click against frame rects that were not on screen,
        # answering with the wrong window or none. It refuses now, and the
        # session keeps its own modal.
        self.bridge.select_event = "modal"
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertIn("would not grant an input grab", str(cm.exception))
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(self.bridge.grabs, [])     # nothing taken, nothing held

    def test_a_second_picker_is_refused_rather_than_left_grabbing(self):
        # Two stage grabs coexist, but only the first captured-event handler
        # sees each event, so the second picker would sit there grabbing and
        # then eat the user's next click.
        self.bridge.select_event = "busy"
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertIn("already in progress", str(cm.exception))
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(self.bridge.grabs, [])

    def test_the_hint_for_an_interactive_selection_says_click(self):
        # wwmctl -a :SELECT: and wxprop's click-select print this before
        # blocking. On GNOME the picker wants a click; only sway's backend,
        # which can do nothing but wait for a focus change, says "focus".
        from hacks.window.backend_sway import SwayBackend
        self.assertEqual(self.b.select_window_hint,
                         "click the target window to select it")
        self.assertEqual(SwayBackend.select_window_hint,
                         "focus the target window to select it")

    def test_select_window_on_an_old_bridge_says_to_reinstall_it(self):
        # A v1 bridge is still installed until the user logs back in; it can
        # only wait for a focus change, so it would hang on the focused
        # window. Say what to do about it instead of hanging.
        self.bridge.version = 1
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertIn("version 1", str(cm.exception))
        self.assertIn("install-bridge.sh", str(cm.exception))
        self.assertEqual(self.calls("SelectWindow"), [])

    # -- pointer hit-test

    def test_window_at_skips_desktop_hidden_and_other_workspaces(self):
        self.assertEqual(self.window_at(150, 100), XTERM)
        # review finding 3: one hit-test, client-side; the bridge has none
        self.assertEqual([m for m, _ in self.bridge.calls], ["ListWindows"])
        self.assertEqual(self.window_at(1800, 1000), 0)       # only the desktop there
        self.assertEqual(self.window_at(600, 400), XTERM)     # calc is on ws 1
        self.assertEqual(self.window_at(-1, -1), 0)
        # nothing focused: topmost hit wins; the minimized editor never does
        self.bridge._focus(0)
        self.bridge.find(XTERM)["x"] = 0
        self.bridge.find(DESKTOP)["focused"] = True  # focused desktop is still looked through
        self.assertEqual(self.window_at(350, 250), XTERM)
        self.b.minimize(XTERM)
        self.assertEqual(self.window_at(350, 250), 0)
        self.b.map(EDITOR)
        self.assertEqual(self.window_at(350, 250), EDITOR)

    def test_getmouselocation_uses_the_shared_hit_test(self):
        from wdotool.input_cmds import _window_under_pointer

        class Ctx:
            def backend(inner):
                return self.b

        self.assertEqual(_window_under_pointer(Ctx(), 150, 100), XTERM)
        self.assertEqual(_window_under_pointer(Ctx(), 1800, 1000), 0)

    # -- richer views

    def test_views(self):
        views = self.b.views()
        self.assertEqual([v.window.id for v in views], [DESKTOP, EDITOR, CALC, XTERM])
        xt = views[-1]
        self.assertIsInstance(xt, View)
        self.assertEqual((xt.xid, xt.instance, xt.cls, xt.app_id, xt.client_type),
                         (XTERM_XID, "xterm", "XTerm", "", "x11"))
        ed = views[1]
        self.assertEqual((ed.xid, ed.instance, ed.cls, ed.app_id, ed.client_type),
                         (0, "org.gnome.TextEditor", "org.gnome.TextEditor",
                          "org.gnome.TextEditor", "wayland"))
        self.assertTrue(ed.minimized and ed.hidden)
        self.assertEqual(ed.desktop_id, "org.gnome.TextEditor.desktop")
        self.assertEqual(views[0].window_type, "DESKTOP")
        self.assertEqual(self.b.list()[0].window_type, "DESKTOP")   # and on Window
        self.assertEqual(views[2].ws_name, "Workspace 2")
        self.assertTrue(all(v.floating for v in views))

    def test_workspaces(self):
        ws = self.b.workspaces()
        self.assertEqual(ws[0], Workspace(index=0, name="Workspace 1", active=True,
                                          work_area=(0, 32, 1920, 1048)))
        self.assertEqual([w.active for w in ws], [True, False, False])

    def test_x_info_from_bridge(self):
        self.assertEqual(self.b.x_info(), (":0", "/run/user/1000/.mutter-Xwaylandauth.AB12CD"))
        self.bridge.xinfo = ("", "")
        with env(DISPLAY=None, XAUTHORITY=None):
            # nothing from the bridge, nothing on this box -> None, no crash
            info = self.b.x_info()
        self.assertTrue(info is None or isinstance(info, tuple))

    def test_events_stream(self):
        gen = self.b.events(timeout=3)
        emitter = Bus(self.mock.address)

        def fire():
            time.sleep(0.3)
            emitter.emit_signal(OBJECT_PATH, IFACE, "WindowEvent", "ts", (XTERM, "focus"))
            emitter.emit_signal(OBJECT_PATH, IFACE, "WorkspaceEvent", "s", ("switch",))
            emitter.emit_signal(OBJECT_PATH, IFACE, "WindowEvent", "ts", (EDITOR, "close"))
        threading.Thread(target=fire, daemon=True).start()
        try:
            self.assertEqual(next(gen), (XTERM, "focus"))
            self.assertEqual(next(gen), (EDITOR, "close"))
        finally:
            gen.close()
            emitter.close()

    def test_monitors_come_back_typed_from_the_bridges_json(self):
        """The other end of the bridge's ListMonitors (tests/test_bridge_js.py
        MonitorsWorkspacesAndDesktop): two heads at geometry_scale 2 and 1.5
        with the second one primary. The scale has to survive as a float --
        wxrandr's callers divide by it, and a 1.5 that arrived as 1 places
        every window on a 125%-scaled head wrong."""
        self.bridge.monitors = [
            {"index": 0, "x": 0, "y": 0, "width": 3840, "height": 2160,
             "scale": 2, "primary": False, "connector": "DP-1"},
            {"index": 1, "x": 1920, "y": 0, "width": 2560, "height": 1440,
             "scale": 1.5, "primary": True, "connector": "HDMI-1"}]
        got = self.b.monitors()
        self.assertEqual(got, self.bridge.monitors)
        self.assertEqual(got[1]["scale"], 1.5)
        self.assertIsInstance(got[1]["scale"], float)
        self.assertEqual([m["connector"] for m in got], ["DP-1", "HDMI-1"])
        # a bridge that answers an object where the list belongs is "no
        # monitors", not a traceback
        self.bridge.garble = {"ListMonitors": "{}"}
        self.assertEqual(self.b.monitors(), [])

    # -- versions

    def test_compositor_version_parses_every_shape_the_shell_reports(self):
        """The only version anyone can ask a GNOME session for is the
        read-only `ShellVersion` property. Measured on the rig: '46.0' on
        noble-gnome, '50.1' on resolute-gnome, '51.beta' on stonking-gnome --
        so a chunk that is not a number ends the tuple rather than raising,
        and a shell that will not answer at all is (), never a crash.

        Read once per backend: wwmctl asks for it on every -e with a gravity
        and this must not be a round trip each time."""
        cases = [("46.0", (46, 0)), ("50.1", (50, 1)), ("51.0", (51, 0)),
                 ("51.beta", (51,)), ("47.alpha", (47,)), ("46.rc", (46,)),
                 ("50.1.1", (50, 1, 1)), ("", ()), (None, ())]
        for raw, want in cases:
            self.bridge.shell_version = raw
            self.bridge.prop_gets = []
            b = GnomeBackend(settle=0.05)
            try:
                self.assertEqual(b.compositor_version(), want, raw)
                self.assertEqual(b.compositor_version(), want, raw)
                self.assertEqual(
                    [a for a in self.bridge.prop_gets if a[1] == "ShellVersion"],
                    [(SHELL_NAME, "ShellVersion")], raw)
            finally:
                b.bus.close()

    # -- a bridge that stalls, garbles or dies

    def test_a_stalled_bridge_times_out_rather_than_hanging(self):
        """gnome-shell wedged in a JS loop still holds its D-Bus connection,
        so nothing but our own timeout ends the call: SetState comes back as
        one line naming the timeout, not a client that never returns.

        Driven twice. Once through _call() with an explicit timeout, which is
        the mechanism; and once through set_state(), which is what `wdotool
        windowstate` and `wwmctl -b` actually reach -- a public method that
        passed a literal of its own would pass the first half and fail the
        second. The default is bound in _call's signature at def time, so a
        module-attribute patch does not reach it; __defaults__ is where it
        lives and where it is both overridden and asserted."""
        self.bridge.stall = {"SetState"}
        self.bridge.stall_for = 1.5
        start = time.monotonic()
        with self.assertRaises(CmdError) as cm:
            self.b._call("SetState", "tss", (XTERM, "FULLSCREEN", "add"),
                         timeout=0.3)
        took = time.monotonic() - start
        self.assertIn("no reply from the bridge within the timeout",
                      str(cm.exception))
        self.assertIn("SetState", str(cm.exception))
        self.assertLess(took, 1.4)

        # the public method, with the bound default swapped for 0.3 so the
        # test does not have to wait CALL_TIMEOUT out
        call = backend_gnome.GnomeBackend._call
        kept = call.__defaults__
        call.__defaults__ = kept[:-1] + (0.3,)
        try:
            start = time.monotonic()
            with self.assertRaises(CmdError) as cm:
                self.b.set_state(XTERM, "FULLSCREEN", 1)    # 1 = add
            took = time.monotonic() - start
        finally:
            call.__defaults__ = kept
        self.assertIn("no reply from the bridge within the timeout",
                      str(cm.exception))
        self.assertIn("SetState", str(cm.exception))
        self.assertLess(took, 1.4)
        # ...and what that default really is, restored: ten seconds, not the
        # 0.3 above and not None
        self.assertEqual(backend_gnome.GnomeBackend._call.__defaults__[-1],
                         backend_gnome.CALL_TIMEOUT)

    def test_a_bridge_that_answers_something_other_than_json(self):
        """Two different failures with two different answers: a reply that is
        not JSON at all is an error (the caller must not think the session is
        empty), while a well-formed JSON document of the wrong SHAPE -- an
        object where the list of windows belongs -- is an empty list, which
        is what every other backend's "no windows" looks like."""
        self.bridge.garble = {"ListWindows": "not json"}
        with self.assertRaises(CmdError) as cm:
            self.b.list()
        self.assertIn("ListWindows returned malformed JSON", str(cm.exception))
        self.bridge.garble = {"ListWindows": "{\"windows\": []}"}
        self.assertEqual(self.b.list(), [])

    def test_losing_the_bus_connection_mid_call_says_so(self):
        """Not the same failure as a bridge that went away: the socket to the
        session bus itself is gone (a dbus-daemon restart, a session teardown
        under us), and the client cannot ask anyone anything after it. The
        bridge stalls so the connection dies with a call in flight."""
        self.bridge.stall = {"ListWindows"}
        self.bridge.stall_for = 3.0
        conn = self.mock.conn_of(self.b.bus.unique_name)

        def cut():
            time.sleep(0.2)
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        t = threading.Thread(target=cut, daemon=True)
        t.start()
        try:
            with self.assertRaises(CmdError) as cm:
                self.b.list()
        finally:
            t.join(5)
        self.assertIn("session bus connection lost", str(cm.exception))

    def test_an_error_name_nobody_knows_still_names_the_method(self):
        """The bridge's four error names are mapped; a fifth from a newer
        extension must still reach the user as one line saying which call
        failed, not as a bare D-Bus name."""
        def weird(m, wid, state, action):
            raise DBusError(IFACE + ".Weird", "the shell is having a moment")

        self.bridge.m_SetState = weird
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(XTERM, "FULLSCREEN", 1)
        self.assertEqual(str(cm.exception),
                         "gnome backend: SetState: the shell is having a moment")
        self.assertFalse(getattr(cm.exception, "unsupported", False))

    # -- errors

    def test_bridge_gone_is_a_clear_error(self):
        self.bridge.close()
        with self.assertRaises(CmdError) as cm:
            self.b.list()
        self.assertIn("bridge vanished", str(cm.exception))
        self.assertIn("install-bridge.sh --check", str(cm.exception))

    def test_unsupported_error_passes_the_bridge_message(self):
        with self.assertRaises(CmdError) as cm:
            self.b._call("SetNWorkspaces", "i", (4,))
        self.assertIn("dynamic workspaces are enabled", str(cm.exception))
        with self.assertRaises(CmdError) as cm:
            self.b._call("SetState", "tss", (XTERM, "FULLSCREEN", "flip"))
        self.assertIn("action must be add|remove|toggle", str(cm.exception))


class ConfirmDisplayChangeTests(_Base):
    """`ConfirmDisplayChange(b keep) -> b` over the wire, against the fake.

    No command calls it: it exists for a client that has just made a
    `--persistent` apply and wants to answer GNOME's 20-second "Keep these
    display settings?" dialog without a human at the screen. What the live
    runs on GNOME 46.0 and 50.1 measured -- true when a dialog was found and
    pressed, false when there was none, the verdict reaching the bridge
    either way, and a second call on an answered dialog being false -- is
    what these pin, so a change to the interface has to change them too."""

    def setUp(self):
        self.bridge = MockBridge(self.mock)
        self.b = GnomeBackend(settle=0.3)

    def tearDown(self):
        self.b.bus.close()
        self.bridge.close()

    def confirm(self, keep):
        (found,) = self.b.bus.call(BUS_NAME, OBJECT_PATH, IFACE,
                                   "ConfirmDisplayChange", "b", (keep,))
        return found

    def test_keep_answers_the_dialog_and_says_it_found_one(self):
        self.bridge.display_change_pending = True
        self.assertIs(self.confirm(True), True)
        self.assertEqual(self.bridge.display_change_verdicts, [True])
        self.assertFalse(self.bridge.display_change_pending)

    def test_revert_answers_it_the_other_way(self):
        self.bridge.display_change_pending = True
        self.assertIs(self.confirm(False), True)
        self.assertEqual(self.bridge.display_change_verdicts, [False])
        self.assertFalse(self.bridge.display_change_pending)

    def test_no_dialog_is_false_and_not_an_error(self):
        # the harmless case: nothing pending, both verdicts, still no error
        self.assertIs(self.confirm(True), False)
        self.assertIs(self.confirm(False), False)
        self.assertEqual(self.bridge.display_change_verdicts, [True, False])

    def test_a_second_call_on_an_answered_dialog_is_false(self):
        self.bridge.display_change_pending = True
        self.assertIs(self.confirm(True), True)
        self.assertIs(self.confirm(True), False)
        self.assertIs(self.confirm(False), False)
        # the verdicts all arrived; only the first one had a dialog to press
        self.assertEqual(self.bridge.display_change_verdicts,
                         [True, True, False])

    def test_the_call_is_one_round_trip_with_the_right_signature(self):
        self.bridge.display_change_pending = True
        self.confirm(True)
        self.assertEqual([a for m, a in self.bridge.calls
                          if m == "ConfirmDisplayChange"], [(True,)])


class SessionReadinessTests(_Base):
    """B5: "there is no backend to talk to" is rc 2, distinct from rc 1 for
    "the session is up and nothing matched"."""

    def test_every_constructor_failure_is_a_no_session_error(self):
        cases = [
            dict(own_shell=True, own_bridge=False),                   # no bridge
            dict(own_shell=True, own_bridge=False, shell_mode="gdm"),  # greeter
            dict(own_shell=True, own_bridge=False,
                 shell_mode="unlock-dialog"),                          # locked
            dict(own_shell=False, own_bridge=False),                   # no shell
        ]
        for kw in cases:
            bridge = MockBridge(self.mock, **kw)
            try:
                with self.assertRaises(NoSessionError) as cm:
                    GnomeBackend()
                self.assertEqual(cm.exception.exit_code, 2, kw)
            finally:
                bridge.close()

    def test_a_missing_window_stays_rc_1(self):
        bridge = MockBridge(self.mock)
        try:
            b = GnomeBackend()
            self.addCleanup(b.bus.close)
            with self.assertRaises(CmdError) as cm:
                b.find(999)
            self.assertNotIsInstance(cm.exception, NoSessionError)
            self.assertEqual(getattr(cm.exception, "exit_code", 1), 1)
        finally:
            bridge.close()


class ConstructorTests(_Base):
    #: what `extension_installed()` answers when a copy is on disk. It is a
    #: path and not a bool since fix 5: the one message that needs the answer
    #: needs the directory too, and a reader who is told which of the four
    #: data directories holds the copy can check it is theirs.
    EXT_DIR = os.path.join("/home/test/.local/share", "gnome-shell",
                           "extensions", EXT_UUID)

    def setUp(self):
        # Every case here but the not-installed ones describes a session
        # where the extension *is* on disk and the diagnosis has to come
        # from the bus. Whether the box running the tests happens to have a
        # copy installed is not part of any of them.
        self.real_installed = backend_gnome.extension_installed
        self.installed(True)

    def installed(self, yes):
        """Answer `extension_installed()` with a directory (True), a
        directory of the caller's (a string), or "" for not on disk."""
        where = yes if isinstance(yes, str) else (self.EXT_DIR if yes else "")
        orig = backend_gnome.extension_installed
        backend_gnome.extension_installed = lambda: where
        self.addCleanup(setattr, backend_gnome, "extension_installed", orig)
        return where

    def test_an_on_disk_bridge_says_log_out_and_names_where_it_found_it(self):
        """fix 5, finding F0.2: a copy on disk that the shell has never heard
        of is waiting for a new session, and the old text sent the reader to
        `gnome/install-bridge.sh` -- a script the .deb does not ship at all
        (measured on the package route, all three GNOME goldens: `apt install
        ./w11.deb` puts the extension in /usr/share and there is no
        install-bridge.sh anywhere on the box). The message now names the
        directory it found and the one action that helps.

        Also review finding 4: this is the common path, and it must not probe
        org.gnome.Shell.Eval -- the bridge sees GetActive and
        GetExtensionInfo, nothing else."""
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            msg = str(cm.exception)
            self.assertIn("installed in %s" % self.EXT_DIR, msg)
            self.assertIn("has not loaded it", msg)
            self.assertIn("log out and back in", msg)
            self.assertNotIn("install-bridge.sh", msg)
            self.assertEqual(msg.count("\n"), 0, msg)
            self.assertEqual([m for m, _ in bridge.calls], ["GetActive", "GetExtensionInfo"])
        finally:
            bridge.close()

    def test_a_bridge_that_is_not_on_disk_still_gets_the_install_hint(self):
        """The other side of fix 5: nothing on disk keeps _HINT word for
        word, because there the script (or the package) IS the answer."""
        self.installed(False)
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            self.assertEqual(str(cm.exception), backend_gnome._HINT)
            self.assertIn("restart the session", str(cm.exception))
        finally:
            bridge.close()

    def test_eval_autoload_is_opt_in(self):
        # unsafe mode on, but nobody asked: still no Eval, still the hint
        self.installed(False)
        bridge = MockBridge(self.mock, own_bridge=False, eval_unsafe=True)
        try:
            for value in (None, "", "0", "no"):
                with env(WDOTOOL_GNOME_AUTOLOAD=value):
                    with self.assertRaises(CmdError) as cm:
                        GnomeBackend()
                self.assertIn("gnome/install-bridge.sh", str(cm.exception))
            self.assertNotIn("Eval", [m for m, _ in bridge.calls])
        finally:
            bridge.close()

    def test_locked_screen_and_disabled_extension_are_diagnosed(self):
        cases = [
            (dict(shell_mode="unlock-dialog"), "screen locked"),
            (dict(shell_mode="unlock-dialog", ext_info={"uuid": EXT_UUID, "state": 2}), "screen locked"),
            (dict(shell_mode="gdm"), "GDM greeter"),
            (dict(ext_info={"uuid": EXT_UUID, "state": 2}), "installed but not enabled"),
            (dict(ext_info={"uuid": EXT_UUID, "state": 3, "error": "boom"}), "failed to load: boom"),
            (dict(ext_info={"uuid": EXT_UUID, "state": 4, "shell-version": "x"}), "out of date"),
            # the unlocked session's mode is "ubuntu" on Ubuntu, "classic" in
            # GNOME Classic: not a lock screen (observed live on 24.04)
            (dict(shell_mode="ubuntu", ext_info={"uuid": EXT_UUID, "state": 2}), "installed but not enabled"),
            (dict(shell_mode="classic", ext_info={"uuid": EXT_UUID, "state": 3, "error": "x"}), "failed to load"),
            # on disk, and the shell has never heard of the uuid: the copy
            # appeared after this session started (fix 5)
            (dict(shell_mode="ubuntu"), "log out and back in"),
            # GNOME 46 keeps Mode at 'ubuntu' behind the lock screen and the
            # extension shows as merely INACTIVE: org.gnome.ScreenSaver tells
            (dict(shell_mode="ubuntu", screensaver_active=True,
                  ext_info={"uuid": EXT_UUID, "state": 2}), "screen is locked"),
            (dict(shell_mode="user", screensaver_active=True), "screen is locked"),
        ]
        for kw, expect in cases:
            bridge = MockBridge(self.mock, own_bridge=False, **kw)
            try:
                with self.assertRaises(CmdError) as cm:
                    GnomeBackend()
                self.assertIn(expect, str(cm.exception), kw)
                if kw.get("shell_mode") not in ("unlock-dialog", "gdm") \
                        and not kw.get("screensaver_active"):
                    self.assertNotIn("locked", str(cm.exception), kw)
            finally:
                bridge.close()

    def test_a_missing_extension_beats_the_lock_message(self):
        """Not installed *and* the screen locked (live, 24.04, on the exact
        path a first-time reader takes: the guide's own `apt install` runs
        past `idle-delay 300`). Both statements are true, and the lock is
        the one the reader can act on least -- unlocking will not make the
        command work. From the bus the two are indistinguishable: behind the
        lock screen the shell disables every extension, ours included."""
        self.installed(False)
        cases = [dict(shell_mode="unlock-dialog"),                    # 50
                 dict(shell_mode="ubuntu", screensaver_active=True),  # 46
                 dict(shell_mode="user", screensaver_active=True),
                 # even reported as merely inactive, which is what a
                 # locked-out extension looks like
                 dict(shell_mode="unlock-dialog",
                      ext_info={"uuid": EXT_UUID, "state": 2})]
        for kw in cases:
            bridge = MockBridge(self.mock, own_bridge=False, **kw)
            try:
                with self.assertRaises(CmdError) as cm:
                    GnomeBackend()
                msg = str(cm.exception)
                self.assertIn("extension is not running in GNOME Shell", msg, kw)
                self.assertIn("gnome/install-bridge.sh", msg, kw)
                self.assertEqual(msg.count("\n"), 0, msg)   # still one line
                # the lock is still said -- it is true, and it is why the
                # re-login is needed anyway -- but it no longer leads
                self.assertIn("locked", msg, kw)
                self.assertLess(msg.index("install-bridge.sh"),
                                msg.index("locked"), msg)
            finally:
                bridge.close()

    def test_an_unlocked_missing_extension_says_nothing_about_locks(self):
        self.installed(False)
        bridge = MockBridge(self.mock, own_bridge=False, shell_mode="ubuntu")
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            self.assertEqual(str(cm.exception), backend_gnome._HINT)
        finally:
            bridge.close()

    def test_the_greeter_is_still_the_greeter(self):
        """gdm's own shell: the per-user directory we would look in is the
        greeter's, so "not installed" says nothing there and the greeter
        diagnosis stays first."""
        self.installed(False)
        bridge = MockBridge(self.mock, own_bridge=False, shell_mode="gdm")
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            self.assertIn("GDM greeter", str(cm.exception))
        finally:
            bridge.close()

    def test_extension_installed_reads_the_same_places_as_the_script(self):
        """`extension_installed()` is the tools' copy of install-bridge.sh's
        `files:` line: <data dir>/gnome-shell/extensions/<uuid>/extension.js,
        per-user first, then the system data dirs."""
        d = tempfile.mkdtemp(prefix="wdotool-ext-")
        self.addCleanup(shutil.rmtree, d, True)
        orig = backend_gnome._extension_dirs
        backend_gnome._extension_dirs = lambda: [d]
        self.addCleanup(setattr, backend_gnome, "_extension_dirs", orig)
        self.assertFalse(self.real_installed())
        p = os.path.join(d, "gnome-shell", "extensions", EXT_UUID)
        os.makedirs(p)
        self.assertFalse(self.real_installed())   # a directory is not a copy
        open(os.path.join(p, "extension.js"), "w").close()
        self.assertTrue(self.real_installed())
        # and the real search path is the script's two destinations
        backend_gnome._extension_dirs = orig
        # the system half follows XDG_DATA_DIRS (a nix sandbox has no /usr/share in
        # it), so the spec's default is set explicitly to pin the derivation
        from unittest import mock
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": "/usr/local/share:/usr/share"}):
            dirs = backend_gnome._extension_dirs()
        self.assertTrue(any(x.endswith("/.local/share") for x in dirs), dirs)
        self.assertIn("/usr/share", dirs)

    def test_out_of_date_names_the_running_shell_and_the_extensions_list(self):
        """fix 4, finding F0.0. Live on stonking-gnome (GNOME Shell 51.beta,
        26.10, the shipped .deb): every tool printed `...marked out of date
        for this GNOME Shell (['45', ..., '50']); reinstall a matching gnome/
        from the repo` -- and the repo's gnome/ carries that same list, so
        the one instruction given could not work. The message has to name the
        shell it is running under (51) and the majors the extension claims,
        because adding the one to the other is the whole fix."""
        listed = ["45", "46", "47", "48", "49", "50"]
        bridge = MockBridge(self.mock, own_bridge=False, shell_version="51.0",
                            ext_info={"uuid": EXT_UUID, "state": 4,
                                      "shell-version": listed})
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            msg = str(cm.exception)
            self.assertIn("out of date for this GNOME Shell 51", msg)
            self.assertIn("(it names 45, 46, 47, 48, 49, 50)", msg)
            self.assertIn('add "51" to shell-version', msg)
            self.assertNotIn("reinstall a matching gnome/ from the repo", msg)
            self.assertEqual(msg.count("\n"), 0, msg)
        finally:
            bridge.close()

    def test_out_of_date_on_a_shell_that_will_not_say_its_version(self):
        """The property is the only version source there is, and a shell that
        refuses it must not turn the message into `GNOME Shell None`."""
        bridge = MockBridge(self.mock, own_bridge=False, shell_version=None,
                            ext_info={"uuid": EXT_UUID, "state": 4,
                                      "shell-version": ["45", "46"]})
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            msg = str(cm.exception)
            self.assertIn("this GNOME Shell (the shell will not say which)", msg)
            self.assertIn("(it names 45, 46)", msg)
            self.assertIn("add this shell's major to shell-version", msg)
            self.assertNotIn("None", msg)
        finally:
            bridge.close()

    def test_extension_dirs_under_sudo_are_the_session_users_not_roots(self):
        """`sudo wdotool` runs as root, and root has no bridge installed: the
        per-user directory that matters is the one belonging to the session
        whose windows are being driven, which is what install-bridge.sh does
        with $SUDO_USER. The runtime dir holding a wayland socket is what
        names that user (uid 1000 here, root's own /run/user/0 beside it)."""
        tmp = tempfile.mkdtemp(prefix="wdotool-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "1000"))
        os.makedirs(os.path.join(tmp, "0"))
        open(os.path.join(tmp, "1000", "wayland-0"), "w").close()

        class _Pw:
            def __init__(self, home):
                self.pw_dir = home

        homes = {0: "/root", 1000: "/home/test"}
        with mock.patch.object(session, "RUN_USER_DIR", tmp), \
                mock.patch.object(backend_gnome.pwd, "getpwuid",
                                  lambda uid: _Pw(homes[uid])), \
                env(SUDO_UID="1000", PKEXEC_UID=None, XDG_RUNTIME_DIR=None,
                    XDG_DATA_HOME=None, XDG_DATA_DIRS="/usr/local/share:/usr/share"):
            dirs = backend_gnome._extension_dirs()
            self.assertEqual(dirs[0], "/home/test/.local/share")
            self.assertNotIn("/root/.local/share", dirs)
            self.assertEqual(dirs, ["/home/test/.local/share",
                                    "/usr/local/share", "/usr/share"])
            # XDG_DATA_HOME is appended once, and never twice when it is the
            # same directory the session user's home already gave us
            with env(XDG_DATA_HOME="/home/test/.local/share"):
                self.assertEqual(backend_gnome._extension_dirs(), dirs)
            with env(XDG_DATA_HOME="/opt/data"):
                self.assertEqual(backend_gnome._extension_dirs()[:2],
                                 ["/home/test/.local/share", "/opt/data"])
            # empty entries in XDG_DATA_DIRS are dropped, the order kept
            with env(XDG_DATA_DIRS="/a::/b:/a"):
                self.assertEqual(backend_gnome._extension_dirs(),
                                 ["/home/test/.local/share", "/a", "/b"])

    def test_an_unreadable_extension_directory_reads_as_not_installed(self):
        """Documents today's answer rather than praising it: a per-user data
        directory this process may not stat is indistinguishable from one
        holding no copy, so the message becomes the install hint. Nothing
        here can tell the two apart without a privileged look."""
        d = tempfile.mkdtemp(prefix="wdotool-ext-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "gnome-shell", "extensions", EXT_UUID)
        os.makedirs(p)
        open(os.path.join(p, "extension.js"), "w").close()
        orig = backend_gnome._extension_dirs
        backend_gnome._extension_dirs = lambda: [d]
        self.addCleanup(setattr, backend_gnome, "_extension_dirs", orig)
        self.assertEqual(self.real_installed(), p)
        os.chmod(os.path.join(d, "gnome-shell", "extensions"), 0)
        self.addCleanup(os.chmod, os.path.join(d, "gnome-shell", "extensions"), 0o700)
        if os.geteuid() == 0:
            self.skipTest("root reads a 0000 directory anyway")
        self.assertEqual(self.real_installed(), "")

    def test_no_shell_at_all(self):
        bridge = MockBridge(self.mock, own_shell=False, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            self.assertIn("org.gnome.Shell is not on the session bus", str(cm.exception))
        finally:
            bridge.close()

    def test_eval_autoload_in_unsafe_mode_when_asked(self):
        bridge = MockBridge(self.mock, own_bridge=False, eval_unsafe=True)
        try:
            with env(WDOTOOL_GNOME_AUTOLOAD="1"):
                b = GnomeBackend()
            self.assertEqual(b.num_desktops(), 3)
            self.assertEqual([m for m, _ in bridge.calls][:2], ["Eval", "GetNWorkspaces"])
            self.assertIn("w11-bridge@w11", bridge.calls[0][1][0])
            b.bus.close()
        finally:
            bridge.close()

    def test_eval_autoload_asked_but_shell_not_unsafe(self):
        self.installed(False)
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with env(WDOTOOL_GNOME_AUTOLOAD="1"):
                with self.assertRaises(CmdError) as cm:
                    GnomeBackend()
            self.assertIn("gnome/install-bridge.sh", str(cm.exception))
            # nothing on disk, so the diagnosis stops before GetExtensionInfo
            self.assertEqual([m for m, _ in bridge.calls], ["Eval", "GetActive"])
        finally:
            bridge.close()

    def test_no_session_bus(self):
        with no_bus():
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
        self.assertIn("no session D-Bus found", str(cm.exception))

    def test_reuses_detects_bus_and_names(self):
        bridge = MockBridge(self.mock)
        try:
            bus = Bus(self.mock.address)
            b = GnomeBackend(bus=bus, names=bus.list_names())
            self.assertIs(b.bus, bus)
            self.assertEqual(b.get_desktop(), 0)
            bus.close()
        finally:
            bridge.close()


class DetectTests(_Base):
    """backend_detect's order: WDOTOOL_BACKEND -> sway socket -> Hyprland socket -> KWin / GNOME / Cinnamon
    (one ListNames, none of the three ever swallowed) -> Wayfire socket -> one registry round trip, which
    picks wlr or COSMIC -> the rc-2 sentence.

    The registry step is a real `wl_fake.registry_server` replaying a recorded global list, not a stub: what
    it decides is a question about bytes on a wl_registry and about nothing else, and the whole reason COSMIC
    needed a step of its own is that cosmic-comp advertises `ext_foreign_toplevel_list_v1` and
    `zcosmic_toplevel_info_v1` and no `zwlr_foreign_toplevel_manager_v1` at all [M recon2/cosmic.md §2, §3].
    The makers are stubbed, because constructing a backend is the next test's subject and not this one's.

    `session.RUN_USER_DIR` is pointed at a private tree: without that, the box the suite runs on decides
    whether there is a Wayland socket to read a registry from."""

    def setUp(self):
        backend_detect.reset()
        self.rundir = tempfile.mkdtemp(prefix="wdotool-detect-run-")
        self.addCleanup(shutil.rmtree, self.rundir, ignore_errors=True)
        p = mock.patch.object(session, "RUN_USER_DIR", self.rundir)
        p.start()
        self.addCleanup(p.stop)
        self.made = []
        self._orig = {}
        self._wlr_calls = []
        self.real_makers = dict(backend_detect._MAKERS)
        self.addCleanup(backend_detect._MAKERS.update, self.real_makers)
        for name in ("_sway", "_hypr", "_wayfire", "_wlr", "_cosmic", "_cinnamon"):
            self._orig[name] = getattr(backend_detect, name)
            stub = self._stub(name)
            setattr(backend_detect, name, stub)
            # `_MAKERS` captured the function objects at import, so the forced
            # path (WDOTOOL_BACKEND) has to be pointed at the stub as well
            for key, fn in list(backend_detect._MAKERS.items()):
                if fn is self._orig[name]:
                    backend_detect._MAKERS[key] = stub
        self.fake = None

    def _stub(self, name):
        """A maker that records the call and refuses, the way a backend whose protocol is not really there
        does. `_sway` keeps its real constructor: the sway-socket test drives it against a real socket."""
        real = self._orig[name]

        def maker():
            self.made.append(name)
            if name == "_wlr":
                self._wlr_calls.append(1)
            if name == "_sway":
                return real()
            raise CmdError("%s: not in this session" % name[1:])
        return maker

    def compositor(self, fixture):
        """Point the session at a `registry_server` replaying tests/fixtures/registries/<fixture>.txt."""
        self.fake = wl_fake.registry_server(fixture)
        self.addCleanup(self.fake.close)
        ctx = env(WAYLAND_DISPLAY=self.fake.path)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return self.fake

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(backend_detect, name, fn)
        backend_detect.reset()

    def test_gnome_with_bridge(self):
        bridge = MockBridge(self.mock)
        try:
            b = backend_detect.detect()
            self.assertEqual(b.name, "gnome")
            self.assertIs(b.bus, backend_detect.session_bus())
            self.assertEqual(b.list()[-1].id, XTERM)
            # one ListNames for the whole detection (the constructor reused it)
            self.assertEqual(backend_detect.session_names().count(BUS_NAME), 1)
        finally:
            bridge.close()
        self.assertEqual(self._wlr_calls, [])

    def test_gnome_without_bridge_is_not_swallowed(self):
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
            self.assertIn("gnome/install-bridge.sh", str(cm.exception))
        finally:
            bridge.close()
        self.assertEqual(self._wlr_calls, [])

    def test_kwin_name_wins_over_gnome(self):
        bridge = MockBridge(self.mock)
        kwin = Bus(self.mock.address)
        try:
            self.assertEqual(kwin.request_name(backend_detect.KWIN_NAME), 1)
            b = backend_detect.detect()
            self.assertEqual(b.name, "kwin")
        finally:
            # the same two-event race MockBridge.close() waits out: the next
            # test asks whether anything at all is on the bus, and org.kde.KWin
            # has to be gone from the table before it does, not merely have had
            # its socket closed
            unique = kwin.unique_name
            kwin.close()
            self.assertTrue(self.mock.wait_dropped(unique))
            bridge.close()

    def test_cinnamon_is_detected_and_kwin_still_beats_it(self):
        """U27. A Cinnamon session owns `org.Cinnamon` and neither of the other two names, so its place in the
        order is decided by what it never collides with rather than by a preference [M recon2/cinnamon.md
        §2.2]; the KWin branch keeps its precedence, because a Plasma box with Cinnamon's packages installed
        is still Plasma."""
        cin = Bus(self.mock.address)
        self.addCleanup(cin.close)
        self.assertEqual(cin.request_name(backend_detect.CINNAMON_NAME), 1)
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_cinnamon"])
        self.made = []
        kwin = Bus(self.mock.address)
        try:
            self.assertEqual(kwin.request_name(backend_detect.KWIN_NAME), 1)
            backend_detect.reset()
            self.assertEqual(backend_detect.detect().name, "kwin")
        finally:
            unique = kwin.unique_name
            kwin.close()
            self.assertTrue(self.mock.wait_dropped(unique))
        self.assertEqual(self.made, [])

    def test_cinnamon_costs_no_second_listnames(self):
        """Three bus names, still one ListNames for the whole detection: the third check is a lookup in a list
        that has already been fetched, not another round trip."""
        cin = Bus(self.mock.address)
        self.addCleanup(cin.close)
        cin.request_name(backend_detect.CINNAMON_NAME)
        before = backend_detect.session_names()
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertIs(backend_detect.session_names(), before)
        self.assertEqual(before.count(backend_detect.CINNAMON_NAME), 1)

    def test_sway_socket_wins_over_dbus(self):
        bridge = MockBridge(self.mock)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = os.path.join(self.rtdir, "sway-ipc.1000.7.sock")
        srv.bind(path)
        srv.listen(1)
        try:
            b = backend_detect.detect()
            self.assertEqual(b.name, "sway")
            b.sock.close()
        finally:
            srv.close()
            os.unlink(path)
            bridge.close()

    def test_the_hypr_socket_is_asked_before_the_bus(self):
        """Hyprland owns no `org.kde.KWin` and no `org.gnome.Shell` [M recon2/hyprland.md §2], so nothing
        below can shadow it -- but it does advertise `zwlr_foreign_toplevel_manager_v1`, so it would be
        swallowed by the registry step, which is honest for reading and wrong about ids, minimize and geometry
        there [M hyprland.md §3]."""
        bridge = MockBridge(self.mock)
        d = os.path.join(self.rundir, str(os.getuid()), "hypr", "sig_1_1")
        os.makedirs(d)
        open(os.path.join(d, ".socket.sock"), "w").close()
        open(os.path.join(d, "hyprland.lock"), "w").close()
        try:
            b = backend_detect.detect()
            self.addCleanup(b.bus.close)
        finally:
            bridge.close()
        # the socket was asked about before the bus was, even though the bus
        # here has an answer of its own
        self.assertEqual(self.made, ["_hypr"])

    def test_a_maker_whose_module_is_not_built_refuses_instead_of_tracebacking(self):
        """Between this batch and batches 5, 6 and 8 the hypr/wayfire/cosmic makers import modules that are
        not in the tree yet. detect() is written around CmdError -- the forced path lets it out and the socket
        arms swallow it -- so a bare ModuleNotFoundError would reach the user as a traceback from every
        wdotool command on a real Hyprland or Wayfire session, where the wlr floor answered before."""
        for name, module in (("_hypr", "hacks.window.backend_hypr"),
                             ("_wayfire", "hacks.window.backend_wayfire"),
                             ("_cosmic", "hacks.window.backend_cosmic")):
            with self.subTest(name):
                if importlib.util.find_spec(module) is not None:
                    self.skipTest("%s is built here; its own batch owns this maker" % module)
                # tearDown puts the stubs back, so the module attribute is simply set here
                setattr(backend_detect, name, self._orig[name])
                backend_detect._MAKERS[name[1:]] = self._orig[name]
                backend_detect.reset()
                self.made = []
                with env(WDOTOOL_BACKEND=name[1:]):
                    with self.assertRaises(CmdError) as cm:
                        backend_detect.detect()
                self.assertNotIsInstance(cm.exception, ModuleNotFoundError)
                self.assertEqual(str(cm.exception),
                                 "%s backend: not built into this install" % name[1:])
                self.assertEqual(self.made, [])

    def test_an_unbuilt_backend_on_its_own_socket_still_reaches_the_sentence(self):
        """The other side of the same guard: with the socket really there and nothing to import, the arm
        refuses like any other failing maker and detect() carries on to its own line -- rc 2 with an
        explanation, not a traceback out of an import."""
        if importlib.util.find_spec("hacks.window.backend_hypr") is not None:
            self.skipTest("wdotool/backend_hypr.py is built here; batch 5 owns this arm")
        setattr(backend_detect, "_hypr", self._orig["_hypr"])
        d = os.path.join(self.rundir, str(os.getuid()), "hypr", "sig_1_1")
        os.makedirs(d)
        open(os.path.join(d, ".socket.sock"), "w").close()
        self.assertTrue(session.find_hypr_socket())
        with self.assertRaises(NoSessionError) as cm:
            backend_detect.detect()
        self.assertIn("no sway/i3, Hyprland or Wayfire IPC socket", str(cm.exception))

    def test_the_wayfire_socket_is_asked_after_the_bus_and_before_the_registry(self):
        """Below the bus names because a Plasma box with wayfire installed must not be misdetected, and above
        the registry because Wayfire does advertise the wlr manager and the floor is wrong there about ids,
        geometry, desktops and WM_CLASS [M recon2/wayfire.md §1.1, §2.3, §2.4]."""
        self.compositor("hyprland")          # a registry with the wlr manager in it
        open(os.path.join(self.rtdir, "wayfire-wayland-1-.socket"), "w").close()
        self.addCleanup(os.unlink, os.path.join(self.rtdir, "wayfire-wayland-1-.socket"))
        bridge = MockBridge(self.mock)
        try:
            b = backend_detect.detect()
            self.addCleanup(b.bus.close)
            self.assertEqual(b.name, "gnome")     # the bus name still wins
            self.assertEqual(self.made, [])
        finally:
            bridge.close()
        backend_detect.reset()
        self.made = []
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_wayfire", "_wlr"])

    def test_the_wayfire_api_gate_stops_detection_instead_of_falling_through(self):
        """Plan A 1.2: "a socket that exists with no `ipc-rules` gives that line rather than falling through
        to wlr".  Wayfire DOES advertise `zwlr_foreign_toplevel_manager_v1`, so the fall-through worked --
        and left a user whose wayfire.ini says `plugins = ipc` on the capability floor, never told that two
        more words buy the window half [requests-batch-1.md, from batch 6].  The gate refusal carries
        `.api_gate`; every other failure here does not, and the test below this one is the control that
        those are still swallowed."""
        self.compositor("hyprland")          # a registry with the wlr manager in it
        sock = os.path.join(self.rtdir, "wayfire-wayland-1-.socket")
        open(sock, "w").close()
        self.addCleanup(os.unlink, sock)

        def gated():
            self.made.append("_wayfire")
            err = CmdError("wayfire backend: this Wayfire's IPC has no window-rules/list-views: the "
                           "window half is Wayfire's own IPC (AGENTS.md route 2) and it is one config "
                           "line away -- `plugins = ipc ipc-rules` in wayfire.ini on Wayfire 0.9 or "
                           "newer -- at the cost of restarting Wayfire to load it")
            err.api_gate = True
            raise err
        backend_detect._wayfire = gated
        with self.assertRaises(CmdError) as cm:
            backend_detect.detect()
        self.assertIn("`plugins = ipc ipc-rules` in wayfire.ini", str(cm.exception))
        self.assertEqual(self.made, ["_wayfire"])       # and NOT ["_wayfire", "_wlr"]

    def test_a_cosmic_registry_with_no_wlr_manager_picks_cosmic(self):
        """U15. Every window command answered the rc-2 sentence on COSMIC for one reason: there is no
        `zwlr_foreign_toplevel_manager_v1` in cosmic-comp's 53 globals, and the detector had nothing else to
        look for [M recon2/cosmic.md §3]."""
        self.compositor("cosmic")
        self.assertNotIn(backend_detect.WLR_TOPLEVEL,
                         [i for i, _v in wl_fake.registry_fixture("cosmic")])
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_cosmic"])
        self.assertEqual(self._wlr_calls, [])

    def test_a_registry_with_both_families_keeps_wlr(self):
        """Hyprland, labwc and river all advertise both; wlr is the older and better-tested path and stays the
        answer, so COSMIC's step can never take a compositor away from it."""
        for fixture in ("hyprland", "labwc", "river"):
            with self.subTest(fixture):
                backend_detect.reset()
                self.made = []
                ifaces = {i for i, _v in wl_fake.registry_fixture(fixture)}
                self.assertIn(backend_detect.WLR_TOPLEVEL, ifaces)
                self.assertIn(backend_detect.EXT_TOPLEVEL, ifaces)
                fake = wl_fake.registry_server(fixture)
                try:
                    with env(WAYLAND_DISPLAY=fake.path):
                        with self.assertRaises(CmdError):
                            backend_detect.detect()
                finally:
                    fake.close()
                self.assertEqual(self.made, ["_wlr"])

    def test_a_registry_with_neither_family_reaches_the_sentence(self):
        """Muffin advertises 23 globals and neither foreign-toplevel protocol, which is why the Cinnamon bus
        name may never be swallowed by this step [M recon2/cinnamon.md §2.1]."""
        self.compositor("cinnamon")
        with self.assertRaises(NoSessionError) as cm:
            backend_detect.detect()
        self.assertEqual(self.made, [])
        self.assertIn("neither wlr-foreign-toplevel nor the COSMIC toplevel protocols",
                      str(cm.exception))

    def test_forcing_wlr_on_cosmic_still_gives_the_wlr_refusal(self):
        """`WDOTOOL_BACKEND` is a forcing and not a hint: it skips the whole order, so the answer comes from
        the backend the user named and says what that backend is missing."""
        self.compositor("cosmic")
        for name, want in (("wlr", "_wlr"), ("cosmic", "_cosmic"), ("hypr", "_hypr")):
            with self.subTest(name):
                self.made = []
                with env(WDOTOOL_BACKEND=name):
                    with self.assertRaises(CmdError):
                        backend_detect.detect()
                self.assertEqual(self.made, [want])

    def test_forcing_wlr_on_cosmic_gives_the_wlr_constructors_own_sentence(self):
        """The other half of U15, with the stub taken back off `_wlr`: the routing is proved above, this is
        the sentence the user actually reads. It comes from WlrBackend's constructor talking to the same
        recorded cosmic-comp registry -- which has no wlr manager to bind [M recon2/cosmic.md §2, §3] -- and
        not from the detector, which by then has stopped deciding anything."""
        backend_detect._MAKERS["wlr"] = self._orig["_wlr"]
        self.compositor("cosmic")
        with env(WDOTOOL_BACKEND="wlr"):
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
        self.assertEqual(str(cm.exception),
                         "wlr backend: compositor does not offer "
                         "zwlr_foreign_toplevel_management_unstable_v1; not yet here, and the route is "
                         "that protocol where the compositor grows it (AGENTS.md route 1), else a "
                         "backend over the compositor's own IPC (route 2), which is what the sway, hypr "
                         "and cinnamon backends already are")
        self.assertEqual(self.made, [])          # the stub was never reached

    def test_a_registry_that_chose_wlr_does_not_get_the_neither_family_sentence(self):
        """When the registry named the family, the backend's own refusal is the answer. Swallowing it and
        falling through would print `the compositor offers neither wlr-foreign-toplevel nor the COSMIC
        toplevel protocols` about a compositor that had just advertised one of them."""
        for fixture, maker in (("labwc", "_wlr"), ("cosmic", "_cosmic")):
            with self.subTest(fixture):
                backend_detect.reset()
                self.made = []
                fake = wl_fake.registry_server(fixture)
                try:
                    with env(WAYLAND_DISPLAY=fake.path, WDOTOOL_BACKEND=None):
                        with self.assertRaises(CmdError) as cm:
                            backend_detect.detect()
                finally:
                    fake.close()
                self.assertEqual(self.made, [maker])
                self.assertNotIn("neither wlr-foreign-toplevel", str(cm.exception))
                self.assertNotIsInstance(cm.exception, NoSessionError)
                self.assertEqual(str(cm.exception), "%s: not in this session" % maker[1:])

    def test_env_override(self):
        bridge = MockBridge(self.mock)
        try:
            with env(WDOTOOL_BACKEND="gnome"):
                self.assertEqual(backend_detect.detect().name, "gnome")
            with env(WDOTOOL_BACKEND="bogus"):
                with self.assertRaises(CmdError) as cm:
                    backend_detect.detect()
            self.assertIn("WDOTOOL_BACKEND=bogus", str(cm.exception))
            self.assertIn("is not one of: sway, hypr, wayfire, wlr, cosmic, kwin, gnome, "
                          "cinnamon", str(cm.exception))
        finally:
            bridge.close()

    def test_i3_is_accepted_as_a_spelling_of_sway(self):
        """It buys nothing but the spelling -- the dialect is read off GET_VERSION and not off the variable --
        so it is in the table and not in the refusal's list of eight."""
        self.assertIs(self.real_makers["i3"], self.real_makers["sway"])
        with env(WDOTOOL_BACKEND="i3"):
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
        self.assertNotIn("WDOTOOL_BACKEND=i3 is not one of", str(cm.exception))

    def test_nothing_on_the_bus_falls_to_the_registry_then_errors(self):
        with self.assertRaises(CmdError) as cm:
            backend_detect.detect()
        self.assertEqual(self._wlr_calls, [])
        self.assertIn("no KWin, GNOME Shell or Cinnamon on the session D-Bus", str(cm.exception))
        self.assertIn("no sway/i3, Hyprland or Wayfire IPC socket", str(cm.exception))

    def test_no_bus_reachable(self):
        with no_bus():
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
        self.assertIn("no session D-Bus reachable", str(cm.exception))
        self.assertIsNone(backend_detect.session_names())
        self.assertNotIn(SHELL_NAME, backend_detect.session_names() or [])

    def test_the_registry_is_read_once_per_process(self):
        """It is one round trip, cached like the ListNames beside it: the detector used to pay it inside
        `_wlr()` and throw the answer away, and every backend below would have paid it again."""
        fake = self.compositor("cosmic")
        with self.assertRaises(CmdError):
            backend_detect.detect()
        first = backend_detect.session_registry()
        self.assertEqual(first["zcosmic_toplevel_info_v1"], 3)
        self.assertIs(backend_detect.session_registry(), first)
        self.assertEqual(fake.connections, 1)
        backend_detect.reset()
        self.assertIsNot(backend_detect.session_registry(), first)
        self.assertEqual(fake.connections, 2)

    def test_no_wayland_socket_at_all_is_not_an_error(self):
        self.assertIsNone(backend_detect.session_registry())


class ShippedFilesTests(unittest.TestCase):
    """Regressions on the files gnome/ ships: the udev rule grants nothing
    beyond the seat user's ACL, the installer restores the node, and the
    extension's embedded interface XML matches the .xml file (no WindowAt)."""

    GNOME = os.path.join(ROOT, "gnome")
    EXT = os.path.join(GNOME, "w11-bridge@w11")

    def test_udev_rule_is_uaccess_only(self):
        # review finding 1: MODE/GROUP would hand every `input` member a
        # standing injection channel; uaccess alone is what wdotool needs
        with open(os.path.join(self.GNOME, "60-w11-uinput.rules")) as f:
            rules = [ln.strip() for ln in f
                     if ln.strip() and not ln.startswith("#")]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertIn('KERNEL=="uinput"', rule)
        self.assertIn('TAG+="uaccess"', rule)
        self.assertIn('OPTIONS+="static_node=uinput"', rule)
        self.assertNotIn("MODE=", rule)
        self.assertNotIn("GROUP=", rule)
        self.assertNotIn("OWNER=", rule)

    def test_installer_parses_and_restores_the_node(self):
        import subprocess
        path = os.path.join(self.GNOME, "install-bridge.sh")
        self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0)
        with open(path) as f:
            src = f.read()
        body = src[src.index("restore_uinput_node() {"):]
        body = body[:body.index("\n}\n")]
        for needle in ("setfacl -b /dev/uinput", "chown root:root /dev/uinput",
                       "chmod 0600 /dev/uinput"):
            self.assertIn(needle, body)
        uninstall = src[src.index('if [ "$MODE" = uninstall ]; then'):]
        uninstall = uninstall[:uninstall.index("return 0")]
        # files first, then udev forgets the node (sticky uaccess tag), then
        # the node's ACL/permissions -- and no trigger that could re-apply it
        self.assertLess(uninstall.index('rm -f "$UDEV_DEST"'), uninstall.index("forget_uinput_tags"))
        self.assertLess(uninstall.index("forget_uinput_tags"), uninstall.index("restore_uinput_node"))
        self.assertNotIn("udevadm trigger", uninstall)
        forget = src[src.index("forget_uinput_tags() {"):]
        forget = forget[:forget.index("\n}\n")]
        for needle in ('"/run/udev/data/c$maj:$min"', '/run/udev/tags/*/"c$maj:$min"',
                       "/run/udev/static_node-tags/uaccess/uinput", "udevadm info -q property"):
            self.assertIn(needle, forget)

    def _extension_js(self):
        with open(os.path.join(self.EXT, "extension.js")) as f:
            return f.read()

    def _select_window_source(self):
        """The SelectWindow region of extension.js (the picker and its
        teardown), so a test can say what is and is not in it."""
        js = self._extension_js()
        start = js.index("    // -- selectwindow ---")
        return js[start:js.index("    // -- window bookkeeping ---", start)]

    def test_select_window_no_longer_waits_for_a_focus_change(self):
        # The defect: waiting on the compositor's focus signal meant clicking
        # the window that already had focus never returned. Nothing in the
        # picker may look at focus again.
        block = self._select_window_source()
        for gone in ("notify::focus-window", "focus_window", "focus-window"):
            self.assertNotIn(gone, block)
        # ...and what replaced it: a grab, resolved by a button press, with
        # Escape and a cap as the ways out
        for needed in ("takeGrab(", "Clutter.EventType", "BUTTON_PRESS",
                       "Clutter.KEY_Escape", "SELECT_MAX_MS",
                       "_windowUnderPointer", "ERR_CANCELLED"):
            self.assertIn(needed, block)
        # the picker may only ever name a window ListWindows reports: Mutter's
        # raw list carries surfaces no other command would resolve
        hit = block[block.index("    _windowUnderPointer(x, y) {"):]
        self.assertIn("for (const d of this._listWindows())", hit)
        self.assertNotIn("_allWindows()", hit)

    def test_the_grab_is_feature_detected_and_always_released(self):
        js = self._extension_js()
        grab = js[js.index("function takeGrab("):js.index("\n}\n", js.index("function takeGrab("))]
        # both spellings, neither assumed to exist (46 vs 50)
        for needed in ("isFn(Main, 'pushModal')", "Main.popModal(grab)",
                       "isFn(global.stage, 'grab')", "grab.dismiss()"):
            self.assertIn(needed, grab)
        # a grab that came back dead is dismissed rather than returned
        self.assertIn("grabIsLive(grab)", grab)
        block = self._select_window_source()
        # the timeout is armed before the grab is taken, so a setup that
        # throws is already bounded
        self.assertLess(block.index("GLib.timeout_add"), block.index("_beginSelect(sel)"))
        # every acquisition has its release in the one teardown
        end = block[block.index("    _endSelect(sel, id, errName, errMsg) {"):]
        for needed in ("obj.disconnect(hid)", "GLib.source_remove(sel.timerId)",
                       "Gio.bus_unwatch_name(sel.watchId)", "sel.grab.release()",
                       "if (sel.done)"):
            self.assertIn(needed, end)
        # a caller that goes away (Ctrl-C) releases it too
        self.assertIn("bus_watch_name_on_connection", block)
        # ...as does disabling the extension
        self.assertIn("safe(() => finish(0, ERR_CANCELLED", js)

    def test_the_picker_refuses_rather_than_grabbing_over_a_modal(self):
        # A picker on top of the overview swallows the click and hit-tests it
        # against frame rects that are not on screen (measured: a window
        # nowhere near the pointer on GNOME 50, none at all on 46). Asking
        # pushModal does not settle it -- it nests on some releases -- so the
        # shell's own modal state is read, every signal feature-detected.
        js = self._extension_js()
        modal = js[js.index("function shellIsModal("):
                   js.index("\n}\n", js.index("function shellIsModal("))]
        for needed in ("Shell.ActionMode.NORMAL", "Main.actionMode",
                       "Main.modalCount", "Main.overview.visible"):
            self.assertIn(needed, modal)
        self.assertEqual(modal.count("safe(() =>"), 4)   # none of them assumed
        # ...and it is checked before anything is taken
        block = self._select_window_source()
        guard = block[:block.index("const asked")]
        self.assertIn("shellIsModal()", guard)
        self.assertIn("ERR_UNSUPPORTED", guard)
        # a pushModal that refuses is still final: no plain-stage-grab retry
        grab = js[js.index("function takeGrab("):js.index("\n}\n", js.index("function takeGrab("))]
        between = grab[grab.index("Main.pushModal("):
                       grab.index("isFn(global.stage, 'grab')")]
        self.assertIn("return null;", between)

    def test_only_one_picker_at_a_time(self):
        block = self._select_window_source()
        guard = block[:block.index("const asked")]
        self.assertIn("this._selects?.size", guard)
        self.assertIn("ERR_UNSUPPORTED", guard)

    def test_the_press_is_not_answered_until_its_release(self):
        # An application must not receive a button-release whose press it
        # never saw. The grab is kept for the matching release, bounded by
        # SELECT_RELEASE_MS, and exactly one timer is armed at a time so the
        # single source_remove in _endSelect stays sufficient.
        js = self._extension_js()
        self.assertIn("const SELECT_RELEASE_MS = ", js)
        block = self._select_window_source()
        for needed in ("BUTTON_RELEASE", "TOUCH_END", "sel.pick",
                       "SELECT_RELEASE_MS", "_pickAt(sel, event)"):
            self.assertIn(needed, block)
        pick = block[block.index("    _pickAt(sel, event) {"):]
        # the old deadline is removed before the new one is armed, and a
        # source that cannot be created answers instead of leaving the grab
        # unbounded
        self.assertLess(pick.index("GLib.source_remove(sel.timerId)"),
                        pick.index("GLib.timeout_add("))
        self.assertIn("sel.finish(sel.pick, null, null);",
                      pick[pick.index("if (id)"):])

    def test_the_picker_swallows_every_discrete_input_event(self):
        # A keystroke, a scroll or a touch aimed at the picker must not land
        # in the window under it; motion is deliberately let through.
        block = self._select_window_source()
        handler = block[block.index("    _onSelectEvent(sel, event) {"):
                        block.index("    _pickAt(sel, event) {")]
        for needed in ("T.KEY_RELEASE", "T.SCROLL", "T.TOUCH_UPDATE",
                       "T.TOUCH_CANCEL", "T.PAD_BUTTON_PRESS",
                       "T.PAD_BUTTON_RELEASE"):
            self.assertIn(needed, handler)
        self.assertNotIn("T.MOTION", handler)

    def test_bridge_version_is_bumped_everywhere(self):
        import json as _json
        js = self._extension_js()
        self.assertIn("const VERSION = %d;" % MockBridge.VERSION, js)
        with open(os.path.join(self.EXT, "metadata.json")) as f:
            meta = _json.load(f)
        self.assertEqual(meta["version"], MockBridge.VERSION)
        # The client refuses to hang on a picker older than v2. It is not
        # the current version: a bridge is bumped for any change to what a
        # method does (v3: SetState folds the maximize pair), and only a
        # change a client cannot live without moves a minimum.
        self.assertLessEqual(backend_gnome.GnomeBackend._SELECT_MIN_VERSION,
                             MockBridge.VERSION)
        self.assertIn("const SELECT_MAX_MS = %d;" % MockBridge.SELECT_MAX_MS, js)

    def test_a_selection_may_not_hold_the_grab_back_to_back(self):
        """The per-call cap bounds one call, not the caller: any process on
        the session bus could start the next selection microseconds after the
        last ended and hold the shell's input grab -- no key, no button, no
        scroll reaching any application -- for as long as it liked. Each
        selection is now followed by a quiet period as long as the grab it
        held, so a loop gets at most half the time and an honest picker (a
        click, in a second or two) is never delayed."""
        js = self._extension_js()
        self.assertIn("let selectCooldownUntil = 0;", js)
        block = self._select_window_source()
        # refused while the shell is still owed its quiet period...
        self.assertIn("const quiet = selectCooldownUntil - Date.now();", block)
        self.assertIn("if (quiet > 0) {", block)
        self.assertIn("ERR_UNSUPPORTED", block)
        # ...which is armed by the one teardown, so every way out arms it
        end = block[block.index("    _endSelect(sel, id, errName, errMsg) {"):]
        self.assertIn("selectCooldownUntil = Date.now() +", end)
        self.assertIn("Math.min(Date.now() - (sel.startedAt || Date.now()), "
                      "SELECT_MAX_MS)", end)
        self.assertIn("startedAt: Date.now()", block)
        # not keyed to the sender: a second bus connection would defeat that
        self.assertNotIn("cooldownBySender", js)

    def test_keep_is_refused_when_no_monitor_would_be_left(self):
        """ApplyMonitorsConfig is reachable by any session process, but a
        configuration that leaves nothing visible self-reverts after ~20 s
        because nobody can press Keep. Pressing it for anyone who asks made
        that permanent; the one state the dialog exists to prevent is now the
        one state the bridge will not confirm."""
        js = self._extension_js()
        conf = js[js.index("    _confirmDisplayChange(keep) {"):]
        conf = conf[:conf.index("\n    }\n")]
        self.assertIn("Main.layoutManager.monitors.length, -1", conf)
        self.assertIn("if (keep && monitors === 0) {", conf)
        self.assertIn("ERR_UNSUPPORTED", conf)
        # an unreadable monitor list is not a refusal, and Revert never is
        self.assertIn(", -1)", conf)

    def test_the_dialog_is_looked_for_where_gnome_shell_puts_it(self):
        """Settled live on GNOME 46.0 and 50.1 (gnome/README.md "Verified
        live"): gnome-shell registers DisplayChangeDialog over
        ModalDialog.ModalDialog, whose _init adds it straight to
        Main.layoutManager.modalDialogGroup; GObject registration leaves
        constructor.name as 'DisplayChangeDialog' (and $gtype.name as
        'Gjs_DisplayChangeDialog'), and _onSuccess/_onFailure are the actions
        of the Keep and Revert buttons. The lookup has to keep asking for all
        of that, because each half is what makes it the right object."""
        js = self._extension_js()
        find = js[js.index("function findDisplayChangeDialog() {"):]
        find = find[:find.index("\n}\n")]
        self.assertIn("Main.layoutManager.modalDialogGroup", find)
        self.assertIn("Main.uiGroup", find)          # only the fallback
        self.assertIn("c.constructor.name", find)
        self.assertIn("c.constructor.$gtype.name", find)
        self.assertIn("/DisplayChangeDialog/.test(name)", find)
        self.assertIn("isFn(c, '_onSuccess') && isFn(c, '_onFailure')", find)

    def test_a_missing_dialog_still_forwards_the_verdict_and_says_false(self):
        """The other half of the live measurement: with no dialog on screen
        the verdict goes to Shell.WM.complete_display_change (present in the
        Shell-14 and Shell-18 typelibs, a no-op with nothing pending) and the
        answer is false, so a caller can tell "pressed it" from "there was
        nothing to press"."""
        js = self._extension_js()
        conf = js[js.index("    _confirmDisplayChange(keep) {"):]
        conf = conf[:conf.index("\n    }\n")]
        self.assertIn("dialog._onSuccess();", conf)
        self.assertIn("dialog._onFailure();", conf)
        self.assertIn("return true;", conf)
        # probed, never assumed -- a shell without it must not throw
        self.assertIn("isFn(global.window_manager, 'complete_display_change')",
                      conf)
        self.assertIn("global.window_manager.complete_display_change(keep)",
                      conf)
        self.assertTrue(conf.rstrip().endswith("return false;"))

    def test_the_method_table_the_xml_and_the_mock_bridge_name_the_same_calls(self):
        """Three copies of the bridge's surface -- the METHODS table in
        extension.js, org.w11.Bridge1.xml beside it, and MockBridge's
        m_<Name> methods, which is what every test in this suite drives
        instead of the extension. A method that exists in one and not the
        others is a test that proves nothing about the shipped file, and the
        out signatures have to agree too or the Variant the extension packs
        does not fit the reply the client unpacks.

        SelectWindow is the one method not in METHODS: it answers
        asynchronously (the grab outlives the D-Bus call), so the extension
        defines SelectWindowAsync by hand."""
        import xml.etree.ElementTree as ET

        js = self._extension_js()
        table = dict(re.findall(r"^    (\w+): \['\(([a-z]*)\)'", js, re.M))
        self.assertGreater(len(table), 20, table)
        with open(os.path.join(self.EXT, "org.w11.Bridge1.xml")) as f:
            root = ET.fromstring(f.read())
        iface = root.find("interface")
        xml_out = {}
        for meth in iface.findall("method"):
            xml_out[meth.get("name")] = "".join(
                a.get("type") for a in meth.findall("arg")
                if a.get("direction") == "out")
        mock_names = {m[2:] for m in dir(MockBridge) if m.startswith("m_")}
        self.assertEqual(set(table) | {"SelectWindow"}, set(xml_out))
        self.assertEqual(set(xml_out), mock_names)
        for name, sig in table.items():
            self.assertEqual(sig, xml_out[name], name)
        # SelectWindow's own answer, packed where it is returned
        self.assertEqual(xml_out["SelectWindow"], "t")
        self.assertIn("sel.invocation.return_value(new GLib.Variant('(t)', "
                      "[Number(id) || 0]));", js)

    def test_the_uuid_is_one_string_in_every_file_that_names_it(self):
        """Four copies, and a mismatch is silent in three of them: the shell
        keys the extension by the directory name in metadata.json, the tools
        ask GetExtensionInfo for EXT_UUID, install-bridge.sh copies into
        $UUID and packaging/common/enable-bridge enables $UUID. Any one of
        those drifting leaves the package installing an extension nothing
        turns on or diagnoses.  The path moved with the RPM/PKGBUILD work
        (batch 3, design decision 8): `git mv debian/enable-bridge
        packaging/common/enable-bridge`, same bytes, and debian/rules copies
        it into the .deb from there."""
        with open(os.path.join(self.EXT, "metadata.json")) as f:
            meta = json.load(f)
        self.assertEqual(meta["uuid"], EXT_UUID)
        self.assertEqual(os.path.basename(self.EXT), EXT_UUID)
        for path in (os.path.join(self.GNOME, "install-bridge.sh"),
                     os.path.join(ROOT, "packaging", "common", "enable-bridge")):
            with open(path) as f:
                src = f.read()
            found = re.findall(r"^UUID='([^']+)'", src, re.M)
            self.assertEqual(found, [EXT_UUID], path)

    def _vm_gnome_majors(self):
        """The GNOME Shell majors the rig's flavor table carries, which is
        the list of shells this project claims to run on."""
        with open(os.path.join(ROOT, "vm", "README.md")) as f:
            md = f.read()
        start = md.index("| flavor | release | desktop (as built) |")
        table = md[start:md.index("\n\n", start)]
        return sorted({int(m) for m in re.findall(r"GNOME Shell (\d+)", table)})

    def test_the_bridge_names_every_gnome_major_the_rig_carries(self):
        """The bridge's shell-version list has to name every GNOME major the
        rig carries and every generation the overlap extension was measured
        on: the bridge is public API and feature-detected, so it runs
        wherever the private layout was measured. Findings F0.0 and F6.2.

        Measured on stonking-gnome (Ubuntu 26.10, GNOME Shell 51.beta, the
        shipped .deb): as installed the bridge is OUT OF DATE and every
        window/desktop/input command refuses. With "51" appended to
        shell-version in the installed metadata.json and one reboot, on a
        fresh instance and nothing else touched, the bridge is ACTIVE
        (`[w11-bridge] enabled (bridge v3, gnome-shell 51.beta)`) and
        every operation works, the maximize pair included through the 49+
        set_maximize_flags() path. So the whole gap is one line.

        The overlap extension's own table already claims 51 (it was measured
        on libmutter-51.so.0), and the overlap needs a running shell to be
        worth anything, so its majors must be a subset of the bridge's:
        public API cannot be supported on fewer shells than a private struct
        layout."""
        with open(os.path.join(self.EXT, "metadata.json")) as f:
            listed = [int(v) for v in json.load(f)["shell-version"]]
        from hacks.display import gnome_overlap
        overlap = sorted(g["shell_major"] for g in gnome_overlap.GENERATIONS)
        self.assertEqual([m for m in self._vm_gnome_majors() if m not in listed],
                         [], "the rig runs a GNOME the bridge will not load on")
        self.assertEqual([m for m in overlap if m not in listed], [],
                         "the overlap claims a shell the bridge does not")
        with open(os.path.join(self.GNOME, "README.md")) as f:
            readme = f.read()
        self.assertNotIn("51 is not in", readme)

    def test_embedded_xml_matches_file_and_has_no_hit_test(self):
        with open(os.path.join(self.EXT, "org.w11.Bridge1.xml")) as f:
            xml = f.read().strip()
        with open(os.path.join(self.EXT, "extension.js")) as f:
            js = f.read()
        start = js.index("const IFACE_XML = `") + len("const IFACE_XML = `")
        embedded = js[start:js.index("`;", start)].strip()
        self.assertEqual(embedded, xml)
        self.assertNotIn("WindowAt", xml)
        self.assertNotIn("_windowAt", js)
        self.assertIn('<method name="GetPointer">', xml)
        for member in ("ListWindows", "GetWindow", "SetState", "SelectWindow",
                       "DisplaySize", "XInfo", "GetVersion"):
            self.assertIn('<method name="%s">' % member, xml)


class SessionTests(unittest.TestCase):
    """The additive session.py pieces: runtime-dir anchoring on the Wayland
    socket, PKEXEC_UID, X display / Xauthority discovery."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wdotool-sess-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # runtime_dir_candidates() ordering, and find_user_bus()'s preference for
    # the bus beside the Wayland socket, moved to tests/test_session.py, which
    # builds the /run/user tree by hand. The two versions that were here read
    # the *host's* real /run/user instead: one kept its only real assertion
    # behind an `if have:` that is empty in a container, so it asserted
    # nothing, and the other ended in an assertIn over a tuple computed from
    # that same tree, so it could pass for the wrong reason.

    def test_pkexec_uid(self):
        with env(SUDO_UID=None, PKEXEC_UID="4242"):
            self.assertEqual(session._sudo_uid(), 4242)
        with env(SUDO_UID="7", PKEXEC_UID="4242"):
            self.assertEqual(session._sudo_uid(), 7)
        with env(SUDO_UID="x", PKEXEC_UID=None):
            self.assertIsNone(session._sudo_uid())

    def test_find_xauthority_prefers_env_then_mutter_cookie(self):
        cookie_old = os.path.join(self.tmp, ".mutter-Xwaylandauth.OLD")
        cookie_new = os.path.join(self.tmp, ".mutter-Xwaylandauth.NEW")
        for p, t in ((cookie_old, 1000), (cookie_new, 2000)):
            with open(p, "w"):
                pass
            os.utime(p, (t, t))
        with env(XAUTHORITY=None, XDG_RUNTIME_DIR=self.tmp, SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_xauthority(os.getuid()), cookie_new)
        with env(XAUTHORITY=cookie_old):
            self.assertEqual(session.find_xauthority(), cookie_old)
        with env(XAUTHORITY="/nonexistent/xauth", XDG_RUNTIME_DIR=self.tmp,
                 SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_xauthority(os.getuid()), cookie_new)

    def test_find_xauthority_reads_the_session_leader(self):
        """A display manager may keep the cookie where no search can find it:
        SDDM writes /tmp/xauth_<random>, which is in nobody's runtime
        directory and is not ~/.Xauthority, so on a Plasma X11 session the
        session's own leader is the only thing that knows where it is -- and
        that leader is not gnome-shell.

        The stand-in is a real process whose /proc/<pid>/comm is one of the
        names we look for: `startplasma-x11` is exactly the 15 characters
        comm holds, so a copy under that name is the honest test of the
        length limit as well.

        The copy is /bin/sh and not /bin/sleep because Ubuntu 26.04's
        coreutils is the uutils multicall binary (/usr/bin/sleep is a symlink
        into /usr/lib/cargo/bin/coreutils/) and dispatches on argv[0]: run as
        `startplasma-x11` it recognises no applet and exits at once, so there
        was nothing left in /proc to read by the time the scan ran. The shell
        blocks in its own `read` builtin rather than on a `sleep` child, so
        the process under test is the one whose name we chose and there is no
        grandchild left holding the inherited pipes open.

        The wait below is for the ENVIRONMENT, not just the name: execve sets
        comm in setup_new_exec() and publishes mm->env_start only afterwards,
        so between the two `/proc/<pid>/environ` reads back empty while comm
        already says `startplasma-x11`. Waiting on the name alone lost that
        race roughly one run in three here (measured: 10 of 30 iterations of
        this body), which is what made this test fail in class order and pass
        on its own."""
        import subprocess

        cookie_dir = os.path.join(self.tmp, "elsewhere")
        os.makedirs(cookie_dir, exist_ok=True)
        cookie = os.path.join(cookie_dir, "xauth_sddm")
        with open(cookie, "w"):
            pass
        leader = os.path.join(self.tmp, "startplasma-x11")
        shutil.copy(os.path.realpath("/bin/sh"), leader)
        proc = subprocess.Popen([leader, "-c", "read line"],
                                stdin=subprocess.PIPE,
                                env={"XAUTHORITY": cookie, "DISPLAY": ":7"})
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(500):
            try:
                with open("/proc/%d/comm" % proc.pid) as f:
                    named = f.read().strip() == "startplasma-x11"
                with open("/proc/%d/environ" % proc.pid, "rb") as f:
                    published = b"XAUTHORITY=" in f.read()
                if named and published:
                    break
            except OSError:
                pass
            time.sleep(0.01)
        else:
            self.skipTest("the stand-in process never reached comm=startplasma-x11")
        # XDG_RUNTIME_DIR holds no cookie, so a hit can only come from /proc
        with env(XAUTHORITY=None, DISPLAY=None, XDG_RUNTIME_DIR=self.tmp,
                 SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_xauthority(os.getuid()), cookie)
            self.assertEqual(session.find_x_display(uid=os.getuid()), ":7")
        # ...and never another user's: the scan is uid-qualified
        with env(XAUTHORITY=None, DISPLAY=None, XDG_RUNTIME_DIR=self.tmp,
                 SUDO_UID=None, PKEXEC_UID=None):
            self.assertNotEqual(session.find_xauthority(os.getuid() + 4242),
                                cookie)

    def test_find_x_display_env(self):
        with env(DISPLAY=":424242"):
            # no socket for it -> not trusted; falls through to the scan
            r = session.find_x_display(uid=0x7fffffff)
            self.assertTrue(r is None or r.startswith(":"))
        with env(DISPLAY="host:0"):
            r = session.find_x_display(uid=0x7fffffff)
            self.assertTrue(r is None or r.startswith(":"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
