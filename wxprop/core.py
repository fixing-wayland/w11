"""Plane resolution, property assembly, -spy loops.

Two planes, per docs/WXPROP.md:

- X windows (XWayland): everything through wdotool.x11_mini against the real
  X server — GetProperty/ListProperties for reads, ChangeProperty/
  DeleteProperty for -set/-remove, PropertyNotify events for -spy. Real
  xprop is the byte oracle.
- native Wayland windows (compositor node ids): a synthesized property set
  built from compositor data, printed through the exact same fmt.py
  machinery so `wxprop -id N WM_CLASS`-style script parsing just works.
  -set/-remove on a native window is a clean one-line error (there is no
  property store to write to); -spy rides the compositor's own event
  stream (backend.events()) and reprints the synthesized property that
  changed.

Window ids: an id that matches a compositor node resolves through the
node (an XWayland node redirects to its real X window id); anything else
goes to the X server when one is reachable, exactly like xprop would.

GNOME (the bridge backend, docs/WXPROP.md "GNOME"): the compositor list comes
from backend.views() (X ids of XWayland windows, WM_CLASS pairs, states,
window types), the X plane is opened with the DISPLAY/XAUTHORITY the bridge
reports (Mutter's Xwayland needs its cookie) and only when Xwayland is
actually running (Mutter spawns it on demand -- a speculative connect
would start a server just to ask), -spy on native windows and the root
rides the bridge's WindowEvent/WorkspaceEvent signals (backend.events()),
and -root merges the real X root with the bridge's view of all windows
(MergedRootTarget).
"""

import os
import queue
import struct
import sys
import threading

from wxprop.fmt import FatalError

try:  # the X error classes for narrow catches in the -name DFS, the X
    # error text for -id on a dead window, and this machine's name
    from wdotool.x11_mini import X11Error, X_ERROR_TEXT, XUnavailable, hostname
except Exception:  # pragma: no cover - x11_mini is pure stdlib, always imports
    X_ERROR_TEXT = {}

    def hostname() -> str:
        return ""

    class X11Error(Exception):
        pass

    class XUnavailable(Exception):
        pass

# PropertyChangeMask | StructureNotifyMask
SPY_EVENT_MASK = 0x420000

# -- Xlib's default error report (what xprop shows on e.g. BadWindow) -------

_X_OPCODE_NAMES = {
    2: "X_ChangeWindowAttributes", 14: "X_GetGeometry", 15: "X_QueryTree",
    16: "X_InternAtom", 17: "X_GetAtomName", 18: "X_ChangeProperty",
    19: "X_DeleteProperty", 20: "X_GetProperty", 21: "X_ListProperties",
    25: "X_SendEvent", 40: "X_TranslateCoords", 43: "X_GetInputFocus",
}

# resource-id errors get the "Resource id in failed request" line
_X_RESOURCE_ERRORS = {3, 4, 6, 7, 9, 12, 13, 14}


def x_error_report(err) -> str:
    text = X_ERROR_TEXT.get(err.code, "%s (unknown error)" % err.name)
    seq = getattr(err, "sequence", 0)
    lines = ["X Error of failed request:  %s" % text,
             "  Major opcode of failed request:  %d (%s)"
             % (err.major, _X_OPCODE_NAMES.get(err.major,
                                               "X_Unknown%d" % err.major))]
    if err.code == 2:
        lines.append("  Value in failed request:  0x%x" % err.bad_value)
    elif err.code == 5:
        lines.append("  AtomID (in failed request):  0x%x" % err.bad_value)
    elif err.code in _X_RESOURCE_ERRORS:
        lines.append("  Resource id in failed request:  0x%x" % err.bad_value)
    lines.append("  Serial number of failed request:  %d" % seq)
    lines.append("  Current serial number in output stream:  %d" % seq)
    return "\n".join(lines) + "\n"


# -- injection seams (unit tests monkeypatch these) --------------------------


def _x11_connect(display, xauthority=None):
    """An x11_mini.X11Conn, or None. Never raises. `xauthority`: the compositor's cookie file when the backend
    knows it (GNOME bridge XInfo); x11_mini otherwise falls back to $XAUTHORITY and the session scan."""
    if os.environ.get("WXPROP_NO_X"):
        return None
    try:
        from wdotool import x11_mini
        if xauthority:
            return x11_mini.X11Conn(display, xauthority=xauthority)
        return x11_mini.X11Conn(display)
    except Exception:
        return None


def _detect_backend():
    """A wdotool window backend, or None when there is none to be had. Raises the detector's CmdError (e.g. the
    GNOME bridge install hint) so Session can keep the reason for the error paths that need it.

    None outright on a plain X11 session -- hardening, not a fix for an observed wrong answer. We only run at
    all there when no real xprop is installed (`maybe_exec_real(..., fallback_native=True)`), and what is
    promised then is an X11 client; but the detector goes by the session bus, and a compositor that is a plain X
    window manager owns its bus name there just the same (KWin on Xorg owns org.kde.KWin), so `-root` used to be
    answered by `MergedRootTarget` with the compositor's `_NET_CLIENT_LIST`/`_NET_ACTIVE_WINDOW`/... over the X
    root's. On both Plasma X11 images this branch measured **byte-identical** to the real xprop -- every window
    on an X11 session is an X window, so the two views agree, and the timings are the same to within noise. What
    it removes is the case where they cannot agree: with no X id among the compositor's windows
    `Session.x_present()` is false and the answer is the *synthesized* root (`_NET_SUPPORTING_WM_CHECK ... 0x0`,
    ids minted from compositor handles), which on a session that has a real X root and a real EWMH window
    manager is simply wrong. That state is reachable in principle and is pinned hermetically
    (tests/test_wxprop_cli.py); it was not reproducible on Plasma 5.27 or 6.6 on Xorg.
    `W11_PASSTHROUGH=never` still says "our own code whatever the session" and still detects, so the
    Wayland paths stay reachable from an X11 development box (and the whole test suite)."""
    from w11common import passthrough
    from wdotool import backend_detect
    if passthrough.session_kind("xprop") == "x11":
        return None
    return backend_detect.detect()


