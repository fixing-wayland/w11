#!/usr/bin/env python3
"""KWin backend tests against a fake KWin served on dbus_mini's in-process
mock bus (tests/test_dbus_mini.py MockBus): the loadScript/run/unloadScript
transport (the negative result, unique plugin names, both Plasma object-path
shapes, the token check, a script that answers nothing), every KwinBackend
method, the Window/View/Workspace mapping for both Plasma payload shapes, the
XWayland id matching, and backend_detect's order. No KWin, no real bus needed.

The fake executes the *operation* the generated script names in its header
line (`/* wdotool-kwin 1 {...} */`) -- there is no JS engine here -- and
answers with a callDBus-shaped method call to the destination the script
carries, sent before run()'s reply, exactly as KWin does."""

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import wl_fake` resolve only with the tests directory
# itself on sys.path: running this file by path puts it there for free,
# `python3 -m unittest tests/<file>.py` does not (tests/test_passthrough.py
# fails over a file that imports one of them without this line).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common import dbus_mini, session
from w11common.dbus_mini import ERR, Bus, DBusError, Message, Variant
from w11common.errors import CmdError
import support
from test_dbus_mini import MockBus
from wdotool import backend, backend_detect, backend_kwin, kwin_js, xid_match
from wdotool.backend import hit_test
from wdotool.backend_kwin import (BUS_NAME, IFACE, KWIN_IFACE,
                                  KWIN_NAME, KWIN_PATH, OBJECT_PATH,
                                  SCRIPTING_IFACE, SCRIPTING_PATH,
                                  SCRIPT_IFACE, VD_IFACE, VD_PATH,
                                  KwinBackend)
from wdotool.ctx import NoSessionError

# See tests/conftest.py: no test process ever hands itself to the real xdotool.
os.environ["W11_PASSTHROUGH"] = "never"

UU = {
    "desktop": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
    "kate": "2c9a1f00-0000-4000-8000-00000000beef",
    "konsole": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "xterm": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
}
WID = {k: backend_kwin._wid(v) for k, v in UU.items()}
XTERM_XID = 0x400005


def _code(js: str) -> str:
    """The script with its /* comments */ stripped: these tests are about
    what the script does, not about what it says about itself."""
    return re.sub(r"/\*.*?\*/", "", js, flags=re.S)


def _clashing_uuid(u: str) -> str:
    """Another uuid with the same first 8 hex digits -- all _wid() reads --
    so the two mint the same window id."""
    return u[:-4] + ("beef" if not u.endswith("beef") else "cafe")


def fixture_windows(six=True):
    """The payload wdotool.kwin_js computes, bottom-to-top: Plasma's desktop
    view, a minimized kate, a konsole parked on desktop 1, and a focused
    XWayland xterm on top. `six`: Plasma 6 (no windowId, no shade) or 5.27."""
    base = {"n": "", "p": 0, "f": False, "m": False, "hi": False, "d": 0,
            "oc": True, "st": False, "fs": False, "mm": 0, "ka": False,
            "kb": False, "sk": False, "sp": False, "at": False, "nb": False,
            "sh": None if six else False, "ty": 0, "ly": 2, "tf": "",
            "df": "", "ro": "", "xid": 0, "o": "Virtual-1"}

    def win(**kw):
        d = dict(base)
        d.update(kw)
        d["u"], d["so"] = UU[d.pop("key")], d.pop("so")
        return d

    rows = [
        win(key="desktop", so=0, t="Desktop", c="plasmashell", n="plasmashell",
            p=900, ty=1, ly=0, sk=True, x=0, y=0, w=1920, h=1080),
        win(key="kate", so=1, t="untitled - Kate", c="org.kde.kate",
            n="kate", p=1300, m=True, hi=True, df="org.kde.kate",
            x=300, y=200, w=800, h=600),
        win(key="konsole", so=2, t="test@kde: ~", c="org.kde.konsole",
            n="konsole", p=1400, d=1, oc=False, df="org.kde.konsole",
            x=500, y=300, w=400, h=500),
        win(key="xterm", so=3, t="test@kde: ~", c="XTerm", n="xterm",
            p=1201, f=True, x=100, y=80, w=640, h=480,
            xid=0 if six else XTERM_XID),
    ]
    # `ix` is the row's place in workspace.windowList(), which kwin_js records
    # before it sorts the payload by stacking order -- here the two orders
    # happen to agree. _match_xids reads it as _NET_CLIENT_LIST's order (see
    # Workspace::propagateWindows), which is the only thing that tells two
    # identical windows of one client apart on Plasma 6.
    for i, row in enumerate(rows):
        row["ix"] = i
    return rows


class KwinScriptingService:
    """org.kde.KWin on a Bus of its own: /Scripting, the per-script object,
    /KWin and /VirtualDesktopManager. `plasma` picks the object-path shape
    (6: /Scripting/Script<id>, 5.27: /<id>) and the payload flavour."""

    def __init__(self, address, plasma=6, own_name=True, silent=False,
                 decoy=False, refuse_load=False, select_uuid=UU["kate"],
                 select_error=None, select_delay=0.05,
                 no_run=False, junk=False, max_desktops=None,
                 min_desktops=1, slow=0.0):
        self.bus = Bus(address)
        self.bus.serve_calls = True
        self.plasma = plasma
        self.silent = silent            # run() replies, the script says nothing
        self.decoy = decoy              # a stale payload arrives first
        self.refuse_load = refuse_load  # loadScript -> -1 (name already loaded)
        self.no_run = no_run            # neither object path answers run()
        self.junk = junk                # the script answers something else
        self.select_uuid = select_uuid
        self.select_error = select_error
        self.select_delay = select_delay
        # Seconds of thinking before Introspect and loadScript answer: a KWin
        # busy with its own compositing does not answer either of them
        # instantly, and those two are where the backend's deadline used to
        # not apply at all.
        self.slow = slow
        self.windows = fixture_windows(plasma >= 6)
        self.max_desktops = max_desktops   # KWin's own cap (20 on 5.27, 25 on 6)
        self.min_desktops = min_desktops   # KWin keeps at least one
        self.desktops = [(0, "d-one", "Desktop 1"), (1, "d-two", "Desktop 2")]
        self.current = 0
        # KWin's own bookkeeping, modelled exactly: `script_list` is
        # m_scripts (an id is its index at load time, so the ids come round
        # when a lower one is unloaded) and `objects` is the D-Bus object
        # registry, where a second registration at a live path is refused
        # and the older script keeps the path.
        self.script_list = []           # [{"id", "plugin", "args"}], in order
        self.objects = {}               # id -> the entry that owns the path
        self.last_args = None           # newest script's A, after unload
        self.plugins = []               # every pluginName ever loaded
        self.loaded = set()             # pluginNames live right now
        self.runs = []                  # object paths run() was tried on
        self.script_modes = []          # permissions each script file had
        self.unloaded = []
        self.calls = []
        self.showing_desktop = False
        self.event_args = None          # the events script's dest/token
        self.events_ready = threading.Event()
        self.size = (1920, 1080)
        self.work_area = (0, 32, 1920, 1048)
        self.cursor = (640, 480)        # workspace.cursorPos; None = no query
        if own_name:
            assert self.bus.request_name(KWIN_NAME) == 1
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self, mock=None):
        if self.bus.sock is None:
            return
        unique = self.bus.unique_name
        try:
            self.bus.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(3)
        self.bus.close()
        # `mock` is the MockBus, not just its address: closing the socket and
        # the bus letting go of org.kde.KWin are two events in two threads.
        # Callers that stand a replacement up straight away pass it.
        if mock is not None and not mock.wait_dropped(unique):
            raise AssertionError(
                "MockBus still holds connection %s five seconds after it "
                "was closed" % unique)

    # -- state helpers

    def find(self, uuid):
        for d in self.windows:
            if d["u"] == uuid:
                return d
        return None

    def emit(self, bus, uuid, change):
        """What the resident event script's callDBus does."""
        dest, token = self.event_args
        bus.send(Message.call(dest, OBJECT_PATH, IFACE, "Event", "sss",
                              (token, uuid, change)))

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
                    continue
                self.bus.reply(m, sig, out)
        except Exception:  # noqa: BLE001 -- socket shut down by close()
            pass

    _INTRO = ('<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object '
              'Introspection 1.0//EN" "http://www.freedesktop.org/standards/'
              'dbus/1.0/introspect.dtd">\n<node>\n%s</node>\n')

    def _introspect(self, path):
        """Child nodes, the way QDBusConnection generates them: Plasma 6
        registers /Scripting/Script<n>, 5.27 /<n> under the root."""
        names = []
        if self.plasma >= 6:
            if path == SCRIPTING_PATH:
                names = ["Script%d" % i for i in sorted(self.objects)]
            elif path == "/":
                names = ["Scripting", "KWin", "VirtualDesktopManager"]
        else:
            if path == "/":
                names = (["Scripting", "KWin", "VirtualDesktopManager"]
                         + [str(i) for i in sorted(self.objects)])
        return "s", (self._INTRO % "".join(
            '  <node name="%s"/>\n' % n for n in names),)

    def _dispatch(self, m):
        a = m.args()
        self.calls.append((m.path, m.member, a))
        if m.interface == "org.freedesktop.DBus.Introspectable":
            if self.slow:
                time.sleep(self.slow)
            return self._introspect(m.path)
        if m.interface == dbus_mini.PROPS_IFACE and m.member == "Get":
            return self._prop(*a)
        if m.path == SCRIPTING_PATH and m.interface == SCRIPTING_IFACE:
            return getattr(self, "s_" + m.member)(*a)
        if m.interface == SCRIPT_IFACE:
            return self._run_path(m, m.path)
        if m.path == KWIN_PATH and m.interface == KWIN_IFACE:
            return getattr(self, "k_" + m.member)(m, *a)
        if m.path == VD_PATH and m.interface == VD_IFACE:
            return getattr(self, "v_" + m.member)(*a)
        raise DBusError(ERR + "UnknownObject", "no object at %s" % m.path)

    # -- /Scripting

    def s_loadScript(self, path, plugin):
        if self.slow:
            time.sleep(self.slow)
        if self.refuse_load or plugin in self.loaded:
            return "i", (-1,)
        self.script_modes.append(os.stat(path).st_mode & 0o777)
        with open(path) as fh:
            source = fh.read()
        head = source.split("\n", 1)[0]
        args = json.loads(head[len(backend_kwin.HEADER):-len(" */")])
        sid = len(self.script_list)     # KWin: scripts.size(), reused
        entry = {"id": sid, "plugin": plugin, "args": args}
        self.last_args = args
        self.script_list.append(entry)
        # registerObject fails when the path is taken, and says nothing
        self.objects.setdefault(sid, entry)
        self.plugins.append(plugin)
        self.loaded.add(plugin)
        return "i", (sid,)

    def s_unloadScript(self, plugin):
        self.unloaded.append(plugin)
        self.loaded.discard(plugin)
        for entry in list(self.script_list):
            if entry["plugin"] == plugin:
                self.script_list.remove(entry)
                if self.objects.get(entry["id"]) is entry:
                    del self.objects[entry["id"]]
        return "b", (True,)

    def s_isScriptLoaded(self, plugin):
        return "b", (plugin in self.loaded,)

    # -- the per-script object

    def _run_path(self, m, path):
        self.runs.append(path)
        if self.no_run:
            raise DBusError(ERR + "UnknownObject", "no object at %s" % path)
        want = re.fullmatch(
            r"/Scripting/Script(\d+)" if self.plasma >= 6 else r"/(\d+)", path)
        if not want:
            raise DBusError(ERR + "UnknownObject", "no object at %s" % path)
        sid = int(want.group(1))
        if sid not in self.objects:
            raise DBusError(ERR + "UnknownObject", "no script %d" % sid)
        args = self.objects[sid]["args"]
        if not self.silent:
            if self.decoy:
                self._send(args, "stale-token", json.dumps({"ok": True, "v": []}))
            try:
                out = {"ok": True, "v": self._op(args)}
            except _ScriptError as e:
                out = {"ok": False, "err": str(e)}
            if out is not None:
                self._send(args, args["token"], json.dumps(out))
        return "", ()

    def _send(self, args, token, blob):
        # KWin's callDBus: an async method call on its own connection, sent
        # during evaluate() -- i.e. before run()'s delayed reply.
        self.bus.send(Message.call(args["dest"], args["path"], args["iface"],
                                   "Result", "ss", (token, blob)))

    # -- the operations the script would perform

    def _op(self, a):
        op = a["op"]
        if self.junk:
            return "not a window list"
        if op == "list":
            return sorted(self.windows, key=lambda d: d["so"])
        if op == "screen":
            return {"w": self.size[0], "h": self.size[1],
                    "areas": [list(self.work_area) for _ in self.desktops]}
        if op == "cursor":
            if self.cursor is None:
                raise _ScriptError("nocursor")
            return {"x": self.cursor[0], "y": self.cursor[1]}
        if op == "events":
            self.event_args = (a["dest"], a["token"])
            self.events_ready.set()
            return {"hooked": len(self.windows)}
        w = self.find(a.get("uuid"))
        if w is None:
            raise _ScriptError("nowindow")
        if op == "info":
            return w
        if op == "activate":
            w["m"] = w["hi"] = False
            if not w["oc"] and w["d"] >= 0:
                self.current = w["d"]
                self._refresh()
            for d in self.windows:
                d["f"] = d is w
            return {"f": True}
        if op == "focus":
            for d in self.windows:
                d["f"] = d is w
            return {"f": True}
        if op == "close":
            self.windows.remove(w)
            return {}
        if op == "minimize":
            w["m"] = w["hi"] = True
            w["f"] = False
            return {}
        if op == "unminimize":
            w["m"] = w["hi"] = False
            return {}
        if op == "raise":
            w["kb"] = False          # as the script does: undo a keepBelow lower
            self.windows.remove(w)
            self.windows.append(w)
            w["so"] = max(d["so"] for d in self.windows) + 1
            return {"how": "raise" if self.plasma >= 6 else "activate"}
        if op == "lower":
            if w["f"]:
                w["so"] = min(d["so"] for d in self.windows) - 1
                return {"how": "lower"}
            w["ka"], w["kb"] = False, True
            return {"how": "keepBelow"}
        if op == "geometry":
            w["mm"], w["fs"] = 0, False       # unclamp() first, as the JS does
            for key, val in (("x", a["x"]), ("y", a["y"]),
                             ("w", a["w"]), ("h", a["h"])):
                if val is not None:
                    w[key] = val
            return {k: w[k] for k in ("x", "y", "w", "h")}
        if op == "state":
            applied, settled = self._state(w, a["state"], a["action"])
            return {"applied": applied, "settled": settled,
                    "xid": w.get("xid") or 0}
        if op == "desktop":
            n = a["n"]
            if n < 0:
                w["st"], w["d"], w["oc"] = True, -1, True
                return {"d": -1}
            if n >= len(self.desktops):
                raise _ScriptError("nodesktop")
            w["st"], w["d"] = False, n
            self._refresh()
            return {"d": n}
        raise _ScriptError("noop")

    _STATES = {"FULLSCREEN": "fs", "HIDDEN": "m", "ABOVE": "ka",
               "BELOW": "kb", "STICKY": "st", "SKIP_TASKBAR": "sk",
               "SKIP_PAGER": "sp", "DEMANDS_ATTENTION": "at"}

    def _state(self, w, state, action):
        """(applied, settled) -- what kwin_js answers with.

        `applied` is the read-back *after* the script has waited for the
        window's own change signal, so a Wayland client that had simply not
        acked the configure yet never shows up here; `settled` is false only
        when nothing could be waited on, and then the read means nothing.
        Window flags: `refuse` = KWin accepted the write and ignored it (a
        window rule, or size hints the state cannot satisfy), `unverifiable`
        = neither a signal nor a timer could be armed."""
        if state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            bit = 1 if state.endswith("VERT") else 2
            cur = bool(w["mm"] & bit)
            want = (not cur) if action == 2 else bool(action)
            if w.get("refuse"):
                return cur, True
            if w.get("unverifiable"):
                return cur, False
            w["mm"] = (w["mm"] | bit) if want else (w["mm"] & ~bit)
            return want, True
        if state == "SHADED":
            if w["sh"] is None:
                raise _ScriptError("noshade")
            if w.get("refuse"):
                # 5.27: `shade` exists on every window and a write to a
                # native one is accepted and ignored
                return w["sh"], True
            w["sh"] = (not w["sh"]) if action == 2 else bool(action)
            return w["sh"], True
        key = self._STATES.get(state)
        if key is None:
            raise _ScriptError("nostate")
        want = (not w[key]) if action == 2 else bool(action)
        if w.get("refuse"):
            return w[key], True     # accepted and ignored, as KWin does
        if w.get("unverifiable"):
            return w[key], False
        w[key] = want
        if key == "m":
            w["hi"] = want
        if key == "st":
            w["d"] = -1 if want else self.current
            w["oc"] = True if want else (w["d"] == self.current)
        return want, True

    def _refresh(self):
        for d in self.windows:
            d["oc"] = d["st"] or d["d"] == self.current

    # -- /KWin

    def k_currentDesktop(self, m):
        return "i", (self.current + 1,)

    def k_setCurrentDesktop(self, m, n):
        if not 1 <= n <= len(self.desktops):
            return "b", (False,)
        self.current = n - 1
        self._refresh()
        return "b", (True,)

    def k_showDesktop(self, m, show):
        self.showing_desktop = bool(show)
        return None, ()          # NoReply, like KWin's annotation

    def k_queryWindowInfo(self, m):
        time.sleep(self.select_delay)     # the user taking aim
        if self.select_error:
            raise DBusError(self.select_error, "the picker said no")
        w = self.find(self.select_uuid)
        info = {"uuid": Variant("s", "{%s}" % w["u"]),
                "caption": Variant("s", w["t"]),
                "resourceClass": Variant("s", w["c"])}
        return "a{sv}", (info,)

    # -- /VirtualDesktopManager

    def _prop(self, iface, name):
        if iface == VD_IFACE:
            if name == "count":
                return "v", (Variant("u", len(self.desktops)),)
            if name == "current":
                return "v", (Variant("s", self.desktops[self.current][1]),)
            if name == "desktops":
                return "v", (Variant("a(iss)", [tuple(d) for d in self.desktops]),)
        if iface == KWIN_IFACE and name == "showingDesktop":
            return "v", (Variant("b", self.showing_desktop),)
        raise DBusError(ERR + "InvalidArgs", "no property %s" % name)

    def v_createDesktop(self, position, name):
        if (self.max_desktops is not None
                and len(self.desktops) >= self.max_desktops):
            # createVirtualDesktop() returns nullptr past maximum() and the
            # D-Bus slot is void: KWin says nothing at all.
            return "", ()
        self.desktops.insert(position, (position, "d-%d" % position, name))
        self.desktops = [(i, d[1], d[2]) for i, d in enumerate(self.desktops)]
        return "", ()

    def v_removeDesktop(self, ident):
        if len(self.desktops) <= self.min_desktops:
            return "", ()               # KWin keeps at least one, silently
        self.desktops = [(i, d[1], d[2]) for i, d in
                         enumerate(d for d in self.desktops if d[1] != ident)]
        self.current = min(self.current, len(self.desktops) - 1)
        return "", ()


class _ScriptError(Exception):
    """What the JS `throw new Error("nowindow")` becomes on the wire."""


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()
        cls.rtdir = tempfile.mkdtemp(prefix="wdotool-kwin-rt-")
        cls._saved = {k: os.environ.get(k) for k in
                      ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
                       "WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK",
                       "WDOTOOL_BACKEND", "SUDO_UID", "PKEXEC_UID", "DISPLAY")}
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = cls.mock.address
        os.environ["XDG_RUNTIME_DIR"] = cls.rtdir
        for k in ("WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK", "WDOTOOL_BACKEND",
                  "SUDO_UID", "PKEXEC_UID", "DISPLAY"):
            os.environ.pop(k, None)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.mock.close()
        shutil.rmtree(cls.rtdir, ignore_errors=True)

    def backend(self, **kw):
        old = getattr(self, "kwin", None)
        if old is not None:
            # Only one connection may own org.kde.KWin -- and the bus letting
            # go of the name happens in its own serving thread, after the
            # socket closes (MockBus.wait_dropped). Standing the replacement
            # up without waiting for that answers the next RequestName 3, not
            # 1, which is what made this file fail under load.
            old.close(self.mock)
        self.kwin = KwinScriptingService(self.mock.address, **kw)
        # with the mock, so the cleanup of one test cannot race the setUp of
        # the next: a new TestCase instance has no `kwin` to close itself
        self.addCleanup(self.kwin.close, self.mock)
        b = KwinBackend(bus=Bus(self.mock.address))
        self.addCleanup(b.bus.close)
        # never let a test reach a real X server for the XWayland ids
        b._x = None
        return b


class TransportTests(_Base):
    """loadScript/run/unloadScript: the negative result, plugin-name
    uniqueness, both object-path shapes, the token, silence, cleanup."""

    def test_list_round_trip_without_a_monitor(self):
        b = self.backend()
        wins = b.list()
        self.assertEqual([w.id for w in wins],
                         [WID["desktop"], WID["kate"], WID["konsole"],
                          WID["xterm"]])
        # Introspect (whose script ids are live? -- the ownership check that
        # stops us running somebody else's script) -> loadScript -> run ->
        # unloadScript, nothing else, no sleep. The second Introspect is the
        # 5.27 path shape, asked for once and then remembered.
        members = [c[1] for c in self.kwin.calls]
        self.assertEqual(members, ["Introspect", "Introspect", "loadScript",
                                   "run", "unloadScript"])
        b.list()
        self.assertEqual([c[1] for c in self.kwin.calls][5:],
                         ["Introspect", "loadScript", "run", "unloadScript"])

    def test_negative_load_script_is_fatal(self):
        b = self.backend(refuse_load=True)
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertIn("already has a script named", str(cm.exception))
        self.assertIn("-1", str(cm.exception))
        # and we never ran somebody else's script id
        self.assertEqual(self.kwin.runs, [])

    def test_plugin_name_is_unique_per_call(self):
        b = self.backend()
        for _ in range(4):
            b.list()
        self.assertEqual(len(self.kwin.plugins), 4)
        self.assertEqual(len(set(self.kwin.plugins)), 4)
        for name in self.kwin.plugins:
            self.assertTrue(name.startswith("wdotool-%d-" % os.getpid()), name)
        # every one of them was unloaded again
        self.assertEqual(sorted(self.kwin.unloaded), sorted(self.kwin.plugins))
        self.assertEqual(self.kwin.loaded, set())

    def test_plugin_name_is_unique_across_processes_too(self):
        # KWin holds a pluginName for as long as the script object lives and
        # nothing can list what is loaded, so a wdotool killed between
        # loadScript and unloadScript leaks its name for the rest of the
        # session. With pid+counter alone the next process handed that pid
        # would fail on its first command, for ever; the name is random too.
        names = {backend_kwin._plugin_name(0) for _ in range(200)}
        self.assertEqual(len(names), 200)
        self.assertNotEqual(backend_kwin._plugin_name(3, "ev"),
                            backend_kwin._plugin_name(3, "ev"))
        for n in list(names)[:5]:
            self.assertTrue(n.startswith("wdotool-%d-0-" % os.getpid()), n)

    def test_plasma_5_27_object_path(self):
        b = self.backend(plasma=5)
        self.assertEqual(len(b.list()), 4)
        # /Scripting/Script0 first (Plasma 6), then /0 (5.27)
        self.assertEqual(self.kwin.runs, ["/Scripting/Script0", "/0"])
        self.assertEqual(self.kwin.loaded, set())

    def test_run_failure_unloads_and_reports(self):
        b = self.backend()
        self.kwin.no_run = True   # neither object path answers
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertIn("kwin backend", str(cm.exception))
        self.assertEqual(self.kwin.runs, ["/Scripting/Script0", "/0"])
        self.assertEqual(self.kwin.unloaded, self.kwin.plugins)

    def test_silent_script_times_out_with_a_clear_error(self):
        b = self.backend(silent=True)
        t0 = time.monotonic()
        with self.assertRaises(CmdError) as cm:
            b._script("list", timeout=0.4)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("sent no result", str(cm.exception))
        self.assertEqual(self.kwin.loaded, set())

    def test_a_malformed_payload_is_a_clear_error(self):
        b = self.backend(junk=True)
        b.script_timeout = 2.0
        for call in (b.list, b.views, b.display_size):
            with self.assertRaises(CmdError) as cm:
                call()
            self.assertIn("kwin backend", str(cm.exception))

    def test_display_size_survives_an_earlier_soft_failure(self):
        b = self.backend(silent=True)
        b.script_timeout = 0.4
        b.workspaces()                       # caches "no screen info"
        with self.assertRaises(CmdError) as cm:
            b.display_size()
        self.assertIn("display size unknown", str(cm.exception))

    def test_a_stale_payload_is_ignored(self):
        b = self.backend(decoy=True)
        self.assertEqual(len(b.list()), 4)

    def test_owns_a_bus_name_and_falls_back_when_it_is_taken(self):
        squatter = Bus(self.mock.address)
        self.addCleanup(squatter.close)
        self.assertEqual(squatter.request_name(BUS_NAME), 1)
        b = self.backend()
        self.assertEqual(b.dest, "%s.p%d" % (BUS_NAME, os.getpid()))
        self.assertEqual(len(b.list()), 4)   # still answered on our own name

    def test_owns_the_plain_name_when_it_is_free(self):
        b = self.backend()
        self.assertEqual(b.dest, BUS_NAME)
        self.assertTrue(b.bus.name_has_owner(BUS_NAME))

    def test_script_source_is_self_describing(self):
        b = self.backend()
        src = b._source("dest.name", "tok", "geometry", uuid="u", x=1, y=2,
                        w=3, h=4)
        head, rest = src.split("\n", 1)
        args = json.loads(head[len(backend_kwin.HEADER):-len(" */")])
        self.assertEqual(args["op"], "geometry")
        self.assertEqual(args["token"], "tok")
        self.assertEqual(args["dest"], "dest.name")
        self.assertEqual(args["path"], OBJECT_PATH)
        self.assertEqual(args["iface"], IFACE)
        self.assertEqual((args["x"], args["y"], args["w"], args["h"]),
                         (1, 2, 3, 4))
        self.assertTrue(rest.startswith("var A = {"))
        self.assertIn(kwin_js.SCRIPT.strip()[:40], src)


class ScriptIdRaceTests(_Base):
    """loadScript answers with m_scripts.size(), so an id comes round the
    moment a lower-numbered script is unloaded while a higher-numbered one is
    still alive -- and KWin then refuses the second registration at that path
    without saying so, leaving run() pointed at the OTHER script.

    Measured on Plasma 6.6: A->0, B->1, unload A, C->1, and
    /Scripting/Script1 is still B. Seven of ten concurrent windowmoves died
    that way, some of them by running a stranger's command twice."""

    def test_the_fake_reproduces_kwins_id_reuse(self):
        """The premise. Without this the rest of the file proves nothing."""
        k = KwinScriptingService(self.mock.address, own_name=False)
        self.addCleanup(k.close)
        js = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
        js.write(backend_kwin.HEADER.encode() + b'{} */\n')
        js.close()
        self.addCleanup(os.unlink, js.name)
        a = k.s_loadScript(js.name, "A")[1][0]
        b = k.s_loadScript(js.name, "B")[1][0]
        self.assertEqual((a, b), (0, 1))
        k.s_unloadScript("A")
        c = k.s_loadScript(js.name, "C")[1][0]
        self.assertEqual(c, 1)                       # the id came round
        self.assertEqual(k.objects[1]["plugin"], "B")  # ...and B keeps it
        d = k.s_loadScript(js.name, "D")[1][0]
        self.assertEqual(d, 2)                       # the next one is free

    def _wedge(self, b):
        """Leave one live script sitting on the id KWin will hand out next --
        exactly the state another wdotool's in-flight command leaves."""
        js = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
        js.write(backend_kwin.HEADER.encode() + b'{} */\n')
        js.close()
        self.addCleanup(os.unlink, js.name)
        self.kwin.s_loadScript(js.name, "stranger-A")
        self.kwin.s_loadScript(js.name, "stranger-B")
        self.kwin.s_unloadScript("stranger-A")
        self.assertEqual(sorted(self.kwin.objects), [1])

    def test_a_command_does_not_run_a_strangers_script(self):
        b = self.backend()
        self._wedge(b)
        wins = b.list()
        self.assertEqual([w.id for w in wins],
                         [WID["desktop"], WID["kate"], WID["konsole"],
                          WID["xterm"]])
        # the stranger's script was never run
        self.assertNotIn("/Scripting/Script1", self.kwin.runs)
        # ...and it is still loaded: we must not unload another client's
        self.assertIn("stranger-B", self.kwin.loaded)

    def test_the_padding_is_cleaned_up(self):
        b = self.backend()
        self._wedge(b)
        before = set(self.kwin.loaded)
        b.list()
        self.assertEqual(set(self.kwin.loaded), before)

    def test_two_backends_interleaving_both_get_their_own_answer(self):
        """The concurrency shape of the live repro, deterministically: each
        of ten commands must come back with its own payload."""
        b = self.backend()
        for _ in range(10):
            self._wedge(b)
            self.assertEqual(len(b.list()), 4)
            for name in [p for p in list(self.kwin.loaded)
                         if p.startswith("stranger")]:
                self.kwin.s_unloadScript(name)

    def test_it_gives_up_rather_than_spinning(self):
        """A scripting client churning as fast as we are: bounded, and it
        says what happened instead of running a stranger's script."""
        b = self.backend()
        # every index a load could be handed is already a live object
        for i in range(64):
            self.kwin.objects[i] = {"id": i, "plugin": "stranger", "args": {}}
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertIn("already taken", str(cm.exception))
        self.assertNotIn("/Scripting/Script0", self.kwin.runs)

    def test_a_compositor_that_will_not_introspect_still_works(self):
        """The check is best effort: an answer we cannot get is not a reason
        to refuse the command."""
        b = self.backend()

        def no_introspect(path):
            raise DBusError(ERR + "UnknownMethod", "no")

        self.kwin._introspect = no_introspect
        self.assertEqual(len(b.list()), 4)

    def test_the_events_script_keeps_the_name_it_ended_up_with(self):
        """_script returns the pluginName for the caller to unload later; an
        id race renames it, and unloading the old name would leak a resident
        script for the rest of the session."""
        b = self.backend()
        self._wedge(b)
        plugin = b._script("events", timeout=2.0)
        self.assertIn(plugin, self.kwin.loaded)
        b.unload(plugin)
        self.assertNotIn(plugin, self.kwin.loaded)

    def test_a_renamed_script_carries_its_new_name(self):
        """The events script unloads itself by the pluginName baked into its
        source (K1's watchdog), so a load that had to be renamed must carry
        the new name -- a stale one would unload nothing and leave a resident
        script hooked to every window for the rest of the session."""
        b = self.backend()
        self._wedge(b)
        plugin = b._script("events", timeout=2.0)
        # the padding is unloaded again before _script returns, so count the
        # names KWin was ever handed rather than what is still loaded
        ours = [n for n in self.kwin.plugins if n.startswith("wdotool-")]
        self.assertGreater(len(ours), 1, "no rename happened; test is vacuous")
        self.assertNotEqual(ours[0], plugin)             # the padding
        self.assertEqual(ours[-1], plugin)
        loaded = [e for e in self.kwin.script_list if e["plugin"] == plugin]
        self.assertEqual(len(loaded), 1, self.kwin.script_list)
        self.assertEqual(loaded[0]["args"]["plugin"], plugin)
        # ...and the padding still carries its own, not this one
        for entry in self.kwin.script_list:
            self.assertEqual(entry["args"].get("plugin", entry["plugin"]),
                             entry["plugin"])
        b.unload(plugin)


class BackendTests(_Base):
    def setUp(self):
        self.b = self.backend()

    # -- listing and identity

    def test_window_mapping(self):
        wins = {w.id: w for w in self.b.list()}
        x = wins[WID["xterm"]]
        self.assertEqual((x.title, x.class_, x.instance, x.pid),
                         ("test@kde: ~", "XTerm", "xterm", 1201))
        self.assertEqual((x.x, x.y, x.w, x.h), (100, 80, 640, 480))
        self.assertTrue(x.focused and x.visible)
        self.assertEqual(x.desktop, 0)
        # minimized: not visible. On another desktop: not visible either.
        self.assertFalse(wins[WID["kate"]].visible)
        self.assertFalse(wins[WID["konsole"]].visible)
        self.assertTrue(wins[WID["desktop"]].visible)

    def test_ids_survive_a_relist_and_map_back_to_uuids(self):
        self.b.list()
        self.assertEqual(self.b._uuid_for(WID["kate"]), UU["kate"])
        first = [w.id for w in self.b.list()]
        self.assertEqual(first, [w.id for w in self.b.list()])

    def test_find_and_unknown_id(self):
        w = self.b.find(WID["konsole"])
        self.assertEqual(w.title, "test@kde: ~")
        with self.assertRaises(CmdError) as cm:
            self.b.find(12345)
        self.assertIn("12345", str(cm.exception))

    def test_stale_id_is_re_read_once(self):
        self.b.list()
        self.b._uuids[999] = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(CmdError) as cm:
            self.b.activate(999)
        self.assertIn("999", str(cm.exception))

    # -- per-window actions

    def test_activate_switches_desktop_and_focuses(self):
        self.b.activate(WID["konsole"])
        self.assertEqual(self.kwin.current, 1)
        self.assertTrue(self.kwin.find(UU["konsole"])["f"])
        self.assertEqual(self.b.get_desktop(), 1)

    def test_focus_does_not_switch_desktop(self):
        self.b.focus(WID["kate"])
        self.assertEqual(self.kwin.current, 0)
        self.assertTrue(self.kwin.find(UU["kate"])["f"])

    def test_close(self):
        self.b.close(WID["kate"])
        self.assertIsNone(self.kwin.find(UU["kate"]))

    def test_kill_uses_the_pid(self):
        killed = []
        real = os.kill
        os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            self.b.kill(WID["xterm"])
        finally:
            os.kill = real
        self.assertEqual(killed, [(1201, 9)])

    def test_minimize_map_unmap_is_mapped(self):
        self.b.minimize(WID["xterm"])
        self.assertTrue(self.kwin.find(UU["xterm"])["m"])
        self.assertFalse(self.b.is_mapped(WID["xterm"]))
        self.b.map(WID["xterm"])
        self.assertTrue(self.b.is_mapped(WID["xterm"]))
        self.b.unmap(WID["xterm"])
        self.assertFalse(self.b.is_mapped(WID["xterm"]))

    def test_raise_and_lower(self):
        self.b.raise_(WID["kate"])
        self.assertEqual(self.kwin.windows[-1]["u"], UU["kate"])
        self.b.lower(WID["xterm"])   # focused: a real lower
        self.assertEqual(min(d["so"] for d in self.kwin.windows),
                         self.kwin.find(UU["xterm"])["so"])

    def test_lower_of_an_inactive_window_warns_and_keeps_below(self):
        err = _capture_stderr()
        with err:
            self.b.lower(WID["kate"])
        self.assertTrue(self.kwin.find(UU["kate"])["kb"])
        self.assertIn("keep-below", err.getvalue())

    def test_raise_clears_the_keep_below_a_lower_set(self):
        """windowlower on a non-active window is approximated with keepBelow,
        and nothing used to clear it: the window stayed pinned to the bottom
        for the rest of the session, across processes, with `raise` unable to
        undo it and no wdotool command able to show it."""
        err = _capture_stderr()
        with err:
            self.b.lower(WID["kate"])
        self.assertTrue(self.kwin.find(UU["kate"])["kb"])
        self.b.raise_(WID["kate"])
        self.assertFalse(self.kwin.find(UU["kate"])["kb"])

    def test_raise_clears_keep_below_on_5_27_too(self):
        b = self.backend(plasma=5)
        err = _capture_stderr()
        with err:
            b.lower(WID["kate"])
            self.assertTrue(self.kwin.find(UU["kate"])["kb"])
            b.raise_(WID["kate"])
        self.assertFalse(self.kwin.find(UU["kate"])["kb"])

    def test_shaded_on_a_native_window_does_not_blame_a_window_rule(self):
        """5.27 has a `shade` property on every window and ignores a write to
        a native one. Sending people to look for a window rule that does not
        exist was the wrong diagnosis; the backend knows the window has no X
        id."""
        b = self.backend(plasma=5)
        w = self.kwin.find(UU["konsole"])       # native: xid 0 in the fixture
        self.assertFalse(w.get("xid"))
        w["refuse"] = True
        why = b.set_state(WID["konsole"], "SHADED", 1) or ""
        self.assertIn("can only shade X11 windows", why)
        self.assertNotIn("window rule", why)

    def test_a_refused_state_on_an_x11_window_still_blames_the_rule(self):
        b = self.backend(plasma=5)
        w = self.kwin.find(UU["xterm"])
        self.assertTrue(w.get("xid"))
        w["refuse"] = True
        self.assertIn("window rule", b.set_state(WID["xterm"], "SHADED", 1) or "")

    def test_raise_on_5_27_warns_that_it_activates(self):
        b = self.backend(plasma=5)
        err = _capture_stderr()
        with err:
            b.raise_(WID["kate"])
        self.assertIn("5.27", err.getvalue())

    def test_move_resize_and_the_maximize_reset(self):
        self.kwin.find(UU["xterm"])["mm"] = 3      # maximized both ways
        self.kwin.find(UU["xterm"])["fs"] = True
        self.b.move_window(WID["xterm"], 40, 50)
        d = self.kwin.find(UU["xterm"])
        self.assertEqual((d["x"], d["y"], d["w"], d["h"]), (40, 50, 640, 480))
        self.assertEqual((d["mm"], d["fs"]), (0, False))
        self.b.resize(WID["xterm"], 300, 200)
        self.assertEqual((d["x"], d["y"], d["w"], d["h"]), (40, 50, 300, 200))
        self.b.move_resize(WID["xterm"], 1, 2, 3, 4)
        self.assertEqual((d["x"], d["y"], d["w"], d["h"]), (1, 2, 3, 4))

    def test_set_state_every_atom(self):
        d = self.kwin.find(UU["kate"])
        for state, key in (("FULLSCREEN", "fs"), ("ABOVE", "ka"),
                           ("BELOW", "kb"), ("SKIP_TASKBAR", "sk"),
                           ("SKIP_PAGER", "sp"), ("DEMANDS_ATTENTION", "at"),
                           ("HIDDEN", "m"), ("STICKY", "st")):
            self.b.set_state(WID["kate"], state, 1)
            self.assertTrue(d[key], state)
            self.b.set_state(WID["kate"], state, 2)
            self.assertFalse(d[key], state)
            self.b.set_state(WID["kate"], state, 1)
            self.b.set_state(WID["kate"], state, 0)
            self.assertFalse(d[key], state)
        self.b.set_state(WID["kate"], "MAXIMIZED_VERT", 1)
        self.assertEqual(d["mm"], 1)
        self.b.set_state(WID["kate"], "MAXIMIZED_HORZ", 1)
        self.assertEqual(d["mm"], 3)
        self.b.set_state(WID["kate"], "MAXIMIZED_VERT", 0)
        self.assertEqual(d["mm"], 2)

    def test_set_state_reports_when_kwin_ignores_it(self):
        # KWin 5.27 accepts fullscreen on a size-hinted X11 client and does
        # nothing with it; the read-back is what says so. The reason comes
        # back rather than going straight to stderr, because wwmctl has a
        # second route for an XWayland window and takes it first.
        d = self.kwin.find(UU["kate"])
        d["refuse"] = True
        err = _capture_stderr()
        with err:
            why = self.b.set_state(WID["kate"], "FULLSCREEN", 1)
        self.assertIn("did not apply", why or "")
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(d["fs"])
        # maximize too: the script waits for the window to agree before it
        # answers, so a state that stays unapplied really is one KWin refused
        why = self.b.set_state(WID["kate"], "MAXIMIZED_VERT", 1)
        self.assertIn("did not apply", why or "")

    def test_set_state_says_nothing_when_it_applied(self):
        self.assertIsNone(self.b.set_state(WID["kate"], "ABOVE", 1))

    def test_set_state_never_complains_on_an_unverifiable_read(self):
        # `settled: false` = the script could arm neither the window's change
        # signal nor a timer, so its read-back says nothing. Warning on it is
        # the false alarm every FULLSCREEN on a Wayland client used to print.
        d = self.kwin.find(UU["kate"])
        d["unverifiable"] = True
        for state in ("FULLSCREEN", "MAXIMIZED_VERT", "ABOVE"):
            err = _capture_stderr()
            with err:
                self.assertIsNone(self.b.set_state(WID["kate"], state, 1),
                                  state)
            self.assertEqual(err.getvalue(), "", state)

    def test_unsupported_states_is_the_hook_wwmctl_asks_for(self):
        """Without it wwmctl never tried the X route for an XWayland window
        whose state KWin has no setter for at all."""
        self.assertIn("SHADED", self.b.unsupported_states())

    def test_set_state_asks_the_script_to_wait(self):
        self.b.set_state(WID["kate"], "FULLSCREEN", 1)
        args = self.kwin.script_list[-1]["args"] if self.kwin.script_list \
            else self.kwin.last_args
        self.assertEqual(args["op"], "state")
        self.assertEqual(args["settle"], backend_kwin.SETTLE_MS)
        self.assertGreater(args["settle"], 0)

    def test_set_state_bad_action_and_unknown_state(self):
        with self.assertRaises(CmdError):
            self.b.set_state(WID["kate"], "FULLSCREEN", 7)
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(WID["kate"], "NO_SUCH_STATE", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))

    def test_shaded_is_a_gap_on_6_and_works_on_5_27(self):
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(WID["kate"], "SHADED", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("shading", str(cm.exception))
        b5 = self.backend(plasma=5)
        b5.set_state(WID["kate"], "SHADED", 1)
        self.assertTrue(self.kwin.find(UU["kate"])["sh"])

    def test_window_desktop_and_move_to_desktop(self):
        self.assertEqual(self.b.window_desktop(WID["konsole"]), 1)
        self.b.set_window_desktop(WID["konsole"], 0)
        self.assertEqual(self.b.window_desktop(WID["konsole"]), 0)
        self.b.set_window_desktop(WID["konsole"], -1)   # sticky
        self.assertTrue(self.kwin.find(UU["konsole"])["st"])
        with self.assertRaises(CmdError) as cm:
            self.b.set_window_desktop(WID["konsole"], 9)
        self.assertIn("desktop 9", str(cm.exception))

    # -- desktops, without a script

    def test_desktop_plumbing_is_plain_dbus(self):
        self.kwin.calls.clear()
        self.assertEqual(self.b.get_desktop(), 0)
        self.assertEqual(self.b.num_desktops(), 2)
        self.b.set_desktop(1)
        self.assertEqual(self.b.get_desktop(), 1)
        self.assertNotIn("loadScript", [c[1] for c in self.kwin.calls])
        with self.assertRaises(CmdError):
            self.b.set_desktop(7)

    def test_an_argument_the_wire_cannot_carry_is_one_line(self):
        """B8: _call let the marshaller's ValueError out, so `wwmctl -s
        4294967296` was a traceback on KDE where GNOME printed a message
        (test_backend_gnome.test_invalid_window_id_is_one_line_not_a_marshal_
        traceback). The two backends answer alike now."""
        with self.assertRaises(CmdError) as cm:
            self.b.set_desktop(2 ** 32)
        self.assertIn("kwin backend: setCurrentDesktop: invalid argument",
                      str(cm.exception))
        for bad in (-(2 ** 40), 2 ** 64):
            with self.assertRaises(CmdError) as cm:
                self.b._call(KWIN_PATH, KWIN_IFACE, "setCurrentDesktop", "i", (bad,))
            self.assertIn("invalid argument", str(cm.exception))
        # nothing reached KWin: a refused argument is not a half-done command
        self.assertNotIn("setCurrentDesktop", [c[1] for c in self.kwin.calls])

    def test_set_num_desktops(self):
        self.b.set_num_desktops(4)
        self.assertEqual(self.b.num_desktops(), 4)
        self.b.set_num_desktops(1)
        self.assertEqual(self.b.num_desktops(), 1)
        with self.assertRaises(CmdError):
            self.b.set_num_desktops(0)

    def test_set_num_desktops_stops_at_kwins_own_cap(self):
        # createVirtualDesktop() returns nullptr past maximum() (20 on 5.27,
        # 25 on 6) and the D-Bus slot is void, so nothing changing is the
        # only refusal there is. Looping on it hammers the compositor with
        # D-Bus calls for ever.
        b = self.backend(max_desktops=5)
        with self.assertRaises(CmdError) as cm:
            b.set_num_desktops(9)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("5", str(cm.exception))
        self.assertEqual(b.num_desktops(), 5)
        creates = [c for c in self.kwin.calls if c[1] == "createDesktop"]
        self.assertLessEqual(len(creates), 5)      # 3 that worked, 1 that did not

    def test_set_num_desktops_stops_when_kwin_will_not_remove(self):
        b = self.backend(min_desktops=2)
        with self.assertRaises(CmdError) as cm:
            b.set_num_desktops(1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("below 2", str(cm.exception))
        self.assertEqual(b.num_desktops(), 2)
        removes = [c for c in self.kwin.calls if c[1] == "removeDesktop"]
        self.assertEqual(len(removes), 1)

    def test_show_desktop(self):
        self.b.show_desktop(True)
        deadline = time.monotonic() + 2
        while not self.kwin.showing_desktop and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.kwin.showing_desktop)

    def test_display_size_is_cached(self):
        self.assertEqual(self.b.display_size(), (1920, 1080))
        n = len([c for c in self.kwin.calls if c[1] == "loadScript"])
        self.assertEqual(self.b.display_size(), (1920, 1080))
        self.assertEqual(len([c for c in self.kwin.calls
                              if c[1] == "loadScript"]), n)

    # -- the interactive picker

    def test_select_window(self):
        self.assertEqual(self.b.select_window(), WID["kate"])
        self.assertEqual(self.b._uuids[WID["kate"]], UU["kate"])

    def test_select_window_cancelled(self):
        b = self.backend(select_error="org.kde.KWin.Error.UserCancel")
        with self.assertRaises(CmdError) as cm:
            b.select_window()
        self.assertIn("cancelled", str(cm.exception))

    def test_select_window_unmanaged(self):
        b = self.backend(select_error="org.kde.KWin.Error.InvalidWindow")
        with self.assertRaises(CmdError) as cm:
            b.select_window()
        self.assertIn("not managed", str(cm.exception))

    def test_select_window_does_not_wait_for_ever(self):
        """KWin has ONE reply slot for queryWindowInfo: a second picker
        started while the first is up takes the click, and the first call is
        never answered at all. timeout=None made that an unkillable wait."""
        b = self.backend(select_delay=30.0)
        old = backend_kwin.SELECT_TIMEOUT
        backend_kwin.SELECT_TIMEOUT = 0.3
        try:
            t0 = time.monotonic()
            with self.assertRaises(CmdError) as cm:
                b.select_window()
        finally:
            backend_kwin.SELECT_TIMEOUT = old
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("never answered", str(cm.exception))
        self.assertIn("another picker", str(cm.exception))

    def test_the_select_deadline_is_overridable(self):
        os.environ["WDOTOOL_SELECT_TIMEOUT"] = "0.25"
        self.addCleanup(os.environ.pop, "WDOTOOL_SELECT_TIMEOUT", None)
        self.assertEqual(backend_kwin._select_timeout(), 0.25)
        for junk in ("", "nonsense", "0", "-3"):
            os.environ["WDOTOOL_SELECT_TIMEOUT"] = junk
            self.assertEqual(backend_kwin._select_timeout(),
                             backend_kwin.SELECT_TIMEOUT, junk)

    # -- typed hooks

    def test_views(self):
        views = {v.window.id: v for v in self.b.views()}
        x = views[WID["xterm"]]
        self.assertEqual((x.instance, x.cls, x.app_id),
                         ("xterm", "XTerm", "XTerm"))
        self.assertEqual(x.client_type, "wayland")   # no X plane in this test
        self.assertEqual(x.window_type, "NORMAL")
        self.assertEqual(views[WID["desktop"]].window_type, "DESKTOP")
        wins = {w.id: w for w in self.b.list()}                      # and on Window
        self.assertEqual(wins[WID["desktop"]].window_type, "DESKTOP")
        self.assertEqual(wins[WID["xterm"]].window_type, "NORMAL")
        self.assertTrue(views[WID["desktop"]].skip_taskbar)
        k = views[WID["kate"]]
        self.assertTrue(k.minimized and k.hidden)
        self.assertEqual(k.desktop_id, "org.kde.kate.desktop")
        self.assertEqual(k.ws_name, "Desktop 1")
        self.assertEqual(views[WID["konsole"]].ws_name, "Desktop 2")

    def test_views_states(self):
        d = self.kwin.find(UU["kate"])
        d.update(mm=3, fs=True, ka=True, st=True, at=True, sk=True, nb=True,
                 ro="MainWindow", tf=UU["konsole"])
        v = [v for v in self.b.views() if v.window.id == WID["kate"]][0]
        self.assertTrue(v.maximized_h and v.maximized_v and v.fullscreen)
        self.assertTrue(v.above and v.sticky and v.urgent and v.skip_taskbar)
        self.assertFalse(v.decorated)
        self.assertEqual(v.role, "MainWindow")
        self.assertEqual(v.transient_for, WID["konsole"])

    def test_workspaces(self):
        ws = self.b.workspaces()
        self.assertEqual([w.index for w in ws], [0, 1])
        self.assertEqual([w.name for w in ws], ["Desktop 1", "Desktop 2"])
        self.assertEqual([w.active for w in ws], [True, False])
        self.assertEqual(ws[0].work_area, (0, 32, 1920, 1048))

    def test_workspaces_degrade_when_the_script_fails(self):
        b = self.backend(silent=True)
        b._screen = None
        # what is being measured is the degradation, not the wait: the real
        # SCRIPT_TIMEOUT is 10s and this test paid all of it
        b.script_timeout = 0.3
        ws = b.workspaces()          # the work-area script answers nothing
        self.assertEqual([w.index for w in ws], [0, 1])
        self.assertEqual(ws[0].work_area, (0, 0, 0, 0))

    def test_x_display_prefers_the_sessions_own_socket(self):
        # KWin creates the listening socket for its Xwayland itself, as the
        # session user; SDDM leaves its greeter's root-owned Xorg behind on
        # the *lower* number. Taking that one (lowest wins, root accepted)
        # handed `sudo wdotool` a DISPLAY the session cookie cannot open,
        # and the whole X plane silently vanished from the answers.
        d = tempfile.mkdtemp(prefix="wdotool-x11-")
        self.addCleanup(shutil.rmtree, d, True)
        for name in ("X0", "X1", "X2", "Xjunk"):
            open(os.path.join(d, name), "w").close()
        owners = {"X0": 0, "X1": 1000, "X2": 1000}
        saved = (session.X11_SOCKET_DIR, session._owner, session._shell_environ)
        session.X11_SOCKET_DIR = d
        session._owner = lambda p: owners.get(os.path.basename(p))
        session._shell_environ = lambda uid: {}
        try:
            self.assertEqual(session.find_x_display(1000), ":1")
            # a plain X11 session's Xorg *is* root's: still the fallback
            owners = {"X0": 0}
            self.assertEqual(session.find_x_display(1000), ":0")
            owners = {}
            self.assertIsNone(session.find_x_display(1000))
        finally:
            (session.X11_SOCKET_DIR, session._owner,
             session._shell_environ) = saved

    def window_at(self, x, y):
        """The shared pointer hit-test over this backend's own list() --
        what getmouselocation runs. The backend has no hit-test of its own:
        it fills Window.window_type and backend.hit_test does the rest."""
        return hit_test(self.b.list(), x, y)

    def test_window_at_looks_through_desktop_and_docks(self):
        # (1500, 900) is over the plasma DESKTOP window only
        self.assertEqual(self.window_at(1500, 900), 0)
        # over the xterm (focused, topmost)
        self.assertEqual(self.window_at(200, 200), WID["xterm"])
        # (900, 700) is inside kate, which is minimized: never a hit
        self.assertEqual(self.window_at(900, 700), 0)

    def test_a_dock_over_a_window_is_looked_through(self):
        """A Plasma panel is a DOCK at the top of the screen and is in
        windowList() like anything else. A click there falls through to what
        is under it on X11, and getmouselocation has to answer the same on
        every backend -- the rule lives in backend.hit_test and the backend's
        only job is to fill window_type."""
        d = self.kwin.find(UU["xterm"])
        d.update(y=0)                       # the xterm now reaches the top
        dock = dict(self.kwin.find(UU["konsole"]))
        dock.update(u="4d0c0000-0000-4000-8000-0000000000dc", t="Panel",
                    c="plasmashell", n="plasmashell", p=900, ty=2, so=9,
                    x=0, y=0, w=1920, h=32, d=-1, oc=True, m=False, hi=False)
        self.kwin.windows.append(dock)
        wins = {w.id: w for w in self.b.list()}
        self.assertEqual(wins[backend_kwin._wid(dock["u"])].window_type,
                         "DOCK")
        self.assertEqual(self.window_at(200, 10), WID["xterm"])
        # over the panel with nothing under it: the DESKTOP below is looked
        # through as well, so no hit at all
        self.assertEqual(self.window_at(1500, 10), 0)

    def test_every_window_type_code_maps_to_its_name(self):
        """KWin's NET::WindowType codes, as the script reports them. The gaps
        are real: 6 is NET::Override, which KWin has not produced since 5.x,
        and anything unknown is NORMAL rather than an error -- a new code in a
        future Plasma must not make a listing fail."""
        rows = {0: "NORMAL", 1: "DESKTOP", 2: "DOCK", 3: "TOOLBAR", 4: "MENU",
                5: "DIALOG", 7: "MENU", 8: "UTILITY", 9: "SPLASHSCREEN",
                10: "DROPDOWN_MENU", 11: "POPUP_MENU", 12: "TOOLTIP",
                13: "NOTIFICATION", 14: "COMBO", 15: "DND",
                16: "NOTIFICATION", 17: "NOTIFICATION", 18: "POPUP_MENU"}
        self.assertEqual(backend_kwin._WINDOW_TYPES, rows)
        for code, name in rows.items():
            with self.subTest(code=code):
                self.kwin.find(UU["kate"])["ty"] = code
                wins = {w.id: w for w in self.b.list()}
                self.assertEqual(wins[WID["kate"]].window_type, name)
        for unknown in (6, 99, -1):
            with self.subTest(code=unknown):
                self.kwin.find(UU["kate"])["ty"] = unknown
                wins = {w.id: w for w in self.b.list()}
                self.assertEqual(wins[WID["kate"]].window_type, "NORMAL")

    def test_window_at_prefers_the_focused_window(self):
        d = self.kwin.find(UU["kate"])
        d.update(m=False, hi=False, x=100, y=80, w=640, h=480, so=9)
        self.assertEqual(self.window_at(200, 200), WID["xterm"])

    def test_x_info_is_the_session_scan(self):
        orig = (session.find_x_display, session.find_xauthority)
        session.find_x_display = lambda uid=None: ":1"
        session.find_xauthority = lambda uid=None: "/run/user/1000/xauth_abc"
        try:
            self.assertEqual(self.b.x_info(), (":1", "/run/user/1000/xauth_abc"))
            session.find_x_display = lambda uid=None: None
            session.find_xauthority = lambda uid=None: None
            self.assertIsNone(self.b.x_info())
        finally:
            session.find_x_display, session.find_xauthority = orig

    def test_events(self):
        it = self.b.events(timeout=1.0, workspaces=True)

        def push():
            # KWin's script engine calls out over KWin's own bus connection,
            # so the events carry KWin's unique name as their sender -- which
            # is what the backend checks (see the spoofing test below).
            self.kwin.events_ready.wait(5)
            for uuid, change in ((UU["kate"], "title"), ("", "workspace"),
                                 (UU["xterm"], "close")):
                self.kwin.emit(self.kwin.bus, uuid, change)

        t = threading.Thread(target=push, daemon=True)
        t.start()
        got = [next(it), next(it), next(it)]
        it.close()
        t.join(5)
        self.assertEqual(got, [(WID["kate"], "title"), (0, "workspace"),
                               (WID["xterm"], "close")])
        # the event script is unloaded when the iteration ends
        self.assertEqual(self.kwin.loaded, set())

    def test_events_from_a_foreign_connection_are_ignored(self):
        """The token is a shared secret in a file KWin must be able to read,
        so it is not proof of who is answering. Only the owner of
        org.kde.KWin is: a same-uid process that read the token out of /tmp
        must not be able to feed window facts to a (possibly root) wdotool."""
        it = self.b.events(timeout=1.0, workspaces=True)
        spoofer = Bus(self.mock.address)
        self.addCleanup(spoofer.close)

        def push():
            self.kwin.events_ready.wait(5)
            self.kwin.emit(spoofer, UU["kate"], "title")          # not KWin
            self.kwin.emit(self.kwin.bus, UU["xterm"], "close")   # KWin

        t = threading.Thread(target=push, daemon=True)
        t.start()
        got = list(it)
        t.join(5)
        self.assertEqual(got, [(WID["xterm"], "close")])

    def test_the_script_file_is_not_world_readable(self):
        """The generated script carries the reply token, the window titles
        and the search patterns of the command. It used to be chmod 0644 in
        /tmp for the lifetime of the call so that KWin could read it when
        wdotool runs as root; KWin runs as us in every other case, and 0600
        is enough. As root the file is chown()ed to the bus owner instead of
        being opened to everybody."""
        self.b.list()
        self.assertTrue(self.kwin.script_modes)
        for mode in self.kwin.script_modes:
            self.assertEqual(mode & 0o077, 0, oct(mode))
        # what happens as root is driven, not grepped: see RootScriptFileTests
        self.assertEqual(len(self.kwin.script_modes),
                         len(set(self.kwin.plugins)))

    def test_the_events_script_knows_its_own_plugin_name(self):
        """It is the only script that stays loaded, and only the process that
        loaded it can unload it -- so it has to be able to do that itself
        when that process is killed (K1)."""
        seen = {}
        orig = self.b._load_run

        def spy(plugin, make_source, deadline):
            # `make_source` is a factory, not a string: an id race renames the
            # load and the name is baked into the text, so the name the script
            # carries has to be the one it is loaded under.
            seen["plugin"] = plugin
            seen["source"] = make_source(plugin)
            seen["renamed"] = make_source("some-other-name")
            return orig(plugin, make_source, deadline)

        self.b._load_run = spy
        self.addCleanup(lambda: self.b.__dict__.pop("_load_run", None))
        self.assertEqual(list(self.b.events(timeout=0.3)), [])
        self.assertIn('"plugin": "%s"' % seen["plugin"], seen["source"])
        self.assertIn('"plugin": "some-other-name"', seen["renamed"])
        self.assertTrue(seen["plugin"].endswith(seen["plugin"].split("-")[-1]))

    def test_events_stop_after_silence(self):
        self.assertEqual(list(self.b.events(timeout=0.3)), [])
        self.assertEqual(self.kwin.loaded, set())


class PayloadShapeTests(_Base):
    """The two Plasma payload flavours the script normalizes into: 5.27
    hands out X11 window ids and a shade flag, 6 has neither."""

    def test_plasma_6_has_no_x11_ids_of_its_own(self):
        b = self.backend(plasma=6)
        raw = b._raw()
        self.assertEqual([d["xid"] for d in raw], [0, 0, 0, 0])
        self.assertEqual(b._xids(raw), {})       # no X plane in this test
        self.assertEqual([v.client_type for v in b.views()],
                         ["wayland"] * 4)

    def test_plasma_5_27_carries_the_x11_id(self):
        b = self.backend(plasma=5)
        views = {v.window.id: v for v in b.views()}
        self.assertEqual(views[WID["xterm"]].xid, XTERM_XID)
        self.assertEqual(views[WID["xterm"]].client_type, "x11")
        self.assertEqual(views[WID["xterm"]].app_id, "")
        self.assertEqual(views[WID["kate"]].xid, 0)
        self.assertEqual(views[WID["kate"]].client_type, "wayland")

    def test_plasma_6_matches_xwayland_client_list(self):
        b = self.backend(plasma=6)
        raw = b._raw()
        b._x = _FakeX([
            # the xterm, as Xwayland reports it: the client rect sits inside
            # KWin's frame, and the title is the same
            {"xid": XTERM_XID, "pid": 1201, "inst": "xterm", "cls": "XTerm",
             "name": "test@kde: ~", "geo": (101, 105, 638, 454)},
            # a second, untitled xterm of the same class and pid family
            {"xid": 0x400009, "pid": 4242, "inst": "xterm", "cls": "XTerm",
             "name": "", "geo": (900, 900, 100, 100)},
        ])
        self.assertEqual(b._xids(raw), {UU["xterm"]: XTERM_XID})
        views = {v.window.id: v for v in b.views()}
        self.assertEqual(views[WID["xterm"]].xid, XTERM_XID)
        self.assertEqual(views[WID["xterm"]].client_type, "x11")
        self.assertEqual(views[WID["xterm"]].app_id, "")

    def test_geometry_separates_two_identical_windows(self):
        raw = [dict(u="a", c="XTerm", n="xterm", p=0, t="", x=0, y=0,
                    w=200, h=100),
               dict(u="b", c="XTerm", n="xterm", p=0, t="", x=800, y=600,
                    w=200, h=100)]
        clients = [{"xid": 11, "pid": 0, "inst": "xterm", "cls": "XTerm",
                    "name": "", "geo": (802, 604, 196, 92)},
                   {"xid": 12, "pid": 0, "inst": "xterm", "cls": "XTerm",
                    "name": "", "geo": (2, 4, 196, 92)}]
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 12, "b": 11})

    def test_a_client_that_agrees_on_nothing_gets_no_id(self):
        # An X client with neither _NET_WM_PID nor WM_CLASS contradicts
        # nothing: matching it on geometry alone would hand its id to a
        # *native* window, which then claims client_type "x11".
        raw = [dict(u="a", c="org.kde.konsole", n="konsole", p=1400,
                    t="test@kde: ~", x=100, y=80, w=640, h=480)]
        anon = [{"xid": 11, "pid": 0, "inst": "", "cls": "",
                 "name": "test@kde: ~", "geo": (100, 108, 640, 452)}]
        self.assertEqual(backend_kwin._match_xids(raw, anon), {})
        # the same client with a class that agrees is matched
        named = [dict(anon[0], cls="konsole", inst="konsole")]
        self.assertEqual(backend_kwin._match_xids(raw, named), {})
        named = [dict(anon[0], cls="org.kde.konsole", inst="konsole")]
        self.assertEqual(backend_kwin._match_xids(raw, named), {"a": 11})
        # ...and so is one that agrees on the pid alone
        self.assertEqual(backend_kwin._match_xids(
            raw, [dict(anon[0], pid=1400)]), {"a": 11})


    def test_two_identical_windows_are_told_apart_by_list_order(self):
        # Two windows of one client, same pid, same class, same title, the
        # same rectangle: title and geometry tie and only the order of the
        # two lists is left. Measured on Plasma 6.6, the old coin flip moved
        # the other window in four runs out of ten.
        raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=2),
               dict(u="b", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=4)]
        clients = [{"xid": 0x1000001, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)},
                   {"xid": 0x1000003, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)}]
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 0x1000001, "b": 0x1000003})
        # the same two windows the other way round in KWin's list
        raw[0]["ix"], raw[1]["ix"] = 4, 2
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 0x1000003, "b": 0x1000001})

    def test_order_is_only_a_tiebreak_geometry_still_wins(self):
        # The positions must never overrule a rectangle that already says
        # which is which -- here the client list is in the other order.
        raw = [dict(u="a", c="XTerm", n="xterm", p=0, t="", x=0, y=0,
                    w=200, h=100, ix=0),
               dict(u="b", c="XTerm", n="xterm", p=0, t="", x=800, y=600,
                    w=200, h=100, ix=1)]
        clients = [{"xid": 11, "pid": 0, "inst": "xterm", "cls": "XTerm",
                    "name": "", "geo": (802, 604, 196, 92)},
                   {"xid": 12, "pid": 0, "inst": "xterm", "cls": "XTerm",
                    "name": "", "geo": (2, 4, 196, 92)}]
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 12, "b": 11})

    def test_a_window_kwin_lists_but_x_does_not_only_shifts_positions(self):
        # An override-redirect popup (a menu, a tooltip) is in
        # workspace.windowList() with its client's pid and class, and never
        # in _NET_CLIENT_LIST. It must not drag the windows after it onto
        # the wrong ids.
        raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=2),
               dict(u="pop", c="XTerm", n="xterm", p=900, t="",
                    x=300, y=228, w=400, h=300, ix=3),
               dict(u="b", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=4)]
        clients = [{"xid": 0x1000001, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)},
                   {"xid": 0x1000003, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)}]
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 0x1000001, "b": 0x1000003})

    def test_a_title_kwin_simplified_still_matches_its_own_window(self):
        # KWin runs an X11 caption through QString::simplified(); the X
        # server hands back what the client set. Compared raw, the window
        # whose title has a trailing space matches the *other* window's
        # title exactly and nothing else -- worse than no title at all.
        raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="Term",
                    x=300, y=200, w=400, h=328, ix=2),
               dict(u="b", c="XTerm", n="xterm", p=900, t="Term",
                    x=300, y=200, w=400, h=328, ix=4)]
        clients = [{"xid": 21, "pid": 900, "inst": "xterm", "cls": "XTerm",
                    "name": "Term ", "geo": (300, 228, 400, 300)},
                   {"xid": 22, "pid": 900, "inst": "xterm", "cls": "XTerm",
                    "name": "Term", "geo": (300, 228, 400, 300)}]
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 21, "b": 22})
        self.assertEqual(xid_match.simplified("  a\t b \n"), "a b")
        self.assertEqual(xid_match.simplified(None), "")

    def test_three_identical_windows_keep_their_own_ids(self):
        raw = [dict(u=u, c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=i)
               for i, u in enumerate("abc")]
        clients = [{"xid": 30 + i, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)} for i in range(3)]
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 30, "b": 31, "c": 32})

    def test_an_unbreakable_tie_gets_no_id_rather_than_a_guess(self):
        # No `ix` (an older script): nothing is left to separate the two,
        # and one of the two ids at random is the outcome that moves the
        # wrong window. Both keep xid 0 and the run says so.
        raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328),
               dict(u="b", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328)]
        clients = [{"xid": 41, "pid": 900, "inst": "xterm", "cls": "XTerm",
                    "name": "Terminal", "geo": (300, 228, 400, 300)},
                   {"xid": 42, "pid": 900, "inst": "xterm", "cls": "XTerm",
                    "name": "Terminal", "geo": (300, 228, 400, 300)}]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(backend_kwin._match_xids(raw, clients), {})
        self.assertIn("could not be told apart", err.getvalue())

    # -- T18: the three ways the (title, distance, order) score used to slip --

    def _tied_pair(self, extra=()):
        """Two windows of one client with the same title and the same
        rectangle, and the two X clients that could each be either.

        Copied from test_a_window_kwin_lists_but_x_does_not_only_shifts_positions:
        the same numbers, so a difference between the two tests is the extra
        row and nothing else."""
        raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=2),
               dict(u="b", c="XTerm", n="xterm", p=900, t="Terminal",
                    x=300, y=200, w=400, h=328, ix=4)]
        raw = list(extra) + raw
        clients = [{"xid": 0x1000001, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)},
                   {"xid": 0x1000003, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "Terminal",
                    "geo": (300, 228, 400, 300)}]
        return raw, clients

    def test_a_window_ahead_of_a_tied_pair_does_not_shift_it(self):
        """kde-x-2: an override-redirect popup *below* the pair (ix=3, ix=6)
        cancelled out, and one *above* it (ix=1) swapped the pair.

        The positions used to be ranked over every candidate at once, so the
        popup -- which is in workspace.windowList() with its client's pid and
        class, and never in _NET_CLIENT_LIST -- pushed both windows one place
        down in the window ranking while the client ranking, which never saw
        it, stayed where it was: |1-0| beat |2-1| and `a` took `b`'s id.
        Measured against KWin 6.6.6: a konsole menu is exactly this row."""
        for ix in (1, 3, 6):
            with self.subTest(popup_ix=ix):
                pop = dict(u="pop", c="XTerm", n="xterm", p=900, t="",
                           x=300, y=228, w=400, h=300, ix=ix)
                raw, clients = self._tied_pair(extra=[pop])
                self.assertEqual(backend_kwin._match_xids(raw, clients),
                                 {"a": 0x1000001, "b": 0x1000003})

    def test_a_native_window_of_the_same_class_ahead_does_not_shift_it(self):
        """The same shape without any X client at all: a Wayland-native
        window of the same application (one Konsole started on Wayland beside
        two on XWayland). It is a candidate -- same pid, same class -- and it
        is first in windowList(), and it must still not move the pair."""
        native = dict(u="native", c="XTerm", n="xterm", p=900, t="Terminal",
                      x=0, y=0, w=200, h=200, ix=0)
        raw, clients = self._tied_pair(extra=[native])
        self.assertEqual(backend_kwin._match_xids(raw, clients),
                         {"a": 0x1000001, "b": 0x1000003})

    def test_a_scaled_xwayland_plane_pairs_the_cascade_correctly(self):
        """kde-x-1: with a scaled output the X rects are device pixels and
        KWin's are logical ones, so the distance between a window and *itself*
        (1500 for a 400x300 window at 100,100 seen at 2x) is larger than the
        distance between the two windows of a cascade (80 for a 40px offset)
        -- and every window took its neighbour's id.

        `ratio` is X-root width / workspace.virtualScreenSize width; the rect
        is divided by it before the distance. 2 is a HiDPI laptop panel, 1.5
        is what Plasma's own scaling slider offers between 1 and 2."""
        for r in (2.0, 1.5):
            with self.subTest(scale=r):
                raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="",
                            x=100, y=100, w=400, h=300, ix=0),
                       dict(u="b", c="XTerm", n="xterm", p=900, t="",
                            x=140, y=140, w=400, h=300, ix=1)]
                clients = [
                    {"xid": 0x1000001, "pid": 900, "inst": "xterm",
                     "cls": "XTerm", "name": "",
                     "geo": (int(100 * r), int(100 * r),
                             int(400 * r), int(300 * r))},
                    {"xid": 0x1000003, "pid": 900, "inst": "xterm",
                     "cls": "XTerm", "name": "",
                     "geo": (int(140 * r), int(140 * r),
                             int(400 * r), int(300 * r))}]
                self.assertEqual(backend_kwin._match_xids(raw, clients, r),
                                 {"a": 0x1000001, "b": 0x1000003})
                # ...and the ratio is load-bearing: read as 1:1 the same two
                # rows come out the other way round
                self.assertEqual(backend_kwin._match_xids(raw, clients, 1.0),
                                 {"a": 0x1000003, "b": 0x1000001})

    def test_an_inconsistent_ratio_drops_the_distance_and_keeps_the_order(self):
        """A layout that mixes scales has no one ratio -- the X root is the
        bounding box at the largest -- so every distance is wrong by a
        different amount. None drops the distance and leaves the title and
        the list order, which are still true."""
        raw = [dict(u="a", c="XTerm", n="xterm", p=900, t="",
                    x=100, y=100, w=400, h=300, ix=0),
               dict(u="b", c="XTerm", n="xterm", p=900, t="",
                    x=140, y=140, w=400, h=300, ix=1)]
        clients = [{"xid": 0x1000001, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "", "geo": (200, 200, 800, 600)},
                   {"xid": 0x1000003, "pid": 900, "inst": "xterm",
                    "cls": "XTerm", "name": "", "geo": (280, 280, 800, 600)}]
        self.assertEqual(backend_kwin._match_xids(raw, clients, None),
                         {"a": 0x1000001, "b": 0x1000003})

    def test_one_row_without_ix_leaves_the_whole_tie_unresolved(self):
        """`ix` is all-or-nothing, and that is the pick, not an oversight.

        A payload where one row lacks it (a script half-replaced by an older
        one, a row a KWin version does not report) could be ranked over the
        rows that do carry it -- but that puts the others at positions that
        are not their list positions, which is a wrong order rather than a
        missing one. So the key stays inert for the whole payload and the
        tied pair keeps xid 0, with the run saying so."""
        raw, clients = self._tied_pair()
        del raw[1]["ix"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(backend_kwin._match_xids(raw, clients), {})
        self.assertIn("could not be told apart", err.getvalue())
        self.assertIn("2 XWayland window(s)", err.getvalue())

    def test_the_script_records_its_list_position_before_it_sorts(self):
        """`ix` has to be taken while the payload is still in
        workspace.windowList() order: the stacking sort two lines later
        destroys it, and stacking order is not _NET_CLIENT_LIST's order."""
        src = _code(kwin_js.SCRIPT)
        self.assertIn("w.ix = i;", src)
        self.assertLess(src.index("w.ix = i;"), src.index("out.sort("))

    def test_two_identical_xterms_through_the_backend_keep_their_own_ids(self):
        """The same tie one layer up: two rows in the fixture, a fake X
        client list in either order, and `views()` reporting each window
        under its own X id. `_FakeX` is handed the clients in the X server's
        order and then reversed, because that order is half the answer."""
        b = self.backend(plasma=6)
        twin = dict(self.kwin.find(UU["xterm"]))
        twin["u"] = "8d0e0000-0000-4000-8000-00000000d00d"
        twin["so"], twin["ix"] = 4, 4
        twin["x"], twin["y"] = 100, 80        # the same rect: only ix is left
        self.kwin.windows.append(twin)
        first, second = 0x1000001, 0x1000003
        clients = [{"xid": first, "pid": 1201, "inst": "xterm",
                    "cls": "XTerm", "name": "test@kde: ~",
                    "geo": (101, 105, 638, 454)},
                   {"xid": second, "pid": 1201, "inst": "xterm",
                    "cls": "XTerm", "name": "test@kde: ~",
                    "geo": (101, 105, 638, 454)}]
        for order in ("as X lists them", "reversed"):
            with self.subTest(order=order):
                cl = (clients if order == "as X lists them"
                      else list(reversed(clients)))
                b._x = _FakeX(cl)
                b._screen = None
                views = {v.window.id: v for v in b.views()}
                # the fixture xterm is ix=3 and the twin ix=4, so the earlier
                # window takes whichever id is earlier in _NET_CLIENT_LIST --
                # both ids move when the list is reversed, which is the whole
                # claim: the order decides, and it is not a coin flip
                self.assertEqual(views[WID["xterm"]].xid, cl[0]["xid"])
                self.assertEqual(views[backend_kwin._wid(twin["u"])].xid,
                                 cl[1]["xid"])
                self.assertEqual(views[WID["xterm"]].client_type, "x11")
        self.assertEqual({first, second}, {0x1000001, 0x1000003})

    def test_the_backend_reads_the_scale_off_the_x_root(self):
        """The seam the fix adds: X root geometry over
        workspace.virtualScreenSize. The fake KWin says 1920x1080; an X root
        of 3840x2160 is a 2x plane, one of 1920x1080 is 1:1, and 3840x1080 --
        two outputs at different scales, the root being the bounding box at
        the larger -- has no answer."""
        b = self.backend(plasma=6)
        self.assertEqual(self.kwin.size, (1920, 1080))
        for root, want in (((1920, 1080), 1.0), ((3840, 2160), 2.0),
                           ((2880, 1620), 1.5), ((3840, 1080), None)):
            with self.subTest(root=root):
                b._screen = None
                self.assertEqual(
                    b._x_ratio(_FakeX([], root_size=root)), want)
        # nothing readable is 1:1, which is what every unscaled session is

        class _NoRoot:
            def root(self):
                raise RuntimeError("no X plane")

        self.assertEqual(b._x_ratio(_NoRoot()), 1.0)


    def test_two_windows_that_collide_both_keep_an_id(self):
        # 30 bits of the uuid: a collision is a one-in-a-million session,
        # and a plain {id: uuid} comprehension would drop one window out of
        # the listing entirely (unlistable, unaddressable).
        u1 = "12345678-0000-4000-8000-000000000001"
        u2 = "12345678-0000-4000-8000-000000000002"   # same first 8 hex
        self.assertEqual(backend_kwin._wid(u1), backend_kwin._wid(u2))
        rows = [{"u": u1}, {"u": u2}]
        table = backend_kwin._id_map(rows)
        self.assertEqual(len(table), 2)
        self.assertEqual(sorted(table.values()), sorted([u1, u2]))
        self.assertNotEqual(rows[0]["_id"], rows[1]["_id"])
        self.assertEqual(rows[0]["_id"], backend_kwin._wid(u1))
        for wid in table:
            self.assertGreaterEqual(wid, 0x40000000)
            self.assertLessEqual(wid, 0xFFFFFFFF)
        # stable while the pair is
        self.assertEqual(backend_kwin._id_map([{"u": u1}, {"u": u2}]), table)

    def test_a_collided_window_is_still_addressable(self):
        b = self.backend()
        clash = dict(self.kwin.find(UU["kate"]))
        clash["u"] = _clashing_uuid(UU["kate"])
        clash["t"], clash["so"] = "clash - Kate", 9
        self.kwin.windows.append(clash)
        wins = {w.title: w.id for w in b.list()}
        self.assertEqual(len(set(wins.values())), len(wins))
        self.assertEqual(b._info(wins["clash - Kate"])["u"], clash["u"])
        self.assertEqual(b._info(wins["untitled - Kate"])["u"], UU["kate"])

    def test_pid_and_class_are_filters(self):
        raw = [dict(u="a", c="XTerm", n="xterm", p=7, t="x", x=0, y=0, w=1, h=1)]
        other_pid = [{"xid": 11, "pid": 9, "inst": "xterm", "cls": "XTerm",
                      "name": "x", "geo": (0, 0, 1, 1)}]
        other_cls = [{"xid": 11, "pid": 7, "inst": "kate", "cls": "Kate",
                      "name": "x", "geo": (0, 0, 1, 1)}]
        self.assertEqual(backend_kwin._match_xids(raw, other_pid), {})
        self.assertEqual(backend_kwin._match_xids(raw, other_cls), {})

    def test_wid_is_32_bit_clean_and_out_of_x_range(self):
        braced = "{7C9E6679-7425-40DE-944B-E07FC1F90AE7}"
        self.assertEqual(backend_kwin._wid(braced), WID["xterm"])
        self.assertEqual(backend_kwin._wid(braced.strip("{}").lower()),
                         WID["xterm"])
        self.assertEqual(backend_kwin._wid(""), 0)
        for u in list(UU.values()) + ["00000000-0000-4000-8000-00000000beef"]:
            wid = backend_kwin._wid(u)
            # every X-shaped consumer truncates to 32 bits, and 0 means
            # "no window" everywhere; Xwayland ids stay well below 0x40000000
            self.assertTrue(0 < wid <= 0xFFFFFFFF, u)
            self.assertGreaterEqual(wid, 0x40000000, u)
            self.assertEqual(wid & 0xFFFFFFFF, wid, u)


KWIN_675_FIXTURE = os.path.join(ROOT, "tests", "fixtures", "live",
                                "arch-kde-6.7.5-fwtwin-windowlist.txt")


def _read_675(path=KWIN_675_FIXTURE):
    """The measured KWin 6.7.5 payload, read out of the fixture rather than retyped.

    What Plasma 6.7.5 / KWin 6.7.5 answered on the arch-kde golden (instance kde-b6) on
    2026-09-11 with two `xterm -T fwtwin -e sh -c 'sleep 600'` (pids 2387, 2388) on its
    Xwayland: the script's own window list with every field the matcher reads, then
    `_NET_CLIENT_LIST` and the clients behind it, then the join the backend made.  Nothing
    is invented -- the uuids, the pids, the rectangles, the titles and the two ORDERS are
    that session's bytes, and the file's header says which commands printed them.
    Returns `(raw, clients, xids, root_list)`; `#` lines are the file's prose."""
    raw, clients, xids, root = [], [], {}, []
    rows = None
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            if line.startswith("_NET_CLIENT_LIST(WINDOW)"):
                root = [int(t, 16) for t in re.findall(r"0x[0-9a-fA-F]+", line)]
            elif line.startswith("RAW:"):
                rows = raw
            elif line.startswith("CLIENTS"):
                rows = clients
            elif line.startswith("xids:"):
                xids = {u: int(x) for u, x in json.loads(line.split(":", 1)[1]).items()}
            elif line.startswith("  {") and rows is not None:
                rows.append(json.loads(line))
    for c in clients:
        c["geo"] = tuple(c["geo"])
    return raw, clients, xids, root


