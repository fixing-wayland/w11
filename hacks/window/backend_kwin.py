"""KWin window backend: one generated KWin script per command, over dbus_mini.

Nothing has to be installed for the user here (unlike the GNOME bridge):
`org.kde.kwin.Scripting.loadScript()` is plain Q_SCRIPTABLE on /Scripting with
no polkit action and no bus policy on Plasma 5.27 and 6 alike, so any client on
the session bus may push JavaScript into KWin. The script (wdotool.kwin_js)
cannot register a D-Bus object or return a value, so it answers with the JS
global callDBus() to a name *we* own:

    name  org.w11.KWin   path /org/w11/KWin
    iface org.w11.KWin1  members Result(s token, s json),
                                Event(s token, s uuid, s change)

Wire sequence per command (four round trips, one temp file):

    id = loadScript(file, "wdotool-<pid>-<seq>-<rand>")   /Scripting
    run()                                   /Scripting/Script<id> (6)
                                            /<id>                 (5.27)
    unloadScript("wdotool-<pid>-<seq>-<rand>")            /Scripting, finally

`Script::run()` is a *delayed* D-Bus reply: KWin sends it only after the file
has been read and QJSEngine::evaluate() has returned, and the script's own
callDBus() rides the same connection during evaluate(). D-Bus preserves
per-connection order, so the payload is already in our receive queue when run()
returns -- no dbus-monitor, no sleep. We still wait with a deadline rather than
assuming it (a script that throws before _ret, a syntax error, KWin refusing
the file: all of them look like silence).

Each call gets its own pluginName (KWin returns -1 for a name that is already
loaded, and reuses script *ids* as soon as a script is destroyed, so a shared
name would eventually drive somebody else's script) and its own token (a stale
payload from an earlier, timed-out call must not be read as this call's
answer). The name carries random bytes as well as the pid and a counter: KWin
keeps a name reserved for as long as the script object lives and loaded
scripts cannot be enumerated, so a wdotool killed between loadScript and
unloadScript leaks its name for the rest of the session -- with the pid alone
the next process to be given that pid would fail on its very first command,
for ever.

Window identity: KWin's only stable handle is `internalId`, a UUID string, and
the scripting API takes nothing else. wdotool prints numeric ids, so one is
minted from the uuid (see _wid: 30 bits of it, in a range Xwayland never hands
a client) and `self._uuids` maps it back; a miss re-reads the window list. Two
uuids colliding in those 30 bits is a one-in-a-million session, and _id_map
re-mints the second window rather than dropping it out of the listing.
`visible` = not minimized, not hidden and on the current desktop (X11's
IsViewable), like the GNOME backend.

No script at all for: the current desktop and the desktop count
(org.kde.KWin.currentDesktop / VirtualDesktopManager.count), the workspace list
(VirtualDesktopManager.desktops), show-desktop, and select_window() --
org.kde.KWin.queryWindowInfo() *is* xdotool's selectwindow, KWin's own
interactive window picker, answering with a map that carries the uuid.

Titles are `captionNormal` on 6 and `caption` with KWin's " <2>" duplicate-
title suffix stripped on 5.27 (which has no captionNormal property), so the
same window is named the same on both, and the same as X names it.

Capability gaps, all documented in docs/WDOTOOL.md: Plasma 6 removed window shading
(SHADED is a gap there, works on 5.27), neither release has a per-window
lower -- lowering a window that is not active falls back to keep-below -- and
KWin caps the number of virtual desktops (20 on 5.27, 25 on 6), which
set_num_desktops reports rather than looping against.
"""

import contextlib
import fcntl
import json
import os
import re
import struct
import tempfile
import time

from w11common import session
from w11common.dbus_mini import (ERR, METHOD_CALL, NAME_FLAG_DO_NOT_QUEUE,
                                 NO_REPLY_EXPECTED, Bus, DBusError, no_bus_text)
from w11common.errors import CmdError
from hacks.window import kwin_js
from hacks.window.backend import ID_BASE as _ID_BASE, ID_MASK as _ID_MASK, ID_SALT_STEP
from hacks.window.backend import View, Window, WindowBackend, Workspace, warn as _warn
# The XWayland matcher moved to a module of its own (wdotool/xid_match.py): every wlroots-family
# backend has the same pairing to do and none of them is KWin. Nothing about it changed.
from hacks.window.xid_match import match_xids as _match_xids
from wdotool.ctx import NoSessionError

KWIN_NAME = "org.kde.KWin"
KWIN_PATH = "/KWin"
KWIN_IFACE = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_IFACE = "org.kde.kwin.Scripting"
SCRIPT_IFACE = "org.kde.kwin.Script"
VD_PATH = "/VirtualDesktopManager"
VD_IFACE = "org.kde.KWin.VirtualDesktopManager"

BUS_NAME = "org.w11.KWin"
EVENTS_NAME = "org.w11.KWin.Events"
OBJECT_PATH = "/org/w11/KWin"
IFACE = "org.w11.KWin1"

CALL_TIMEOUT = 10.0             # plain KWin D-Bus calls answer in milliseconds
SCRIPT_TIMEOUT = 10.0           # load + run + the script's own reply
SETTLE_MS = 1000                # how long the script waits for a state to land
HEADER = "/* wdotool-kwin 1 "   # first line of every generated script

