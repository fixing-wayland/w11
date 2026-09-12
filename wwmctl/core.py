"""Unified window model + wmctrl semantics over the wdotool
compositor backend, with X11 enrichment for XWayland windows when available.

Dual-plane design per docs/WWMCTL.md:
- the window LIST and all ACTIONS come from the compositor backend
  (wdotool.backend_detect.detect(); backend-native ids address windows),
  with -e sent as one move_resize() where the backend offers it,
- XWayland windows are printed with their REAL X11 window id (the backend's
  views() carry it -- GNOME bridge `xid`; sway's raw tree exposes it as the
  node's "window" field) so xprop/real-wmctrl interoperate, and X-only
  data (WM_CLASS, WM_CLIENT_MACHINE, geometry) is read from the XWayland
  server via wdotool.x11_mini when it is reachable -- with the DISPLAY and
  cookie file the backend reports (x_info(): gnome-shell's own on GNOME,
  where Xwayland runs with -auth), else the session scan,
- with no X server everything still works compositor-only (class from
  the compositor's WM_CLASS, machine falls back to the local hostname —
  XWayland clients are local by construction).

Listing sources, in order: backend.views() (typed View records: GNOME),
the sway-private _nodes() tree, the generic backend.list(). Desktops come
from backend.workspaces() (GNOME: names + work areas; sway: one row per
workspace) or are synthesized from get_desktop()/num_desktops(); -k/-n reach
the backend's show_desktop()/set_num_desktops() when it has them (GNOME) and
warn otherwise.

Output strings below are byte-parity copies of wmctrl 1.07 (main.c)."""

import dataclasses
import os
import re
import struct
import sys
import time

from w11common import session
from w11common.errors import CmdError
from wdotool import backend_detect
from wdotool.backend import state_steps as _backend_state_steps
from wdotool.backend import warn as _warn
from wdotool.cnum import atoi as _atoi
from wdotool.x11_mini import XUnavailable, hostname

# _NET_WM_STATE actions (EWMH)
STATE_REMOVE = 0
STATE_ADD = 1
STATE_TOGGLE = 2
# How long the EWMH fallback waits for the X window manager to act on the ClientMessage before calling it
# dropped. It returns as soon as the state shows up, so this is only ever paid by a message nobody honoured.
_X_SETTLE = 0.4

SELECT_WINDOW_MAGIC = ":SELECT:"
ACTIVE_WINDOW_MAGIC = ":ACTIVE:"

#: Backends whose Xwayland window manager is MEASURED not to reparent, so that the original wmctrl's
#: doubled `-G` origin (`Core._geometry_column`) is what it prints there. 2026-09-12, this guest and the
#: committed recordings: sway 1.11 and labwc 0.9.3 headless both answered `xwininfo -tree` with
#: `Parent window id: 0x234 (the root window)` for an xterm; `resolute-hypr-0.53.3-...-replay.txt` has the
#: original printing `600 400` for the xmessage our clone read at `300 200`; the same file for
#: resolute-labwc/-budgie/-lxqt-wayland (all the `wlr` backend) has `1436 790` against `718 395` and the
#: two other pairs. GNOME is measured the OTHER way and is deliberately not here (mutter frames every
#: decorated X11 window on Wayland too), and so is cosmic: measured 2026-09-12 on the arch-cosmic 1.8.0
#: golden (instance acos-b18), an xterm's `xwininfo -tree` gave `Parent window id: 0x200020` against a
#: root of `0x2f2` -- Smithay's X WM REPARENTS, so cosmic frames and stays out (item 8). kwin, cinnamon
#: and wayfire are NOT YET measured and are out until they are: one `xwininfo -id <xterm> -tree` on each
#: golden, rung 4, minutes apiece.
NON_REPARENTING_XWM = frozenset(("wlr", "sway", "hypr"))

#: Backends whose Xwayland window manager never puts `_NET_WM_DESKTOP` on the window, so the original
#: falls back to `_WIN_WORKSPACE` and then to a flat 0 (`Core._desktop_column`). Measured 2026-09-12:
#: muffin on both session kinds (the `resolute-cinnamon` golden and goal2/requests-batch-13.md), and
#: sway 1.11 / labwc 0.9.3 headless on this guest, where `xprop _NET_WM_DESKTOP` on an xterm answers
#: `no such atom on any window` -- the wlroots xwm does not even intern it. cosmic-comp 1:1.8.0-1 is
#: the same: measured 2026-09-12 on the arch-cosmic 1.8.0 golden (instance acos-b18), an xterm made
#: sticky with `wmctrl -b add,sticky` answered `_NET_WM_DESKTOP: no such atom on any window` and so did
#: `_WIN_WORKSPACE` -- Smithay's X WM interns neither (item 8). GNOME publishes 0xFFFFFFFF and is
#: deliberately not here. kwin, hypr and wayfire are NOT YET measured and are out until they are: one
#: `xprop -id <xterm> _NET_WM_DESKTOP` after a `wmctrl -b add,sticky` on each golden, rung 4, minutes apiece.
NO_NET_WM_DESKTOP_XWM = frozenset(("cinnamon", "cosmic", "wlr", "sway"))


# -- injection seams (unit tests monkeypatch these) --------------------------

def _detect_backend():
    return backend_detect.detect()


def _x11_connect(display=None, xauthority=None):
    """An x11_mini.X11Conn to the XWayland server, or None. Never raises. `display`/`xauthority` are what the
    backend knows about its Xwayland (GNOME: gnome-shell's own DISPLAY and Mutter's cookie file); without them
    x11_mini falls back to the environment and the session scan."""
    if os.environ.get("WWMCTL_NO_X"):
        return None
    try:
        from wdotool import x11_mini
        if display is None and xauthority is None:
            return x11_mini.X11Conn()
        return x11_mini.X11Conn(display, xauthority=xauthority)
    except Exception:
        # covers XUnavailable, the not-yet-implemented stub (AttributeError),
        # and any wire-level failure: degrade to compositor-only silently.
        return None


@dataclasses.dataclass
class UWindow:
    id: int              # printed id: real X11 id for XWayland, else node id
    node_id: int         # compositor node id — actions always go through this
    is_x: bool = False
    title: str | None = None
    class_: str | None = None   # "instance.class" / "app_id.app_id"
    machine: str | None = None
    pid: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    desktop: int = 0     # 0-based workspace index, -1 = all/hidden
    focused: bool = False
    # the compositor's own rectangle (frame rect on GNOME, content rect on sway) -- what move/resize address;
    # x/y/w/h above may be overwritten by the X plane's client rectangle for -lG
    fx: int = 0
    fy: int = 0
    fw: int = 0
    fh: int = 0
    # the X-plane origin relative to its X parent (from GetGeometry): the frame offset wmctrl's -G adds to
    # the absolute origin under a framing xwm; 0,0 for a native window or a fake without get_geometry_raw
    rel_x: int = 0
    rel_y: int = 0