#: The two xterms are `ix` 2 and 3 while the panel is `ix` 1 and `so` 3, which is the case the
#: `ix` key exists for: workspace.windowList() is not the stacking order the payload is sorted
#: into.  `KWIN_675_CLIENTS` is in `_NET_CLIENT_LIST` order, as KwinBackend._x_clients reads it,
#: and each X rect is the CLIENT area (28 px of KWin titlebar above it, and 28 px shorter), which
#: is why the pairing distance is small but never zero.  `KWIN_675_XIDS` is the join that session
#: made: `wwmctl -lpx` printed 0x00e00012 and 0x01000012 on those two rows.
KWIN_675_RAW, KWIN_675_CLIENTS, KWIN_675_XIDS, KWIN_675_ROOT_LIST = _read_675()


class Kwin675Tests(_Base):
    """KWin 6.7.5 publishes its X11 windows and the id join still works on them.

    The 2026-09-11 rig run counted 0 for `wdotool search --name '^fwtwin$'` on arch-kde and
    fedora44-kde, and the first reading of that was that Plasma 6.7 had stopped handing X11 windows
    to org.kde.KWin.Scripting. The guest says otherwise: both xterms were in the list, under the
    title their interactive SHELL had written over `-T fwtwin` (`test@kde-b6:~`, out of Arch's
    /etc/bash.bashrc), and the real `xdotool search --name fwtwin` found nothing either -- the
    clone agreed with X, and the check was asking for a title no window carried
    (vm/live-smoke.d/kde.sh, F4.1). These pin the half that would have been broken if the first
    reading had been right, on that session's own bytes -- kept as
    tests/fixtures/live/arch-kde-6.7.5-fwtwin-windowlist.txt and read by `_read_675`."""

    def test_the_675_payload_joins_both_twins_to_their_own_x_ids(self):
        self.assertEqual(xid_match.match_xids(KWIN_675_RAW, KWIN_675_CLIENTS, 1.0),
                         KWIN_675_XIDS)

    def test_the_backend_makes_that_join_out_of_the_same_two_lists(self):
        """`_xids` over the measured payload and a fake X plane serving the measured client
        list: the same answer the session gave, through the backend's own ratio and screen
        read (1920x1080 logical, a 1920x1080 X root, so ratio 1.0)."""
        b = self.backend(plasma=6)
        b._x = _FakeX(KWIN_675_CLIENTS)
        self.assertEqual(b._x_ratio(b._x), 1.0)
        self.assertEqual(b._xids(KWIN_675_RAW), KWIN_675_XIDS)

    def test_the_twins_are_told_apart_with_no_pid_on_the_x_side(self):
        """The measured pair differs by pid, which is the first filter and the easy case. An X
        client that publishes no _NET_WM_PID leaves the title tied as well -- and the rectangles
        and the two list orders still put each window on its own id."""
        anon = [dict(c, pid=0) for c in KWIN_675_CLIENTS]
        self.assertEqual(xid_match.match_xids(KWIN_675_RAW, anon, 1.0), KWIN_675_XIDS)

    def test_the_recorded_clients_stand_in_the_recorded_root_list_order(self):
        """The fixture is one session's bytes and its halves have to agree: the clients
        `_x_clients` read stand in the order `xprop -root _NET_CLIENT_LIST` printed on the same
        run (0xe00012, then 0x1000012), and the two twins run in that same order in the KWin
        payload (pid 2387 at `ix` 2, pid 2388 at `ix` 3). That agreement is what the next test
        leans on, and the fixture's tail holds an earlier run of the same pair where the ids
        came out the other way round and the two orders still agreed."""
        self.assertEqual(KWIN_675_ROOT_LIST, [0x00E00012, 0x01000012])
        self.assertEqual([c["xid"] for c in KWIN_675_CLIENTS], KWIN_675_ROOT_LIST)
        twins = sorted((w for w in KWIN_675_RAW if w["t"] == "fwtwin"), key=lambda w: w["ix"])
        self.assertEqual([w["p"] for w in twins], [c["pid"] for c in KWIN_675_CLIENTS])

    def test_the_client_list_order_is_read_as_an_order(self):
        """Reversing _NET_CLIENT_LIST reverses the answer: with the pids gone the two twins are
        separated by where they sit in the two lists. On the two recorded runs of this pair the
        X list and windowList() ran in the same order (the test above), so the tie-break lands
        on the right window; nothing here rests on that, only the direction does."""
        anon = [dict(c, pid=0) for c in reversed(KWIN_675_CLIENTS)]
        # the rectangles still hold the pair apart, so a reversed list pairs the same way ...
        self.assertEqual(xid_match.match_xids(KWIN_675_RAW, anon, 1.0), KWIN_675_XIDS)
        # ... and once the rectangles tie too, the order is the only key left
        flat = [dict(c, pid=0, geo=(718, 373, 484, 316)) for c in KWIN_675_CLIENTS]
        self.assertEqual(xid_match.match_xids(KWIN_675_RAW, flat, 1.0), KWIN_675_XIDS)
        self.assertEqual(xid_match.match_xids(KWIN_675_RAW, list(reversed(flat)), 1.0),
                         {"3afbbacc-28fd-4446-9de6-8dd6ad19c26c": 0x01000012,
                          "0b364979-1a8c-49be-bbc5-a4afb4539de2": 0x00E00012})