def _progname() -> str:
    """argv[0] the way xprop prints it in its diagnostics.

    Verbatim, not basename(): xprop sets `program_name = argv[0]` and never trims it, so
    `/usr/bin/xprop -badflag` says "/usr/bin/xprop: ...". We print the same string for the same invocation,
    which is the whole point of a drop-in. `python -m wxprop` has no name worth printing, so the tool name
    stands in (WXPROP_ARGV0 overrides both)."""
    override = os.environ.get("WXPROP_ARGV0")
    if override:
        return override
    argv0 = sys.argv[0] or ""
    if not argv0 or os.path.basename(argv0) in ("__main__.py", "-c", "-m"):
        return "wxprop"
    return argv0


def _xwayland_running() -> bool:
    try:
        from w11common import session
        return session.xwayland_running()
    except Exception:
        return False


# marker key on node dicts synthesized from backend.views(): the richer
# state/type synthesis below keys on it so sway trees print as before
_VIEW_KEY = "_view"


def _node_from_view(v) -> dict:
    """A sway-shaped node dict (the keys NativeViewTarget reads) from a
    typed View, plus the extras only a views() backend knows."""
    w = v.window
    xid = int(v.xid or 0)
    node = {
        "id": w.id, "name": w.title, "pid": w.pid,
        "window": xid or None,
        "app_id": (v.app_id or None) if not xid else None,
        "window_properties": ({"instance": v.instance, "class": v.cls,
                               "title": w.title} if xid else None),
        "fullscreen_mode": 1 if v.fullscreen else 0,
        "visible": not (v.minimized or v.hidden),
        "sticky": bool(v.sticky), "urgent": bool(v.urgent),
        "focused": bool(w.focused),
        "maximized_h": bool(v.maximized_h),
        "maximized_v": bool(v.maximized_v),
        "above": bool(v.above), "below": bool(v.below),
        # the flag only Cinnamon's muffin sets; every other View defaults it False
        "shaded": bool(getattr(v, "shaded", False)),
        "skip_taskbar": bool(v.skip_taskbar),
        "skip_pager": bool(v.skip_pager),
        "window_type": v.window_type or "NORMAL",
        "transient_for": int(v.transient_for or 0),
        "client_type": v.client_type, "instance": v.instance, "class": v.cls,
        _VIEW_KEY: True,
    }
    if not xid and not node["app_id"] and (v.instance or v.cls):
        # a native window without an app id but with a WM_CLASS pair
        node["window_properties"] = {"instance": v.instance, "class": v.cls, "title": w.title}
    return node


class Session:
    """Lazy handles on the two planes."""

    def __init__(self, display=None):
        self.display = display
        self._x = "unset"
        self._backend = "unset"
        self._views = None  # did the backend answer views()? None = unknown
        self.backend_error = None

    def _x_info(self):
        """(DISPLAY, XAUTHORITY) from the backend, or None."""
        b = self.backend()
        fn = getattr(b, "x_info", None) if b is not None else None
        if not callable(fn):
            return None
        try:
            info = fn()
        except Exception:
            return None
        if not info or not (info[0] or info[1]):
            return None
        return info[0] or None, info[1] or None

    def x11(self):
        if self._x == "unset":
            display, xauth = self.display, None
            if display is None:
                info = self._x_info()
                if info:
                    display, xauth = info
            self._x = _x11_connect(display, xauth)
        return self._x

    def backend(self):
        if self._backend == "unset":
            try:
                self._backend = _detect_backend()
            except Exception as e:
                self._backend = None
                self.backend_error = str(e) or type(e).__name__
        return self._backend

    def has_views(self) -> bool:
        """Does the backend hand out typed views (GNOME bridge)?"""
        if self._views is None:
            self.nodes()
        return bool(self._views)

    def x_present(self) -> bool:
        """May the X plane be opened without side effects? Always on sway and generic backends (the old
        behavior: try it); on a views() backend only when it lists an X window or an Xwayland process exists --
        Mutter spawns Xwayland on demand, and a speculative connect from `wxprop -root` would start one just to
        look."""
        if self._x not in ("unset", None):
            return True
        if self.display is not None or os.environ.get("WXPROP_NO_X"):
            return True
        if not self.has_views():
            return True
        if any(node.get("window") for node, _w in self.nodes()):
            return True
        return _xwayland_running()

    def nodes(self):
        """[(node, win)] from the compositor, [] when unavailable. `node` is the raw tree dict for sway (carries
        "window" for XWayland views), a dict built from a typed View (backend.views(): GNOME), or a minimal
        synthesized dict for generic backends."""
        b = self.backend()
        if b is None:
            self._views = False
            return []
        views_fn = getattr(b, "views", None)
        if callable(views_fn):
            try:
                views = views_fn()
            except Exception as e:
                self.backend_error = str(e) or type(e).__name__
                views = None
            if views is not None:
                self._views = True
                return [(_node_from_view(v), v.window) for v in views]
        self._views = False
        nodes_fn = getattr(b, "_nodes", None)
        if nodes_fn is not None:
            try:
                return [(node, win) for node, win, _f, _ws in nodes_fn()]
            except Exception:
                pass
        try:
            wins = b.list()
        except Exception:
            return []
        out = []
        for w in wins:
            out.append(({"app_id": w.class_ or None, "name": w.title,
                         "pid": w.pid, "fullscreen_mode": 0,
                         "visible": w.visible, "sticky": False}, w))
        return out

    def workspace_names(self):
        """Workspace names from backend.workspaces(), or None."""
        b = self.backend()
        fn = getattr(b, "workspaces", None) if b is not None else None
        if not callable(fn):
            return None
        try:
            ws = fn()
        except Exception:
            return None
        if ws is None:
            return None
        return [w.name or "%d" % w.index for w in ws]


# -- the synthesized native atom table --------------------------------------