class Core:
    def __init__(self, backend=None, verbose=False, utf8=False, true_geometry=False):
        self._backend = backend
        self._x11 = "unset"
        self._views_seen = None  # last views() outcome: True/False/None
        self.verbose = verbose
        self.utf8 = utf8         # wmctrl's envir_utf8: a UTF-8 locale or -u
        #: `--true-geometry`, a flag wmctrl never had: print the real origin in the -G column
        #: instead of the original's doubled one (see `_geometry_column`)
        self.true_geometry = true_geometry
        self._id_clash_warned = False

    def vprint(self, msg: str):
        if self.verbose:
            sys.stderr.write(msg)

    def backend(self):
        if self._backend is None:
            self._backend = _detect_backend()
        return self._backend

    def _x_params(self):
        """(display, xauthority) the backend knows for its X plane, or None
        (sway, generic backends: x11_mini discovers on its own)."""
        try:
            info_fn = getattr(self.backend(), "x_info", None)
            info = info_fn() if callable(info_fn) else None
        except Exception:
            return None
        if not info:
            return None
        display, xauth = info
        return (display or None), (xauth or None)

    def x11(self):
        if self._x11 == "unset":
            params = self._x_params()
            if params is None:
                self._x11 = _x11_connect()
            else:
                self._x11 = _x11_connect(display=params[0], xauthority=params[1])
        return self._x11

    def _x_is_up(self) -> bool:
        """May the X plane be opened without side effects? Mutter starts Xwayland on demand: a connect just to
        ask spawns a server. With a views() backend the answer is "an X window is listed, or an Xwayland process
        exists"; sway and the rest keep the old behavior (try to connect)."""
        if self._x11 not in ("unset", None):
            return True
        try:
            wins = self.windows()
        except CmdError:
            return True
        if self._views_seen is not True:
            return True
        if any(w.is_x for w in wins):
            return True
        return session.xwayland_running()

    # -- unified window list ------------------------------------------------

    def _from_views(self, views, host) -> list[UWindow]:
        out = []
        for v in views:
            win = v.window
            xid = int(v.xid or 0)
            # only a native view falls back to its app id; an XWayland view
            # has a real WM_CLASS pair, and so does a native one that set it
            cls = ("%s.%s" % (v.app_id, v.app_id) if not xid and v.app_id
                   else _dot_class(v.instance or None, v.cls or None))
            out.append(_uwindow(win, host, xid, cls, win.title))
        return out

    def windows(self) -> list[UWindow]:
        backend = self.backend()
        host = hostname() or None   # UWindow.machine: None means unknown
        out = None
        views_fn = getattr(backend, "views", None)
        if callable(views_fn):  # typed View records (GNOME bridge)
            views = views_fn()
            if views is not None:
                out = self._from_views(views, host)
                self._views_seen = True
                self._enrich(out)
                self._check_id_clash(out)
                return out
        self._views_seen = False
        nodes_fn = getattr(backend, "_nodes", None)
        if nodes_fn is not None:  # sway/i3: raw tree carries the X plane
            # _nodes() is private to wdotool.backend_sway; its tuple shape is a documented contract, but guard
            # the unpacking so a shape drift degrades to the generic listing, not a traceback.
            try:
                out = []
                for node, win, _floating, _ws in nodes_fn():
                    xid = node.get("window")
                    wp = node.get("window_properties") or {}
                    cls = _dot_class(wp.get("instance"), wp.get("class"))
                    if node.get("app_id"):
                        cls = "%s.%s" % (node["app_id"], node["app_id"])
                    out.append(_uwindow(win, host, xid, cls, node.get("name")))
            except (TypeError, ValueError, KeyError) as e:
                self.vprint("_nodes() has an unexpected shape (%s: %s); "
                            "using the generic backend listing\n"
                            % (type(e).__name__, e))
                out = None
        if out is None:
            out = []
            for win in backend.list():
                cls = "%s.%s" % (win.class_, win.class_) if win.class_ else None
                out.append(_uwindow(win, host, 0, cls, win.title or None))
        self._enrich(out)
        self._check_id_clash(out)
        return out

    def _check_id_clash(self, wins: list[UWindow]):
        """Bridge ids and X11 ids share one integer space.

        Mutter seeds meta_display_generate_window_id() from g_random_int(), so a bridge id CAN land on an
        XWayland window's X id. `-i` then has two candidates and resolves the X one (find_target); the other
        window becomes unreachable by id. Rare enough never to have been seen live, cheap enough to say out loud
        once."""
        if self._id_clash_warned:
            return
        seen = {}
        for w in wins:
            other = seen.get(w.id)
            if other is not None and other is not w:
                self._id_clash_warned = True
                _warn("two windows share the id 0x%08x; -i uses the X11 "
                      "one" % w.id)
                return
            seen[w.id] = w
            if w.node_id != w.id:
                seen.setdefault(w.node_id, w)

    def _enrich(self, wins: list[UWindow]):
        """Fill X-only fields for XWayland windows from the X server.

        Per-window failures (BadWindow on a window that died mid-listing) degrade field by field; a
        connection-level failure (XUnavailable — a hung or vanished XWayland) drops the X plane for the rest of
        the process, so the whole listing pays at most one timeout, not 4 calls x N windows. Compositor data
        stands either way."""
        if not any(w.is_x for w in wins):
            return
        x = self.x11()
        if x is None:
            return
        for w in wins:
            if not w.is_x:
                continue
            try:
                self._enrich_one(x, w)
            except XUnavailable:
                try:
                    x.close()
                except Exception:
                    pass
                self._x11 = None
                return

    def _enrich_one(self, x, w: UWindow):
        """One window's X reads. XUnavailable propagates (connection dead);
        anything else degrades to the compositor value, field by field."""
        try:
            inst, cls = x.get_wm_class(w.id)
            if inst or cls:
                w.class_ = _dot_class(inst, cls)
        except XUnavailable:
            raise
        except Exception:
            pass
        try:
            machine = x.get_client_machine(w.id)
            if machine:
                w.machine = machine
        except XUnavailable:
            raise
        except Exception:
            pass
        try:
            raw = getattr(x, "get_geometry_raw", None)
            if raw is not None:
                # keep the parent-relative origin the -G column needs on a framing xwm; get_geometry_raw
                # exists on the real x11_mini and on this batch's GNOME fake, absent on the other fakes
                w.x, w.y, w.w, w.h, w.rel_x, w.rel_y = raw(w.id)
            else:
                w.x, w.y, w.w, w.h = x.get_geometry(w.id)
        except XUnavailable:
            raise
        except Exception:
            pass
        if w.pid <= 0:
            try:
                w.pid = x.get_pid(w.id)
            except XUnavailable:
                raise
            except Exception:
                pass

    # -- target selection (wmctrl <WIN> argument) ---------------------------

    def find_target(self, param_window: str, match_by_id: bool,
                    match_by_cls: bool, full_match: bool) -> UWindow | None:
        """Resolve wmctrl's <WIN>. Returns None for a silent exit-1 (wmctrl exits 1 without a message when no
        window matches). Raises CmdError with "Cannot convert argument to number." for a bad -i argument."""
        if match_by_id:
            wid = _parse_win_id(param_window)
            if wid is None:
                raise CmdError("Cannot convert argument to number.")
            wins = self.windows()
            # X-plane ids first (real X ids live at 0x00400000+ resource
            # bases, far above sway node ids — but be explicit anyway).
            for w in wins:
                if w.is_x and w.id == wid:
                    return w
            for w in wins:
                if w.node_id == wid or w.id == wid:
                    return w
            # A no-match by title or class is wmctrl's silent exit 1, but an -i id that names nothing is not a
            # search: real wmctrl asks the X server about it and Xlib prints BadWindow. Say so, in one line,
            # rather than exiting 1 with nothing at all.
            _warn("no window with id 0x%08x" % wid)
            return None
        if param_window == SELECT_WINDOW_MAGIC:
            # real wmctrl shows a crosshair cursor; the closest we can do is say what the blocking wait is for
            # -- which is not the same sentence on every backend (a click on GNOME and KDE, the next focus
            # change on sway), so the backend supplies it.
            b = self.backend()
            _warn(b.select_window_hint)
            node = b.select_window()
            for w in self.windows():
                if w.node_id == node:
                    return w
            return None
        if param_window == ACTIVE_WINDOW_MAGIC:
            for w in self.windows():
                if w.focused:
                    return w
            return None
        needle = param_window if full_match else param_window.casefold()
        for w in self.windows():
            hay = w.class_ if match_by_cls else w.title
            if hay is None:
                continue
            if full_match:
                if hay == needle:
                    return w
            elif needle in hay.casefold():
                return w
        return None

    # -- actions ------------------------------------------------------------

    def activate(self, w: UWindow):
        self.backend().activate(w.node_id)

    def close(self, w: UWindow):
        self.backend().close(w.node_id)

    def to_desktop(self, w: UWindow, desktop: int) -> int:
        backend = self.backend()
        if desktop == -1:
            # -R / -t -1: the current desktop. sway can say this directly, which also works when the focused
            # workspace is named (no number — get_desktop() would return -1 and the numeric route would mis-file
            # the window on a workspace called "0").
            if backend.move_to_current_desktop(w.node_id):
                return 0
            desktop = backend.get_desktop()
        if desktop < 0:
            # negative desktops cannot exist; wmctrl would fire the request into the void and exit 0 — warn
            # instead of passing sway a confusing off-by-one workspace number
            _warn("desktop %d does not exist; ignoring" % desktop)
            return 0
        backend.set_window_desktop(w.node_id, desktop)
        return 0

    def to_current_and_activate(self, w: UWindow) -> int:  # -R
        self.to_desktop(w, -1)
        if self.backend().name != "sway":
            # wmctrl sleeps to give an asynchronous WM time to move the window; the sway IPC round-trip above is
            # synchronous, so only non-sway backends need the grace period
            time.sleep(0.1)
        self.activate(w)
        return 0

    def move_resize(self, w: UWindow, arg: str) -> int:  # -e
        argerr = ('The -e option expects a list of comma separated integers: '
                  '"gravity,X,Y,width,height"\n')
        if not arg:
            sys.stderr.write(argerr)
            return 1
        m = re.match(r"\s*([+-]?\d+),\s*([+-]?\d+),\s*([+-]?\d+)"
                     r",\s*([+-]?\d+),\s*([+-]?\d+)", arg)
        if not m:
            sys.stderr.write(argerr)
            return 1
        grav, x, y, ww, hh = (int(g) for g in m.groups())
        if grav < 0:
            sys.stderr.write("Value of gravity mustn't be negative. Use zero"
                             " to use the default gravity of the window.\n")
            return 1
        grflags = grav
        if x != -1:
            grflags |= 1 << 8
        if y != -1:
            grflags |= 1 << 9
        if ww != -1:
            grflags |= 1 << 10
        if hh != -1:
            grflags |= 1 << 11
        self.vprint("grflags: %d\n" % grflags)
        # The compositor is the WM: route the request through it with _NET_MOVERESIZE_WINDOW's meaning. `W,H`
        # are the CLIENT size and the gravity names the point of the frame the request positions: NorthWest its
        # top-left, Center its centre, SouthEast its bottom-right, Static the client's own top-left (0 = "the
        # window's own WM_SIZE_HINTS gravity", taken as NorthWest, the ICCCM default and what toolkits set). A
        # -1 keeps that point where it is, so a bare resize under SouthEast pins the frame's bottom-right corner
        # and grows up and to the left -- Mutter's own behavior, verified against real wmctrl. The frame extents
        # (Mutter's server-side titlebar on an XWayland window) turn the client rectangle into the frame
        # rectangle the bridge's Move/Resize address; where the compositor manages the client rectangle itself
        # (native windows, sway's content rect, X plane not reached) they are zero and every gravity but Static
        # collapses to NorthWest. Requests the compositor cannot honor (moving a tiled window, touching a
        # fullscreen one) are warned about and ignored, matching "the WM may ignore the request".
        backend = self.backend()
        ext = self._measure_extents(w)
        if ext is None:
            _warn("window moved while measuring the frame; ignoring")
            return 0
        left, top, right, bottom = ext
        cw = ww if ww != -1 else w.fw - left - right
        ch = hh if hh != -1 else w.fh - top - bottom
        fw, fh = cw + left + right, ch + top + bottom
        col, row = _GRAVITY_CORNER.get(grav, (0, 0))
        static = grav == _GRAVITY_STATIC
        # A -1 keeps an axis, but what "keep" means depends on the request as a whole AND on the compositor.
        # With one coordinate given, the -1 on the other axis holds that axis' unchanged frame edge -- anchoring
        # it on the gravity point instead put us up to 80 px from where real wmctrl leaves the window (GNOME 46,
        # `9,-1,200,400,300`). With BOTH coordinates omitted, GNOME 46 holds the gravity's reference point, so a
        # SouthEast resize grows up and to the left, while GNOME 50 applies no gravity at all and keeps the
        # top-left corner. Both measured against real wmctrl.
        keep_anchor = x == -1 and y == -1 and self._bare_resize_gravity()
        fx = _place_axis(col, static, x, left, w.fx, w.fw, cw, fw, keep_anchor)
        fy = _place_axis(row, static, y, top, w.fy, w.fh, ch, fh, keep_anchor)
        resizing = ww != -1 or hh != -1
        # a move was asked for, or the gravity's anchor requires one
        moving = x != -1 or y != -1 or (fx, fy) != (w.fx, w.fy)
        both = getattr(backend, "move_resize", None)
        if resizing and moving and callable(both):
            # One request when the backend can take one (KWin). Sending a resize and a move a few milliseconds
            # apart is a race against a Wayland client: the compositor's rectangle only changes when the client
            # acks the configure, so the move, reading the not-yet-changed size back, re-requests the old one
            # and cancels the resize. Observed live on KWin 6.6 with konsole.
            try:
                both(w.node_id, fx, fy, fw, fh)
            except CmdError as e:
                _warn("%s; ignoring" % e)
            return 0
        if resizing:
            try:
                backend.resize(w.node_id, fw, fh)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        if moving:
            try:
                backend.move_window(w.node_id, fx, fy)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        return 0

    def _bare_resize_gravity(self) -> bool:
        """Does the compositor anchor `-e G,-1,-1,W,H` -- a resize with no coordinates -- on the gravity point?

        Mutter did on GNOME 46 and does not on GNOME 50, where such a request keeps the window's top-left corner
        whatever the gravity says (both measured against real wmctrl on the same window). The cut sits right
        after the release measured to anchor: 47-49 were not measured, and a rule that keeps applying to 51+ is
        worth more than a guess in their favour. A compositor that does not report a version keeps the older
        behaviour, which is what sway and every non-GNOME backend have always done."""
        fn = self._backend_hook("compositor_version")
        try:
            v = fn() if callable(fn) else ()
        except Exception:
            v = ()
        return not (v and v[0] >= 47)

    def _frame_extents(self, w: UWindow):
        """(left, top, right, bottom) between the compositor's frame rect and the X plane's client rect of an
        XWayland window listed by views(): Mutter's server-side titlebar and border. Zero when the two are the
        same rectangle (native windows, the sway tree's content rect, an X plane that was not reached); None
        when the client rectangle does not sit inside the frame at all -- either the window moved between the
        two reads, or the X coordinate space is scaled differently from the compositor's. _measure_extents tells
        those two apart; do not use this on its own to decide a geometry request."""
        if not (self._views_seen and w.is_x):
            return 0, 0, 0, 0
        return _extents_of((w.fx, w.fy, w.fw, w.fh), (w.x, w.y, w.w, w.h))

    def _sample_rects(self, w: UWindow, x):
        """One (frame rect, client rect) pair, read back to back from the
        compositor and from the X server. None when either read fails."""
        try:
            d = self.backend().find(w.node_id)
        except Exception:
            return None
        try:
            client = tuple(x.get_geometry(w.id))
        except Exception:
            return None
        return (d.x, d.y, d.w, d.h), client

    def _measure_extents(self, w: UWindow):
        """The frame extents a -e request may rely on, or None.

        The frame rectangle and the client rectangle come from two servers a round trip apart, so a window that
        is moving (a drag, an animation, another script) makes their difference meaningless -- and it used to
        come out silently zero, which collapsed every gravity to NorthWest and resized the frame to the client
        size. Sample until two consecutive pairs agree: that means the window held still, and a client rectangle
        that still does not fit inside the frame is then a coordinate space we cannot subtract in (a scaled X
        plane), where zero extents are the honest answer. A window that never holds still yields None and the
        caller drops the request. w's rectangles are updated to the pair that agreed, so the arithmetic that
        follows describes one single instant."""
        if not (self._views_seen and w.is_x):
            return 0, 0, 0, 0
        x = self.x11()
        if x is None:
            return 0, 0, 0, 0
        prev = ((w.fx, w.fy, w.fw, w.fh), (w.x, w.y, w.w, w.h))
        for _ in range(_EXTENT_SAMPLES):
            cur = self._sample_rects(w, x)
            if cur is None:  # the window or a plane went away: no extents
                return 0, 0, 0, 0
            if cur == prev:
                frame, client = cur
                w.fx, w.fy, w.fw, w.fh = frame
                w.x, w.y, w.w, w.h = client
                return _extents_of(frame, client) or (0, 0, 0, 0)
            prev = cur
        return None

    def window_state(self, w: UWindow, arg: str) -> int:  # -b
        argerr = ('The -b option expects a list of comma separated parameters'
                  ': "(remove|add|toggle),<PROP1>[,<PROP2>]"\n')
        if not arg or "," not in arg:
            sys.stderr.write(argerr)
            return 1
        head, rest = arg.split(",", 1)
        if head == "remove":
            action = STATE_REMOVE
        elif head == "add":
            action = STATE_ADD
        elif head == "toggle":
            action = STATE_TOGGLE
        else:
            sys.stderr.write("Invalid action. Use either remove, add or "
                             "toggle.\n")
            return 1
        if "," in rest:
            p1, p2 = rest.split(",", 1)
            if not p2:
                sys.stderr.write("Invalid zero length property.\n")
                return 1
            self.vprint("State 2: _NET_WM_STATE_%s\n" % p2.upper())
        else:
            p1, p2 = rest, None
        if not p1:
            sys.stderr.write("Invalid zero length property.\n")
            return 1
        self.vprint("State 1: _NET_WM_STATE_%s\n" % p1.upper())
        # An XWayland window has a second route: Mutter is a full EWMH window manager for the X plane and
        # honours the _NET_WM_STATE ClientMessage real wmctrl sends -- including for the states its Wayland API
        # cannot express (below, skip_taskbar, skip_pager). The compositor stays the first choice: `hidden`
        # really minimizes through the bridge, where the X route is a no-op.
        skip = self._compositor_cannot_set() if w.is_x else frozenset()
        names = [p.upper() for p in (p1, p2) if p is not None]
        for name, atoms in self._state_steps(names):
            if name in skip and self._x_set_state(w, atoms, action):
                continue
            try:
                why = self.backend().set_state(w.node_id, name, action)
            except CmdError as e:
                if self._x_set_state(w, atoms, action):
                    continue
                _warn("%s; ignoring" % e)
                continue
            # Accepted and ignored: KWin does that for a window rule and for size hints a fullscreen cannot
            # satisfy. Real wmctrl gets these through, because the X plane is a different window manager -- so
            # take that route before calling it a loss.
            if why and not self._x_set_state(w, atoms, action):
                _warn("%s; ignoring" % why)
        return 0

    def _state_steps(self, names):
        """The -b properties as (state to ask the backend for, the EWMH names it stands for) steps. One step per
        property, except that both maximize axes together become a single request wherever the backend has a
        name for the pair -- GNOME does, and there it is not an optimization but the fix for a corrupted restore
        size.

        Mutter unmaximizes to the window's *current* frame rect, taking only the axis it is unmaximizing from
        the saved rectangle (meta_window_set_unmaximize_flags, window.c). The second single-axis call is issued
        microseconds after the first, long before the Wayland client has answered its configure, so it reads a
        frame rect that is still maximized and carries the maximized half into its target; and once both flags
        are clear Mutter saves that rectangle as the restore size (maybe_save_rect). Measured on GNOME 46 and
        50: `-b remove,maximized_vert,maximized_horz` left a 200,150 900x600 window at 200,32 900x1048, and no
        later state change got the height back. Reversing the two only moves the damage to the other axis
        (67,150 1853x600).

        Mutter's own EWMH path never splits the pair: it collects both atoms of one _NET_WM_STATE ClientMessage
        into a single `directions` bitmask and makes one meta_window_set_[un]maximize_flags() call
        (window-x11.c) -- which is exactly what real wmctrl's single message asks for, so folding here is
        parity, not a special case.

        The grouping itself is backend.state_steps(), so that every command that can be handed both axes at once
        gets it from one place."""
        try:
            b = self.backend()
        except CmdError:
            b = None            # no session: nothing to fold, and the loop
                                # below reports it exactly as it did before
        return _backend_state_steps(b, names)

    def _compositor_cannot_set(self):
        """_NET_WM_STATE names the compositor backend answers "not applied" to. The backend reports them (GNOME:
        the bridge's own gaps); one that does not say lets the CmdError path decide."""
        fn = self._backend_hook("unsupported_states")
        try:
            return frozenset(fn() or ()) if callable(fn) else frozenset()
        except Exception:
            return frozenset()

    def _x_set_state(self, w: UWindow, names, action: int) -> bool:
        """The EWMH _NET_WM_STATE ClientMessage for ONE -b step, sent to the X root about an XWayland window --
        byte for byte what real wmctrl does. `names` are that step's atom names (one, or the two maximize axes).
        False when there is no X window, no X plane to send it on, or the window manager dropped the message.

        _NET_WM_STATE carries TWO atoms in one message (data.l[1] and data.l[2]) and every EWMH window manager
        applies them as one change: wmctrl's own `-b add,maximized_vert,maximized_horz` is a single message with
        both, which is why it does not corrupt a Mutter window's restore size the way two messages do (see
        _state_steps). Splitting the pair here sent two messages where wmctrl sends one, so the fallback was not
        the thing it claims to be. A step with one name puts 0 in the second slot, as wmctrl does.

        Real wmctrl returns as soon as the message is on the wire, and that is all it can do. This is a
        *fallback*: its caller has already been told the compositor said no, and answering "sent" for a message
        KWin 6 drops on the floor turned a failure into silence. So the properties are read back -- all of them,
        because a pair the window manager honoured on one axis only is not what was asked for."""
        if not w.is_x:
            return False
        x = self.x11() if self._x_is_up() else None
        if x is None:
            return False
        names = list(names)
        if not names:
            return False
        try:
            atoms = [x.atom("_NET_WM_STATE_%s" % n) for n in names]
            before = [self._x_state_has(x, w.id, a) for a in atoms]
            x.send_root_message(w.id, "_NET_WM_STATE",
                                [action, atoms[0], atoms[1] if len(atoms) > 1 else 0, 0, 0])
        except Exception as e:
            self.vprint("_NET_WM_STATE ClientMessage failed: %s\n" % e)
            return False
        # want: add -> present, remove -> absent, toggle -> the other one. Which flag a TOGGLE reads is the
        # window manager's business, and there are two cases here because there are only two:
        #   one atom  -- every EWMH window manager reads that atom's own flag, which is before[0].
        #   both maximize atoms -- Mutter special-cases the pair: it folds them into a single `directions`
        #     bitmask and makes one decision, off the horizontal flag (window-x11.c: `action == ADD ||
        #     (action == TOGGLE && !...is_maximized_horizontally (...))`), so both axes land the same way.
        # A step only ever carries two names where the backend names the pair (backend.state_steps ->
        # WindowBackend.maximize_pair_state), which is GNOME and nothing else -- KWin and sway answer None
        # there and reach this with one atom at a time -- so modelling Mutter for the pair is not a guess about
        # the compositor. It is also the only multi-atom step state_steps() can produce; if another is ever
        # folded, the reference flag for it has to be decided here rather than falling out of names[0].
        if action == STATE_TOGGLE:
            ref = before[names.index("MAXIMIZED_HORZ")] if "MAXIMIZED_HORZ" in names else before[0]
            if ref is None:
                return True     # unreadable: "sent" is the best answer there is
            want = [not ref] * len(atoms)
        else:
            if all(b is None for b in before):
                return True     # as above
            want = [action == STATE_ADD] * len(atoms)
        deadline = time.monotonic() + _X_SETTLE
        while True:
            now = [self._x_state_has(x, w.id, a) for a in atoms]
            if all(n is None or n == wanted for n, wanted in zip(now, want)):
                return True
            if time.monotonic() >= deadline:
                self.vprint("_NET_WM_STATE_%s did not appear on 0x%08x "
                            "within %gs\n" % ("/".join(names), w.id, _X_SETTLE))
                return False
            time.sleep(0.02)

    @staticmethod
    def _x_state_has(x, win: int, atom: int):
        """Is `atom` in the window's _NET_WM_STATE? None when it cannot be
        read (an unmapped or already-gone window)."""
        try:
            return atom in x.get_prop_ints(win, "_NET_WM_STATE")
        except Exception:
            return None

    def set_title(self, w: UWindow, title: str, mode: str) -> int:  # -N/-I/-T
        if not w.is_x:
            _warn("window titles cannot be set from outside on Wayland "
                  "(native window); ignoring")
            return 0
        x = self.x11()
        if x is None:
            _warn("cannot reach the XWayland server to set the window "
                  "title; ignoring")
            return 0
        try:
            x.set_name(w.id, title, icon=mode in ("T", "I"), long_=mode in ("T", "N"), utf8=self.utf8)
        except Exception as e:
            _warn("cannot set the window title: %s; ignoring" % e)
        return 0

    # -- listings -----------------------------------------------------------

    def _list_row(self, w: UWindow, show_pid: bool, show_geometry: bool,
                  show_class: bool, machine_len: int) -> str:
        """One `-l` row. Column widths count BYTES like printf's %*s."""
        line = "0x%08x %2d" % (w.id, self._desktop_column(w))
        if show_pid:
            line += " %-6d" % (w.pid if w.pid > 0 else 0)
        if show_geometry:
            gx, gy = self._geometry_column(w)
            line += " %-4d %-4d %-4d %-4d" % (gx, gy, w.w, w.h)
        if show_class:
            cls = w.class_ if w.class_ is not None else "N/A"
            line += " %s%s " % (cls, " " * max(0, 20 - _blen(cls)))
        machine = w.machine or "N/A"
        return line + " %s%s %s" % (" " * max(0, machine_len - _blen(machine)),
                                    machine,
                                    w.title if w.title is not None else "N/A")

    def _desktop_column(self, w: UWindow) -> int:
        """The desktop number the `-l` column prints, which for an X-plane row is the ORIGINAL's own.

        wmctrl reads `_NET_WM_DESKTOP` off the window, falls back to `_WIN_WORKSPACE`, and prints **0**
        when neither is there -- not -1. Measured 2026-09-12 on Xvfb :79 on this guest: an openbox window
        with both properties removed by `xprop -remove` printed `0x0040000c  0` from the pinned wmctrl
        1.07 and from Ubuntu's 1.07+git20240228 alike.

        muffin never writes 0xFFFFFFFF, even for a window it marks sticky. Measured on the
        `resolute-cinnamon` golden (Cinnamon 6.4.13, muffin 6.4.1, wmctrl 1.07+git20240228, 2026-09-12,
        an X11 session so this is muffin's own X path and not Xwayland's):

            nemo-desktop 0x02a00030   _NET_WM_DESKTOP: not found.  _WIN_WORKSPACE: not found.
                                      _NET_WM_STATE = SKIP_PAGER, SKIP_TASKBAR, STICKY
                                      wmctrl -l -> `0x02a00030  0 ... Desktop`
            an xterm made sticky with `wmctrl -b add,sticky`:
                                      _NET_WM_DESKTOP(CARDINAL) = 0   (unchanged)
                                      _NET_WM_STATE = STICKY, FOCUSED
                                      wmctrl -l -> `0x02c0000e  0 ... stickytest`
            the same xterm moved with `wmctrl -t 2`:
                                      _NET_WM_DESKTOP(CARDINAL) = 2, wmctrl -l -> `2`

        So on Cinnamon the original's answer for a sticky window is 0 on both session kinds -- on Wayland
        muffin publishes no `_NET_WM_DESKTOP` on the X window at all and the property-absent rule gives
        the same 0 [M goal2/requests-batch-13.md, six csd-background/nemo-desktop windows, 76 pass /
        1 fail]. Our backends report -1 for "on all workspaces, or no workspace object"
        (`cinnamon_js.py:55`, and the same convention everywhere), and printing that made `wwmctl -l`
        disagree with `wmctrl -l` on exactly those six rows -- the one red check on that flavor.

        The rule is therefore the original's: an X-plane row whose compositor gives no workspace prints
        0. A NATIVE row keeps the -1, because the only original that can see one reads it through the
        proxy, whose shadow publishes `_NET_WM_DESKTOP = 0xFFFFFFFF` for it (`xw11/shadow.py`) and which
        wmctrl prints as -1 -- measured on a headless labwc on this guest the same day, a `foot` shadow
        `0x00600001 -1` from the original and `-1` from this column. Both sides already agree there and
        a blanket 0 would have broken it. `_NET_WM_STATE_STICKY` is where "on every desktop" lives in
        either case, and `wxprop` still prints it.

        Which xwms those are is a MEASUREMENT and not a guess, because Mutter is the other way round:
        on `noble-gnome` (mutter 46.2, GNOME Wayland, 2026-09-12) an xterm given `wmctrl -b add,sticky`
        came back `_NET_WM_DESKTOP(CARDINAL) = 4294967295` and the original printed `0x00800020 -1
        gnome-dbg stickytest`, which is exactly what this column already said off the gnome backend's
        own -1 [M goal2/recon/b17-review-measurements.md 1]. So the fallback is confined to
        `NO_NET_WM_DESKTOP_XWM`, which now also carries cosmic (measured 2026-09-12 on the arch-cosmic
        1.8.0 golden: a sticky xterm has neither `_NET_WM_DESKTOP` nor `_WIN_WORKSPACE`). kwin, hypr and
        wayfire are NOT YET measured: one `xprop -id <xterm> _NET_WM_DESKTOP` on each golden beside a
        `wmctrl -b add,sticky` settles it, rung 4 (the X server beside the compositor) and about ten
        minutes of one VM apiece."""
        if w.desktop >= 0 or not w.is_x:
            return w.desktop
        if self.backend().name not in NO_NET_WM_DESKTOP_XWM:
            return w.desktop
        return 0

    def _geometry_column(self, w: UWindow) -> "tuple[int, int]":
        """The x and y the `-G` column prints: the ORIGINAL's number, bug and all.

        wmctrl 1.07's `-G` is `XGetGeometry` for the size and then `XTranslateCoordinates` fed with the
        window's OWN x,y as the source point instead of 0,0 (main.c, `list_windows`), so the offset is
        counted twice: what it prints is `absolute origin + origin relative to the X parent`. Under a
        REPARENTING window manager the relative half is only the frame border and the error is a few
        pixels -- measured 2026-09-12 on this guest, one xterm placed at 398,215 with `wmctrl -e`, Xvfb
        :77-:79 with three real X window managers:

            openbox   absolute 399,235  relative 1,20   -> wmctrl -lG printed 400  255
            xfwm4     absolute 403,244  relative 5,29   -> wmctrl -lG printed 408  273
            marco     absolute 404,242  relative 16,37  -> wmctrl -lG printed 420  279

        Under a NON-reparenting one the X parent is the root, the relative half IS the absolute one, and
        the number doubles. The wlroots xwm is non-reparenting (`xwininfo -tree` on a headless sway 1.11
        and a headless labwc 0.9.3 on this guest both answered `Parent window id: 0x234 (the root
        window)`), and so is the shadow plane this tree's own proxy publishes. Measured the same day on a
        headless labwc 0.9.3, one xterm at 398,215 484x316, all four readings of the same window:

            pinned wmctrl 1.07 -lGpx   0x0040000c 0 3144704 796  430  484  316  xterm.XTerm
            Ubuntu wmctrl 1.07-7       0x0040000c 0 3144704 796  430  484  316  xterm.XTerm
            through xw11 (W11_PROXY)   0x0040000c 0 3153979 796  430  484  316  xterm.XTerm
            pinned xdotool 4.2026      Position: 398,215 (screen: 0) / Geometry: 484x316
            xwininfo                   Absolute upper-left X: 398  Y: 215

        AGENTS.md: byte parity with the original is the oracle and the originals' bugs are reproduced,
        because a script that reads this column and halves it is a script that must keep working. So this
        prints what wmctrl prints, and `--true-geometry` -- a flag wmctrl never had, the way `wxrandr
        --persistent` is one -- prints the rectangle `xdotool getwindowgeometry` and `xwininfo` agree on.
        `wdotool getwindowgeometry` is NOT this column: xdotool translates from 0,0, so the xdotool clone
        was right as it stood [M goal2/requests-batch-12.md 2].

        A NATIVE row doubles on every backend: the only original that can see one reads it through xw11,
        whose shadows are children of the root (`xw11/req_read.py` answers `TranslateCoordinates` with
        `x + src_x`, which is what a real root child does), and the two programs printing the same four
        numbers for the same `foot` is the parity claim `tests/test_xw11_live.py` pins.

        An X-PLANE row doubles only where the xwm is MEASURED not to reparent -- `NON_REPARENTING_XWM`.
        Mutter frames its Xwayland windows on Wayland as well: on `noble-gnome` (mutter 46.2,
        2026-09-12) the same xterm read `Parent window id: 0xa00004` against a root of `0x221`,
        `xwininfo` put it at absolute 398,252 with a relative origin of 14,49, and BOTH originals printed
        `412 301` -- `absolute + parent-relative`, not `796 504` [M goal2/recon/b17-review-measurements.md
        1]. `x11_mini.get_geometry_raw` now returns that parent-relative `x, y` off GetGeometry
        (`_read_x_props` stores it in `w.rel_x, w.rel_y`), so the exact term IS added here: the
        `-G` column is `absolute + parent-relative`, which is wmctrl's OWN formula on ANY xwm and byte-parity
        with the original. On a reparenting (framing) xwm the parent is the frame and the term is the frame
        offset -- GNOME/mutter: 398,252 + 14,49 = 412,301 (rung 5, one field off the reply the geometry read
        already makes); on a NON-reparenting xwm the parent is the root, so the parent-relative x equals the
        absolute x and the same term gives 2x, the doubling. Framing is MEASURED on mutter (14,49) and on
        cosmic-comp 1:1.8.0-1 (which reparents, but its Xwayland reports a parent-relative origin of 0,0, so
        the term adds nothing and ours == the original: CI run 34667595059 r3 printed both at `4558 169`).
        kwin, muffin and wayfire are NOT YET measured for reparenting -- requests-batch-18.md item C hands
        them to B16 -- but the formula needs no per-xwm list: `absolute + parent-relative` is right on all of
        them. `_NET_FRAME_EXTENTS` is NOT a substitute -- marco 6,6,27,7 for a relative origin of 16,37;
        mutter 0,0,37,0 for 14,49. Where the relative origin was not read (a fake without get_geometry_raw) it
        is 0,0, so the NON_REPARENTING_XWM branch below (which doubles explicitly) is what still serves those
        fakes; on the real x11_mini it is redundant, since `w.x + w.rel_x` already equals 2*w.x there."""
        if self.true_geometry:
            return w.x, w.y
        if w.is_x and self.backend().name not in NON_REPARENTING_XWM:
            # a FRAMING xwm: the original prints absolute + parent-relative (the frame offset)
            return w.x + w.rel_x, w.y + w.rel_y
        return w.x * 2, w.y * 2

    def list_one_window(self, w: UWindow, show_pid: bool, show_geometry: bool, show_class: bool) -> int:  # -L
        """1.07+git's `-r <WIN> -L`: the window's own `-l` row. Its machine column is sized from that one window
        (display_window's `max_client_machine_len == 0` branch), so nothing is padded."""
        sys.stdout.write(self._list_row(w, show_pid, show_geometry,
                                        show_class,
                                        _blen(w.machine or "N/A")) + "\n")
        return 0

    def set_mini_icon(self, w: UWindow, path: str) -> int:  # -M
        """1.07+git's `-r <WIN> -M <PATH>`: an XPM file becomes the window's `_NET_WM_ICON`. An X-only property,
        so a native Wayland window warns like -N/-I/-T does."""
        if not w.is_x:
            _warn("window icons cannot be set from outside on Wayland "
                  "(native window); ignoring")
            return 0
        x = self.x11() if self._x_is_up() else None
        if x is None:
            _warn("cannot reach the XWayland server to set the window "
                  "icon; ignoring")
            return 0
        try:
            width, height, pixels = _read_xpm(path, x)
        except Exception:
            sys.stderr.write("Invalid icon path.\n")
            return 0   # the oracle's own return value: it drops the result
        data = struct.pack("<%dI" % (len(pixels) + 2), width, height, *pixels)
        try:
            x.change_property(w.id, "_NET_WM_ICON", "CARDINAL", 32, data)
        except Exception as e:
            _warn("cannot set the window icon: %s; ignoring" % e)
        return 0

    def list_windows(self, show_pid: bool, show_geometry: bool, show_class: bool) -> int:
        wins = self.windows()
        # The machine column is right-aligned to the LONGEST WM_CLIENT_MACHINE. wmctrl 1.07 uses the LAST row's
        # instead (a bug in main.c), which is stable there only because its list is _NET_CLIENT_LIST, i.e.
        # creation order; ours is Mutter's stacking order, so copying the quirk would re-flow the whole column
        # every time a window is raised. On any real session every row carries the same hostname and the two
        # rules print the same bytes. Widths count BYTES like printf's %*s, not characters.
        machine_len = max([_blen(w.machine) for w in wins if w.machine] or [0])
        for w in wins:
            sys.stdout.write(self._list_row(w, show_pid, show_geometry, show_class, machine_len) + "\n")
        return 0

    def list_current_desktop(self) -> int:  # -j (1.07+git)
        """wmctrl prints _NET_CURRENT_DESKTOP with "%-2d\n"."""
        sys.stdout.write("%-2d\n" % self.backend().get_desktop())
        return 0

    def _desktop_rows(self):
        """[(id, current?, dg, vp, wa, name)]. backend.workspaces() when the backend has it (GNOME: index, name,
        work area -- what wmctrl reads from _NET_DESKTOP_NAMES/_NET_WORKAREA; sway: one row per workspace, index
        num-1, the same mapping wdotool's desktop commands use); otherwise synthesized from the generic backend
        API."""
        backend = self.backend()
        size_fn = getattr(backend, "display_size", None)
        try:
            dg = "%dx%d" % size_fn() if size_fn else "N/A"
        except Exception:
            dg = "N/A"
        rows = []
        ws_fn = getattr(backend, "workspaces", None)
        typed = ws_fn() if callable(ws_fn) else None
        if typed is not None:
            # A nameless workspace prints its index.
            cur = next((i for i, ws in enumerate(typed) if ws.active), -1)
            vps = self._viewports(len(typed), cur)
            for i, ws in enumerate(typed):
                wx, wy, ww, wh = ws.work_area
                rows.append((ws.index, ws.active, dg, vps[i],
                             "%d,%d %dx%d" % (wx, wy, ww, wh),
                             ws.name or "%d" % ws.index))
            return rows
        cur = backend.get_desktop()
        n = backend.num_desktops()
        vps = self._viewports(n, cur)
        for i in range(n):
            rows.append((i, i == cur, dg, vps[i], "N/A", "N/A"))
        return rows

    def _viewports(self, n: int, cur: int) -> list[str]:
        """The VP column for `n` desktops, wmctrl's rule exactly.

        wmctrl prints `_NET_DESKTOP_VIEWPORT[2i]` and `[2i+1]` for desktop i when the array reaches that far and
        N/A when it does not, with ONE exception, which is the whole of its `-o` world: an array of exactly one
        pair is a WM that scrolls a single viewport around, so the pair belongs to the current desktop and every
        other row prints N/A — row 0 included, which is the reading a plain `[2i]` rule gets wrong. Read it from
        the X server when one is already there, so the column is the same as `wmctrl -d`'s whatever the
        compositor publishes; a longer-but-short array is still indexed and still prints N/A past its end, and
        that N/A does not move to the current row [M 2026-09-11, Xvfb :81 on this box, the pinned wmctrl 1.07,
        twelve readings: `[7,9]` over 2 desktops prints `7,9`/`N/A` with cur=0 and `N/A`/`7,9` with cur=1;
        `[7,9,1,2]` over 3 prints `7,9`/`1,2`/`N/A` for cur=0, 1 and 2 alike; `[7,9,1]` prints `7,9`/`N/A`/`N/A`;
        `[7]` prints N/A everywhere; absent prints N/A everywhere].

        With no property to read at all, every row is `0,0`: X is the oracle and a WM that publishes viewports
        publishes an origin for each desktop, so `0,0` everywhere is the answer, not `0,0` on the current row
        and an absence on the others. It is also the same bytes the proxy publishes for a session with no X of
        its own (`_NET_DESKTOP_VIEWPORT` as `[0, 0]` per desktop, xw11/shadow.py:root_props), which is what
        makes real `wmctrl -d` through `xw11` and `wwmctl -d` agree on this column [M goal2/recon/gaps.md 3a,
        Xvfb :77, 2026-09-11; and M 2026-09-11 on this box, headless sway with two workspaces: both routes
        print `VP: 0,0` on BOTH rows, where this rule used to print `N/A` on the non-current one]."""
        vals = []
        if self._x11 not in ("unset", None) or session.xwayland_running():
            x = self.x11()
            if x is not None:
                try:
                    vals = x.get_prop_ints(x.root(), "_NET_DESKTOP_VIEWPORT")
                except Exception:
                    vals = []
        out = []
        for i in range(n):
            if len(vals) == 2:             # one pair: the current desktop's, and nobody else's
                out.append("%d,%d" % (vals[0], vals[1]) if i == cur else "N/A")
            elif len(vals) >= 2 * (i + 1):
                out.append("%d,%d" % (vals[2 * i], vals[2 * i + 1]))
            elif vals:
                out.append("N/A")          # a real, short array: wmctrl's own N/A
            else:
                out.append("0,0")          # nothing published: every desktop begins at its origin
        return out

    def list_desktops(self) -> int:
        rows = self._desktop_rows()
        dgw = max((len(r[2]) for r in rows), default=0)
        vpw = max((len(r[3]) for r in rows), default=0)
        waw = max((len(r[4]) for r in rows), default=0)
        for did, cur, dg, vp, wa, name in rows:
            sys.stdout.write("%-2d %s DG: %s  VP: %s  WA: %s  %s\n" % (
                did, "*" if cur else "-", dg.ljust(dgw), vp.ljust(vpw),
                wa.ljust(waw), name))
        return 0

    def wm_info(self) -> int:
        name = class_ = None
        pid = 0
        showing = None
        got_x = False
        # On GNOME the X plane is only opened when Xwayland is already running (it is spawned on demand): the
        # answer is the same either way, Mutter's check window says "GNOME Shell" and the bridge's wm_name says
        # the same.
        x = self.x11() if self._x_is_up() else None
        if x is not None:
            try:
                sup = x.get_prop_ints(x.root(), "_NET_SUPPORTING_WM_CHECK")
                if not sup and self._views_seen:
                    # Xwayland just came up and the compositor has not finished its WM setup on the root yet:
                    # give it a moment rather than misreporting the compositor name
                    deadline = time.monotonic() + 2.0
                    while not sup and time.monotonic() < deadline:
                        time.sleep(0.05)
                        sup = x.get_prop_ints(x.root(), "_NET_SUPPORTING_WM_CHECK")
                if sup:
                    got_x = True
                    name = _xtry(lambda: x.get_prop_string(sup[0], "_NET_WM_NAME")) or None
                    class_ = _xtry(lambda: x.get_prop_string(sup[0], "WM_CLASS")) or None
                    pid = _xtry(lambda: x.get_pid(sup[0])) or 0
                    ints = _xtry(lambda: x.get_prop_ints(x.root(), "_NET_SHOWING_DESKTOP"))
                    showing = ints[0] if ints else None
            except Exception:
                got_x = False
        if not got_x:
            # pure Wayland: the compositor IS the window manager; a backend that knows what its WM calls itself
            # (GNOME: the same string Mutter puts on the X check window) says so
            backend = self.backend()
            name = getattr(backend, "wm_name", None) or getattr(backend, "name", None)
        if name is None:
            self.vprint("Cannot get name of the window manager "
                        "(_NET_WM_NAME).\n")
        if class_ is None:
            self.vprint("Cannot get class of the window manager "
                        "(WM_CLASS).\n")
        if pid <= 0:
            self.vprint("Cannot get pid of the window manager "
                        "(_NET_WM_PID).\n")
        if showing is None:
            self.vprint("Cannot get the _NET_SHOWING_DESKTOP property.\n")
        sys.stdout.write("Name: %s\n" % (name if name else "N/A"))
        sys.stdout.write("Class: %s\n" % (class_ if class_ else "N/A"))
        sys.stdout.write("PID: %s\n" % (pid if pid > 0 else "N/A"))
        sys.stdout.write('Window manager\'s "showing the desktop" mode: '
                         "%s\n" % ("N/A" if showing is None
                                   else "ON" if showing == 1 else "OFF"))
        return 0

    # -- desktop-level actions ----------------------------------------------

    def switch_desktop(self, param: str) -> int:  # -s
        target = _atoi(param)
        if target < 0:
            # wmctrl only rejects exactly -1 (other negatives go into the void, exit 0); negative desktops
            # cannot exist here, so reject them all with the same message instead of confusing sway
            sys.stderr.write("Invalid desktop ID.\n")
            return 1
        self.backend().set_desktop(target)
        return 0

    def showing_desktop(self, param: str) -> int:  # -k
        if param not in ("on", "off", "toggle"):
            # `toggle` is our extension, but the sentence is wmctrl's and
            # is parity-checked: keep the extension, keep the message.
            sys.stderr.write('The argument to the -k option must be either '
                             '"on" or "off"\n')
            return 1
        if param == "toggle":
            param = "off" if self._showing_desktop() == 1 else "on"
        # Mutter's own show-desktop mode is reachable from the X plane: _NET_SHOWING_DESKTOP on the X root is
        # what real wmctrl sends, and on GNOME it really works -- every window is hidden, `-k off` brings them
        # all back untouched, and `-m`, which reads the same property, agrees. That is the mode; the bridge's
        # minimize-everything stand-in exists because the shell exports no API for it, and is the fallback for a
        # session with no X plane.
        if self._x_showing_desktop(param == "on"):
            return 0
        show_fn = self._backend_hook("show_desktop")
        if show_fn is None:
            _warn("Wayland compositors have no 'showing the desktop' mode; "
                  "ignoring")
            return 0
        try:
            show_fn(param == "on")
        except CmdError as e:
            _warn("%s; ignoring" % e)
        return 0

    def _x_showing_desktop(self, show: bool) -> bool:
        """_NET_SHOWING_DESKTOP to the X root, real wmctrl's own request.

        False when the X plane is not there to take it, and false when the window manager does not answer --
        Mutter reports the mode by updating the root property, so a missing update means an Xwayland whose WM
        half is not up, and the caller falls back to the compositor's own stand-in rather than doing nothing at
        all."""
        x = self.x11() if self._x_is_up() else None
        if x is None:
            return False
        want = 1 if show else 0
        if self._showing_desktop() == want:
            return True          # already in that mode: nothing to ask for
        try:
            x.send_root_message(x.root(), "_NET_SHOWING_DESKTOP", [want, 0, 0, 0, 0])
        except Exception as e:
            self.vprint("_NET_SHOWING_DESKTOP ClientMessage failed: %s\n" % e)
            return False
        deadline = time.monotonic() + 1.0
        while True:
            if self._showing_desktop() == want:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _showing_desktop(self):
        """The X root's _NET_SHOWING_DESKTOP (Mutter keeps it for the XWayland plane, and -k drives it there
        too), or None when there is no X plane to ask -- `-k toggle` then reads as "currently off", which is
        what real wmctrl does with an absent property, minus the NULL dereference."""
        x = self.x11() if self._x_is_up() else None
        if x is None:
            return None
        vals = _xtry(lambda: x.get_prop_ints(x.root(), "_NET_SHOWING_DESKTOP"))
        return vals[0] if vals else None

    def _backend_hook(self, name: str):
        """An optional backend method (GNOME: show_desktop, set_num_desktops), or None -- also when no backend
        can be found, so the warn-and-succeed fallbacks stay session-less."""
        try:
            return getattr(self.backend(), name, None)
        except CmdError:
            return None

    def change_viewport(self, param: str) -> int:  # -o
        if not _parse_two_uints(param):
            sys.stderr.write("The -o option expects two integers separated "
                             "with a comma.\n")
            return 1
        _warn("desktop viewports do not exist on Wayland; ignoring")
        return 0

    def change_geometry(self, param: str) -> int:  # -g
        if not _parse_two_uints(param):
            sys.stderr.write("The -g option expects two integers separated "
                             "with a comma.\n")
            return 1
        _warn("desktop geometry is managed by the compositor; ignoring")
        return 0

    def change_number_of_desktops(self, param: str) -> int:  # -n
        if re.match(r"\s*[+-]?\d+", param or "") is None:
            sys.stderr.write("The -n option expects an integer.\n")
            return 1
        set_fn = self._backend_hook("set_num_desktops")
        if set_fn is None:
            _warn("Wayland workspaces are managed by the compositor; ignoring")
            return 0
        try:
            set_fn(_atoi(param))
        except CmdError as e:
            # "The window manager may ignore the request" (GNOME's dynamic
            # workspaces do exactly that)
            _warn("%s; ignoring" % e)
        return 0

    # -- driver for window actions ------------------------------------------

    def action_window(self, mode: str, param_window: str, param: str | None,
                      match_by_id: bool, match_by_cls: bool,
                      full_match: bool, show_pid: bool = False,
                      show_geometry: bool = False,
                      show_class: bool = False) -> int:
        w = self.find_target(param_window, match_by_id, match_by_cls, full_match)
        if w is None:
            return 1  # wmctrl exits 1 silently when nothing matches
        self.vprint("Using window: 0x%08x\n" % w.id)
        if mode == "a":
            self.activate(w)
            return 0
        if mode == "c":
            self.close(w)
            return 0
        if mode == "R":
            return self.to_current_and_activate(w)
        if mode == "t":
            return self.to_desktop(w, _atoi(param or ""))
        if mode == "e":
            return self.move_resize(w, param or "")
        if mode == "y":  # 1.07+git: -e, then activate
            rv = self.move_resize(w, param or "")
            self.activate(w)
            return rv
        if mode == "Y":  # 1.07+git: iconify (XIconifyWindow)
            self.backend().minimize(w.node_id)
            return 0
        if mode == "z":  # 1.07+git, undocumented: XLowerWindow
            self.backend().lower(w.node_id)
            return 0
        if mode == "L":  # 1.07+git (distro patch): this window's -l row
            return self.list_one_window(w, show_pid, show_geometry, show_class)
        if mode == "M":  # 1.07+git (distro patch): _NET_WM_ICON from an XPM
            return self.set_mini_icon(w, param or "")
        if mode == "E":  # 1.07+git, undocumented: print the title
            sys.stdout.write("%s\n" % (w.title if w.title is not None else ""))
            return 0
        if mode == "b":
            return self.window_state(w, param or "")
        if mode in ("N", "I", "T"):
            return self.set_title(w, param or "", mode)
        sys.stderr.write("Unknown action: '%s'\n" % mode)
        return 1