class XPropertyCorrectionTests(_Base):
    """5.27 hands the X id straight to the script, so nothing ever asked the
    X server about those windows -- and KWin's own answers are not the ones
    xdotool and wmctrl give."""

    class _FakeX:
        def __init__(self, cls=("xterm", "XTerm"), name=""):
            self.cls, self.name, self.asked = cls, name, []

        def get_wm_class(self, xid):
            self.asked.append(("class", xid))
            return self.cls

        def get_prop_string(self, xid, prop):
            self.asked.append((prop, xid))
            return self.name if prop == "_NET_WM_NAME" else ""

    def setUp(self):
        self.b = self.backend(plasma=5)      # only 5.27 has windowId

    def test_wm_class_case_comes_from_x(self):
        """kde-7: KWin 5.27 lower-cases resourceClass, so `wdotool
        getwindowclassname` on an xterm printed "xterm" where xdotool (and
        the X server) say "XTerm"."""
        self.kwin.find(UU["xterm"])["c"] = "xterm"      # what 5.27 reports
        self.kwin.find(UU["xterm"])["n"] = "xterm"
        self.b._x = self._FakeX()
        w = {x.id: x for x in self.b.list()}[WID["xterm"]]
        self.assertEqual((w.instance, w.class_), ("xterm", "XTerm"))

    def test_an_empty_caption_is_filled_from_x(self):
        """kde-tools kde-4: KWin leaves the caption empty for a client whose
        WM_NAME is COMPOUND_TEXT with no _NET_WM_NAME beside it."""
        self.kwin.find(UU["xterm"])["t"] = ""
        self.b._x = self._FakeX(name="a compound title")
        w = {x.id: x for x in self.b.list()}[WID["xterm"]]
        self.assertEqual(w.title, "a compound title")

    def test_a_caption_kwin_has_is_not_overwritten(self):
        x = self._FakeX(name="from the X server")
        self.b._x = x
        w = {y.id: y for y in self.b.list()}[WID["xterm"]]
        self.assertEqual(w.title, "test@kde: ~")
        self.assertNotIn(("_NET_WM_NAME", XTERM_XID), x.asked)

    def test_native_windows_are_left_alone(self):
        x = self._FakeX()
        self.b._x = x
        self.b.list()
        self.assertEqual([a for a in x.asked if a[1] != XTERM_XID], [])

    def test_no_x_server_is_not_an_error(self):
        self.b._x = None
        self.assertEqual(len(self.b.list()), 4)


