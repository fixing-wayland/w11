"""Window-management backend interface: four backends implement it, three tools drive them.

The View/Workspace dataclasses and the optional hooks at the end of
WindowBackend (views, workspaces, x_info, events). They let a backend that knows more than Window carries (X ids
of XWayland windows, WM_CLASS instance/class, workspace names) hand it to wwmctl/wxprop/getmouselocation without
those tools reaching into backend privates (sway's `_nodes()` tuple). Every hook defaults to "not available";
callers fall back to list()/find()."""

import dataclasses
import hashlib
import os
import signal
import sys

from w11common.errors import CmdError

#: The name the backends put in front of their own warnings. wwmctl and
#: wxprop drive these same backends, and a line reading "wdotool: ..." in
#: the middle of a `wmctrl -b` run names a tool the user did not run. Each
#: CLI's main() sets it -- wxprop's to whatever argv[0] says, like the
#: original it replaces -- so one process running two of them in turn (the
#: in-process `main([...])` callers) still gets one name per run.
_PROGRAM = "wdotool"


def set_program(name) -> None:
    """Name the running tool. Called once, first thing, by each main()."""
    global _PROGRAM
    _PROGRAM = str(name) if name else "wdotool"


def program() -> str:
    return _PROGRAM


# -- minted window ids --------------------------------------------------------
#
# Three compositors publish no numeric window id at all: KWin's handle is a
# uuid string, Hyprland's is `address` (a pointer, and its own `stableId` does
# not exist before 0.56 [M recon2/arch.md, hyprland fixtures]), COSMIC's is the
# `identifier` string of ext-foreign-toplevel-list. An id has to be minted for
# them, and it has to be 32-bit clean because everything downstream is X-shaped
# and truncates there -- `wxprop -id` (dsimple.c parses into a 32-bit XID), the
# synthesized _NET_CLIENT_LIST, wmctrl's 0x%08lx -- and biased into a range no
# Xwayland client is ever given (X ids are (client << 21) | serial), so a
# minted id can never be read as the X id of an XWayland window in the same
# listing. backend_kwin.py mints from the uuid's own hex digits and predates
# this; new backends come here.
ID_BASE = 0x40000000
ID_MASK = 0x3FFFFFFF

#: the golden-ratio odd constant KWin's `_wid` re-mints with, so a collision
#: walks the same way on every backend
ID_SALT_STEP = 0x9E3779B1


def mint_id(key, salt: int = 0) -> int:
    """A stable 30-bit window id for `key` (a str or bytes handle), 0 for an empty one.

    blake2b rather than a slice of the handle: COSMIC's identifier is 32 base62 characters whose entropy is not
    in any particular eight of them, and Hyprland's `address` is a heap pointer whose low bits move together
    (two windows opened in a row differed by 0x20). The digest is the whole handle, so the id is stable for the
    life of the window and identical in two processes reading the same session -- which is the property the wlr
    floor's arrival-order ids do not have [M recon2/hyprland.md §3: 0x000f4241 became 0x000f4240 when another
    window closed].

    `salt` re-mints the same handle into a different id, for the ~1e-6 chance that two live windows collide."""
    if not key:
        return 0
    raw = key.encode("utf-8", "replace") if isinstance(key, str) else bytes(key)
    n = int.from_bytes(hashlib.blake2b(raw, digest_size=4).digest(), "big")
    return ID_BASE | ((n + salt * ID_SALT_STEP) & ID_MASK)


def mint_map(keys) -> "dict[str, int]":
    """{handle: id} for one window list, colliding handles re-minted in list order.

    A plain comprehension would drop one of a colliding pair and leave that window with no id at all --
    unlistable and unaddressable. Whoever comes second in the list is re-minted instead, so every window has an
    id of its own and the id is stable while the pair is."""
    out: "dict[str, int]" = {}
    taken: "dict[int, str]" = {}
    for key in keys:
        if key in out:
            continue
        salt = 0
        wid = mint_id(key)
        while wid in taken:
            salt += 1
            wid = mint_id(key, salt)
        out[key] = wid
        taken[wid] = key
    return out


# the two _NET_WM_STATE names a window manager may take as one operation
_MAXIMIZE_AXES = frozenset(("MAXIMIZED_VERT", "MAXIMIZED_HORZ"))