# -- small parsing/format helpers -------------------------------------------

def _uwindow(win, host: str | None, xid: int | None, cls: str | None, title: str | None) -> UWindow:
    """A UWindow over one backend Window, for all three listing sources.

    `xid` is the window's X id when it has one (0/None for a native view): it becomes the printed id and decides
    is_x, while the compositor's node id stays in node_id, which is what every action addresses. `title` is a
    parameter and not just `win.title` because the three sources disagree about the absent title -- sway carries
    it on the tree node and the generic listing folds "" to None -- and -l prints None as "N/A"."""
    return UWindow(
        id=xid if xid else win.id,
        node_id=win.id,
        is_x=bool(xid),
        title=title,
        class_=cls,
        machine=host,
        pid=win.pid,
        x=win.x, y=win.y, w=win.w, h=win.h,
        desktop=win.desktop,
        focused=win.focused,
        fx=win.x, fy=win.y, fw=win.w, fh=win.h,
    )


def _dot_class(instance: str | None, class_: str | None) -> str | None:
    """WM_CLASS "inst\\0cls\\0" printed the wmctrl way: "inst.cls".

    A class of "" means "absent" (degenerate single-string WM_CLASS, where
    get_wm_class cannot express absence in its str return): print just
    "inst" with no trailing dot, like the wmctrl oracle. An empty INSTANCE
    is kept — b"\\0cls\\0" really prints ".cls"."""
    if class_ == "":
        class_ = None
    if instance is not None and class_ is not None:
        return "%s.%s" % (instance, class_)
    if class_ is not None:
        return class_
    if instance is not None:
        return instance
    return None