_PREDEFINED_ATOMS = (
    "PRIMARY", "SECONDARY", "ARC", "ATOM", "BITMAP", "CARDINAL", "COLORMAP",
    "CURSOR", "CUT_BUFFER0", "CUT_BUFFER1", "CUT_BUFFER2", "CUT_BUFFER3",
    "CUT_BUFFER4", "CUT_BUFFER5", "CUT_BUFFER6", "CUT_BUFFER7", "DRAWABLE",
    "FONT", "INTEGER", "PIXMAP", "POINT", "RECTANGLE", "RESOURCE_MANAGER",
    "RGB_COLOR_MAP", "RGB_BEST_MAP", "RGB_BLUE_MAP", "RGB_DEFAULT_MAP",
    "RGB_GRAY_MAP", "RGB_GREEN_MAP", "RGB_RED_MAP", "STRING", "VISUALID",
    "WINDOW", "WM_COMMAND", "WM_HINTS", "WM_CLIENT_MACHINE", "WM_ICON_NAME",
    "WM_ICON_SIZE", "WM_NAME", "WM_NORMAL_HINTS", "WM_SIZE_HINTS",
    "WM_ZOOM_HINTS", "MIN_SPACE", "NORM_SPACE", "MAX_SPACE", "END_SPACE",
    "SUPERSCRIPT_X", "SUPERSCRIPT_Y", "SUBSCRIPT_X", "SUBSCRIPT_Y",
    "UNDERLINE_POSITION", "UNDERLINE_THICKNESS", "STRIKEOUT_ASCENT",
    "STRIKEOUT_DESCENT", "ITALIC_ANGLE", "X_HEIGHT", "QUAD_WIDTH", "WEIGHT",
    "POINT_SIZE", "RESOLUTION", "COPYRIGHT", "NOTICE", "FONT_NAME",
    "FAMILY_NAME", "FULL_NAME", "CAP_HEIGHT", "WM_CLASS", "WM_TRANSIENT_FOR",
)

_EXTENDED_ATOMS = (
    "UTF8_STRING", "WM_STATE", "WM_PROTOCOLS", "WM_DELETE_WINDOW",
    "WM_TAKE_FOCUS", "_NET_WM_NAME", "_NET_WM_ICON_NAME", "_NET_WM_PID",
    "_NET_WM_DESKTOP", "_NET_WM_STATE", "_NET_WM_STATE_FULLSCREEN",
    "_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_STICKY", "_NET_WM_STATE_FOCUSED",
    "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_ICON",
    "_NET_SUPPORTED", "_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING",
    "_NET_ACTIVE_WINDOW", "_NET_SUPPORTING_WM_CHECK", "_NET_CURRENT_DESKTOP",
    "_NET_NUMBER_OF_DESKTOPS", "_NET_DESKTOP_NAMES",
    # what a views() backend (GNOME) additionally knows
    "_NET_WM_STATE_MAXIMIZED_HORZ", "_NET_WM_STATE_MAXIMIZED_VERT",
    "_NET_WM_STATE_ABOVE", "_NET_WM_STATE_BELOW",
    "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER",
    "_NET_WM_STATE_DEMANDS_ATTENTION",
    # muffin kept the state mutter dropped (`shade`/`unshade`/`is_shaded` are
    # Meta.Window methods on Cinnamon and nowhere else), and real xprop on a
    # Cinnamon X11 session prints this atom for a shaded window -- X is the
    # oracle, so the native plane has to be able to name it too
    "_NET_WM_STATE_SHADED", "_NET_WM_WINDOW_TYPE_DESKTOP",
    "_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DIALOG",
    "_NET_WM_WINDOW_TYPE_TOOLBAR", "_NET_WM_WINDOW_TYPE_MENU",
    "_NET_WM_WINDOW_TYPE_UTILITY", "_NET_WM_WINDOW_TYPE_SPLASH",
    "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU", "_NET_WM_WINDOW_TYPE_POPUP_MENU",
    "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
    "_NET_WM_WINDOW_TYPE_COMBO", "_NET_WM_WINDOW_TYPE_DND",
)

# Meta.WindowType name (bridge `window_type`) -> _NET_WM_WINDOW_TYPE atom
_WINDOW_TYPES = {
    "NORMAL": "_NET_WM_WINDOW_TYPE_NORMAL",
    "DESKTOP": "_NET_WM_WINDOW_TYPE_DESKTOP",
    "DOCK": "_NET_WM_WINDOW_TYPE_DOCK",
    "DIALOG": "_NET_WM_WINDOW_TYPE_DIALOG",
    "MODAL_DIALOG": "_NET_WM_WINDOW_TYPE_DIALOG",
    "TOOLBAR": "_NET_WM_WINDOW_TYPE_TOOLBAR",
    "MENU": "_NET_WM_WINDOW_TYPE_MENU",
    "UTILITY": "_NET_WM_WINDOW_TYPE_UTILITY",
    "SPLASHSCREEN": "_NET_WM_WINDOW_TYPE_SPLASH",
    "DROPDOWN_MENU": "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
    "POPUP_MENU": "_NET_WM_WINDOW_TYPE_POPUP_MENU",
    "TOOLTIP": "_NET_WM_WINDOW_TYPE_TOOLTIP",
    "NOTIFICATION": "_NET_WM_WINDOW_TYPE_NOTIFICATION",
    "COMBO": "_NET_WM_WINDOW_TYPE_COMBO",
    "DND": "_NET_WM_WINDOW_TYPE_DND",
}


# Where the synthesized atom ids start. They must not look like ids the X server could have handed out: numbered
# from 0x100 they sat squarely inside the server's own allocated range, so a numeric id copied out of a native
# window's dump and fed back to a real X tool named a plausible WRONG atom instead of failing. No X server
# allocates this high.
_SYNTH_ATOM_BASE = 0x40000000


class NativeAtoms:
    """Fake atom table for the native plane: predefined X atoms keep their real ids (1..68) and EWMH names get
    stable synthesized ids; -f/-set intern new names process-locally, mirroring XInternAtom(create)."""

    def __init__(self):
        self.by_name = {}
        self.by_id = {}
        for i, name in enumerate(_PREDEFINED_ATOMS, start=1):
            self.by_name[name] = i
            self.by_id[i] = name
        for i, name in enumerate(_EXTENDED_ATOMS, start=_SYNTH_ATOM_BASE):
            self.by_name[name] = i
            self.by_id[i] = name
        self._next = _SYNTH_ATOM_BASE + len(_EXTENDED_ATOMS)

    def intern(self, name: str, create: bool) -> int:
        a = self.by_name.get(name)
        if a:
            return a
        if not create:
            return 0
        a = self._next
        self._next += 1
        self.by_name[name] = a
        self.by_id[a] = name
        return a

    def name(self, a: int):
        return self.by_id.get(a)


# -- property wire builders (native plane) -----------------------------------