class PointerTests(_Base):
    """B6 on KWin: getmouselocation used to fall through to the input
    daemon's model of the last position it injected -- which needs
    /dev/uinput open for a pure query and answers 0 0 after a restart."""

    def setUp(self):
        self.b = self.backend()

    def test_pointer_reads_the_compositor(self):
        self.kwin.cursor = (137, 42)
        self.assertEqual(self.b.pointer(), (137, 42))

    def test_pointer_is_the_hook_input_cmds_looks_for(self):
        from wdotool import input_cmds

        ctx = types.SimpleNamespace(backend=lambda: self.b)
        self.kwin.cursor = (11, 22)
        self.assertEqual(input_cmds._backend_pointer(ctx), (11, 22))

    def test_a_compositor_with_no_cursor_query_falls_back(self):
        """None, not an exception: the caller's fallback is the daemon."""
        self.kwin.cursor = None
        self.assertIsNone(self.b.pointer())

    def test_no_script_is_left_loaded(self):
        self.b.pointer()
        loaded = [c[2][0] for c in self.kwin.calls if c[1] == "loadScript"]
        self.assertEqual(len(self.kwin.unloaded), len(loaded))


class ViewStateTests(_Base):
    """States KWin reports that the View used to drop on the floor: BELOW is
    what `windowlower` leaves behind, so it has to be readable."""

    def setUp(self):
        self.b = self.backend()

    def _view(self, uuid):
        for v in self.b.views():
            if v.window.id == backend_kwin._wid(uuid):
                return v
        self.fail("no view for %s" % uuid)

    def test_below_and_skip_pager_reach_the_view(self):
        w = self.kwin.find(UU["kate"])
        w["kb"], w["sp"] = True, True
        v = self._view(UU["kate"])
        self.assertTrue(v.below)
        self.assertTrue(v.skip_pager)
        self.assertFalse(v.above)
        other = self._view(UU["xterm"])
        self.assertFalse(other.below)
        self.assertFalse(other.skip_pager)

    def test_a_lowered_window_reads_back_as_below(self):
        err = _capture_stderr()
        with err:
            self.b.lower(WID["kate"])
        self.assertTrue(self._view(UU["kate"]).below)