def state_steps(backend, names):
    """The _NET_WM_STATE names of one request, grouped into the set_state() calls that express it: [(state to
    send, the names it stands for)], in the order given. Two maximize axes standing next to each other become
    one call wherever the backend names the pair (WindowBackend.maximize_pair_state -- GNOME does, KWin and sway
    do not); everything else stays one call per name. `backend` may be None (no session): nothing is grouped
    then, and the caller's own fallback path decides what to do.

    Every command that can be handed both axes at once has to group them here:
    `wwmctl -b remove,maximized_vert,maximized_horz`, and
    `wdotool windowstate --remove MAXIMIZED_VERT --remove MAXIMIZED_HORZ` once it honours more than the last
    option. Sending the axes one after the other corrupts a Mutter window's saved rectangle -- see
    maximize_pair_state()."""
    names = [n.upper() for n in names]
    pair = getattr(backend, "maximize_pair_state", None)
    both = pair() if pair else None
    out, i = [], 0
    while i < len(names):
        if both and set(names[i:i + 2]) == _MAXIMIZE_AXES:
            out.append((both, names[i:i + 2]))
            i += 2
        else:
            out.append((names[i], [names[i]]))
            i += 1
    return out


def warn(msg: str) -> None:
    """One warning line on stderr, in the running tool's name."""
    sys.stderr.write("%s: %s\n" % (_PROGRAM, msg))


@dataclasses.dataclass
class Window:
    id: int = 0
    title: str = ""
    class_: str = ""  # app_id on Wayland; the WM_CLASS *class* for X clients
    # WM_CLASS *instance* of an X/XWayland client ("" when the backend cannot tell it apart from class_);
    # `search --classname` matches this, falling back to class_ so native Wayland toplevels still match their
    # app_id.
    instance: str = ""
    pid: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    focused: bool = False
    visible: bool = True
    desktop: int = -1  # 0-based workspace index, -1 unknown/sticky
    # Mutter window-type name (NORMAL DESKTOP DOCK DIALOG ...); the backends that know it fill it in, the rest
    # leave it NORMAL. hit_test() is the only reader: it is what lets one rule look through the desktop and dock
    # layers on every backend that can tell them apart.
    window_type: str = "NORMAL"


#: Window types the pointer hit-test looks through: the desktop-icon layer
#: and docks/panels, which a click on X11 falls straight through.
_LAYER_TYPES = {"DESKTOP", "DOCK"}


def hit_test(wins: "list[Window]", x: int, y: int) -> int:
    """The window under (x, y) in a list() result, 0 for none.

    One rule for every backend, so getmouselocation and the backends cannot drift apart: DESKTOP and DOCK layers
    are looked through, invisible windows (minimized, or on another workspace) are never hits, the focused
    window wins among the rest, and otherwise the topmost -- list() is stacking order bottom to top, so that is
    the last hit.

    Client-side on purpose. Neither the GNOME bridge nor KWin exports a hit-test, and KWin 6's
    workspace.windowAt() would answer for one Plasma release only."""
    hits = [w for w in wins
            if w.visible and w.window_type not in _LAYER_TYPES
            and w.w > 0 and w.h > 0
            and w.x <= x < w.x + w.w and w.y <= y < w.y + w.h]
    if not hits:
        return 0
    for w in hits:
        if w.focused:
            return w.id
    return hits[-1].id