def _p_string(s: str):
    """A latin-1 property (type STRING is ISO 8859-1, by definition), or UTF8_STRING when the text does not fit
    in it.

    Typing UTF-8 bytes as STRING is not a legibility trade-off, it is wrong: xprop's STRING-to-locale rule
    decodes them as latin-1 and re-encodes them for the locale, so every character above U+00FF prints as
    mojibake. Titles that DO fit latin-1 stay STRING, which is what the XWayland twin's real WM_NAME carries."""
    try:
        return ("STRING", 8, s.encode("latin-1"))
    except UnicodeEncodeError:
        return ("UTF8_STRING", 8, s.encode("utf-8"))


def _latin1(s: str) -> bytes:
    """ISO 8859-1 bytes for a property whose type is STRING by definition (WM_CLASS), with anything that does
    not fit replaced -- the property has no way to say "this is UTF-8"."""
    return s.encode("latin-1", "replace")


def _p_utf8(s: str):
    return ("UTF8_STRING", 8, s.encode("utf-8"))


def _p_cardinal(vals):
    return ("CARDINAL", 32, struct.pack("<%dI" % len(vals), *[v & 0xFFFFFFFF for v in vals]))


def _p_window(vals):
    return ("WINDOW", 32, struct.pack("<%dI" % len(vals), *[v & 0xFFFFFFFF for v in vals]))


def _p_atoms(atoms: NativeAtoms, names):
    ids = [atoms.intern(n, True) for n in names]
    return ("ATOM", 32, struct.pack("<%dI" % len(ids), *ids))


# -- targets -----------------------------------------------------------------


class XTarget:
    plane = "x"

    def __init__(self, conn, win: int):
        self.conn = conn
        self.win = win

    def intern(self, name: bytes, create: bool) -> bool:
        return bool(self.conn.atom(name.decode("latin-1"), only_if_exists=not create))

    def fetch(self, name: bytes):
        r = self.conn.read_property(self.win, name.decode("latin-1"))
        if r is None:
            return None
        tname, size, data = r
        return tname, size, data

    def list_names(self):
        out = []
        for a in self.conn.list_properties(self.win):
            name = self.conn.get_atom_name(a)
            if name is None:
                name = "undefined atom # 0x%x" % a
            out.append(name.encode("latin-1"))
        return out

    def atom_name(self, a: int):
        return self.conn.get_atom_name(a)

    def remove_prop(self, name: bytes) -> bool:
        return self.conn.delete_property(self.win, name.decode("latin-1"))

    def set_prop(self, name: bytes, type_name: str, size: int, data: bytes):
        self.conn.change_property(self.win, name.decode("latin-1"), type_name, size, data)


class NativeTarget:
    """Shared shape for native views and the native root: a synthesized,
    lazily rebuilt property dict rendered through the same formatter."""

    plane = "native"

    def __init__(self, sess: Session, atoms: NativeAtoms):
        self.sess = sess
        self.atoms = atoms

    def _props(self):  # {name_bytes: (type_name, size, wire)}
        raise NotImplementedError

    def intern(self, name: bytes, create: bool) -> bool:
        s = name.decode("latin-1")
        if s in self.atoms.by_name or name in self._props():
            return True
        if create:
            self.atoms.intern(s, True)
            return True
        return False

    def fetch(self, name: bytes):
        return self._props().get(name)

    def list_names(self):
        return list(self._props().keys())

    def atom_name(self, a: int):
        return self.atoms.name(a)


class NativeViewTarget(NativeTarget):
    def __init__(self, sess, atoms, node, win):
        super().__init__(sess, atoms)
        self.node = node
        self.win = win
        self.node_id = win.id

    def refresh(self):
        for node, win in self.sess.nodes():
            if win.id == self.node_id:
                self.node, self.win = node, win
                return True
        return False

    def _props(self):
        node, win = self.node, self.win
        props = {}
        states = []
        rich = bool(node.get(_VIEW_KEY))  # a views() backend: more states
        # Mutter's own _NET_WM_STATE order (window-x11.c set_net_wm_state), so native and XWayland windows on
        # GNOME print alike; the sway subset (FULLSCREEN, HIDDEN, STICKY) keeps its relative order.  SHADED
        # comes first because that is where muffin's own meta_window_x11_set_net_wm_state puts it
        # (src/x11/window-x11.c, linuxmint/muffin master, read 2026-09-11) -- the state mutter dropped and
        # Cinnamon kept, and the one every other backend leaves False.  Until this arm existed,
        # `wxprop -id <id> _NET_WM_STATE` on a shaded muffin window printed `_NET_WM_STATE_FOCUSED` and
        # nothing else while the shell's own is_shaded() answered true
        # [M vm/live-smoke.d/cinnamon-wayland.sh:307 comment, resolute-cinnamon-wayland 2026-09-09;
        # goal2/recon/gaps.md 1b #11].
        if rich and node.get("shaded"):
            states.append("_NET_WM_STATE_SHADED")
        if rich and node.get("skip_pager"):
            states.append("_NET_WM_STATE_SKIP_PAGER")
        if rich and node.get("skip_taskbar"):
            states.append("_NET_WM_STATE_SKIP_TASKBAR")
        if rich and node.get("maximized_h"):
            states.append("_NET_WM_STATE_MAXIMIZED_HORZ")
        if rich and node.get("maximized_v"):
            states.append("_NET_WM_STATE_MAXIMIZED_VERT")
        if node.get("fullscreen_mode"):
            states.append("_NET_WM_STATE_FULLSCREEN")
        # a shaded window is HIDDEN as well as SHADED, because muffin writes both for it:
        # `if (!meta_window_showing_on_its_workspace (window) || window->shaded)` is the HIDDEN arm of
        # meta_window_x11_set_net_wm_state (src/x11/window-x11.c, linuxmint/muffin master, read 2026-09-11),
        # and showing_on_its_workspace stays true for a shaded window -- it is mapped, only rolled up.  Real
        # xprop on a shaded Cinnamon X11 window therefore prints the pair, and a dump that named SHADED alone
        # would differ from the oracle on the one window this arm exists for
        if not node.get("visible", getattr(win, "visible", True)) or (rich and node.get("shaded")):
            states.append("_NET_WM_STATE_HIDDEN")
        if rich and node.get("above"):
            states.append("_NET_WM_STATE_ABOVE")
        if rich and node.get("below"):
            states.append("_NET_WM_STATE_BELOW")
        if rich and node.get("urgent"):
            states.append("_NET_WM_STATE_DEMANDS_ATTENTION")
        if node.get("sticky"):
            states.append("_NET_WM_STATE_STICKY")
        if rich and node.get("focused"):
            # last, where Mutter's own set_net_wm_state puts it
            states.append("_NET_WM_STATE_FOCUSED")
        props[b"_NET_WM_STATE"] = _p_atoms(self.atoms, states)
        wtype = "_NET_WM_WINDOW_TYPE_NORMAL"
        if rich:
            wtype = _WINDOW_TYPES.get(node.get("window_type") or "NORMAL", wtype)
        props[b"_NET_WM_WINDOW_TYPE"] = _p_atoms(self.atoms, [wtype])
        transient = node.get("transient_for") or 0
        if transient:
            props[b"WM_TRANSIENT_FOR"] = _p_window([transient])
        desktop = getattr(win, "desktop", -1)
        props[b"_NET_WM_DESKTOP"] = _p_cardinal([desktop if desktop >= 0 else 0xFFFFFFFF])
        pid = node.get("pid") or getattr(win, "pid", 0)
        if pid:
            props[b"_NET_WM_PID"] = _p_cardinal([pid])
        host = hostname()
        if host:
            props[b"WM_CLIENT_MACHINE"] = _p_string(host)
        wp = node.get("window_properties") or {}
        instance = node.get("app_id") or wp.get("instance")
        cls = node.get("app_id") or wp.get("class")
        if instance or cls:
            # WM_CLASS is STRING by ICCCM whatever the app id looks like, so this pair is not routed through
            # _p_string's UTF8_STRING escape hatch -- an X twin's WM_CLASS is STRING too.
            data = _latin1(instance or "") + b"\0" + _latin1(cls or "") + b"\0"
            props[b"WM_CLASS"] = ("STRING", 8, data)
        title = node.get("name")
        if title is None:
            title = getattr(win, "title", None)
        if title is not None:
            props[b"_NET_WM_NAME"] = _p_utf8(title)
            props[b"WM_NAME"] = _p_string(title)
        if rich:
            # ICCCM WM_STATE: 1 NormalState, 3 IconicState, and no icon window. Mutter writes it on every X11
            # window it manages, so a native one that answers "is this window minimized?" the same way keeps a
            # script working across the two planes.
            state = 1 if node.get("visible", True) else 3
            props[b"WM_STATE"] = ("WM_STATE", 32, struct.pack("<II", state, 0))
        return props