# ICCCM win_gravity (wmctrl -e's first field) as (column, row), with 0 = the left/top edge, 1 = the middle, 2 =
# the right/bottom edge: which point of the window the request positions. StaticGravity (10) addresses the
# client rather than the frame and is handled apart; 0 (use the window's own WM_SIZE_HINTS gravity) and unknown
# values are NorthWest.
_GRAVITY_CORNER = {1: (0, 0), 2: (1, 0), 3: (2, 0), 4: (0, 1), 5: (1, 1),
                   6: (2, 1), 7: (0, 2), 8: (1, 2), 9: (2, 2)}
_GRAVITY_STATIC = 10


# how many (frame rect, client rect) pairs -e samples while waiting for two
# consecutive ones to agree, i.e. for the window to hold still
_EXTENT_SAMPLES = 4


def _extents_of(frame, client):
    """(left, top, right, bottom) of `frame` around `client`, or None when
    the client rectangle does not sit inside the frame."""
    fx, fy, fw, fh = frame
    cx, cy, cw, ch = client
    left, top = cx - fx, cy - fy
    right, bottom = fw - cw - left, fh - ch - top
    if min(left, top, right, bottom) < 0:
        return None
    return left, top, right, bottom


def _anchor(pos: int, size: int) -> int:
    """Where the gravity's reference point sits inside a rectangle of `size`: its leading edge, its middle, or
    its trailing edge (Mutter's adjust_for_gravity arithmetic, halves truncated)."""
    if pos == 1:
        return size // 2
    if pos == 2:
        return size
    return 0