@dataclasses.dataclass
class View:
    """One toplevel with everything a wmctrl/xprop clone wants to print. `window` is the plain Window; the rest
    is extra. `xid` is 0 for native Wayland toplevels; `instance`/`cls` are the WM_CLASS pair (app_id twice when
    the compositor has no WM_CLASS); `app_id` is the Wayland app id / GTK application id ("" for pure X11
    clients)."""

    window: Window
    xid: int = 0
    instance: str = ""
    cls: str = ""
    app_id: str = ""
    fullscreen: bool = False
    maximized_h: bool = False
    maximized_v: bool = False
    above: bool = False
    below: bool = False
    sticky: bool = False
    urgent: bool = False
    minimized: bool = False
    hidden: bool = False
    #: shaded -- rolled up to its titlebar.  Cinnamon's muffin is the only
    #: compositor in the rig that still has the state (`shade`/`unshade`/
    #: `is_shaded`); mutter dropped it and KWin 6 removed it, so every other
    #: backend leaves this False.  `wxprop` turns it into
    #: `_NET_WM_STATE_SHADED` and `_NET_WM_STATE_HIDDEN`, which is the pair
    #: muffin's own meta_window_x11_set_net_wm_state writes -- and therefore
    #: what real xprop prints for a shaded window on a Cinnamon X11 session.
    shaded: bool = False
    skip_taskbar: bool = False
    skip_pager: bool = False
    floating: bool = True  # tiling compositors only; GNOME windows all float
    ws_name: str = ""
    window_type: str = "NORMAL"
    client_type: str = "wayland"  # "wayland" | "x11"
    role: str = ""
    desktop_id: str = ""  # .desktop file id, "" when unknown
    monitor: int = -1
    transient_for: int = 0
    decorated: bool = True


@dataclasses.dataclass
class Workspace:
    index: int
    name: str = ""
    active: bool = False
    work_area: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h