class NativeRootTarget(NativeTarget):
    """-root without an X server: a minimal EWMH-ish root property set synthesized from the compositor
    (documented in docs/WXPROP.md's terms: the _NET_SUPPORTING_WM_CHECK-ish set). _NET_SUPPORTING_WM_CHECK is 0x0 —
    there is no WM check window to point at."""

    node_id = None

    def refresh(self):
        return True

    def _props(self):
        sess = self.sess
        props = {}
        nodes = sess.nodes()
        rich = any(n.get(_VIEW_KEY) for n, _w in nodes)
        supported = [
            "_NET_SUPPORTED", "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW",
            "_NET_SUPPORTING_WM_CHECK", "_NET_CURRENT_DESKTOP",
            "_NET_NUMBER_OF_DESKTOPS", "_NET_WM_NAME", "_NET_WM_PID",
            "_NET_WM_DESKTOP", "_NET_WM_STATE", "_NET_WM_STATE_FULLSCREEN",
            "_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_STICKY",
            "_NET_WM_WINDOW_TYPE",
        ]
        if rich:
            supported[2:2] = ["_NET_CLIENT_LIST_STACKING"]
            supported += ["_NET_DESKTOP_NAMES", "_NET_WM_STATE_MAXIMIZED_HORZ",
                          "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_ABOVE",
                          "_NET_WM_STATE_SKIP_TASKBAR",
                          "_NET_WM_STATE_DEMANDS_ATTENTION",
                          # only ever set on Cinnamon, but _NET_SUPPORTED is
                          # what a reader consults before it asks for a state,
                          # and muffin's own root window lists it
                          "_NET_WM_STATE_SHADED"]
        props[b"_NET_SUPPORTED"] = _p_atoms(self.atoms, supported)
        # With a views() backend every window is listed by the id the tools print for it (the X id of an
        # XWayland window, the bridge id of a native one) -- the list wwmctl -l prints. The sway tree path keeps
        # listing node ids.
        if rich:
            ids = [n.get("window") or w.id for n, w in nodes]
            props[b"_NET_CLIENT_LIST"] = _p_window(ids)
            props[b"_NET_CLIENT_LIST_STACKING"] = _p_window(ids)
        else:
            props[b"_NET_CLIENT_LIST"] = _p_window([w.id for _n, w in nodes])
        active = 0
        for n, w in nodes:
            if w.focused:
                active = (n.get("window") or w.id) if rich else w.id
                break
        props[b"_NET_ACTIVE_WINDOW"] = _p_window([active])
        b = sess.backend()
        num, cur = 1, 0
        try:
            num = b.num_desktops()
        except Exception:
            pass
        try:
            cur = max(b.get_desktop(), 0)
        except Exception:
            pass
        # A compositor can be on a desktop it does not count: sway creates a workspace on demand and
        # GET_WORKSPACES lists only the ones that exist, so "current 3 of 2 desktops" reached the root and no
        # EWMH reader can make sense of that.
        num = max(num, cur + 1)
        props[b"_NET_NUMBER_OF_DESKTOPS"] = _p_cardinal([num])
        props[b"_NET_CURRENT_DESKTOP"] = _p_cardinal([cur])
        if rich:
            names = sess.workspace_names()
            if names is not None:
                props[b"_NET_DESKTOP_NAMES"] = (
                    "UTF8_STRING", 8,
                    b"".join(n.encode("utf-8") + b"\0" for n in names))
        props[b"_NET_SUPPORTING_WM_CHECK"] = _p_window([0])
        return props


# root properties the compositor knows better than Mutter's X root, which
# only ever sees XWayland clients
_ROOT_OVERRIDES = (b"_NET_CLIENT_LIST", b"_NET_CLIENT_LIST_STACKING",
                   b"_NET_ACTIVE_WINDOW", b"_NET_NUMBER_OF_DESKTOPS",
                   b"_NET_CURRENT_DESKTOP", b"_NET_DESKTOP_NAMES")


