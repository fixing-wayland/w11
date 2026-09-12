"""Cinnamon (Muffin) window backend over org.Cinnamon.Eval.

Cinnamon is two targets wearing one name. The **X11 session** is the default everywhere and needs no backend
at all: it is a plain EWMH session and the tools hand over to the real xdotool/wmctrl/xprop, measured
byte-identical (`wwmctl -lGpx` against `wmctrl -lGpx` on a live Cinnamon 6.4.13) [M recon2/cinnamon.md §3.1].
This module is for the **Wayland session** (`cinnamon-wayland.desktop`, experimental through 6.6 and not
experimental from Cinnamon 6.8 / Mint 24), where before it every window command answered `no Wayland session
found` [M cinnamon.md §3.2].

Why Eval and not a protocol or an extension: muffin advertises 23 Wayland globals and not one of them is a
foreign-toplevel or output-management protocol, of either family [M cinnamon.md §2.1], and Cinnamon has no
window-listing D-Bus method (`org.Cinnamon` has `GetMonitors`, `Screenshot*`, `ShowOSD` and no windows)
[M cinnamon.md §2.2]. What it does have is `org.Cinnamon.Eval`, ungated -- so this is KWin's `loadScript`
shape: one JS program per operation, in wdotool/cinnamon_js.py, and nothing installed for the user.

That interface is also the honest thing to say out loud: `org.Cinnamon.Eval` grants arbitrary code execution
inside the shell to every client of the session bus, with no consent step, and it is there whether or not
this project exists. wdotool only uses what is already open.

Field mapping (the list program's record -> Window), each measured:
  id       get_stable_sequence() -- small and dense, X-id-shaped
  pid      get_pid(), falling back to get_client_pid() where the first is -1
           (native Wayland windows: measured `"pid":-1,"cpid":86275` on foot)
  x,y,w,h  get_frame_rect() -- the frame, which wwmctl's extents logic already
           reconciles with the client rect for a views() backend
  visible  not minimized AND on the active workspace (or sticky)
  desktop  workspace index, -1 for a window on all workspaces

Two things this backend does that the GNOME one does not have to:

* a resize of a native Wayland window is **asynchronous**. The same command chain read the old size back and
  the new one two seconds later [M cinnamon.md §4], so move and resize poll `get_frame_rect()` for up to
  `settle` seconds, the way GnomeBackend._wait_focused waits for focus.
* `events()` is polled. Eval has no signal route -- there is no way to register a D-Bus object from inside
  an Eval'd program -- so a `-spy` watcher diffs one list against the last every `POLL` seconds. Said out
  loud in docs/WDOTOOL.md: it is not the compositor telling us, it is us asking.

`x_info()` returns None on purpose: `global.display.get_x11_display()` does not exist in muffin (measured),
and it does not need to -- muffin writes `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.XXXXXX`, which is exactly
what w11common/session.py's cookie discovery already looks for [M cinnamon.md §2.3]."""

import json
import time

from w11common.dbus_mini import ERR, Bus, DBusError, no_bus_text
from w11common.errors import CmdError
from wdotool import cinnamon_js as js
from wdotool.backend import View, Window, WindowBackend, Workspace, warn
from wdotool.ctx import NoSessionError

BUS_NAME = "org.Cinnamon"
OBJECT_PATH = "/org/Cinnamon"
IFACE = "org.Cinnamon"

CALL_TIMEOUT = 10.0
#: how long move/resize wait for the client to take the new rectangle
SETTLE = 0.5
#: and how often they look
STEP = 0.03
#: events(): one list diff per interval. 250 ms is the same order as the ~9 ms
#: a round trip costs, so a watcher is not a busy loop and is not slow either.
POLL = 0.25

_GONE = ("cinnamon backend: %s is no longer owned on the session bus "
         "(cinnamon restarting, or the session ended)" % BUS_NAME)

#: Meta.MaximizeFlags: HORIZONTAL is bit 0 and VERTICAL is bit 1
#: (meta/window.h). Measured only as the pair -- `get_maximized()` answered 3
#: on a both-ways maximized window [M recon2/cinnamon.md §4] -- which is the
#: one value that cannot tell the two bits apart, so the enum decides.
MAX_HORZ, MAX_VERT = 1, 2