class ScriptTextTests(unittest.TestCase):
    """The one script has to cover both Plasma generations by itself."""

    def test_strips_kwins_duplicate_title_suffix_on_5_27(self):
        # caption on 5.27 is captionNormal + " <2>" + U+200E for a duplicate
        # title; 6 has captionNormal itself. Both must name the window what
        # X names it.
        self.assertIn(r"replace(/ <\d+>\u200e$/", kwin_js.SCRIPT)
        self.assertIn("captionNormal", kwin_js.SCRIPT)

    def test_covers_both_apis(self):
        js = kwin_js.SCRIPT
        for pair in (("workspace.windowList", "workspace.clientList"),
                     ("activeWindow", "activeClient"),
                     ("w.desktops", "w.desktop >"),
                     ("captionNormal", "w.caption"),
                     ("windowAdded", "clientAdded"),
                     ("windowRemoved", "clientRemoved"),
                     ("windowActivated", "clientActivated"),
                     ("raiseWindow", "slotWindowRaise")):
            for token in pair:
                self.assertIn(token, js)

    def test_raise_clears_keep_below(self):
        """The Python fake mirrors the JS; this is the JS itself."""
        js = kwin_js.SCRIPT
        body = js[js.index('if (op === "raise")'):js.index('if (op === "lower")')]
        self.assertIn("w.keepBelow = false;", body)

    def test_the_state_answer_carries_the_x_id(self):
        js = kwin_js.SCRIPT
        self.assertIn("function xnum(w)", js)
        body = js[js.index('if (op === "state")'):js.index('if (op === "desktop")')]
        self.assertEqual(body.count("xid: xnum(w)"), 2)

    def test_the_cursor_op_uses_the_property_both_releases_have(self):
        js = kwin_js.SCRIPT
        self.assertIn("workspace.cursorPos", js)
        self.assertIn('if (op === "cursor")', js)

    def test_never_uses_the_globals_kwin_does_not_have(self):
        # print() is gone from KWin's script globals in 5.27 and 6 alike
        self.assertNotIn("print(", kwin_js.SCRIPT)

    def test_answers_on_every_path(self):
        js = kwin_js.SCRIPT
        self.assertIn("_ret({ok: true", js)
        self.assertIn("_ret({ok: false", js)
        self.assertIn("} catch (e) {", js)

    def test_resets_the_clamps_before_writing_geometry(self):
        js = kwin_js.SCRIPT
        body = js[js.index("function geom("):js.index("function activate(")]
        self.assertIn("unclamp(w)", body)
        unclamp = js[js.index("function unclamp("):js.index("function geom(")]
        for token in ("setMaximize(false, false)", "w.tile = null",
                      "w.fullScreen = false"):
            self.assertIn(token, unclamp)
        # unconditionally: `if (w.maximizeMode)` is always false on 5.27
        # (no such property), which left a maximized window maximized while
        # its geometry was written out from under it
        self.assertNotIn("if (w.maximizeMode)", _code(unclamp))

    def test_maximize_mode_is_derived_where_the_property_is_missing(self):
        # 5.27's window.h declares no maximizeMode Q_PROPERTY at all (only
        # 6 has one), so reading it as a number reported every window as
        # restored -- and every setMaximize() cleared the other axis.
        js = kwin_js.SCRIPT
        self.assertIn("function mmode(w)", js)
        mmode = js[js.index("function mmode(w)"):js.index("function info(w)")]
        self.assertIn("typeof m === \"number\"", mmode)
        self.assertIn("maxArea(w)", mmode)
        # not "equal to the maximize area": KWin honours an X11 client's
        # size increments and centres the remainder, so a maximized xterm
        # is 1918x1033 at (1,0) of a 1920x1036 area
        self.assertIn("function covers(gp, gs, ap, as)", js)
        self.assertIn("covers(num(g.y), num(g.height)", mmode)
        self.assertIn("covers(num(g.x), num(g.width)", mmode)
        # ...and a fullscreen frame is bigger than the maximize area, so it
        # would otherwise report MAXIMIZED_HORZ for any fullscreen window
        self.assertIn("if (w.fullScreen) {", mmode)
        self.assertIn("KWin.MaximizeArea", kwin_js.SCRIPT)
        self.assertIn("workspace.clientArea(opt, w)", kwin_js.SCRIPT)
        # nothing reads the raw property anywhere else
        self.assertEqual(_code(js).count("w.maximizeMode"), 1)
        for fn, end in (("function readState(", "var _STATE_PROPS"),
                        ("function writeState(", "/* The signals")):
            body = js[js.index(fn):js.index(end)]
            self.assertIn("mmode(w)", body)

    def test_the_events_script_unloads_itself_when_orphaned(self):
        # A killed wdotool (Ctrl-C, OOM, a crash) cannot unload the resident
        # events script, and its pluginName -- the only handle KWin offers --
        # died with it. What was left ran for the session, connected to every
        # window's signals, sending a D-Bus call per frame of every drag. The
        # script now watches its owner and unloads itself when the answers
        # stop; the Python side answers every call on that connection, so a
        # busy owner just answers late.
        js = kwin_js.SCRIPT
        hook = js[js.index("function hookEvents()"):js.index("var WATCH_MS")]
        self.assertIn("watchOwner();", hook)
        watch = js[js.index("function watchOwner()"):js.index("function main()")]
        for token in ("new QTimer()", "wd.interval = WATCH_MS",
                      '"Ping", A.token', "missed >= WATCH_MISSES",
                      '"unloadScript", String(A.plugin)'):
            self.assertIn(token, watch)
        # no QTimer (a KWin build without it): exactly the old behaviour
        self.assertIn("} catch (e) { }", watch)

    def test_a_state_write_waits_for_the_window_to_agree(self):
        # a Wayland client applies fullscreen/maximize on the configure ack,
        # so the read-back inside the same run is stale and warning on it is
        # a false alarm on every single windowstate command
        js = kwin_js.SCRIPT
        for token in ("var _WATCH = {", "fullScreenChanged",
                      "maximizedChanged", "frameGeometryChanged",
                      "function later(w, s, want)", "new QTimer()",
                      "t.singleShot = true", "t.timeout.connect",
                      "num(A.settle)", "return DEFER;",
                      "if (_v !== DEFER)"):
            self.assertIn(token, js)
        # answered exactly once, whichever of the two gets there first
        self.assertIn("if (_done) {", js)
        state = js[js.index('if (op === "state") {'):
                   js.index('if (op === "desktop") {')]
        self.assertIn("settled: true", state)
        self.assertIn("settled: false", state)

    def test_the_output_property_is_only_read_on_plasma_6(self):
        # KWin::Output* has no QJSEngine converter on 5.27: *reading* it logs
        # "QMetaProperty::read: Unable to handle unregistered datatype" into
        # the journal once per window per command.
        js = kwin_js.SCRIPT
        self.assertIn("var out = SIX ? w.output : null;", js)
        self.assertEqual(_code(js).count("w.output"), 1)