class MergedRootTarget:
    """-root on GNOME with Xwayland up: the real X root window (Mutter is a full EWMH window manager for
    Xwayland: _NET_SUPPORTED, _NET_SUPPORTING_WM_CHECK, _NET_WORKAREA, _NET_SHOWING_DESKTOP, ...) with the
    window-list properties re-synthesized from the bridge, because the X root lists X clients only:
    _NET_CLIENT_LIST(_STACKING) and _NET_ACTIVE_WINDOW cover native windows too (X id or bridge id, the ids the
    tools print), _NET_NUMBER_OF_DESKTOPS/_NET_CURRENT_DESKTOP/_NET_DESKTOP_NAMES come from the workspace
    manager directly. Reads of anything else, and -set/-remove, go to the X root untouched."""

    plane = "x"
    node_id = None

    def __init__(self, xt: XTarget, native: NativeRootTarget):
        self.xt = xt
        self.native = native
        self.conn = xt.conn
        self.win = xt.win
        self._written = set()   # override names this run -set / -remove'd

    def refresh(self):
        return True

    def intern(self, name: bytes, create: bool) -> bool:
        return self.xt.intern(name, create) or name in _ROOT_OVERRIDES

    def fetch(self, name: bytes):
        """The X root's property, with the six window-list ones answered from the compositor -- except for a
        name THIS RUN has written or removed, which is read straight from the X root so the tool can see what it
        just did.

        The absence of an override is not evidence of damage: Mutter writes _NET_CURRENT_DESKTOP only when the
        workspace first changes, so a fresh session legitimately lacks it while the compositor knows the answer.
        Damage is reported at the moment it is done instead -- see _note()."""
        if name in _ROOT_OVERRIDES and name not in self._written:
            p = self.native._props().get(name)
            if p is not None:
                return p
        return self.xt.fetch(name)

    def list_names(self):
        names = self.xt.list_names()
        for n in _ROOT_OVERRIDES:
            if n not in names and n not in self._written:
                names.append(n)
        return names

    def atom_name(self, a: int):
        return self.xt.atom_name(a)

    def remove_prop(self, name: bytes) -> bool:
        self._note("-remove", name)
        return self.xt.remove_prop(name)

    def set_prop(self, name: bytes, type_name: str, size: int, data: bytes):
        self._note("-set", name)
        self.xt.set_prop(name, type_name, size, data)

    def _note(self, what: str, name: bytes):
        """-set/-remove address the real X root, never the synthesis, so the tool could not see what it had just
        done -- and an operator who has broken every EWMH client on the X plane
        (`-root -remove _NET_CLIENT_LIST`; `wmctrl -l` then says "Cannot get client list properties") got
        silence. Say it once, and read that name straight from the X root for the rest of the run."""
        if name not in _ROOT_OVERRIDES or name in self._written:
            return
        self._written.add(name)
        sys.stderr.write(
            "%s:  %s %s writes the X root only; XWayland clients read it, "
            "the compositor does not\n"
            % (_progname(), what, name.decode("latin-1", "replace")))


class FontTarget:
    """`xprop -font <name>`: a core X font's FONTPROPs.

    xprop's Get_Font_Property_Data_And_Type hands every value back as a typeless 32-bit CARD32, so the lines
    carry no `(TYPE)` and the format comes from the font table alone (an unmapped property falls back to xprop's
    default `0x` and prints as a bare hex number). XWayland does serve the core fonts xfonts-base installs,
    `fixed` among them."""

    plane = "font"
    node_id = None
    win = None

    def __init__(self, conn, name: str, props):
        self.conn = conn
        self.name = name
        self.props = props        # [(atom id, CARD32)] in the font's order

    def refresh(self):
        return True

    def intern(self, name: bytes, create: bool) -> bool:
        return bool(self.conn.atom(name.decode("latin-1"), only_if_exists=not create))

    def fetch(self, name: bytes):
        atom = self.conn.atom(name.decode("latin-1"), only_if_exists=True)
        if not atom:
            return None
        for a, v in self.props:
            if a == atom:
                return None, 32, struct.pack("<I", v & 0xFFFFFFFF)
        return None

    def list_names(self):
        out = []
        for a, _v in self.props:
            n = self.conn.get_atom_name(a)
            if n is None:
                n = "undefined atom # 0x%x" % a
            out.append(n.encode("latin-1"))
        return out

    def atom_name(self, a: int):
        return self.conn.get_atom_name(a)


def resolve_font(sess: Session, name: str):
    """The -font target, or xprop's own "cannot load it" fatal."""
    x = sess.x11()
    if x is None:
        raise FatalError("Unable to open font %s!" % name)
    try:
        props = x.font_properties(name)
    except Exception:
        raise FatalError("Unable to open font %s!" % name) from None
    return FontTarget(x, name, props)


# -- window selection --------------------------------------------------------


class MissingWindowTarget:
    """-id N with no X server and no matching compositor node. The error is deferred to the first window
    operation, so pure parse errors ("format specified without atom") still fire first, like xprop, whose window
    ids are only ever touched at property-access time."""

    plane = "missing"

    def __init__(self, wid: int, hint: str | None = None):
        self.wid = wid
        self.hint = hint  # why the compositor plane is missing (GNOME)

    def _fatal(self):
        if self.hint:
            raise FatalError("cannot look up window id # 0x%x: %s" % (self.wid, self.hint))
        raise FatalError("window id # 0x%x does not exists!" % self.wid)

    def intern(self, name, create=False):
        self._fatal()

    def fetch(self, name):
        self._fatal()

    def list_names(self):
        self._fatal()

    def atom_name(self, a):
        return None


def resolve_id(sess: Session, wid: int):
    """xprop -id N: a compositor node id resolves through the node (an XWayland node redirects to its real X
    window id); otherwise the id is handed to the X server, exactly like xprop. The error wording for a hopeless
    id is xprop's own (grammar and all).

    X ids are matched first, across all nodes, before any compositor id. `-id` is an X window id in every xprop
    manual there is, and the two spaces are not disjoint: KWin mints its own toplevel ids and Mutter's are
    Mutter's, so one window's compositor id can equal another window's X id, and a single pass in node order
    then answered about whichever of the two the compositor happened to list first."""
    nodes = list(sess.nodes())
    for node, win in nodes:
        if node.get("window") == wid:
            x = sess.x11()      # an X window is listed: Xwayland is up
            if x is not None:
                return XTarget(x, wid)
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    for node, win in nodes:
        if win.id == wid:
            xid = node.get("window")
            if xid:
                x = sess.x11()
                if x is not None:
                    return XTarget(x, xid)
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    # unknown to the compositor: hand it to the X server like xprop would
    # (on GNOME only when Xwayland is running -- a typo must not spawn one)
    x = sess.x11() if sess.x_present() else None
    if x is not None:
        return XTarget(x, wid)
    return MissingWindowTarget(wid, sess.backend_error)