#: _NET_WM_STATE names muffin has no setter for and that change nothing these
#: tools can observe: warn and succeed, the same rule as GNOME's. BELOW is a
#: real gap (no API), SHADED deliberately is NOT one here -- muffin kept
#: shading where mutter dropped it [M cinnamon.md §2.3].
_COSMETIC_STATES = {"SKIP_TASKBAR", "SKIP_PAGER", "MODAL"}
_GAP_REASONS = {"BELOW": "Muffin has no API for it"}

#: The second half of every gap sentence in this file. Eval already runs arbitrary JS inside Cinnamon
#: (AGENTS.md route 2) and an extension is the same API from inside (route 3), so neither reaches a setter
#: muffin does not have: the rung that does is muffin's own code. What XWayland windows get instead is not
#: hypothetical -- `unsupported_states()` below hands them to wwmctl's X-plane route, which is where these
#: states have always worked.
_GAP_ROUTE = ("not yet here: for an XWayland window wwmctl sets it on the X plane instead, and for a "
              "native one the route is a patched muffin (AGENTS.md route 6), one setter each")


class CinnamonBackend(WindowBackend):
    name = "cinnamon"
    #: what the X check window's _NET_WM_NAME says on a Cinnamon session, so
    #: `wmctrl -m` answers the same with or without Xwayland running
    #: [M recon2/cinnamon.md §3.1]
    wm_name = "Mutter (Muffin)"
    #: no picker yet: `global.stage.grab` does not exist in muffin's Clutter, and `global.begin_modal`
    #: (which does) is a keyboard grab, not a click [M cinnamon.md §2.3]. The route is a reactive full-stage
    #: Clutter actor, and the lowest rung that carries it is the one this whole backend already rides:
    #: `org.Cinnamon.Eval`, which installs nothing (AGENTS.md route 2). Shipping the same actor as a
    #: Cinnamon extension is route 3 and only buys surviving a Cinnamon restart. Either way the cost is a
    #: modal grab that has to be released even when wdotool dies holding it, which is why it is not written
    #: yet.
    select_window_hint = ("picking a window by clicking is not yet done on Cinnamon (AGENTS.md route 2, a "
                          "reactive Clutter actor through org.Cinnamon.Eval); name the window another way")

    def __init__(self, bus: Bus | None = None, names: list[str] | None = None,
                 settle: float = SETTLE):
        """`bus`/`names`: reuse backend_detect's connection and its one ListNames result. `settle`: how long
        move/resize wait for a Wayland client to take the new rectangle."""
        self.settle = settle
        if bus is None:
            try:
                bus = Bus()
            except DBusError as e:
                raise NoSessionError("cinnamon backend: %s" % no_bus_text(e)) from None
        self.bus = bus
        if names is None:
            try:
                names = self.bus.list_names()
            except DBusError as e:
                raise CmdError("cinnamon backend: ListNames failed: %s" % e) from None
        if BUS_NAME not in names:
            # rc 2, like every other backend's "there is nothing to talk to":
            # a script must be able to tell it from "no matching window".
            raise NoSessionError("cinnamon backend: %s is not on the session bus "
                                 "(no Cinnamon session?)" % BUS_NAME)

    # -- plumbing -----------------------------------------------------------

    def _eval(self, script: str):
        """One Eval round trip. Returns the decoded result -- Cinnamon's own `JSON.stringify(eval(code))`,
        parsed once, so a program that answers a number answers an int here and one that answers its own
        JSON string answers that string (see `_json`).

        `(false, text)` is the shape a program that threw comes back as: Cinnamon puts the exception's
        message and stack in the string, and there is no error name to map. So it is one line with the text
        in it, and never a traceback of ours."""
        try:
            ok, out = self.bus.call(BUS_NAME, OBJECT_PATH, IFACE, "Eval", "s",
                                    (script,), timeout=CALL_TIMEOUT)
        except DBusError as e:
            raise self._map_error(e) from None
        if not ok:
            raise CmdError("cinnamon backend: Eval failed: %s" % (out or "(no message)"))
        if out == "":
            # `JSON.stringify(undefined)` is the empty string: a program whose
            # last expression produced nothing. Every program here ends in a
            # value on purpose, so this means the shell ran something else.
            raise CmdError("cinnamon backend: Eval answered nothing")
        try:
            return json.loads(out)
        except ValueError:
            raise CmdError("cinnamon backend: Eval returned malformed JSON: %s"
                           % out[:200]) from None

    def _json(self, script: str):
        """A program that answers with its own `JSON.stringify(...)`: Eval stringifies that string again, so
        the result comes back twice-encoded and is decoded twice."""
        raw = self._eval(script)
        if not isinstance(raw, str):
            raise CmdError("cinnamon backend: expected a JSON string, got %r" % (raw,))
        try:
            return json.loads(raw)
        except ValueError:
            raise CmdError("cinnamon backend: Eval returned malformed JSON: %s"
                           % raw[:200]) from None

    @staticmethod
    def _map_error(e: DBusError) -> CmdError:
        if e.name in (ERR + "ServiceUnknown", ERR + "NameHasNoOwner"):
            return CmdError(_GONE)
        if e.name == ERR + "NoReply":
            return CmdError("cinnamon backend: no reply from %s within the timeout "
                            "(is cinnamon hung?)" % BUS_NAME)
        if e.name == ERR + "Disconnected":
            return CmdError("cinnamon backend: session bus connection lost (%s)" % e.message)
        return CmdError("cinnamon backend: Eval failed: %s" % e)

    def _act(self, script: str):
        """A per-window program: "ok", or the id named no window."""
        out = self._eval(script)
        if out == js.NOWIN:
            raise CmdError("window not found")
        return out

    def _window_act(self, wid: int, script: str):
        try:
            return self._act(script)
        except CmdError as e:
            if str(e) == "window not found":
                raise CmdError("window %d not found" % wid) from None
            raise

    def _raw_list(self) -> "list[dict]":
        data = self._json(js.LIST)
        return data if isinstance(data, list) else []

    def _raw_get(self, wid: int) -> dict:
        for d in self._raw_list():
            if int(d.get("id", 0)) == wid:
                return d
        raise CmdError("window %d not found" % wid)

    @staticmethod
    def _win(d: dict) -> Window:
        ws = int(d.get("ws", -1))
        return Window(
            id=int(d.get("id", 0)),
            title=d.get("title") or "",
            class_=d.get("cls") or "",
            instance=d.get("inst") or "",
            pid=int(d.get("pid") or 0),
            x=int(d.get("x", 0)), y=int(d.get("y", 0)),
            w=int(d.get("w", 0)), h=int(d.get("h", 0)),
            focused=bool(d.get("foc")),
            visible=(not d.get("min")) and ws in (-1, int(d.get("act", -1))),
            desktop=ws,
            window_type=d.get("wt") or "NORMAL",
        )

    @classmethod
    def _view(cls, d: dict, names=None) -> View:
        win = cls._win(d)
        x11 = int(d.get("ct", 0)) == 1
        maximized = int(d.get("max") or 0)
        return View(
            window=win,
            # get_xwindow() is the X id itself for an X client and 0 for a
            # native one -- no matching against the X plane needed here
            xid=int(d.get("xid") or 0),
            instance=d.get("inst") or win.class_,
            cls=d.get("cls") or win.class_,
            app_id=("" if x11 else (d.get("cls") or "")),
            fullscreen=bool(d.get("full")),
            maximized_h=bool(maximized & MAX_HORZ),
            maximized_v=bool(maximized & MAX_VERT),
            above=bool(d.get("above")),
            sticky=win.desktop == -1,
            minimized=bool(d.get("min")),
            hidden=bool(d.get("min")),
            # `is_shaded()` is in the LIST program already (cinnamon_js.py:57) and
            # was read by nothing until wxprop grew the _NET_WM_STATE_SHADED arm
            shaded=bool(d.get("shaded")),
            skip_taskbar=bool(d.get("skipt")),
            floating=True,
            ws_name=(names or {}).get(win.desktop, ""),
            window_type=win.window_type,
            client_type=("x11" if x11 else "wayland"),
            decorated=bool(d.get("dec", True)),
        )

    def _wait_rect(self, wid: int, want, index):
        """Poll `get_frame_rect()` until the window has taken the move (index 0) or the resize (index 2), or
        `settle` runs out. A native Wayland client answers a configure when it is ready and not before: the
        same chain read the old size and had the new one two seconds later [M recon2/cinnamon.md §4].

        `settle` 0 -- the constructor argument, which is what an X11 client under Xwayland could always
        take since its configure is synchronous -- means no wait at all, and no wait means no read either:
        the read is the wait's first step, and issuing one anyway would charge every move an extra Eval
        round trip to answer a question the caller's own timeout has already answered.

        With a settle set, the poll runs for an X11 client too, whose configure is synchronous and whose
        first read therefore always matches. That is one Eval and no more, and there is no cheaper way to
        avoid it: `client_type` is a field of the very list reply the poll would fetch, so learning that
        the wait was unnecessary costs exactly the wait."""
        if self.settle <= 0:
            return
        deadline = time.monotonic() + self.settle
        while True:
            try:
                w = self.find(wid)
            except CmdError:
                return
            got = (w.x, w.y, w.w, w.h)[index:index + 2]
            if tuple(got) == tuple(want):
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(STEP)

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        return [self._win(d) for d in self._raw_list()]

    def find(self, wid: int) -> Window:
        return self._win(self._raw_get(wid))

    def activate(self, wid: int):
        self._window_act(wid, js.action(wid, "activate"))

    def focus(self, wid: int):
        self._window_act(wid, js.action(wid, "focus"))

    def close(self, wid: int):
        self._window_act(wid, js.action(wid, "close"))

    def kill(self, wid: int):
        # muffin kills the client itself, so this works whatever uid we are --
        # unlike the default, which needs a pid and a signal
        self._window_act(wid, js.action(wid, "kill"))

    def minimize(self, wid: int):
        self._window_act(wid, js.action(wid, "minimize"))

    def map(self, wid: int):
        self._window_act(wid, js.action(wid, "unminimize"))

    def unmap(self, wid: int):
        self.minimize(wid)

    def is_mapped(self, wid: int) -> bool:
        return not self._raw_get(wid).get("min", False)

    def raise_(self, wid: int):
        self._window_act(wid, js.action(wid, "raise"))

    def lower(self, wid: int):
        self._window_act(wid, js.action(wid, "lower"))

    def move_window(self, wid: int, x: int, y: int):
        self._window_act(wid, js.move(wid, x, y))
        self._wait_rect(wid, (int(x), int(y)), 0)

    def resize(self, wid: int, w: int, h: int):
        self._window_act(wid, js.resize(wid, w, h))
        self._wait_rect(wid, (int(w), int(h)), 2)

    def set_state(self, wid: int, state: str, action: int) -> "str | None":
        state = state.upper()
        if action == 2:
            # muffin has no toggle of its own, and reading the flag here costs
            # the list this backend was about to send anyway
            action = 0 if self._has_state(wid, state) else 1
        if state not in js.STATES:
            if state in _COSMETIC_STATES:
                warn("windowstate %s: Muffin has no setter for it; %s; ignoring" % (state, _GAP_ROUTE))
                return None
            raise self._state_gap(state)
        self._window_act(wid, js.state(wid, state, action == 1))
        return None

    def _state_gap(self, state: str) -> CmdError:
        err = CmdError("windowstate %s is not supported by the cinnamon backend (%s); %s"
                       % (state, _GAP_REASONS.get(state, "Muffin has no API for it"), _GAP_ROUTE))
        err.unsupported = True
        return err

    def _has_state(self, wid: int, state: str) -> bool:
        d = self._raw_get(wid)
        maximized = int(d.get("max") or 0)
        return {
            "MAXIMIZED": maximized == (MAX_HORZ | MAX_VERT),
            "MAXIMIZED_HORZ": bool(maximized & MAX_HORZ),
            "MAXIMIZED_VERT": bool(maximized & MAX_VERT),
            "FULLSCREEN": bool(d.get("full")),
            "ABOVE": bool(d.get("above")),
            "STICKY": int(d.get("ws", -1)) == -1,
            "SHADED": bool(d.get("shaded")),
            "HIDDEN": bool(d.get("min")),
        }.get(state, False)

    def maximize_pair_state(self) -> str:
        """"MAXIMIZED": `MaximizeFlags.BOTH` in one call. Muffin is Mutter, so the reason is Mutter's -- two
        single-axis calls corrupt the saved rectangle (WindowBackend.maximize_pair_state)."""
        return "MAXIMIZED"

    def unsupported_states(self) -> "set[str]":
        """What wwmctl should reach an XWayland window through the X server for instead. SHADED is not in
        here: muffin implements it, and its EWMH advertises `_NET_WM_STATE_SHADED` [M cinnamon.md §3.1]."""
        return set(_COSMETIC_STATES) | set(_GAP_REASONS)

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def set_window_desktop(self, wid: int, n: int):
        self._window_act(wid, js.set_workspace(wid, n))

    def get_desktop(self) -> int:
        return int(self._eval(js.GET_DESKTOP))

    def set_desktop(self, n: int):
        self._eval(js.set_desktop(n))

    def num_desktops(self) -> int:
        return int(self._eval(js.NUM_DESKTOPS))

    def display_size(self) -> tuple[int, int]:
        size = self._json(js.DISPLAY_SIZE)
        try:
            w, h = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            raise CmdError("cinnamon backend: display size unknown") from None
        if w <= 0 or h <= 0:
            raise CmdError("cinnamon backend: display size unknown")
        return w, h

    def pointer(self) -> "tuple[int, int] | None":
        """`global.get_pointer()` -- where the pointer really is, whoever moved it."""
        try:
            p = self._json(js.POINTER)
        except CmdError:
            return None
        try:
            return int(p[0]), int(p[1])
        except (TypeError, ValueError, IndexError):
            return None

    def select_window(self) -> int:
        """The hint above says what would close this; the refusal says it again, because a script reads the
        error and not the hint."""
        err = CmdError("selectwindow is not supported by the cinnamon backend: %s" % self.select_window_hint)
        err.unsupported = True
        raise err

    # -- optional hooks -----------------------------------------------------

    def views(self) -> "list[View]":
        names = {}
        try:
            names = {w.index: w.name for w in self.workspaces()}
        except CmdError:
            pass
        return [self._view(d, names) for d in self._raw_list()]

    def workspaces(self) -> "list[Workspace]":
        out = []
        for d in self._json(js.WORKSPACES) or []:
            wa = d.get("wa") or (0, 0, 0, 0)
            out.append(Workspace(index=int(d.get("i", len(out))),
                                 name=d.get("name") or "",
                                 active=bool(d.get("act")),
                                 work_area=(int(wa[0]), int(wa[1]), int(wa[2]), int(wa[3]))))
        return out

    def x_info(self):
        """None: muffin exports no X11 display object to JS, and the session scan already finds what it
        writes (`.mutter-Xwaylandauth.*`) [M recon2/cinnamon.md §2.3]."""
        return None

    def events(self, timeout: float | None = None):
        """(id, change) in sway's vocabulary, from one list diff per `POLL` seconds: `new`, `close`, `focus`,
        `title`. Polled because Eval has no signal route at all -- an Eval'd program cannot register a D-Bus
        object, and `org.Cinnamon` publishes no window signal [M recon2/cinnamon.md §2.2].

        `timeout` is silence, as everywhere else: it ends the iterator when nothing has changed for that
        long, and any event restarts it."""
        last = {w.id: w for w in self.list()}
        quiet = time.monotonic()
        while True:
            time.sleep(POLL)
            now = {w.id: w for w in self.list()}
            events = []
            for wid, w in now.items():
                was = last.get(wid)
                if was is None:
                    events.append((wid, "new"))
                    if w.focused:
                        # sway sends the two as separate events and wxprop -spy keys on `focus` to reprint
                        # _NET_WM_STATE_FOCUSED; a window that opens already focused (one mapped under the
                        # pointer) has no later poll to raise it in -- `was.focused` is True from here on.
                        events.append((wid, "focus"))
                    continue
                if w.title != was.title:
                    events.append((wid, "title"))
                if w.focused and not was.focused:
                    events.append((wid, "focus"))
            for wid in last:
                if wid not in now:
                    events.append((wid, "close"))
            last = now
            if events:
                quiet = time.monotonic()
                yield from events
            elif timeout is not None and time.monotonic() - quiet >= timeout:
                return