class WindowBackend:
    name = "none"

    # The four sentences below belong to this class and not to any compositor: a backend reaches one by not
    # having overridden the method, so what is missing is our work.  Measured on 2026-09-09 against the eight
    # backend modules: `set_num_desktops` falls through here on sway, wlr, cosmic, hypr, cinnamon and wayfire,
    # `select_window` and `events` on wlr and cosmic, `set_window_desktop` on wlr, and every other default
    # below is overridden everywhere -- gnome and kwin reach none of them.  The bare form is what a fake or a
    # half-written backend prints, which is why the nine unreachable defaults were left as they were.

    #: `wmctrl -n 4` writes _NET_NUMBER_OF_DESKTOPS and the X window manager makes four, so this is owed on
    #: every compositor.  Every backend that lands here already drives a workspace surface for `set_desktop`,
    #: and on wlr and cosmic that surface can already do it: `ext_workspace_manager_v1` carries
    #: `create_workspace` on the group and `remove` on the handle (wdotool/ext_workspace.py's `_WS_REMOVE`
    #: and `CAP_REMOVE`), the first gated on the GROUP's capabilities and the second on the workspace's,
    #: both read for nothing else today.
    #: sway, hypr, cinnamon and wayfire have their own IPC or bus instead, which is a rung lower.
    NOT_YET_NUM_DESKTOPS = (
        "counting workspaces into existence is not yet done here, and the route is the surface this "
        "backend's set_desktop already drives -- ext_workspace_manager_v1, whose group creates a workspace "
        "and whose handle removes one, on wlr and cosmic (AGENTS.md route 1), the compositor's own IPC or "
        "bus on sway, hypr, cinnamon and wayfire (route 2) -- at the cost of honouring the group's "
        "capability that gates create and the workspace's that gates remove, and of a count that does "
        "not read back where a compositor drops a workspace as soon as it empties")

    #: xdotool grabs the pointer and answers with the window under the next button press.  Neither protocol
    #: backend that lands here publishes a pointer position, and the geometry to put a click in is missing
    #: on one and unreliable on the other: zwlr_foreign_toplevel carries none (backend_wlr.NO_GEOMETRY), and
    #: cosmic's `geometry` event, which this tree does parse (backend_cosmic._on_cosmic), is sent only
    #: alongside an output_enter or a change and never arrived at all in the nested rig (backend_cosmic's
    #: module header).  So the click has to be caught AND placed.
    NOT_YET_SELECT_WINDOW = (
        "a picker is not yet built here: this backend's protocols carry no pointer position, and no window "
        "geometry on wlr and only a sometimes-sent one on cosmic, to put a click in; the route is a "
        "wlr-layer-shell overlay that takes the press (AGENTS.md route 1) over a geometry source that names "
        "what is under it, at the cost of a surface that eats the click it reads and has to be unmapped "
        "again")

    #: sway's IPC vocabulary is the one every caller of `events()` speaks (see the signature above).  The
    #: toplevel protocols on wlr and cosmic deliver the same changes as events on the handle -- title, app_id,
    #: state, closed -- which is how those backends keep their own listings up to date.
    NOT_YET_EVENTS = (
        "an event stream is not yet built here: the toplevel protocol this backend already listens to "
        "delivers the changes (title, app_id, state, closed) and nothing turns them into the (window, "
        "change) pairs callers read, so the route is that same protocol (AGENTS.md route 1), at the cost "
        "of a connection held open for the whole wait and its vocabulary mapped onto sway's, which is the "
        "one every caller of events() speaks")

    #: wlr is the only backend that lands here.  `zwlr_foreign_toplevel_handle_v1` has no workspace request
    #: and `ext_workspace_manager_v1` names workspaces without taking windows; cosmic-comp is where the pair
    #: exists, and backend_cosmic.set_window_desktop already sends its `move_to_ext_workspace`.
    NOT_YET_SET_WINDOW_DESKTOP = (
        "binding a toplevel to a workspace is not yet done here: the handle has no such request and "
        "ext_workspace_manager_v1 names workspaces without taking windows.  The route is a protocol that "
        "does both, which cosmic-comp already ships as move_to_ext_workspace and the cosmic backend here "
        "already sends (AGENTS.md route 1), else a patched compositor (route 6)")

    def _unsupported(self, op: str, why: "str | None" = None):
        # `unsupported` marks a capability gap (as opposed to a failed operation) so callers can downgrade it to
        # a warning -- see set_num_desktops, which must not fail a chain on a compositor with a fixed workspace
        # count.
        #
        # `why` is the half AGENTS.md asks for: what is not done yet, the lowest rung of the ladder that would
        # close it and what that rung costs. A backend that owns its gap appends its own (backend_wlr._not_yet,
        # backend_cosmic._not_yet, backend_hypr._no); this argument is for the DEFAULTS below, which a backend
        # reaches by not having overridden them -- so the gap there is ours and not a compositor's.
        err = CmdError(f"{op} is not supported by the {self.name} backend"
                       + (f": {why}" if why else ""))
        err.unsupported = True
        raise err

    # required
    def list(self) -> list[Window]:
        raise NotImplementedError

    def activate(self, wid: int):
        raise NotImplementedError

    def close(self, wid: int):
        raise NotImplementedError

    def get_desktop(self) -> int:
        raise NotImplementedError

    def set_desktop(self, n: int):
        raise NotImplementedError

    def num_desktops(self) -> int:
        raise NotImplementedError

    # optional, with defaults
    def focus(self, wid: int):
        self.activate(wid)

    def find(self, wid: int) -> Window:
        for w in self.list():
            if w.id == wid:
                return w
        raise CmdError(f"window {wid} not found")

    def kill(self, wid: int):
        pid = self.find(wid).pid
        if pid <= 0:
            raise CmdError(f"no pid for window {wid}")
        os.kill(pid, signal.SIGKILL)

    def move_window(self, wid: int, x: int, y: int):
        self._unsupported("windowmove")

    def resize(self, wid: int, w: int, h: int):
        self._unsupported("windowsize")

    def minimize(self, wid: int):
        self._unsupported("windowminimize")

    def map(self, wid: int):
        self._unsupported("windowmap")

    def unmap(self, wid: int):
        self._unsupported("windowunmap")

    def raise_(self, wid: int):
        self._unsupported("windowraise")

    def lower(self, wid: int):
        self._unsupported("windowlower")

    def set_state(self, wid: int, state: str, action: int) -> "str | None":
        """state: uppercase _NET_WM_STATE suffix (e.g. "FULLSCREEN"); action: 0=remove 1=add 2=toggle.

        Returns None when the state applied, or when the backend cannot tell. Returns a one-line reason when the
        compositor ACCEPTED the request and did not apply it -- which KWin does for a window rule, for size
        hints a fullscreen cannot satisfy, and for SHADED on anything but an X11 window. Raise a CmdError only
        for a request the backend could not make at all.

        The caller decides what to do with a reason: wdotool prints it and succeeds (the X tools cannot tell
        either), wwmctl first tries the EWMH ClientMessage, which reaches an XWayland window through the X
        server the compositor's own API just refused."""
        self._unsupported("windowstate")

    def maximize_pair_state(self) -> "str | None":
        """The one state name that sets or clears BOTH maximize axes in a single set_state() call, for a backend
        where doing the two axes one after the other is not the same thing; None where sending one axis at a
        time is correct (KWin and sway settle each axis before they answer, so there is nothing to fold).

        GNOME's is "MAXIMIZED". Mutter unmaximizes to the window's *current* frame rect and takes only the axis
        it is unmaximizing from the saved rectangle, so a second single-axis call that arrives before the
        Wayland client has answered the first configure carries the still maximized half into its target -- and
        once both flags are clear Mutter saves that rectangle as the restore size. Mutter's own EWMH handler
        never splits the pair: it folds both atoms of one ClientMessage into a single call.

        Any command that can be asked for both axes at once has to fold them the way wwmctl.core._state_steps
        does -- `wdotool windowstate --add MAXIMIZED_VERT --add MAXIMIZED_HORZ` included, once it honours more
        than the last option."""
        return None

    def set_num_desktops(self, n: int):
        """Ask the compositor for exactly n workspaces (set_num_desktops). Raises a CmdError with .unsupported
        set where the count is not the caller's to choose (dynamic workspaces)."""
        self._unsupported("set_num_desktops", self.NOT_YET_NUM_DESKTOPS)

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def is_mapped(self, wid: int) -> bool:
        """X11 map state, as close as the backend can tell: the looser "not minimized", NOT "visible". A window
        on an unfocused workspace is mapped, and `windowmap --sync` waiting on visibility would never return for
        one. Backends that track it exactly (sway: not in the scratchpad) override this; the default is the
        visibility flag, which is all a compositor that reports nothing else can offer."""
        return self.find(wid).visible

    def display_size(self) -> tuple[int, int]:
        """The bounding box of the whole output layout, in logical pixels. CmdError when the compositor will not
        say -- callers fall back to a warning and 1920x1080 (wdotool) or print N/A (wwmctl)."""
        self._unsupported("display_size")

    def set_window_desktop(self, wid: int, n: int):
        self._unsupported("set_desktop_for_window", self.NOT_YET_SET_WINDOW_DESKTOP)

    # What to tell the user while an interactive selection is pending. The backends that implement
    # select_window() properly want a click (GNOME's bridge grab, KWin's own picker); the sway backend, which
    # can only wait for a focus change, says so instead -- callers print this rather than guess, because the two
    # are opposite instructions.
    select_window_hint = "click the target window to select it"

    def select_window(self) -> int:
        """Interactively pick a window (selectwindow); return its id.

        xdotool grabs the pointer and answers with the window under it at the next button press. GNOME (bridge
        grab) and KDE (KWin's own picker) do exactly that; sway/i3 have no picker and no pointer query in their
        IPC, so that backend still waits for the next focus change and says so. Cancelling (Escape, or the
        picker's own timeout) raises CmdError -- rc 1, never a made-up window."""
        self._unsupported("selectwindow", self.NOT_YET_SELECT_WINDOW)

    # optional richer views (additive, see the module docstring)
    def views(self) -> "list[View] | None":
        """list() with the View extras, or None when the backend has no
        richer view than Window (callers then synthesize from list())."""
        return None

    def workspaces(self) -> "list[Workspace] | None":
        """Named workspaces with work areas, or None (callers synthesize from
        get_desktop()/num_desktops())."""
        return None

    def move_to_current_desktop(self, wid: int) -> bool:
        """Move `wid` to whatever desktop is current, in one operation, and say whether that happened. False --
        the answer everywhere but sway -- means "ask me which desktop is current and move it by number"; sway
        answers True because a focused workspace can be named and have no number at all, which get_desktop() can
        only report as -1."""
        return False

    def x_info(self) -> tuple[str, str] | None:
        """(DISPLAY, XAUTHORITY) of the session's Xwayland, or None when the backend cannot tell (callers fall
        back to session.find_x_display / find_xauthority)."""
        return None

    def pointer(self) -> tuple[int, int] | None:
        """The compositor's real pointer position in global layout coordinates, or None when the compositor
        offers no pointer query (sway's IPC does not). Callers fall back to the input daemon's model of the last
        position it injected."""
        return None

    def events(self, timeout: float | None = None):
        """Iterator of (window_id, change) with sway's vocabulary (new, close, focus, title, fullscreen_mode,
        move, urgent, workspace); stops after `timeout` seconds of silence (None = never)."""
        self._unsupported("window events", self.NOT_YET_EVENTS)