class DetectTests(_Base):
    """A KWin failure is fatal: KWin implements neither wlr-foreign-toplevel
    nor ext-foreign-toplevel-list, so nothing below it could ever work."""

    def setUp(self):
        backend_detect.reset()
        self._wlr = backend_detect._wlr
        self.wlr_calls = []

        def no_wlr():
            self.wlr_calls.append(1)
            raise CmdError("wlr: no foreign-toplevel")
        backend_detect._wlr = no_wlr
        self.addCleanup(self._restore)

    def _restore(self):
        backend_detect._wlr = self._wlr
        backend_detect.reset()

    def test_kwin_is_detected_and_reuses_the_connection(self):
        kwin = KwinScriptingService(self.mock.address)
        self.addCleanup(kwin.close)
        b = backend_detect.detect()
        self.addCleanup(backend_detect.reset)
        self.assertEqual(b.name, "kwin")
        self.assertIs(b.bus, backend_detect.session_bus())
        self.assertEqual(len(b.list()), 4)
        self.assertEqual(self.wlr_calls, [])

    def test_a_kwin_failure_never_falls_through_to_wlr(self):
        kwin = KwinScriptingService(self.mock.address, refuse_load=True)
        self.addCleanup(kwin.close)
        b = backend_detect.detect()
        with self.assertRaises(CmdError) as cm:
            b.list()
        self.assertIn("kwin backend", str(cm.exception))
        self.assertEqual(self.wlr_calls, [])

    def test_a_constructor_failure_is_the_error_the_user_sees(self):
        kwin = KwinScriptingService(self.mock.address)
        self.addCleanup(kwin.close)
        real = backend_detect._kwin

        def broken():
            raise CmdError("kwin backend: KWin refused to load a script")
        backend_detect._kwin = broken
        try:
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
        finally:
            backend_detect._kwin = real
        self.assertIn("refused to load a script", str(cm.exception))
        self.assertEqual(self.wlr_calls, [])

    def test_no_kwin_name_is_a_session_error(self):
        kwin = KwinScriptingService(self.mock.address, own_name=False)
        self.addCleanup(kwin.close)
        bus = Bus(self.mock.address)
        self.addCleanup(bus.close)
        with self.assertRaises(CmdError) as cm:
            KwinBackend(bus=bus)
        self.assertIsInstance(cm.exception, NoSessionError)
        self.assertIn("org.kde.KWin", str(cm.exception))

    def test_env_override_reaches_the_kwin_backend(self):
        kwin = KwinScriptingService(self.mock.address)
        self.addCleanup(kwin.close)
        os.environ["WDOTOOL_BACKEND"] = "kwin"
        try:
            self.assertEqual(backend_detect.detect().name, "kwin")
        finally:
            os.environ.pop("WDOTOOL_BACKEND", None)
            backend_detect.reset()