def _place_axis(pos: int, static: bool, req: int, lead: int,
                f_old: int, fs_old: int, cs_new: int, fs_new: int,
                keep_anchor: bool = True) -> int:
    """The frame's new leading edge on one axis.

    `pos` is the gravity's reference point (see _GRAVITY_CORNER), `static` marks StaticGravity, `req` the
    requested coordinate (-1 = keep), `lead` the leading frame extent (left or top), `f_old`/`fs_old` the
    frame's current edge and size, `cs_new`/`fs_new` the client and frame sizes the request asks for. A request
    positions the frame's reference point on the same point of the requested client rectangle.

    `keep_anchor` says what a -1 means, and it is a property of the whole request rather than of this axis: for
    `G,-1,-1,W,H` -- a bare resize -- Mutter keeps the gravity's reference point, so SouthEast grows up and to
    the left; where the other axis carries a coordinate, it keeps this axis's frame edge instead. Both were
    measured against Mutter on GNOME 46; the second case is why `9,-1,200,400,300` used to land 80 px from where
    real wmctrl leaves the window."""
    if static:                       # the client itself is addressed
        return f_old if req == -1 else req - lead
    if req == -1:
        if not keep_anchor:
            return f_old
        return f_old + _anchor(pos, fs_old) - _anchor(pos, fs_new)
    return req + _anchor(pos, cs_new) - _anchor(pos, fs_new)