def resolve_root(sess: Session):
    if sess.has_views():
        # GNOME: the bridge knows every window; the X root (when Xwayland
        # is up) contributes Mutter's real EWMH root properties
        native = NativeRootTarget(sess, NativeAtoms())
        x = sess.x11() if sess.x_present() else None
        if x is not None:
            return MergedRootTarget(XTarget(x, x.root()), native)
        return native
    x = sess.x11()
    if x is not None:
        return XTarget(x, x.root())
    if sess.backend() is None:
        if sess.backend_error:
            raise FatalError("cannot examine the root window: %s" % sess.backend_error)
        raise FatalError("cannot examine the root window: no X server and "
                         "no compositor backend")
    return NativeRootTarget(sess, NativeAtoms())


def _x_fetch_name(x, win: int):
    """XFetchName: WM_NAME only when its type is STRING (format 8). A BadWindow (the window died between the
    tree read and this fetch) is a normal DFS miss; a lost connection (XUnavailable) propagates so main reports
    the real fault instead of a misleading 'no window' verdict."""
    try:
        r = x.read_property(win, "WM_NAME")
    except X11Error:
        return None
    if r is None or r[0] != "STRING" or r[1] != 8:
        return None
    return r[2].split(b"\0", 1)[0]


def _window_with_name(x, top: int, name: bytes, depth: int = 0):
    """dsimple.c's Window_With_Name: pre-order DFS, exact strcmp on WM_NAME, first match wins. Depth-bounded
    against a lying server. A window vanishing mid-search (BadWindow from query_tree) prunes that subtree; only
    a lost connection escapes."""
    if depth > 64:
        return 0
    if _x_fetch_name(x, top) == name:
        return top
    try:
        children = x.query_tree(top)
    except X11Error:
        return 0
    for ch in children:
        w = _window_with_name(x, ch, name, depth + 1)
        if w:
            return w
    return 0


def resolve_name(sess: Session, name: str):
    """-name: real xprop semantics first (exact WM_NAME match, DFS from the X root) so X-plane behavior is
    untouched; the native plane then gets the same courtesy (exact title, then exact app_id) for windows real
    xprop could never see."""
    x = sess.x11() if sess.x_present() else None
    if x is not None:
        w = _window_with_name(x, x.root(), os.fsencode(name))
        if w:
            return XTarget(x, w)
    nodes = sess.nodes()
    for node, win in nodes:
        if node.get("window") and x is not None:
            continue  # X windows are only ever matched the X way above
        if node.get("name") == name:
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    for node, win in nodes:
        if node.get("window") and x is not None:
            continue
        if node.get("app_id") == name:
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    raise FatalError("No window with name %s exists!" % name)


def select_target(sess: Session, prog: str):
    """No selector: compositor next-focus selection with a stderr hint
    (the wwmctl pattern; there is no X pointer grab to borrow)."""
    b = sess.backend()
    if b is None:
        if sess.backend_error:
            raise FatalError("can't select a window: %s" % sess.backend_error)
        raise FatalError("can't select a window without a compositor "
                         "backend; use -root, -id or -name")
    # The instruction differs by backend: click on GNOME and KDE, focus the
    # window on sway, whose IPC has no picker (see WindowBackend).
    sys.stderr.write("%s: %s\n" % (prog, b.select_window_hint))
    sys.stderr.flush()
    try:
        node_id = b.select_window()
    except Exception as e:
        raise FatalError("window selection failed: %s" % e) from None
    return resolve_id(sess, node_id)


# -- Show_Prop ---------------------------------------------------------------


def show_prop(formatter, target, out: bytearray, fmt_b, dfmt_b, prop_b: bytes):
    out += prop_b
    if not target.intern(prop_b, create=False):
        out += b":  no such atom on any window.\n"
        return
    r = target.fetch(prop_b)
    if r is None:
        out += b":  not found.\n"
        return
    type_name, size, wire = r
    formatter.render_property(out, prop_b, type_name, size, wire, fmt_b, dfmt_b)


def show_all_props(formatter, target, out: bytearray):
    for name in target.list_names():
        show_prop(formatter, target, out, None, None, name)


# -- -spy --------------------------------------------------------------------


def _write_flush(data: bytes):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def spy_x(formatter, target: XTarget, specs):
    """After the initial dump: PropertyNotify -> Show_Prop, DestroyNotify ->
    exit 0, BadWindow/BadMatch -> empty line + exit 0 (xprop.c:2109)."""
    x = target.conn
    try:
        x.select_input(target.win, SPY_EVENT_MASK)
        while True:
            sys.stdout.buffer.flush()
            ev = x.next_event(None)
            if ev is None:
                continue
            if ev["type"] == "DestroyNotify":
                return 0
            if ev["type"] != "PropertyNotify":
                continue
            name = x.get_atom_name(ev["atom"])
            if name is None:
                name = "undefined atom # 0x%x" % ev["atom"]
            name_b = name.encode("latin-1")
            fmt_b = dfmt_b = None
            if specs is not None:
                for sname, sfmt, sdfmt in specs:
                    if sname == name_b:
                        fmt_b, dfmt_b = sfmt, sdfmt
                        break
                else:
                    continue
            out = bytearray()
            show_prop(formatter, target, out, fmt_b, dfmt_b, name_b)
            _write_flush(out)
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (3, 8):  # BadWindow/BadMatch: the window went away
            _write_flush(b"\n")
            return 0
        raise


# which synthesized properties a sway window event touches, in the order
# the X plane would emit them (WM_NAME before _NET_WM_NAME, like xterm)
_NATIVE_EVENT_PROPS = {
    "title": (b"WM_NAME", b"_NET_WM_NAME"),
    "fullscreen_mode": (b"_NET_WM_STATE",),
    "move": (b"_NET_WM_DESKTOP", b"_NET_WM_STATE"),
    "floating": (),
    "focus": (),
    "urgent": (),
}