class ScriptDeadlineTests(_Base):
    """T35 / fix 33: the deadline `_script()` computes has to cover everything
    it does, not just run().

    `_script(timeout=T)` sets one deadline and then made three D-Bus calls that
    ignored it -- two Introspects and loadScript, each on CALL_TIMEOUT (10 s) --
    before handing run() `max(0.1, deadline - now)`. A KWin busy enough to be
    slow on those three (compositing a full-screen video, a stalled GPU) burned
    the whole budget outside the script and then reported the *script* at fault:
    30 s of waiting nobody asked for, ending in a wrong diagnosis."""

    def _timeouts(self, b):
        """Record (member, timeout) for every D-Bus call the backend makes."""
        seen = []
        orig = b.bus.call

        def spy(dest, path, iface, member, sig="", args=(), timeout=None,
                **kw):
            seen.append((member, timeout))
            return orig(dest, path, iface, member, sig, args, timeout=timeout,
                        **kw)

        b.bus.call = spy
        return seen

    def test_the_deadline_covers_the_introspects_and_the_load(self):
        b = self.backend(plasma=6)
        b.script_timeout = 0.5
        seen = self._timeouts(b)
        self.assertEqual(len(b.list()), 4)
        wanted = [(m, t) for m, t in seen
                  if m in ("Introspect", "loadScript", "run")]
        self.assertTrue(wanted, seen)
        for member, timeout in wanted:
            with self.subTest(member=member):
                self.assertIsNotNone(timeout, member)
                # inside the caller's budget, and nowhere near CALL_TIMEOUT
                self.assertLessEqual(timeout, 0.5, member)
                self.assertGreater(timeout, 0.0, member)
        self.assertEqual(backend_kwin.CALL_TIMEOUT, 10.0)
        self.assertIn("Introspect", [m for m, _t in wanted])
        self.assertIn("loadScript", [m for m, _t in wanted])

    def test_a_slow_kwin_fails_inside_the_budget_and_leaves_nothing_loaded(self):
        """0.3 s on every Introspect and loadScript against a 0.5 s budget.

        The wall clock is not the assertion here -- the fake sleeps in one
        serving thread, so a call that times out on our side still holds the
        next one up, and the total drifts between 0.6 s and 5 s depending on
        what else the runner is doing. What is exact is that no single call
        was ever given longer than the caller's budget: before the fix the
        three of them were on CALL_TIMEOUT, 10 s each, and a 0.5 s `list()`
        could have cost 30 s before run() started."""
        b = self.backend(slow=0.3)
        seen = self._timeouts(b)
        b.script_timeout = 0.5
        with self.assertRaises(CmdError) as cm:
            b.list()
        asked = [t for m, t in seen
                 if m in ("Introspect", "loadScript", "run")]
        self.assertTrue(asked, seen)
        self.assertLessEqual(max(asked), 0.5, seen)
        self.assertEqual(self.kwin.loaded, set())
        # the call that ran out of time is named; the script is not blamed for
        # a budget that was spent before it ran
        self.assertIn("loadScript", str(cm.exception))
        self.assertNotIn("sent no result", str(cm.exception))

    def test_a_slow_kwin_inside_a_generous_budget_still_answers(self):
        """The same 0.3 s per call with 5 s to spend: four windows, no error --
        the deadline shortens the calls, it does not refuse them."""
        b = self.backend(slow=0.3)
        seen = self._timeouts(b)
        b.script_timeout = 5.0
        self.assertEqual(len(b.list()), 4)
        asked = [t for m, t in seen
                 if m in ("Introspect", "loadScript", "run")]
        self.assertLessEqual(max(asked), 5.0, seen)
        self.assertGreater(min(asked), 0.3, seen)

    def test_a_silent_kwin_costs_the_script_budget_and_no_more(self):
        """A KWin that takes the script, answers run() and then says nothing:
        the whole cost is the script budget. With the shipped SCRIPT_TIMEOUT
        that is 10 s, not 10 plus three D-Bus calls' worth. Measured here with
        a 0.6 s budget: 0.60 s."""
        b = self.backend(silent=True)
        b.script_timeout = 0.6
        start = time.monotonic()
        with self.assertRaises(CmdError):
            b.list()
        waited = time.monotonic() - start
        self.assertLess(waited, 2.0, "%.2fs" % waited)
        self.assertEqual(self.kwin.loaded, set())
        self.assertEqual(backend_kwin.SCRIPT_TIMEOUT, 10.0)