# XPM color keys, most specific first: wmctrl's own order is c then g
_XPM_KEYS = ("c", "g", "g4", "m", "s")


def _read_xpm(path: str, x):
    """(width, height, [ARGB pixels]) from an XPM file, the way wmctrl's -M reads one: XpmReadFileToXpmImage
    plus XParseColor per colour.

    Colour names are resolved by the X server itself (LookupColor, which is what XParseColor asks), so the
    server's own rgb.txt decides -- and a name it does not know becomes 0, exactly as wmctrl's "color parsing
    failed" branch does. "None" is 0 too: transparent."""
    with open(path, "rb") as fh:
        text = fh.read().decode("latin-1")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if not items:
        raise ValueError("no XPM strings")
    head = items[0].split()
    width, height, ncolors, cpp = (int(v) for v in head[:4])
    if min(width, height, ncolors, cpp) < 0 or len(items) < 1 + ncolors + height:
        raise ValueError("truncated XPM")
    table = {}
    for spec in items[1:1 + ncolors]:
        chars, rest = spec[:cpp], spec[cpp:]
        toks = rest.split()
        colors, key = {}, None
        for t in toks:
            if t in _XPM_KEYS and key is None:
                key = t
                colors[key] = []
            elif key is None:
                continue
            elif t in _XPM_KEYS:
                key = t
                colors[key] = []
            else:
                colors[key].append(t)
        name = None
        for k in ("c", "g"):
            if colors.get(k):
                name = " ".join(colors[k])
                break
        table[chars] = _xpm_pixel(name, x)
    pixels = []
    for row in items[1 + ncolors:1 + ncolors + height]:
        for i in range(width):
            pixels.append(table.get(row[i * cpp:(i + 1) * cpp], 0))
    return width, height, pixels