# the same for the bridge's WindowEvent vocabulary (backend.events()): `workspace` also fires when stickiness
# changes, `minimized`/`urgent` are states the views() synthesis prints, `move` is geometry only
_VIEW_EVENT_PROPS = {
    "title": (b"WM_NAME", b"_NET_WM_NAME"),
    "fullscreen_mode": (b"_NET_WM_STATE",),
    "workspace": (b"_NET_WM_DESKTOP", b"_NET_WM_STATE"),
    "minimized": (b"_NET_WM_STATE",),
    "urgent": (b"_NET_WM_STATE",),
    "move": (),
    "focus": (),
    "new": (),
}

# root-level, sway: no stacking list (the tree is not a stacking order) and no desktop names (workspaces are
# numbered), so two atoms fewer than the bridge's table below
_SWAY_ROOT_EVENT_PROPS = {
    "new": (b"_NET_CLIENT_LIST",),
    "close": (b"_NET_CLIENT_LIST", b"_NET_ACTIVE_WINDOW"),
    "focus": (b"_NET_ACTIVE_WINDOW",),
    "workspace": (b"_NET_CURRENT_DESKTOP", b"_NET_NUMBER_OF_DESKTOPS"),
}

# root-level: what a bridge event changes among the synthesized root props
_ROOT_EVENT_PROPS = {
    "new": (b"_NET_CLIENT_LIST", b"_NET_CLIENT_LIST_STACKING"),
    "close": (b"_NET_CLIENT_LIST", b"_NET_CLIENT_LIST_STACKING",
              b"_NET_ACTIVE_WINDOW"),
    "focus": (b"_NET_ACTIVE_WINDOW",),
    "workspace": (b"_NET_CURRENT_DESKTOP", b"_NET_NUMBER_OF_DESKTOPS",
                  b"_NET_DESKTOP_NAMES"),
}


def _events_hook(backend):
    """backend.events(...) when the backend really implements it (the
    WindowBackend default only raises), else None."""
    fn = getattr(backend, "events", None)
    if fn is None:
        return None
    try:
        from wdotool.backend import WindowBackend
        if getattr(type(backend), "events", None) is WindowBackend.events:
            return None
    except Exception:
        pass
    return fn


def _show_names(formatter, target, names, specs) -> bytes:
    """Show_Prop for each name in `names` that the -spy specs (if any)
    selected, with their format/dformat."""
    out = bytearray()
    for name_b in names:
        fmt_b = dfmt_b = None
        if specs is not None:
            for sname, sfmt, sdfmt in specs:
                if sname == name_b:
                    fmt_b, dfmt_b = sfmt, sdfmt
                    break
            else:
                continue
        show_prop(formatter, target, out, fmt_b, dfmt_b, name_b)
    return bytes(out)


def _native_events(backend, workspaces: bool):
    """backend.events(): (id, change) forever, in the backend's own change vocabulary. FatalError when the
    backend has no event stream (the WindowBackend default only raises)."""
    hook = _events_hook(backend)
    if hook is None:
        raise FatalError("-spy on a native window needs the sway backend, "
                         "the GNOME bridge or KWin")
    try:
        return hook(None, workspaces=workspaces)
    except TypeError:  # a hook without the workspaces flag
        return hook(None)


def _is_sway(backend) -> bool:
    """Which change vocabulary the events carry. The two tables above are deliberately not merged: the same word
    means different things to sway and to the bridge."""
    return getattr(backend, "name", "") == "sway"


def spy_native_view(formatter, target: NativeViewTarget, specs):
    backend = target.sess.backend()
    table = _NATIVE_EVENT_PROPS if _is_sway(backend) else _VIEW_EVENT_PROPS
    for wid, change in _native_events(backend, workspaces=False):
        if wid != target.node_id:
            continue
        if change == "close":
            return 0
        names = table.get(change)
        if not names:
            continue
        if not target.refresh():
            return 0  # gone between the event and the re-read
        out = _show_names(formatter, target, names, specs)
        if out:
            _write_flush(out)
    return 0


def spy_native_root(formatter, target: NativeRootTarget, specs):
    backend = target.sess.backend()
    table = _SWAY_ROOT_EVENT_PROPS if _is_sway(backend) else _ROOT_EVENT_PROPS
    for _wid, change in _native_events(backend, workspaces=True):
        names = table.get(change, ())
        out = _show_names(formatter, target, names, specs)
        if out:
            _write_flush(out)
    return 0


def spy_merged_root(formatter, target: MergedRootTarget, specs):
    """-root -spy on GNOME with Xwayland up: PropertyNotify on the X root for Mutter's own root properties, the
    bridge's events for the synthesized ones (which the X root's own updates of those names are NOT allowed to
    reprint -- they would show the X-only view). The bridge stream runs on a thread of its own (one Bus per
    thread) and is drained between X polls."""
    x = target.conn
    hook = _events_hook(target.native.sess.backend())
    q: "queue.Queue" = queue.Queue()
    stop = threading.Event()

    def pump():
        try:
            for item in hook(None, workspaces=True):
                q.put(item)
                if stop.is_set():
                    return
        except Exception as e:  # surfaced by the main loop
            q.put(("error", str(e)))

    if hook is not None:
        threading.Thread(target=pump, daemon=True).start()
    try:
        x.select_input(target.win, SPY_EVENT_MASK)
        while True:
            sys.stdout.buffer.flush()
            ev = x.next_event(0.25)
            if ev is not None and ev["type"] == "PropertyNotify":
                name = x.get_atom_name(ev["atom"])
                if name is None:
                    name = "undefined atom # 0x%x" % ev["atom"]
                name_b = name.encode("latin-1")
                if name_b not in _ROOT_OVERRIDES:
                    out = _show_names(formatter, target, (name_b,), specs)
                    if out:
                        _write_flush(out)
            while True:
                try:
                    wid, change = q.get_nowait()
                except queue.Empty:
                    break
                if wid == "error":
                    raise FatalError("bridge event stream failed: %s" % change)
                names = _ROOT_EVENT_PROPS.get(change, ())
                out = _show_names(formatter, target, names, specs)
                if out:
                    _write_flush(out)
    finally:
        stop.set()