class RootScriptFileTests(_Base):
    """T35: what `_write_source()` does about the script file's owner.

    The file carries the reply token, the window titles and the search patterns
    of the command, and whoever can read the token can answer in KWin's place.
    As root (the sudo path) KWin still reads it as the session user, so the file
    is handed to that user -- and only opened to everybody when there is no user
    to hand it to."""

    def setUp(self):
        self.b = self.backend()

    def _write_as_root(self, owner):
        """_write_source() with euid 0 and `owner` as the bus owner (an
        exception class means _owner_uid() raises)."""
        calls = []
        if isinstance(owner, type) and issubclass(owner, Exception):
            def owner_uid():
                raise owner("no such name")
        else:
            def owner_uid():
                return owner
        self.b.bus._owner_uid = owner_uid
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("os.chown", side_effect=lambda *a: calls.append(
                    ("chown",) + a)), \
                mock.patch("os.chmod", side_effect=lambda *a: calls.append(
                    ("chmod",) + a)):
            path = self.b._write_source("// nothing")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path, calls

    def test_as_root_the_file_is_chowned_to_the_bus_owner(self):
        path, calls = self._write_as_root(1000)
        self.assertEqual(calls, [("chown", path, 1000, -1)])
        # mkstemp's own mode, left alone: nobody else may read it
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_as_root_with_no_owner_it_falls_back_to_0644(self):
        """An unreadable script file would fail the call outright, so when
        there is no uid to hand it to the file is opened instead -- the one
        case where the token is world-readable, and it is deliberate."""
        path, calls = self._write_as_root(DBusError)
        self.assertEqual(calls, [("chmod", path, 0o644)])

    def test_as_a_user_neither_happens(self):
        calls = []
        with mock.patch("os.geteuid", return_value=1000), \
                mock.patch("os.chown",
                           side_effect=lambda *a: calls.append(a)), \
                mock.patch("os.chmod",
                           side_effect=lambda *a: calls.append(a)):
            path = self.b._write_source("// nothing")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertEqual(calls, [])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class ScriptLockTests(_Base):
    """T35: the advisory lock over load->run.

    KWin hands back a script id the moment a lower-numbered script is unloaded,
    so two wdotools sharing a runtime dir must not interleave one's unload with
    the other's load. The lock is best effort by design: a runtime dir that
    cannot be written to is not a reason to refuse the command, because the id
    check in _load_run_locked is what makes the result correct."""

    LOCK = "wdotool-kwin-script.lock"

    def setUp(self):
        self.b = self.backend()

    def test_the_lock_file_is_0600_under_the_runtime_dir(self):
        path = os.path.join(self.rtdir, self.LOCK)
        if os.path.exists(path):
            os.unlink(path)
        self.assertEqual(len(self.b.list()), 4)
        self.assertTrue(os.path.exists(path), os.listdir(self.rtdir))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_a_read_only_runtime_dir_still_lists(self):
        ro = tempfile.mkdtemp(prefix="wdotool-kwin-ro-")
        self.addCleanup(shutil.rmtree, ro, True)
        os.chmod(ro, 0o500)
        self.addCleanup(os.chmod, ro, 0o700)
        with support.env(XDG_RUNTIME_DIR=ro):
            self.assertEqual(len(self.b.list()), 4)
        self.assertEqual(os.listdir(ro), [])


class _FakeX:
    """Stands in for wdotool.x11_mini.X11Conn in the id-matching tests.

    `root_size` is the X root's own geometry -- the whole layout in device
    pixels, which KwinBackend._x_ratio divides by workspace.virtualScreenSize
    (the same layout in logical pixels) to learn what units the client rects
    are in."""

    ROOT = 0x1a5

    def __init__(self, clients, root_size=(1920, 1080)):
        self.clients = clients
        self.root_size = root_size

    def root(self):
        return self.ROOT

    def client_list(self):
        return [c["xid"] for c in self.clients]

    def _find(self, xid):
        return [c for c in self.clients if c["xid"] == xid][0]

    def get_wm_class(self, xid):
        c = self._find(xid)
        return c["inst"], c["cls"]

    def get_prop_string(self, xid, name):
        return self._find(xid)["name"] if name == "_NET_WM_NAME" else ""

    def get_geometry(self, xid):
        if xid == self.ROOT:
            return (0, 0) + tuple(self.root_size)
        return self._find(xid)["geo"]

    def get_pid(self, xid):
        return self._find(xid)["pid"]


def _capture_stderr():
    import contextlib
    import io

    class _Ctx(io.StringIO):
        def __enter__(self):
            self._redirect = contextlib.redirect_stderr(self)
            self._redirect.__enter__()
            return self

        def __exit__(self, *exc):
            return self._redirect.__exit__(*exc)

    return _Ctx()


class TheMatcherMoved(unittest.TestCase):
    """U32: `wdotool/xid_match.py` is `backend_kwin`'s matcher, moved and not rewritten.

    Every wlroots-family backend has the same pairing to do -- a compositor toplevel list with no X ids on one
    side, `_NET_CLIENT_LIST` on the other -- and none of them is KWin: labwc, Budgie, Xfce-on-Wayland, Wayfire
    and Hyprland all listed an xterm whose real X id is `0x40000c` as a synthetic id with the wrong WM_CLASS
    [M recon2/labwc.md §4, budgie.md, xfce-wayland.md, wayfire.md §2.4, hyprland.md §3]. The 25 matcher tests
    above are the proof that the move changed nothing: they run against the new module through the name
    backend_kwin still imports it under, and this pins that they are the same object."""

    def test_backend_kwin_imports_the_moved_function(self):
        self.assertIs(backend_kwin._match_xids, xid_match.match_xids)

    def test_the_module_carries_no_copy_of_its_own(self):
        """A second definition left behind in backend_kwin.py would keep every test above green while the
        wlroots backends read a matcher nobody exercises."""
        with open(os.path.join(ROOT, "wdotool", "backend_kwin.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("def _match_xids(", src)
        self.assertNotIn("def _simplified(", src)
        self.assertIn("from wdotool.xid_match import match_xids", src)

    def test_it_imports_without_backend_kwin(self):
        """The point of the move: a backend that has no D-Bus, no KWin scripting and no Qt gets the matcher
        without dragging 1200 lines of KWin in behind it."""
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; from wdotool import xid_match; "
             "print('wdotool.backend_kwin' in sys.modules); "
             "print(xid_match.match_xids([], []))"],
            capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["False", "{}"])

    def test_the_shared_minted_id_range_is_one_definition(self):
        """KWin's ids and the ids Hyprland and COSMIC will mint have to be in the same range and out of
        Xwayland's, or a native id in a listing could be read as an X window's."""
        self.assertIs(backend_kwin._ID_BASE, backend.ID_BASE)
        self.assertIs(backend_kwin._ID_MASK, backend.ID_MASK)
        self.assertEqual(backend.ID_BASE, 0x40000000)
        self.assertEqual(backend.ID_BASE | backend.ID_MASK, 0x7FFFFFFF)
        for uuid in ("c0ffee00-1111-2222-3333-444444444444", "0", "ffffffff-0-0-0-0"):
            self.assertEqual(backend_kwin._wid(uuid) & ~backend.ID_MASK, backend.ID_BASE, uuid)


class MintedIds(unittest.TestCase):
    """`backend.mint_id`: the id a backend whose compositor publishes none has to make up.

    Three do: KWin's handle is a uuid string (and keeps its own older mint, from the uuid's hex), Hyprland's is
    `address` -- a heap pointer, because `stableId` does not exist before 0.56 [M recon2/arch.md, hyprland
    fixtures] -- and COSMIC's is `identifier`, 32 base62 characters [M recon2/cosmic.md §4]. The property that
    matters is the one the wlr floor's arrival-order ids do not have: the id survives another window closing
    [M recon2/hyprland.md §3: `0x000f4241` became `0x000f4240`]."""

    #: two of the recorded COSMIC identifiers and two recorded Hyprland addresses
    KEYS = ("LsUebsS7Qe8NowEoh7IP065Bbw8xd69L", "2eoqv7wMaz7wrTrUQORF0E7otL6j0vF9",
            "0x59daae6de8f0", "0x59daae933ac0")

    def test_an_id_is_stable_and_in_range(self):
        for key in self.KEYS:
            wid = backend.mint_id(key)
            self.assertEqual(wid, backend.mint_id(key), key)
            self.assertEqual(wid & ~backend.ID_MASK, backend.ID_BASE, key)
            self.assertLess(wid, 1 << 31, key)

    def test_two_windows_of_one_session_do_not_share_an_id(self):
        ids = {backend.mint_id(k) for k in self.KEYS}
        self.assertEqual(len(ids), len(self.KEYS))

    def test_addresses_that_differ_in_the_low_bits_alone_still_separate(self):
        """Hyprland's handles are heap pointers: two windows opened in a row differ by 0x20, and a mint that
        sliced the front of the string would give them the same id."""
        base = 0x59DAAE6DE8F0
        ids = {backend.mint_id("0x%x" % (base + 0x20 * i)) for i in range(16)}
        self.assertEqual(len(ids), 16)

    def test_an_empty_handle_is_zero_and_not_an_id(self):
        self.assertEqual(backend.mint_id(""), 0)
        self.assertEqual(backend.mint_id(None), 0)

    def test_a_salt_re_mints_into_a_different_id(self):
        for key in self.KEYS:
            self.assertNotEqual(backend.mint_id(key), backend.mint_id(key, 1), key)
            self.assertEqual(backend.mint_id(key, 1) & ~backend.ID_MASK, backend.ID_BASE)

    def test_a_collision_leaves_every_window_addressable(self):
        """30 bits is about 1e-6 odds of two live windows colliding; a plain dict comprehension would drop one
        of them and leave it with no id at all -- unlistable and unaddressable."""
        keys = list(self.KEYS)
        table = backend.mint_map(keys)
        self.assertEqual(sorted(table), sorted(keys))
        self.assertEqual(len(set(table.values())), len(keys))

        # A real 30-bit collision needs ~40k handles to find, so the first mint is forced by a stand-in. What
        # is asserted about it is mint_map's rule and not the stand-in's numbers: three handles mint to one
        # id, and the third clashes again on its first re-mint, so the walk has to keep going rather than
        # stop at salt 1. The salt arithmetic itself is mint_id's, pinned by the test above.
        real = backend.mint_id
        forced = {(keys[0], 0): 1,
                  (keys[1], 0): 1, (keys[1], 1): 2,
                  (keys[2], 0): 1, (keys[2], 1): 2, (keys[2], 2): 3}
        calls = []

        def colliding(key, salt=0):
            calls.append((key, salt))
            low = forced.get((key, salt))
            return real(key, salt) if low is None else backend.ID_BASE | low

        with mock.patch.object(backend, "mint_id", colliding):
            table = backend.mint_map(keys)
        self.assertEqual(sorted(table), sorted(keys))             # nobody dropped
        self.assertEqual(len(set(table.values())), len(keys))     # nobody sharing
        self.assertEqual(table[keys[0]], backend.ID_BASE | 1)     # first in the list keeps the mint
        self.assertEqual(table[keys[1]], backend.ID_BASE | 2)
        self.assertEqual(table[keys[2]], backend.ID_BASE | 3)     # walked past a second clash
        # and it walked one at a time from 0, never skipping and never asking the same salt twice
        self.assertEqual([salt for key, salt in calls if key == keys[2]], [0, 1, 2])

    def test_a_repeated_handle_is_one_window(self):
        """One handle listed twice is one window and not a collision with itself: it keeps its own mint
        rather than being salted out of the way of the id it already holds."""
        table = backend.mint_map(list(self.KEYS) + [self.KEYS[0]])
        self.assertEqual(len(table), len(self.KEYS))
        self.assertEqual(table[self.KEYS[0]], backend.mint_id(self.KEYS[0]))


if __name__ == "__main__":
    unittest.main()