# KWin's NET::WindowType (netwm_def.h) -> the Mutter window-type names the
# View contract speaks (wxprop maps them back to _NET_WM_WINDOW_TYPE atoms).
_WINDOW_TYPES = {
    0: "NORMAL", 1: "DESKTOP", 2: "DOCK", 3: "TOOLBAR", 4: "MENU",
    5: "DIALOG", 7: "MENU", 8: "UTILITY", 9: "SPLASHSCREEN",
    10: "DROPDOWN_MENU", 11: "POPUP_MENU", 12: "TOOLTIP", 13: "NOTIFICATION",
    14: "COMBO", 15: "DND", 16: "NOTIFICATION", 17: "NOTIFICATION",
    18: "POPUP_MENU",
}
# _NET_WM_STATE atoms KWin has no setter for at all
_GAP_REASONS = {"SHADED": "Plasma 6 removed window shading"}
# How many times a load may lose the id race before it is called a fight
_LOAD_ATTEMPTS = 8
# select_window()'s deadline: see _select_timeout()
SELECT_TIMEOUT = 120.0


_UNSET = object()   # "not resolved yet", distinct from "no answer"


class KwinBackend(WindowBackend):
    name = "kwin"
    # wmctrl -m: what KWin writes into _NET_SUPPORTING_WM_CHECK's _NET_WM_NAME
    # on the X root, so the answer is the same with or without Xwayland up.
    wm_name = "KWin"

    def __init__(self, bus: Bus | None = None, names: list[str] | None = None):
        """`bus`/`names`: reuse backend_detect's connection and its ListNames
        result (one round trip per process)."""
        if bus is None:
            try:
                bus = Bus()
            except DBusError as e:
                raise NoSessionError("kwin backend: %s" % no_bus_text(e)) from None
        self.bus = bus
        if names is None:
            try:
                names = self.bus.list_names()
            except DBusError as e:
                raise CmdError("kwin backend: ListNames failed: %s" % e) from None
        if KWIN_NAME not in names:
            raise NoSessionError("kwin backend: %s is not on the session bus "
                                 "(no KWin/Plasma session?)" % KWIN_NAME)
        # Serve the script's callDBus ourselves: with serve_calls left False,
        # dbus_mini would answer the payload with UnknownMethod and drop it.
        self.bus.serve_calls = True
        self.dest = _own(self.bus, BUS_NAME)
        self.script_timeout = SCRIPT_TIMEOUT
        self._seq = 0
        self._kwin_name = _UNSET           # KWin's unique bus name, resolved once
        self._path_shape = ""              # "scripting" (6) / "root" (5.27)
        self._uuids: dict[int, str] = {}   # printed id -> KWin internalId
        self._screen = None                # cached screen size + work areas
        self._x = "unset"                  # lazy X11Conn for XWayland ids

    # -- transport ----------------------------------------------------------

    def _source(self, dest: str, token: str, op: str, **kw) -> str:
        """The script text: one JSON line naming the call, then the constant.

        The same JSON is repeated as a comment on the first line so that a file left behind by a crash explains
        itself (and so the tests can drive a fake KWin without a JS engine)."""
        args = dict(kw, op=op, token=token, dest=dest, path=OBJECT_PATH, iface=IFACE)
        # ensure_ascii: a JSON string may carry U+2028/9 raw, a JS string
        # literal may not (before ES2019, and KWin 5.27 is Qt 5).
        blob = json.dumps(args, ensure_ascii=True, sort_keys=True)
        return "%s%s */\nvar A = %s;\n%s" % (HEADER, blob, blob, kwin_js.SCRIPT)

    def _call(self, path: str, iface: str, member: str, sig: str = "",
              args=(), timeout: float | None = CALL_TIMEOUT, flags: int = 0):
        try:
            return self.bus.call(KWIN_NAME, path, iface, member, sig, args, timeout=timeout, flags=flags)
        except DBusError as e:
            raise _map_error(member, e) from None
        except (ValueError, OverflowError, struct.error) as e:
            # An argument the wire format cannot carry (a desktop number or a window id that is negative or
            # wider than its D-Bus type): one line, rc 1, never a marshalling traceback (B8). backend_gnome has
            # answered this way all along; `wwmctl -s 4294967296` was a traceback on KDE and a message on GNOME.
            raise CmdError("kwin backend: %s: invalid argument: %s" % (member, e)) from None

    def _script(self, op: str, timeout: float | None = None, **kw):
        """Run one operation inside KWin and return its `v` payload."""
        timeout = self.script_timeout if timeout is None else timeout
        seq = self._seq
        self._seq += 1
        token = "%d-%d-%s" % (os.getpid(), seq, os.urandom(4).hex())
        plugin = _plugin_name(seq)
        deadline = time.monotonic() + timeout
        # The pluginName is in the source (the events script unloads itself by
        # it), so the text is built per attempt: an id race renames the load.
        plugin = self._load_run(
            plugin,
            lambda name: self._source(self.dest, token, op, plugin=name, **kw),
            deadline)
        if op == "events":
            # the script stays loaded; the caller unloads it when it is done
            return plugin
        try:
            raw = self._collect(self.bus, token, deadline)
        finally:
            self.unload(plugin)
        return _payload(raw)

    def _live_script_ids(self, deadline: float | None = None):
        """The script ids that own a D-Bus object right now, read off the child nodes of the scripting object;
        None when neither shape answered.

        Plasma 6 registers `/Scripting/Script<n>`, 5.27 `/<n>` among KWin's other root objects, and the shape is
        remembered once a run() has landed so the second introspection is paid for at most once.

        `deadline` is the whole operation's, not this call's: these two introspections and the loadScript that
        follows them used to take CALL_TIMEOUT each (10 s) whatever the caller asked for, so a `_script()` given
        0.5 s could spend 30 s here and then hand run() its `max(0.1, ...)` floor and report the compositor hung
        -- a wrong diagnosis of a KWin that was merely slow, after a wait nobody asked for."""
        out, got = set(), False
        def left():
            if deadline is None:
                return CALL_TIMEOUT
            return max(0.1, deadline - time.monotonic())
        if self._path_shape != "root":
            try:
                xml = self.bus.introspect(KWIN_NAME, SCRIPTING_PATH, timeout=left())
                out |= {int(n) for n in re.findall(r'<node name="Script(\d+)"', xml)}
                got = True
            except DBusError:
                pass
        if self._path_shape != "scripting":
            try:
                xml = self.bus.introspect(KWIN_NAME, "/", timeout=left())
                out |= {int(n) for n in re.findall(r'<node name="(\d+)"', xml)}
                got = True
            except DBusError:
                pass
        return frozenset(out) if got else None

    def _load_run(self, plugin: str, make_source, deadline: float) -> str:
        """loadScript + run(); returns the pluginName the script that ran is loaded under, which is `plugin`
        unless an id race renamed it.

        `make_source(name)` builds the script text for a given pluginName -- a callable rather than a string
        because the name is baked into the source (the resident events script unloads itself by it) and a race
        changes it.

        `loadScript` answers with `m_scripts.size()`, so an id comes back round the moment a *lower*-numbered
        script is unloaded while a higher-numbered one is still alive: the count lands back on a live index,
        KWin fails to register the new object at that path (silently -- the id is still returned), and run()
        there drives the OTHER script. Measured on Plasma 6.6: load A -> 0, B -> 1, unload A, load C -> 1, and
        /Scripting/Script1 is still B. Ten concurrent windowmoves lost seven that way.

        So the id is checked against the objects that existed before the load -- one round trip, next to a file
        read and a JS evaluation. On a collision the colliding script is left LOADED as padding (unloading it
        would hand the same index straight back) and another is loaded, which lands on the next index up; the
        padding goes away with the files."""
        padding: list[str] = []
        files: list[str] = []
        try:
            # Held across load+run only. Two wdotools sharing a runtime dir cannot then interleave a load with
            # an unload at all, which is the whole of the reproducible case; the id check is what covers a
            # foreign scripting client, and a wdotool running as another user.
            with _script_lock():
                return self._load_run_locked(plugin, make_source, deadline, padding, files)
        finally:
            for name in padding:
                self.unload(name)
            for path in files:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _write_source(self, source: str) -> str:
        """The script text in a file KWin can read; returns its path."""
        fd, path = tempfile.mkstemp(prefix="wdotool-kwin-", suffix=".js", dir="/tmp")
        try:
            os.write(fd, source.encode("utf-8"))
        finally:
            os.close(fd)
        # KWin reads the file as the session user; wdotool may be root (the sudo path), and mkstemp's 0600 would
        # then be unreadable for it. Hand the file to that user rather than to everybody: it carries the reply
        # token, and whoever reads the token can answer in KWin's place (a fabricated window list, geometry,
        # ids), plus the window titles and search patterns of the command itself. Not root: KWin runs as us
        # then, and 0600 is already right.
        if os.geteuid() == 0:
            owner = None
            try:
                owner = self.bus._owner_uid()
            except Exception:
                owner = None
            try:
                if owner is not None:
                    os.chown(path, owner, -1)
                else:
                    os.chmod(path, 0o644)   # unreadable would break the call
            except OSError:
                pass
        return path

    def _load_run_locked(self, plugin: str, make_source, deadline: float, padding: list, files: list) -> str:
        name = plugin
        for attempt in range(_LOAD_ATTEMPTS):
            path = self._write_source(make_source(name))
            files.append(path)
            live = self._live_script_ids(deadline)
            (sid,) = self._call(SCRIPTING_PATH, SCRIPTING_IFACE, "loadScript", "ss", (path, name),
                                timeout=max(0.1, deadline - time.monotonic()))
            sid = int(sid)
            if sid < 0:
                # -1: a script with that pluginName is already loaded. Never run a script id we did not get --
                # that would drive somebody else's script (the bug this backend used to have).
                raise CmdError("kwin backend: KWin already has a script named "
                               "%s loaded (loadScript returned %d)"
                               % (name, sid))
            if live is not None and sid in live:
                padding.append(name)
                name = _plugin_name(self._seq, "r%d" % attempt)
                self._seq += 1
                continue
            last = None
            for objpath in ("%s/Script%d" % (SCRIPTING_PATH, sid), "/%d" % sid):
                try:
                    self._call(objpath, SCRIPT_IFACE, "run", timeout=max(0.1, deadline - time.monotonic()))
                    self._path_shape = "scripting" if objpath.startswith(SCRIPTING_PATH + "/") else "root"
                    return name
                except CmdError as e:
                    last = e
            self.unload(name)
            raise last
        raise CmdError(
            "kwin backend: every script id KWin handed out was already taken "
            "(%d tries); another scripting client is loading and unloading "
            "scripts as fast as we are" % _LOAD_ATTEMPTS)

    def unload(self, plugin: str):
        try:
            self._call(SCRIPTING_PATH, SCRIPTING_IFACE, "unloadScript", "s", (plugin,))
        except CmdError:
            pass   # best effort: the script is gone, or KWin is

    def _kwin_unique_name(self):
        """KWin's unique bus name (`:1.42`), or None when it cannot be asked.

        The reply token is a shared secret written into a file KWin has to be able to read, so it is not on its
        own proof of who is answering. The bus knows: the sender of a genuine Result is whoever owns
        org.kde.KWin. Cached -- one GetNameOwner per backend, and a name cannot change owner without KWin having
        died."""
        if self._kwin_name is _UNSET:
            self._kwin_name = None
            try:
                self._kwin_name = self.bus.get_name_owner(KWIN_NAME)
            except Exception:
                pass
        return self._kwin_name

    def _from_kwin(self, m) -> bool:
        """Was this message sent by KWin? Unknown counts as yes: a bus that cannot resolve the name (or does not
        stamp senders) leaves us exactly where we were before, and the token still has to match."""
        owner = self._kwin_unique_name()
        return owner is None or m.sender is None or m.sender == owner

    def _collect(self, bus: Bus, token: str, deadline: float) -> str:
        """The script's Result payload, already queued behind run()'s reply.

        Anything else that reached our name is answered and dropped: a payload carrying another token belongs to
        a call that timed out earlier (or to another wdotool), never to this one."""
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            for m in bus.messages(remain):
                if m.type != METHOD_CALL:
                    continue
                hit = None
                if m.interface == IFACE and m.member == "Result":
                    try:
                        a = m.args()
                    except (ValueError, IndexError):
                        a = ()
                    if len(a) >= 2 and a[0] == token and self._from_kwin(m):
                        hit = a[1]
                if not m.flags & NO_REPLY_EXPECTED:
                    try:
                        if hit is None and m.member not in ("Result", "Event"):
                            bus.error_reply(m, ERR + "UnknownMethod", "no such method")
                        else:
                            bus.reply(m)
                    except DBusError:
                        pass
                if hit is not None:
                    return hit
                if time.monotonic() >= deadline:
                    break
        raise CmdError("kwin backend: KWin ran the script but sent no result "
                       "(a script error is only logged: journalctl --user "
                       "-t kwin_wayland)")

    # -- window facts -------------------------------------------------------

    def _raw(self) -> "list[dict]":
        """The window list, bottom-to-top, refreshing the id -> uuid cache."""
        data = self._script("list")
        if not isinstance(data, list) or not all(isinstance(d, dict) and d.get("u") for d in data):
            raise CmdError("kwin backend: the window list came back malformed")
        self._uuids = _id_map(data)
        self._fix_x_props(data)
        return data

    def _fix_x_props(self, data: "list[dict]"):
        """Take the WM_CLASS pair, and a caption KWin could not read, from the X server for the XWayland windows
        in the list.

        KWin 5.27 lower-cases `resourceClass`, so an xterm came out as "xterm"/"xterm" where X (and xdotool, and
        wmctrl) say "xterm"/"XTerm". And KWin leaves the caption empty for a client whose WM_NAME is
        COMPOUND_TEXT with no _NET_WM_NAME beside it, where the X tools print the title.

        Only 5.27 reaches this: KWin 6 has no windowId property, so its ids come from the X server in the first
        place and _match_xids has read both already. Nothing is opened unless Xwayland is running -- connecting
        would start it -- and one failure turns the correction off for the rest of the process."""
        want = [d for d in data if d.get("xid")]
        if not want:
            return
        x = self._x11()
        if x is None:
            return
        for d in want:
            xid = int(d["xid"])
            try:
                inst, cls = x.get_wm_class(xid)
                title = ("" if d.get("t") else
                         (x.get_prop_string(xid, "_NET_WM_NAME")
                          or x.get_prop_string(xid, "WM_NAME")))
            except Exception:  # a window that just died
                continue
            if cls:
                d["c"] = cls
            if inst:
                d["n"] = inst
            if title:
                d["t"] = title

    def _uuid_for(self, wid: int) -> str:
        u = self._uuids.get(wid)
        if u is None:
            self._raw()
            u = self._uuids.get(wid)
        if u is None:
            raise CmdError("window %d not found" % wid)
        return u

    def _info(self, wid: int) -> dict:
        d = self._act(wid, "info")
        if not isinstance(d, dict):
            raise CmdError("window %d not found" % wid)
        return d

    def _act(self, wid: int, op: str, **kw):
        try:
            return self._script(op, uuid=self._uuid_for(wid), **kw)
        except CmdError as e:
            if getattr(e, "kwin_error", "") == "nowindow":
                # the id was stale: re-read once, then give up
                self._uuids.pop(wid, None)
                try:
                    return self._script(op, uuid=self._uuid_for(wid), **kw)
                except CmdError as again:
                    if getattr(again, "kwin_error", "") == "nowindow":
                        raise CmdError("window %d not found" % wid) from None
                    raise
            raise

    @staticmethod
    def _win(d: dict) -> Window:
        return Window(
            id=d.get("_id") or _wid(d["u"]),
            title=d.get("t") or "",
            class_=d.get("c") or "",
            instance=d.get("n") or "",
            pid=int(d.get("p") or 0),
            x=int(d.get("x", 0)), y=int(d.get("y", 0)),
            w=int(d.get("w", 0)), h=int(d.get("h", 0)),
            focused=bool(d.get("f")),
            visible=not (d.get("m") or d.get("hi")) and bool(d.get("oc")),
            desktop=int(d.get("d", -1)),
            window_type=_WINDOW_TYPES.get(int(d.get("ty", 0)), "NORMAL"),
        )

    @classmethod
    def _view(cls, d: dict, xid: int, ws_name: str) -> View:
        win = cls._win(d)
        mm = int(d.get("mm") or 0)
        cls_name = d.get("c") or ""
        return View(
            window=win,
            xid=xid,
            instance=d.get("n") or cls_name,
            cls=cls_name,
            app_id="" if xid else cls_name,
            fullscreen=bool(d.get("fs")),
            maximized_h=bool(mm & 2),
            maximized_v=bool(mm & 1),
            above=bool(d.get("ka")),
            below=bool(d.get("kb")),
            sticky=bool(d.get("st")),
            urgent=bool(d.get("at")),
            minimized=bool(d.get("m")),
            hidden=bool(d.get("m") or d.get("hi")),
            skip_taskbar=bool(d.get("sk")),
            skip_pager=bool(d.get("sp")),
            floating=True,
            ws_name=ws_name,
            window_type=win.window_type,
            client_type="x11" if xid else "wayland",
            role=d.get("ro") or "",
            desktop_id=_desktop_id(d.get("df") or ""),
            monitor=-1,
            transient_for=_wid(d["tf"]) if d.get("tf") else 0,
            decorated=not d.get("nb"),
        )

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        return [self._win(d) for d in self._raw()]

    def activate(self, wid: int):
        self._act(wid, "activate")

    def focus(self, wid: int):
        self._act(wid, "focus")

    def close(self, wid: int):
        self._act(wid, "close")

    def minimize(self, wid: int):
        self._act(wid, "minimize")

    def map(self, wid: int):
        self._act(wid, "unminimize")

    def unmap(self, wid: int):
        self._act(wid, "minimize")

    def is_mapped(self, wid: int) -> bool:
        return not self._info(wid).get("m")

    def raise_(self, wid: int):
        how = (self._act(wid, "raise") or {}).get("how")
        if how == "activate":
            _warn("windowraise: KWin 5.27 has no per-window raise; activating "
                  "the window instead (this also focuses it)")

    def lower(self, wid: int):
        how = (self._act(wid, "lower") or {}).get("how")
        if how == "keepBelow":
            _warn("windowlower: KWin has no per-window lower; marking the window keep-below instead")

    def move_window(self, wid: int, x: int, y: int):
        self._act(wid, "geometry", x=int(x), y=int(y), w=None, h=None)

    def resize(self, wid: int, w: int, h: int):
        self._act(wid, "geometry", x=None, y=None, w=int(w), h=int(h))

    def move_resize(self, wid: int, x: int, y: int, w: int, h: int):
        self._act(wid, "geometry", x=int(x), y=int(y), w=int(w), h=int(h))

    def set_state(self, wid: int, state: str, action: int) -> "str | None":
        if action not in (0, 1, 2):
            raise CmdError("windowstate: bad action %r" % (action,))
        try:
            out = self._act(wid, "state", state=state, action=int(action), settle=SETTLE_MS) or {}
            applied = out.get("applied")
            if (action in (0, 1) and applied is not None
                    and out.get("settled", True)
                    and bool(applied) != bool(action)):
                # Accepted and ignored: KWin refuses a state that a window rule or the client's own size hints
                # forbid (5.27 will not fullscreen a window whose hints cannot fill the screen). The X tools
                # succeed silently here; warn and succeed. The read-back is the script's *settled* one -- it
                # waited for the window's change signal, so a Wayland client that had simply not acked the
                # configure yet is not reported here (`settled: false` means it could not be checked at all).
                if state == "SHADED" and not out.get("xid"):
                    # KWin shades X11 windows only: the `shade` property exists on every window on 5.27 and
                    # writing it to a native one is accepted and ignored. Blaming a window rule sent people
                    # looking through kcmshell5 for a rule that was never there.
                    return ("windowstate SHADED: KWin can only shade X11 "
                            "windows; window %d is a native Wayland window"
                            % wid)
                return ("windowstate %s: KWin did not apply it to window %d "
                        "(a window rule, or the window's size hints)"
                        % (state, wid))
        except CmdError as e:
            kind = getattr(e, "kwin_error", "")
            if kind in ("nostate", "noshade"):
                err = CmdError("windowstate %s is not supported by the kwin "
                               "backend (%s)"
                               % (state, _GAP_REASONS.get(state,
                                                          "KWin has no API for it")))
                err.unsupported = True
                raise err from None
            raise
        return None

    def unsupported_states(self) -> "set[str]":
        """_NET_WM_STATE names KWin has no setter for at all, so wwmctl knows to reach an XWayland window
        through the X server instead -- where KWin, a full EWMH window manager for the X plane, honours the
        ClientMessage. The dynamic "accepted and ignored" case is not in here; set_state reports that one per
        call."""
        return set(_GAP_REASONS)

    def pointer(self) -> "tuple[int, int] | None":
        """The compositor's real pointer (B6), from workspace.cursorPos.

        Without this, `getmouselocation` fell back to the input daemon's model of the last position it injected:
        it needed /dev/uinput open for what is a pure query, it answered "0 0" after a daemon restart, and it
        knew nothing about a physical mouse. GNOME has had this since B6; KWin exports the same thing and it was
        simply never asked."""
        try:
            d = self._script("cursor")
        except CmdError:
            return None
        if not isinstance(d, dict) or "x" not in d or "y" not in d:
            return None
        return int(d["x"]), int(d["y"])

    def window_desktop(self, wid: int) -> int:
        return int(self._info(wid).get("d", -1))

    def set_window_desktop(self, wid: int, n: int):
        try:
            self._act(wid, "desktop", n=int(n))
        except CmdError as e:
            if getattr(e, "kwin_error", "") == "nodesktop":
                raise CmdError("desktop %d does not exist" % n) from None
            raise

    def get_desktop(self) -> int:
        return int(self._call(KWIN_PATH, KWIN_IFACE, "currentDesktop")[0]) - 1

    def set_desktop(self, n: int):
        # KWin answers false both for "no such desktop" and for "you are already there"
        # (VirtualDesktopManager::setCurrent returns false when the desktop does not change): only the first one
        # is an error.
        (ok,) = self._call(KWIN_PATH, KWIN_IFACE, "setCurrentDesktop", "i", (int(n) + 1,))
        if not ok and self.get_desktop() != n:
            raise CmdError("desktop %d does not exist" % n)

    def num_desktops(self) -> int:
        try:
            return int(self.bus.get_property(KWIN_NAME, VD_PATH, VD_IFACE, "count", timeout=CALL_TIMEOUT))
        except DBusError as e:
            raise _map_error("count", e) from None

    def set_num_desktops(self, n: int):
        n = int(n)
        if n < 1:
            raise CmdError("set_num_desktops: %d is not a workspace count" % n)
        rows = self._desktops()
        # Both D-Bus slots are void and KWin silently refuses past its own limits --
        # VirtualDesktopManager::createVirtualDesktop() returns nullptr at maximum() desktops (20 on 5.27, 25 on
        # 6) and nothing is removed below one. The count itself is the only progress report, so stop the moment
        # a call changes nothing: without that this is an endless loop hammering the compositor with D-Bus
        # calls.
        while len(rows) != n:
            before = len(rows)
            if before < n:
                self._call(VD_PATH, VD_IFACE, "createDesktop", "us", (before, "Desktop %d" % (before + 1)))
            else:
                self._call(VD_PATH, VD_IFACE, "removeDesktop", "s", (rows[-1][1],))
            rows = self._desktops()
            if len(rows) == before:
                err = CmdError(
                    "KWin would not go %s %d virtual desktop%s (that is its "
                    "own limit)"
                    % ("above" if before < n else "below", before,
                       "" if before == 1 else "s"))
                err.unsupported = True
                raise err

    def select_window(self) -> int:
        """xdotool selectwindow: KWin's own interactive picker. The reply is delayed until the user clicks a
        window (or cancels).

        KWin has ONE reply slot for queryWindowInfo: a second picker started while the first is up takes the
        click, and the first call is never answered at all. With timeout=None that was an unkillable wait -- no
        click, no cancel key and no error would end it. The deadline is long enough not to interrupt a person
        deciding (and is overridable with WDOTOOL_SELECT_TIMEOUT for a script that wants a short one)."""
        try:
            (info,) = self.bus.call(KWIN_NAME, KWIN_PATH, KWIN_IFACE,
                                    "queryWindowInfo",
                                    timeout=_select_timeout())
        except DBusError as e:
            if e.name == ERR + "NoReply" or e.name == ERR + "Timeout":
                raise CmdError(
                    "selectwindow: KWin never answered the window picker "
                    "(another picker may have taken the click: KWin has one "
                    "reply slot for queryWindowInfo)") from None
            if e.name == "org.kde.KWin.Error.UserCancel":
                raise CmdError("selectwindow: cancelled") from None
            if e.name == "org.kde.KWin.Error.InvalidWindow":
                raise CmdError("selectwindow: that window is not managed by KWin") from None
            raise _map_error("queryWindowInfo", e) from None
        u = _norm_uuid(str(info.get("uuid") or ""))
        if not u:
            raise CmdError("selectwindow: KWin named no window")
        wid = _wid(u)
        self._uuids[wid] = u
        return wid

    def display_size(self) -> tuple[int, int]:
        s = self._screen_info()
        w, h = int(s.get("w") or 0), int(s.get("h") or 0)
        if w <= 0 or h <= 0:
            raise CmdError("kwin backend: display size unknown")
        return w, h

    def show_desktop(self, show: bool):
        """wmctrl -k. org.kde.KWin.showDesktop is annotated NoReply."""
        self._call(KWIN_PATH, KWIN_IFACE, "showDesktop", "b", (bool(show),), flags=NO_REPLY_EXPECTED)

    # -- optional hooks -----------------------------------------------------

    def views(self) -> "list[View]":
        raw = self._raw()
        xids = self._xids(raw)
        names = {}
        try:
            names = {pos: name for pos, _id, name in self._desktops()}
        except CmdError:
            pass
        return [self._view(d, xids.get(d["u"], 0), names.get(int(d.get("d", -1)), "")) for d in raw]

    def workspaces(self) -> "list[Workspace]":
        rows = self._desktops()
        try:
            cur = str(self.bus.get_property(KWIN_NAME, VD_PATH, VD_IFACE, "current", timeout=CALL_TIMEOUT))
        except DBusError:
            cur = ""
        areas = self._screen_info(soft=True).get("areas") or []
        out = []
        for i, (pos, ident, name) in enumerate(rows):
            wa = areas[i] if i < len(areas) else [0, 0, 0, 0]
            out.append(Workspace(index=int(pos), name=name or "",
                                 active=(ident == cur),
                                 work_area=(int(wa[0]), int(wa[1]),
                                            int(wa[2]), int(wa[3]))))
        return out

    def x_info(self) -> tuple[str, str] | None:
        """(DISPLAY, XAUTHORITY) of the session's Xwayland. KWin publishes neither over D-Bus, so this is the
        session scan -- qualified by the uid that owns the bus socket, which is what makes it right under
        sudo."""
        uid = None
        try:
            uid = self.bus._owner_uid()
        except Exception:  # diagnostics only
            uid = None
        display = session.find_x_display(uid) or ""
        xauth = session.find_xauthority(uid) or ""
        if not display and not xauth:
            return None
        return display, xauth

    def events(self, timeout: float | None = None, workspaces: bool = False):
        """(id, change) in sway's vocabulary, from a script that stays loaded for as long as the iteration runs:
        KWin exports no window signals on D-Bus, but a script may connect to workspace's and every window's Qt
        signals and callDBus each one out. Its own connection, so queued events never pile up behind the command
        connection."""
        bus = Bus(self.bus.address)
        bus.serve_calls = True
        plugin = None
        try:
            dest = _own(bus, EVENTS_NAME)
            token = "%d-ev-%s" % (os.getpid(), os.urandom(4).hex())
            plugin = _plugin_name(self._seq, "ev")
            self._seq += 1
            plugin = self._load_run(
                plugin,
                lambda name: self._source(dest, token, "events", plugin=name),
                time.monotonic() + SCRIPT_TIMEOUT)
            while True:
                got = False
                for m in bus.messages(timeout):
                    if m.type != METHOD_CALL:
                        continue
                    if not m.flags & NO_REPLY_EXPECTED:
                        try:
                            bus.reply(m)
                        except DBusError:
                            return
                    if m.interface != IFACE or m.member != "Event":
                        continue
                    if not self._from_kwin(m):
                        continue    # window facts come from KWin, not from
                                    # whoever else found the token
                    try:
                        tok, u, change = m.args()[:3]
                    except (ValueError, IndexError):
                        continue
                    if tok != token:
                        continue
                    got = True
                    if not u:
                        if workspaces:
                            yield 0, str(change)
                        continue
                    yield _wid(_norm_uuid(str(u))), str(change)
                if not got:
                    return
        finally:
            if plugin is not None:
                self.unload(plugin)
            bus.close()

    # -- helpers ------------------------------------------------------------

    def _desktops(self) -> "list[tuple[int, str, str]]":
        try:
            rows = self.bus.get_property(KWIN_NAME, VD_PATH, VD_IFACE, "desktops", timeout=CALL_TIMEOUT)
        except DBusError as e:
            raise _map_error("desktops", e) from None
        out = [(int(p), str(i), str(n)) for p, i, n in (rows or [])]
        out.sort()
        return out

    def _screen_info(self, soft: bool = False) -> dict:
        """Virtual screen size and per-desktop work areas -- one script, cached for the process. `soft`: an
        unreachable KWin degrades to zeroes instead of failing the command."""
        if self._screen is None:
            try:
                s = self._script("screen")
            except CmdError:
                if not soft:
                    raise
                s = {}
            self._screen = s if isinstance(s, dict) else {}
        return self._screen

    # -- XWayland window ids ------------------------------------------------

    def _xids(self, raw: "list[dict]") -> "dict[str, int]":
        """{uuid: X11 window id} for the XWayland windows in `raw`.

        Plasma 5.27 hands the id straight to the script (X11Window's
        `windowId` property); KWin 6 dropped every Q_PROPERTY from
        x11window.h, so there the ids come from Xwayland itself -- KWin keeps
        _NET_CLIENT_LIST current for its X11 clients (Workspace::
        propagateWindows) -- matched back to KWin's windows on pid, WM_CLASS,
        title and geometry. Nothing is opened unless Xwayland is already
        running: connecting would start it."""
        direct = {d["u"]: int(d["xid"]) for d in raw if d.get("xid")}
        if direct:
            return direct
        if not raw:
            return {}
        x = self._x11()
        if x is None:
            return {}
        try:
            clients = self._x_clients(x)
        except Exception:  # any X failure: no ids, no crash
            self._x = None
            return {}
        return _match_xids(raw, clients, self._x_ratio(x))

    def _x_ratio(self, x) -> "float | None":
        """X device pixels per KWin logical pixel, or None when the layout has no one answer.

        The X root is the whole layout in device pixels; workspace.virtualScreenSize is the same layout in
        logical pixels, which is what the script reports every window rect in. On a 2x screen every X rect is
        exactly twice its KWin rect, so the raw distance between a window and *itself* (1500 for a 400x300
        window at 100,100) is larger than the distance between the two windows of a tied cascade (80 for a
        40px offset) -- and the pair came out swapped. Dividing the X rect by the ratio first puts both back in
        the same units.

        A layout that mixes scales has no single ratio -- the X root is the bounding box at the largest one --
        and there the distance means nothing: None drops it and leaves the title and the list order to decide.
        Anything unreadable (no root geometry, no screen info) is 1.0, which is what every unscaled session is
        and what this did before there was a ratio at all."""
        try:
            rw, rh = x.get_geometry(x.root())[2:]
        except Exception:
            return 1.0
        try:
            s = self._screen_info(soft=True)
        except Exception:
            return 1.0
        vw, vh = int(s.get("w") or 0), int(s.get("h") or 0)
        if vw <= 0 or vh <= 0 or int(rw) <= 0 or int(rh) <= 0:
            return 1.0
        sx, sy = rw / vw, rh / vh
        if abs(sx - sy) > 0.01:
            return None
        return sx

    def _x11(self):
        if self._x != "unset":
            return self._x
        self._x = None
        uid = None
        try:
            uid = self.bus._owner_uid()
        except Exception:
            uid = None
        if not session.xwayland_running(uid):
            return None
        info = self.x_info() or ("", "")
        try:
            from hacks.window import x11_mini
            self._x = x11_mini.X11Conn(info[0] or None, xauthority=info[1] or None)
        except Exception:  # no X plane: xid stays 0
            self._x = None
        return self._x

    @staticmethod
    def _x_clients(x) -> "list[dict]":
        """The X clients, in _NET_CLIENT_LIST order -- which _match_xids reads as an order and not just a set (a
        window that has just died drops out, and that leaves the order of the rest alone)."""
        out = []
        for xid in x.client_list():
            try:
                inst, cls = x.get_wm_class(xid)
                name = x.get_prop_string(xid, "_NET_WM_NAME") or x.get_prop_string(xid, "WM_NAME")
                geo = x.get_geometry(xid)
                pid = x.get_pid(xid)
            except Exception:  # a window that just died
                continue
            out.append({"xid": int(xid), "pid": int(pid), "inst": inst, "cls": cls, "name": name, "geo": geo})
        return out