def _xpm_pixel(name, x) -> int:
    """One XPM colour as the ARGB CARD32 wmctrl packs into _NET_WM_ICON."""
    if not name or name.lower() == "none":
        return 0
    rgb = _parse_color(name)
    if rgb is None:
        # a name, not a numeric spec: the server's own rgb.txt decides,
        # and a name it does not know is 0 (wmctrl's "parsing failed")
        rgb = _xtry(lambda: x.lookup_color(name))
    if rgb is None:
        return 0
    r, g, b = (v >> 8 for v in rgb)
    return 0xFF000000 | (r << 16) | (g << 8) | b


def _parse_color(spec: str):
    """XParseColor's client-side numeric forms as 16-bit (r, g, b), or None for a colour NAME (which only the
    server can resolve). `#rgb`, `#rrggbb`, `#rrrgggbbb`, `#rrrrggggbbbb` are left-aligned in 16 bits, exactly
    as Xlib scales them; `rgb:r/g/b` takes 1-4 digits per component."""
    if spec.startswith("#"):
        digits = spec[1:]
        if len(digits) % 3 or not 3 <= len(digits) <= 12:
            return None
        n = len(digits) // 3
        try:
            vals = [int(digits[i * n:(i + 1) * n], 16) for i in range(3)]
        except ValueError:
            return None
        return tuple(v << (16 - 4 * n) for v in vals)
    if spec.lower().startswith("rgb:"):
        parts = spec[4:].split("/")
        if len(parts) != 3:
            return None
        out = []
        for part in parts:
            if not 1 <= len(part) <= 4:
                return None
            try:
                v = int(part, 16)
            except ValueError:
                return None
            out.append(v << (16 - 4 * len(part)))
        return tuple(out)
    return None


def _parse_win_id(s: str) -> int | None:
    """wmctrl -i: sscanf "0x%lx" / "0X%lx" / "%lu" prefix semantics."""
    m = re.match(r"\s*0[xX]([0-9a-fA-F]+)", s or "")
    if m:
        return int(m.group(1), 16)
    m = re.match(r"\s*\+?(\d+)", s or "")
    if m:
        return int(m.group(1))
    return None


def _parse_two_uints(s: str):
    """sscanf(s, "%lu,%lu") == 2 (%lu accepts a sign — strtoul wraps
    negatives, so "-1,-1" parses; the oracle exits 0 on it)"""
    m = re.match(r"\s*[+-]?(\d+),\s*[+-]?(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _blen(s: str) -> int:
    """printf column widths count bytes, not characters."""
    return len(s.encode("utf-8", "replace"))


def _xtry(fn):
    try:
        return fn()
    except Exception:
        return None