# ---------------------------------------------------------------- module bits

def _select_timeout() -> float:
    """How long select_window() waits for the picker's answer, in seconds. Two minutes by default -- a person
    choosing a window is slow, a picker whose click another process stole never answers at all."""
    raw = os.environ.get("WDOTOOL_SELECT_TIMEOUT", "").strip()
    try:
        val = float(raw)
    except ValueError:
        return SELECT_TIMEOUT
    return val if val > 0 else SELECT_TIMEOUT


def _plugin_name(seq: int, tag: str = "") -> str:
    """The pluginName one script is loaded under.

    Random, not just pid+counter: KWin holds a pluginName for as long as the script object lives, `unloadScript`
    is the only way to give it back and there is no way to enumerate what is loaded, so a wdotool killed between
    loadScript and unloadScript leaks its names until the session ends. With the pid alone the next process to
    be handed that pid would then fail on its first command and keep failing (pids are recycled within minutes);
    with the random part a leaked name harms nobody, which is what lets "loadScript returned -1" stay a hard
    error."""
    return "wdotool-%d-%d%s-%s" % (os.getpid(), seq, "-" + tag if tag else "", os.urandom(4).hex())


@contextlib.contextmanager
def _script_lock():
    """An advisory lock over the whole load->run window, shared by every wdotool with the same runtime dir.

    KWin reuses a script id as soon as a lower-numbered script is unloaded (see _load_run), and two of our own
    processes racing was the way to see it: with this held, one wdotool's unload can never land between
    another's load and its run. Best effort -- no runtime dir, a read-only one or a lock we cannot take is not a
    reason to refuse the command, because the id check in _load_run_locked is what makes it correct."""
    try:
        rt = session.runtime_dir()
    except CmdError:
        rt = ""
    fd = None
    if rt:
        try:
            fd = os.open(os.path.join(rt, "wdotool-kwin-script.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _own(bus: Bus, name: str) -> str:
    """Own a bus name for the script's callDBus to answer to, and return the destination to put in the script. A
    second wdotool on the same session (or a wedged one still holding the name) gets a name of its own rather
    than the other process's payloads."""
    for candidate in (name, "%s.p%d" % (name, os.getpid())):
        try:
            if bus.request_name(candidate, NAME_FLAG_DO_NOT_QUEUE) in (1, 4):
                return candidate
        except DBusError:
            break
    return bus.unique_name or name


def _payload(raw: str):
    try:
        data = json.loads(raw)
    except ValueError:
        raise CmdError("kwin backend: the script replied with malformed JSON") from None
    if not isinstance(data, dict):
        raise CmdError("kwin backend: the script replied with malformed JSON")
    if data.get("ok"):
        return data.get("v")
    kind = str(data.get("err") or "unknown")
    err = CmdError(_SCRIPT_ERRORS.get(kind, "kwin backend: the script failed: %s" % kind))
    err.kwin_error = kind
    raise err


_SCRIPT_ERRORS = {
    "nowindow": "window not found",
    "nodesktop": "no such desktop",
    "noshade": "windowstate SHADED: Plasma 6 removed window shading",
    "nostate": "windowstate: KWin has no API for that state",
}


def _map_error(member: str, e: DBusError) -> CmdError:
    n = e.name
    if n in (ERR + "ServiceUnknown", ERR + "NameHasNoOwner"):
        return CmdError("kwin backend: KWin left the session bus (compositor restarting?)")
    if n == ERR + "NoReply":
        return CmdError("kwin backend: %s: no reply from KWin within the "
                        "timeout (is the compositor hung?)" % member)
    if n == ERR + "Disconnected":
        return CmdError("kwin backend: session bus connection lost (%s)" % e.message)
    if n == "org.kde.kwin.Scripting.FileError":
        return CmdError("kwin backend: KWin could not read the script file "
                        "(%s); is /tmp readable for the session user?"
                        % (e.message or "FileError"))
    if n in (ERR + "UnknownObject", ERR + "UnknownMethod", ERR + "UnknownInterface"):
        return CmdError("kwin backend: %s: %s" % (member, e.message or n))
    return CmdError("kwin backend: %s failed: %s" % (member, e))


def _desktop_id(name: str) -> str:
    """KWin's desktopFileName ("org.kde.konsole") as the .desktop file id the
    View contract carries (GNOME reports "org.gnome.TextEditor.desktop")."""
    if not name:
        return ""
    return name if name.endswith(".desktop") else name + ".desktop"


def _norm_uuid(u: str) -> str:
    return u.strip().strip("{}").lower()


# Native window ids are 0x40000000 | 30 bits of the uuid, in wdotool.backend's shared minted-id range (the
# reason for the range is written out there). The mint itself stays here: it is the uuid's own hex digits, it
# predates backend.mint_id()'s digest, and every id KWin has ever printed comes out of it.


def _wid(u: str, salt: int = 0) -> int:
    """KWin's uuid -> the id wdotool prints, and KwinBackend._uuids maps back. KWin's own handle is the uuid and
    nothing else; ids have to be minted here (there is no numeric window id anywhere in the scripting API), so
    this is 30 bits of the uuid in a range of our own. `salt` re-mints the same uuid into a different id, for
    the rare collision."""
    hexd = "".join(c for c in _norm_uuid(u) if c in "0123456789abcdef")
    if not hexd:
        return 0
    n = int(hexd[:8], 16) + salt * ID_SALT_STEP
    return _ID_BASE | (n & _ID_MASK)


def _id_map(rows: "list[dict]") -> "dict[int, str]":
    """{printed id: uuid} for one window list, stamping each row's `_id`.

    30 bits is 1e-6-ish odds of two live windows colliding in a session; a plain dict comprehension would then
    drop one of them and leave it with no id at all (unlistable and unaddressable). Whoever comes second in
    stacking order is re-minted instead, so every window in the list has an id of its own; the id is stable
    while the pair is."""
    out: "dict[int, str]" = {}
    for d in rows:
        salt = 0
        wid = _wid(d["u"])
        while wid in out and out[wid] != d["u"]:
            salt += 1
            wid = _wid(d["u"], salt)
        d["_id"] = wid
        out[wid] = d["u"]
    return out
