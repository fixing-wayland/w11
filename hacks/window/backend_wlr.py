"""wlr-foreign-toplevel window backend (zwlr_foreign_toplevel_management_v1) via wayland_mini.

The floor every wlroots compositor without an IPC socket falls back to: labwc, river, Wayfire's sessions
before the Wayfire backend, Budgie 10.10 and Xfce 4.20 on Wayland (both of which run labwc), LXQt on Wayland.
The protocol carries a title, an app id and four state bits and nothing else -- no geometry, no pid, no
stacking -- so the refusals here name that protocol and, for each gap, the rung of AGENTS.md's ladder that
would close it, rather than a compositor's layout policy.

Four things this backend does on top of the bare protocol, each for a measured defect:

* **Verify after act.** river 0.4 registers no listener for any handle request: `windowclose`,
  `windowactivate`, `windowminimize` and `windowstate --add FULLSCREEN` all returned 0 and changed nothing,
  while river-classic 0.3.17 really closed and really fullscreened [M recon2/river.md §2a, R `Window.zig`
  v0.4.8: `setTitle`/`setAppId`/`setActivated`/`destroy` are pushed at the handle and no `request_*` listener
  exists]. There is no version bit to gate on -- river advertises manager v3 exactly like sway -- so every
  mutating request waits up to VERIFY_TIMEOUT for the handle to say it happened and reports the silence.
* **Desktops over `ext_workspace_manager_v1`.** labwc, Budgie and Xfce-on-Wayland publish workspaces their
  own panels show while `wwmctl -d` refused [M recon2/labwc.md §6a, budgie.md, xfce-wayland.md]. Bound only
  when the global is there, so sway 1.11 and Wayfire 0.10 keep the old refusal [M wayfire.md §1.1].
* **X ids for XWayland windows.** An xterm whose real X id is `0x40000c` was listed as `0x000f4240` with a
  synthesized `xterm.xterm` for `xterm.XTerm`, on labwc, Budgie, Xfce-Wayland, Wayfire and Hyprland alike; a
  same-compositor control (one sway, native backend vs `WDOTOOL_BACKEND=wlr`) proved it is this backend and
  not the compositor [M labwc.md §4]. `views()` joins the toplevels to `_NET_CLIENT_LIST`.
* **X rectangles for those same windows.** The same join, in `list()`, so that `getwindowgeometry` answers
  the X server for a window the X server knows: on the resolute-labwc golden 2026-09-12 an xterm at
  `718,395 484x316` read `0,0 1920x1080` out of `wdotool getwindowgeometry` and `718,395 484x316` out of the
  oracle `xdotool getwindowgeometry 0x40000c`, one session, one second apart. That is AGENTS.md route 5 and
  it reaches XWayland windows only; a native toplevel keeps the floor rectangle and sets
  `geometry_is_floor` (see `list()`).

Window ids are 1000000 + arrival order and are only stable within one wdotool process; unlike COSMIC's
`identifier` and Hyprland's `address` there is no handle to mint from [backend.mint_id]."""

import os
import struct
import time

from w11common import session
from w11common.errors import CmdError
from w11common.wayland_mini import WlConn
from hacks.window import ext_workspace, x11_mini, xid_match
from hacks.window.backend import View, Window, WindowBackend, poll_diff_events, warn

BASE_ID = 1000000

#: Where w11's labwc geometry shim writes each view's rectangle for this backend to read. AGENTS.md route
#: 6 (our own code loaded into an UNMODIFIED labwc from the outside -- an LD_PRELOAD set by the w11-labwc
#: session entry, the distro's labwc and libwlroots untouched): zwlr_foreign_toplevel_management_v1 carries
#: no rectangle, so packaging/labwc-shim/w11-labwc-shim.c -- interposing libwlroots' exported
#: wlr_scene_xdg_surface_create and wlr_scene_output_build_state -- emits `pid\tx\ty\tw\th\tapp_id\ttitle`
#: per view into $XDG_RUNTIME_DIR/<this> whenever the scene changes (map, move/resize, unmap, title/app_id),
#: and `_labwc_geometry` folds it onto the native toplevels the wire gives no geometry for. Absent when the
#: shim is not loaded (a plain labwc session, or any other compositor), where every native row keeps the floor.
GEOMETRY_FILE = "w11-labwc-geometry"

#: How long a mutating request waits for the compositor to say it happened. Half a second: the state event
#: arrives inside the same roundtrip on every compositor that honours the request at all (sway, labwc,
#: river-classic), and a compositor that is going to ignore it is never going to answer.
VERIFY_TIMEOUT = 0.5

#: What "accepted and not applied" is called. The `WindowBackend.set_state` contract already carries this
#: shape for KWin's window rules. The first clause is fixed; the cause after it is per request, because one
#: sentence naming every cause is a false attribution on whichever compositor is not the one at fault.
READ_ONLY_REASON = ("the compositor accepted %s and did not apply it "
                    "(river 0.4's wlr-foreign-toplevel is read-only)")

#: The same, for the two requests that also go nowhere on compositors that are not read-only at all:
#: `WDOTOOL_BACKEND=wlr wdotool windowminimize` on a headless sway 1.11 waited the full VERIFY_TIMEOUT and
#: warned, correctly -- sway has no minimized state (measured on this guest, 2026-09-08) -- and
#: river-classic 0.3.17 does the same [M recon2/river.md §2a].
NO_MINIMIZE_REASON = ("the compositor accepted %s and did not apply it "
                      "(sway and river-classic have no minimized state; river 0.4's "
                      "wlr-foreign-toplevel is read-only); not yet here, and the route is the "
                      "compositor's own IPC where it has one -- sway's scratchpad, which the sway "
                      "backend already uses (AGENTS.md route 2) -- and a minimized state in the "
                      "compositor where it has no such IPC, which is river (route 6)")

#: `close` has a third cause the other requests do not: the client itself. A window that answers close with
#: an unsaved-changes dialog changes nothing on its handle, and blaming river or sway for that on labwc --
#: where close was measured to work [M labwc.md §3] -- would be a lie about the compositor.
CLOSE_REASON = ("the window did not close within %.1f s: the client may be asking to save, or the "
                "compositor ignored the request (river 0.4)")


def read_only_reason(name: str) -> str:
    """The reason line for one request name, naming only the causes that can produce this silence."""
    if "minimized" in name:
        return NO_MINIMIZE_REASON % name
    return READ_ONLY_REASON % name

#: The constructor's own precondition: no manager, no backend.  Batch 19 gave the COSMIC twin of this
#: sentence its rung (backend_cosmic's two preconditions) and left this one stopping at the lack, because
#: tests/test_backend_gnome.py pins it byte for byte and was another batch's file; both moved together on
#: 2026-09-09.  The head is kept contiguous because tests/test_wire_hardening.py greps the substring `does
#: not offer` out of it.  (vm/live-smoke.d/cosmic.sh:14's recorded transcript quotes a similar shape, but
#: that is backend_detect's `no Wayland session found` sentence and not this one.)
NO_MANAGER = ("wlr backend: compositor does not offer zwlr_foreign_toplevel_management_unstable_v1; not "
              "yet here, and the route is that protocol where the compositor grows it (AGENTS.md route "
              "1), else a backend over the compositor's own IPC (route 2), which is what the sway, hypr "
              "and cinnamon backends already are")

#: `activate` takes a wl_seat and this backend will not send a null one (tests/test_backend_wlr.py's
#: `test_activate_without_a_seat_refuses_rather_than_sending_null`).  Nobody has produced a wlr session
#: carrying the toplevel manager and no seat either; the branch exists because the request takes one.  The
#: rungs are written for the session that turns up anyway: wl_seat is core, so a registry that carries it
#: is the first thing to reach for, and sway, hypr and cinnamon focus a window by id over their own IPC
#: and never name a seat at all.
NO_SEAT = ("compositor offers no wl_seat; cannot activate windows; not yet here, and the route is a "
           "registry that carries one (AGENTS.md route 1), else the compositor's own IPC where it has "
           "one, which focuses a window by id and needs no seat at all (route 2)")

#: The refusal for the four geometry commands on a NATIVE toplevel. An XWayland window no longer reaches
#: it -- `move_window`/`resize`/`raise_`/`lower` route it through the X plane (`_xid_of` + `_x_configure`),
#: AGENTS.md route 5, a real ConfigureWindow, exactly as `xdotool` does -- so this is the native half's
#: sentence, and it keeps its head (`zwlr_foreign_toplevel_management_v1 carries no geometry`) because
#: labwc.sh:137-140 and river.sh pin the substring on a native `foot`. labwc is a *stacking* compositor and
#: still cannot move a native window, so the reason names the protocol, not a tiling policy [M labwc.md §6c].
NO_GEOMETRY = ("zwlr_foreign_toplevel_management_v1 carries no geometry and no stacking; an XWayland window "
               "goes through the X plane instead (AGENTS.md route 5, the ConfigureWindow windowmove and "
               "windowsize now send -- windowraise does not restack there because the wlroots xwm drops the "
               "stack mode in xwayland/xwm.c xwm_handle_configure_request). Not yet for a native toplevel, "
               "and the route is a patched compositor (route 6): wlroots + labwc src/xwayland.c, one request "
               "each")

#: The same, for the `_NET_WM_STATE` tail on a NATIVE toplevel. The handle's state array has four members
#: [R the protocol XML's `state` enum]; every other `_NET_WM_STATE` name xdotool and wmctrl take -- SHADED,
#: ABOVE, BELOW, SKIP_* -- has nowhere on this wire, so an XWayland window takes the X plane (route 5, the
#: ClientMessage `wmctrl -b` sends, which labwc reads back) and only a native one lands here.
NO_SUCH_STATE = ("zwlr_foreign_toplevel_management_v1 carries maximized, minimized, activated and "
                 "fullscreen and no other state; an XWayland window goes through the X plane instead "
                 "(AGENTS.md route 5, the _NET_WM_STATE ClientMessage wmctrl -b sends). Not yet for a "
                 "native toplevel, and the route is a patched compositor (route 6): wlroots "
                 "xwayland/xwm.c's xwm_handle_net_wm_state_message has no request_above/below/shade to hand "
                 "labwc, one state bit and one request each")

#: Fullscreen arrived with version 2 of the same protocol, so a v1 manager is a build away and not a
#: rewrite -- the lowest rung there is.
NO_V2 = ("this compositor's zwlr_foreign_toplevel_manager_v1 is version 1, whose handles have no "
         "set_fullscreen; not yet here, and the route is version 2 of the protocol it already speaks "
         "(AGENTS.md route 1), which costs a newer compositor build")

#: sway 1.11 and Wayfire 0.10 publish no workspace global at all, which is why binding it changed nothing
#: for them [M recon2/wayfire.md §1.1]; labwc, Budgie and Xfce-on-Wayland do and take the path above.
NO_WORKSPACES = ("this compositor publishes no ext_workspace_manager_v1; not yet here, and the routes "
                 "are that protocol where the compositor grows it (AGENTS.md route 1) or the "
                 "compositor's own IPC where it has one (route 2), which is a backend per compositor")

#: `_NET_WM_STATE` names with no member in the handle's four-bit array (maximized/minimized/activated/
#: fullscreen cover MAXIMIZED_*, HIDDEN and FULLSCREEN). `unsupported_states()` hands these to wwmctl's own
#: X-plane route for an XWayland window, and `set_state` takes the same route for a wdotool caller that has
#: no X fallback; a native toplevel gets the NO_SUCH_STATE refusal.
_TAIL_STATES = frozenset((
    "MODAL", "STICKY", "SHADED", "SKIP_TASKBAR", "SKIP_PAGER", "ABOVE", "BELOW",
    "DEMANDS_ATTENTION", "FOCUSED",
))

# zwlr_foreign_toplevel_handle_v1 state enum
_ST_MAXIMIZED = 0
_ST_MINIMIZED = 1
_ST_ACTIVATED = 2
_ST_FULLSCREEN = 3

# handle requests
_REQ_SET_MAXIMIZED = 0
_REQ_UNSET_MAXIMIZED = 1
_REQ_SET_MINIMIZED = 2
_REQ_UNSET_MINIMIZED = 3
_REQ_ACTIVATE = 4
_REQ_CLOSE = 5
_REQ_SET_FULLSCREEN = 8
_REQ_UNSET_FULLSCREEN = 9


class _Toplevel:
    __slots__ = ("oid", "title", "app_id", "states", "closed")

    def __init__(self, oid):
        self.oid = oid
        self.title = ""
        self.app_id = ""
        self.states: set[int] = set()
        self.closed = False


class XPlaneViews:
    """`views()` for the two foreign-toplevel backends: the Wayland listing joined to `_NET_CLIENT_LIST`.

    Both wlroots and cosmic-comp run an Xwayland whose windows the toplevel protocols report under a
    synthesized id, and neither protocol carries an X id, a pid or a rectangle -- so the join, its matcher
    and its failure rules are one piece of code with two users. `CosmicBackend` imports it from here rather
    than the other way round because the wlr floor is the older of the two.

    What a backend supplies: `self.uid` (the session's uid, for the X socket), `list()` and `_view_flags()`,
    a list of `View` keyword dicts parallel to the listing. The two calls are adjacent on purpose: both are
    built from the same records in arrival order, and no event can land between them because `wayland_mini`
    reads the socket only inside a roundtrip.

    `_x_join` is the pairing itself, split out of `views()` so that `WlrBackend.list()` can fold the X
    server's rectangle into the rows the toplevel protocol gives no rectangle for -- AGENTS.md route 5,
    the X plane, which is the lowest rung in reach for `getwindowgeometry` on this floor."""

    #: (the listing object, the join computed from it), so the two readers of one listing pay for one join.
    #: A class attribute, not an `__init__` line: `CosmicBackend` has its own constructor and this mixin has
    #: none.
    _join = None

    def views(self) -> "list[View] | None":
        """The listing with the X plane folded in, or None when there is no X plane to fold.

        The pairing has only the title and the app id against `WM_CLASS` -- no pid filter and no geometry
        score, which is why `match_xids` is called with `ratio=None` and with no `ix` key: the toplevel
        arrival order is not `_NET_CLIENT_LIST` order the way KWin's window list is, so using it as a
        tie-break would be a guess. The KWin rule stands unchanged: a pair must agree on something, and a tie
        nobody can break keeps xid 0 [M labwc.md §4, cosmic.md §5.4, xid_match.match_xids]."""
        wins = self.list()
        flags = self._view_flags()
        join = self._x_join(wins)
        if join is None:
            return None
        xids, by_xid, blocked = join
        if blocked:
            # The tie is said here and nowhere else. `list()` runs the same join under every window
            # command there is (`search`, `getwindowgeometry`, a `--sync` poll), and the original xdotool
            # prints nothing at all on any of them: a warning from down there is a stderr parity
            # regression, once per poll. `wwmctl -l` is the reader the sentence was written for -- it
            # prints the X id column, and a 0 in it is what wants explaining.
            warn(xid_match.tie_warning(blocked))
        out = []
        for w, fl in zip(wins, flags):
            xid = xids.get(str(w.id), 0)
            c = by_xid.get(xid)
            if c is None:
                # a native toplevel: `app_id` and no WM_CLASS pair, which is what makes wwmctl render it as
                # `foot.foot` rather than inventing an instance it never read
                out.append(View(window=w, app_id=w.class_, **fl))
                continue
            gx, gy, gw, gh = c["geo"]
            w.pid, w.x, w.y, w.w, w.h = int(c["pid"]), gx, gy, gw, gh
            w.instance = c["inst"]
            w.class_ = c["cls"] or w.class_
            out.append(View(window=w, xid=xid, instance=c["inst"], cls=c["cls"],
                            client_type="x11", **fl))
        return out

    def _x_join(self, wins) -> "tuple[dict, dict, int] | None":
        """Pair `wins` against `_NET_CLIENT_LIST`: `({str(window id): xid}, {xid: client record}, ties)`,
        or None when there is no X plane to pair against or the read failed halfway through.

        The join is SILENT: `match_xids_quiet` hands back the number of windows a tie left unpaired
        instead of printing it, and `views()` above is the one place that says it. `list()` calls this too
        now, and `list()` is under every window command, so a warning in here is a line the original
        xdotool never printed on `search` or `getwindowgeometry` -- and once per poll under `--sync`.

        Memoised on the listing OBJECT, because both readers of one listing want the same pairing:
        `WlrBackend.list()` folds the rectangle in and the `views()` above reads the ids out of the very
        listing that just did it. Without the memo one `wwmctl -l` asks the X server for
        `_NET_CLIENT_LIST` plus five properties per client twice over. Measured
        in-process against a headless labwc on this guest, 2026-09-12: `list()` is 0.02 ms with no X plane,
        0.10 ms with one X client joined and 0.53 ms with eight -- about 0.06 ms per client, which is what
        the second reader stops paying. The key is the list object and the cache keeps a reference to it,
        so its identity cannot be recycled under a later listing the way an `id()` could be."""
        if self._join is not None and self._join[0] is wins:
            return self._join[1]
        x = self._x11()
        if x is None:
            return None
        try:
            clients = self._x_clients(x)
        except Exception:   # any X failure: the floor listing, no crash
            self._drop_x(x)
            return None
        raw = [{"u": str(w.id), "c": w.class_, "n": "", "t": w.title} for w in wins]
        xids, blocked = xid_match.match_xids_quiet(raw, clients, None)
        join = (xids, {c["xid"]: c for c in clients}, blocked)
        self._join = (wins, join)
        return join

    def _x11(self):
        """The X connection, opened once, or None. Never starts an Xwayland: labwc Depends on xwayland and
        has one already, while sway and Wayfire spawn theirs on demand and a connect would be the spawn."""
        if self._x != "unset":
            return self._x
        self._x = None
        if not session.xwayland_running(self.uid):
            return None
        display = session.find_x_display(self.uid) or None
        xauth = session.find_xauthority(self.uid) or None
        try:
            from hacks.window import x11_mini
            self._x = x11_mini.X11Conn(display, xauthority=xauth)
        except Exception:   # no X plane: every xid stays 0
            self._x = None
        return self._x

    def _drop_x(self, x):
        """Forget a connection that failed mid-read -- and close it first: wdotool's daemon and a wwmctl
        loop both outlive one views() call, and a dropped reference is a leaked fd there."""
        try:
            x.close()
        except OSError:
            pass
        self._x = None

    @staticmethod
    def _x_clients(x) -> "list[dict]":
        """The X clients as `xid_match` wants them, in `_NET_CLIENT_LIST` order."""
        out = []
        for xid in x.client_list():
            try:
                inst, cls = x.get_wm_class(xid)
                name = x.get_prop_string(xid, "_NET_WM_NAME") or x.get_prop_string(xid, "WM_NAME")
                geo = x.get_geometry(xid)
                pid = x.get_pid(xid)
            except Exception:   # a window that just died
                continue
            out.append({"xid": int(xid), "pid": int(pid), "inst": inst, "cls": cls,
                        "name": name, "geo": geo})
        return out

    # -- the X plane as the geometry/stacking/state route (AGENTS.md route 5) --
    #
    # `zwlr_foreign_toplevel_management_v1` and the COSMIC toplevel protocols carry no rectangle, no stacking
    # and only their handful of state bits, so `windowmove`, `windowsize`, `windowraise`, `windowlower` and the
    # `_NET_WM_STATE` tail (SHADED/ABOVE/BELOW/SKIP_*) have nowhere to go on the Wayland wire. For an XWayland
    # window there is a second wire: the compositor runs an X window manager, so a real `ConfigureWindow` and a
    # real `_NET_WM_STATE` ClientMessage reach it exactly as the original `xdotool`/`wmctrl` send them — and
    # whether they LAND is then the xwm's business and identical to what those originals get on the same
    # session. Measured 2026-09-14: on labwc 0.9.3 a move, a resize and every `_NET_WM_STATE` toggle land and
    # read back (raise does not restack — the wlroots xwm drops the stack mode); on cosmic-comp 1.8.0 the
    # resize lands, the move and the state toggles are no-ops (Smithay's xwm drops them), byte for byte what
    # `xdotool`/`wmctrl` do there. A NATIVE toplevel has no X id, keeps the refusal and its rung-6 route.

    def _xid_of(self, wid: int) -> int:
        """The X id `wid` was joined to, or 0 for a native toplevel / no X plane. The join is the very one
        `list()`/`views()` use, keyed by `str(window id)`, so an XWayland window resolves to the same X id the
        listing shows in its `-x` column."""
        join = self._x_join(self.list())
        if join is None:
            return 0
        return join[0].get(str(wid), 0)

    def geometry_is_client_rect(self, wid: int) -> bool:
        """True for an XWayland window: its move/resize go out as a `ConfigureWindow` on the X plane, whose
        x/y/width/height are the CLIENT rectangle -- the compositor's xwm does not reparent, so there is no
        frame between the request and the window. `move_resize` (-e) reads this and zeroes the frame extents,
        so `wwmctl -e 0,-1,-1,640,360` sends the client width 640, not 640+left+right; without it a cosmic-comp
        XWayland window came out 36 px too tall (its server-side title bar folded into the extents). A native
        toplevel has no X id and reaches no X plane, so this is False for it and the -e path never runs anyway
        (move/resize refuse before extents matter)."""
        return self._xid_of(wid) != 0

    def _x_configure(self, xid: int, op: str, **kw) -> None:
        """One `ConfigureWindow` on the X plane for the XWayland window `xid` (route 5)."""
        x = self._x11()
        if x is None:   # xid came from a join that opened it, so this is belt-and-braces
            raise CmdError("%s backend: the X plane went away" % self.name)
        try:
            x.configure_window(xid, **kw)
        except Exception as e:
            raise CmdError("%s backend: %s on 0x%08x: %s" % (self.name, op, xid, e)) from None

    def state_route_is_x_plane(self, wid: int, state: str) -> bool:
        """True for an XWayland window's tail state: `set_state` here sends exactly the `_NET_WM_STATE`
        ClientMessage wwmctl's `_x_set_state` already sends (both are `_x_state`/`send_root_message` on the
        same X id), so wwmctl skips this backend for those and the message goes out once, matching `wmctrl`.
        False for a native toplevel (no X id, `set_state` refuses at rung 6) and for a state the handle's
        array carries (`set_state` uses the Wayland setter), so wwmctl still reaches those the usual way."""
        return state in self.unsupported_states() and self._xid_of(wid) != 0

    def _x_state(self, xid: int, state: str, action: int) -> None:
        """The `_NET_WM_STATE` ClientMessage real `wmctrl -b` sends, for the XWayland window `xid` (route 5).
        Fire-and-forget, exactly as `wmctrl` is: the message goes to the root with
        SubstructureNotify|SubstructureRedirect and the xwm applies it or not — labwc reads it back, cosmic-comp
        drops it, and neither original prints a word about which. `action` is the EWMH 0/1/2 (remove/add/toggle),
        which is `set_state`'s own action byte unchanged."""
        x = self._x11()
        if x is None:
            raise CmdError("%s backend: the X plane went away" % self.name)
        try:
            atom = x.atom("_NET_WM_STATE_%s" % state)
            x.send_root_message(xid, "_NET_WM_STATE", [action, atom, 0, 0, 0])
        except Exception as e:
            raise CmdError("%s backend: _NET_WM_STATE_%s ClientMessage on 0x%08x: %s"
                           % (self.name, state, xid, e)) from None

    # -- events() and select_window() from the toplevel protocol's own changes (AGENTS.md route 1) --
    #
    # Both floors already listen to their foreign-toplevel protocol and rebuild the whole listing on demand,
    # and `activated`/title/closed are exactly the changes `list()` reflects (focused, title, presence). So the
    # (id, change) stream every caller of `events()` speaks -- and the `selectwindow` that waits on a focus
    # change -- come out of a `list()` diff, the same poll the cinnamon backend runs. This is the doc's own
    # route for the wlr/cosmic `events()`/`selectwindow` gap, and it retires the two base-class not-yets there.

    def events(self, timeout: "float | None" = None):
        """(id, change) in sway's vocabulary (new, focus, title, close), polled from the toplevel protocol's
        own `activated`/title/closed changes via a `list()` diff. `timeout` is silence, as everywhere."""
        return poll_diff_events(self.list, timeout)

    #: waiting for a focus change, not clicking: these floors carry no pointer position and no per-window
    #: geometry (wlr) / only a sometimes-sent one (cosmic), so there is nothing to click *with* -- the same
    #: reason and the same instruction as sway.  A click-to-pick overlay is the NOT_YET_SELECT_WINDOW route.
    select_window_hint = "focus the target window to select it"

    def select_window(self) -> int:
        """The sway shape (AGENTS.md route 1): wait for the next `activated` change -- a focus event -- and
        return that window.  Not `xdotool`'s window-under-the-next-click (these protocols have no pointer and no
        rectangle to click into, see NOT_YET_SELECT_WINDOW), and knowingly so: focusing the already-focused
        window does not end this wait; focus another to pick it."""
        for wid, change in self.events():
            if change == "focus":
                return wid
        raise CmdError("%s backend: the window event stream ended" % self.name)


class WlrBackend(XPlaneViews, WindowBackend):
    name = "wlr"
    #: What every wlroots xwm puts on its `_NET_SUPPORTING_WM_CHECK` window -- read off the X plane on labwc,
    #: Wayfire and river, byte-identical to sway's [M labwc.md §3 wwmctl, wayfire.md §2.4, river.md §3]. So
    #: `wwmctl -m` from a root shell, or on a session with no Xwayland, says what the in-session run says
    #: instead of falling back to the backend token `wlr`.
    wm_name = "wlroots wm"

    def __init__(self, conn=None):
        """`conn` is a live `WlConn` whose registry has been read -- detection's, so a session opens one
        connection and not two. Without it this opens its own and owns it; with it the caller keeps it, and
        keeps it after a constructor failure too."""
        self._own_conn = conn is None
        self.uid = None
        if conn is None:
            hit = session.find_wayland_socket()
            if not hit:
                raise CmdError("wlr backend: no Wayland socket found")
            self.uid, _rd, sockpath = hit
            try:
                self.c = WlConn(sockpath)
            except OSError as e:
                raise CmdError("wlr backend: cannot connect to %s: %s" % (sockpath, e)) from None
        else:
            self.c = conn
        try:
            reg = self.c.get_registry()
            g = self.c.find_global("zwlr_foreign_toplevel_manager_v1")
        except (OSError, RuntimeError, struct.error) as e:
            self._close_own()
            raise CmdError("wlr backend: %s" % e) from None
        if not g:
            self._close_own()
            raise CmdError(NO_MANAGER)
        self.tops: dict[int, _Toplevel] = {}  # handle oid -> record
        self.order: list[int] = []            # handle oids, arrival order
        self.mgr_ver = min(g[1], 3)
        self.mgr = self.c.bind(g[0], "zwlr_foreign_toplevel_manager_v1", self.mgr_ver)
        self.c.on(self.mgr, self._on_mgr)

        self.seat = None
        sg = self.c.find_global("wl_seat")
        if sg:
            self.seat = self.c.bind(sg[0], "wl_seat", min(sg[1], 2))
            self.c.on(self.seat, lambda op, cur, fds: None)

        self.out_w = self.out_h = 0
        for name, (iface, ver) in list(reg.items()):
            if iface == "wl_output":
                oid = self.c.bind(name, "wl_output", min(ver, 2))
                self.c.on(oid, self._on_output)

        # The workspace protocol is a separate global, and most of the family does not have it: None here is
        # what keeps sway 1.11's and Wayfire 0.10's desktop refusals exactly as they were.
        self.ws = ext_workspace.WorkspaceClient.bind(self.c)

        self._x = "unset"   # lazy X11Conn for the X-plane join; None once it is known there is none
        #: set once a listing had to report an output rectangle for a window the X plane did not answer for
        self.geometry_is_floor = False
        self._pump()  # toplevel announcements
        self._pump()  # each handle's initial title/app_id/state/done

    def _close_own(self):
        """Drop the connection only if this constructor opened it: a caller that handed one in still holds
        it, and closing it here would take detection's registry away from whoever asks next."""
        if self._own_conn:
            try:
                self.c.close()
            except OSError:
                pass

    # -- events -------------------------------------------------------------

    def _on_mgr(self, op, cur, fds):
        if op == 0:  # toplevel(new_id)
            oid = cur.u32()
            t = _Toplevel(oid)
            self.tops[oid] = t
            self.order.append(oid)
            self.c.on(oid, lambda o, c, f, t=t: self._on_top(t, o, c))
        # op 1 = finished

    def _on_top(self, t: _Toplevel, op, cur):
        if op == 0:
            t.title = cur.string()
        elif op == 1:
            t.app_id = cur.string()
        elif op == 4:
            arr = cur.array()
            t.states = set(struct.unpack("<%dI" % (len(arr) // 4), arr))
        elif op == 6:
            t.closed = True
        # 2/3 output enter/leave, 5 done, 7 parent: ignored

    def _on_output(self, op, cur, fds):
        if op == 1:  # mode(flags, width, height, refresh)
            flags, w, h = cur.u32(), cur.i32(), cur.i32()
            if flags & 1:  # current mode
                self.out_w = max(self.out_w, w)
                self.out_h = max(self.out_h, h)

    def _pump(self):
        """One roundtrip, with the wire's failures turned into one clear line.

        A compositor that goes away mid-session (RuntimeError), or answers with an event whose payload is
        shorter than the interface says (struct.error), or whose socket errors or times out (OSError), is a
        routine thing for a session that is restarting -- not a traceback."""
        try:
            self.c.roundtrip()
        except CmdError:
            raise
        except (OSError, RuntimeError, struct.error) as e:
            raise CmdError("wlr backend: %s" % e) from None

    def _by_wid(self, wid: int) -> _Toplevel:
        self._pump()
        idx = wid - BASE_ID
        if 0 <= idx < len(self.order):
            t = self.tops[self.order[idx]]
            if not t.closed:
                return t
        raise CmdError("window %d not found" % wid)

    def _request(self, t: _Toplevel, opcode: int, args=(), name=None, check=None, reason=None):
        """Send one handle request and say whether the compositor acted on it.

        Returns None when it did (or when there is nothing to check), and a reason line when VERIFY_TIMEOUT
        passed with the handle unchanged -- `reason` where the caller has one of its own, else the line
        `read_only_reason` picks for this request. `check(t)` reads the record the compositor's own events
        fill in, so this is the wire's answer and not a guess: the protocol has no reply for any of these
        requests, and river 0.4 returns success for all four of them [M river.md §2a]."""
        try:
            self.c.send(t.oid, opcode, args)
        except OSError as e:
            raise CmdError("wlr backend: %s" % e) from None
        self._pump()
        if check is None:
            return None
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while not check(t):
            left = deadline - time.monotonic()
            if left <= 0:
                return reason or read_only_reason(name or "the request")
            try:
                self.c.dispatch(left)
            except (OSError, RuntimeError, struct.error) as e:
                raise CmdError("wlr backend: %s" % e) from None
        return None

    def _act(self, t: _Toplevel, opcode: int, name: str, args=(), check=None, reason=None):
        """`_request` for the commands that have nowhere to return a reason: warn and carry on, which is the
        idiom wdotool already uses for KWin's accepted-and-ignored operations (decision C5.17)."""
        why = self._request(t, opcode, args, name=name, check=check, reason=reason)
        if why:
            warn(why)

    @staticmethod
    def _has(bit: int, on: bool):
        return lambda t: (bit in t.states) == on

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        """The toplevels, with the X server's rectangle and pid folded into the rows it has them for.

        AGENTS.md route 5. `zwlr_foreign_toplevel_management_v1` carries no rectangle, so before this every
        row read `0,0` plus the widest output's mode -- measured on the resolute-labwc golden 2026-09-12,
        where `wdotool getwindowgeometry 1000000` printed `Position: 0,0 (screen: 0)` / `Geometry:
        1920x1080` for an xterm the X server put at `718,395 484x316` (the pinned oracle
        `xdotool getwindowgeometry 0x40000c`, same session, same second). The X plane is the one route in
        reach and it reaches XWayland windows only, so that is exactly how far this goes: a row joined to an
        X client answers the X server, a native toplevel keeps the floor and sets `geometry_is_floor`.
        A rectangle for the native half is rung 6 -- our own code loaded into an unmodified labwc from the
        outside: the LD_PRELOAD shim (packaging/labwc-shim/) that reads each view's scene rectangle and
        writes it to the runtime file `_labwc_geometry` reads, one .so covering the four labwc goldens
        (labwc, Budgie, Xfce-on-Wayland, LXQt-on-Wayland) -- and rung 3 on river, a
        `river_window_manager_v1` WM client that reads `river_window_v1.dimensions` for the size and the
        position it set with `river_node_v1.set_position`. Upstreaming that into a foreign-toplevel protocol
        that carries a rect (which wlroots would have to write and ship) is what turns it into rung 1 later.

        Folding it here and not only in `views()` is what puts it where the xdotool clones read it: the two
        readers of a rectangle are `getwindowgeometry` (through `find()`) and `hit_test`, which is
        `getmouselocation`'s `window:` field, and both go through `list()` and never through `views()`.
        (`search --onlyvisible` reads `w.visible` alone -- no rectangle is involved there.)

        The pid rides the same join and for the same reason. `_NET_WM_PID` is on the X window and the
        toplevel protocol carries no pid at all, so `wdotool getwindowpid` on an XWayland window answered
        `window 1000000 has no pid associated with it` while `wwmctl -lGpx` -- which reads `views()`, where
        the pid was already folded -- printed `0x0040000c -1 2045 718 395 484 316` for that very window on
        the resolute-labwc golden, 2026-09-12 [M goal2/requests-batch-12.md 4]. X answers it, so we owe it,
        and it is the same assignment in the same loop. It turns `getwindowpid` and `windowkill` into
        working commands for XWayland windows on labwc, river, Budgie, Xfce-on-Wayland and LXQt-on-Wayland
        (`WindowBackend.kill` sends SIGKILL to `find(wid).pid`). A native toplevel takes the pid from the
        same file the rectangle comes from -- the shim reads it from the surface's wl_client -- so on the
        labwc family `getwindowpid`/`windowkill` reach a native window too; without the shim (a plain labwc
        session, or river, whose rung is 3) a native row keeps pid 0 and keeps xdotool's own refusal, which
        is the byte the original prints for an X window with no `_NET_WM_PID`. Same reachable rung as the
        rectangle -- 6 (the LD_PRELOAD shim) and 3 on river -- the same per-toplevel field, closed the same
        way, and rung 1 later if a foreign-toplevel protocol carries the pid.

        Two XWayland windows the join cannot tell apart -- two xterms under the default title, which is a
        common shape on this floor -- both keep the floor, because `match_xids` hands out no id on a tie
        and an unknown rectangle beats a wrong one. The tie-break route 5 lacks is a rectangle on the
        Wayland side, which is the same native-geometry gap above, from the other end."""
        self._pump()
        wins = []
        for i, oid in enumerate(self.order):
            t = self.tops[oid]
            if t.closed:
                continue
            wins.append(Window(
                id=BASE_ID + i,
                title=t.title,
                class_=t.app_id,
                pid=0,
                x=0, y=0, w=self.out_w, h=self.out_h,  # the floor, until the X plane says otherwise below
                focused=_ST_ACTIVATED in t.states,
                visible=_ST_MINIMIZED not in t.states,
                desktop=-1,
            ))
        join = self._x_join(wins)
        # The tie count is `views()`'s to print (see `_x_join`): every window command runs this listing.
        xids, by_xid = join[:2] if join is not None else ({}, {})
        geo_file = self._labwc_geometry()
        for w in wins:
            c = by_xid.get(xids.get(str(w.id), 0))
            if c is not None:
                w.pid, (w.x, w.y, w.w, w.h) = int(c["pid"]), c["geo"]
                continue
            # No X client for this row: a native toplevel, or an X plane that is not there (sway and
            # Wayfire spawn Xwayland on demand and `_x11` will not be the one to start it). AGENTS.md
            # route 6: w11's LD_PRELOAD shim in an unmodified labwc writes each view's rectangle to a
            # runtime file, joined here by (app_id, title) -- the same pair the X-plane join uses, a tie
            # keeping the floor. When the shim is loaded the native toplevel answers its true rectangle;
            # when it is not (a plain labwc session, or river, whose rung is 3) the row keeps the floor and
            # latches `geometry_is_floor`, the way backend_cosmic's `_rect` latches it.
            fr = geo_file.get((w.class_, w.title))
            if fr is not None:
                fpid, w.x, w.y, w.w, w.h = fr
                if fpid > 0:
                    w.pid = fpid
                continue
            self.geometry_is_floor = True
        return wins

    def _session_runtime_dir(self) -> str:
        """The runtime directory the compositor's geometry file lives in -- the SESSION's, not this
        process's.

        `self.uid` is set only when this backend opened its own connection; detection hands the connection
        in and leaves it None, and then a root `wwmctl` (attaching to the seated user's session, the whole
        point of running these tools as root) would read root's own `/run/user/0` and never the user's file.
        So when the uid is unknown, ask `find_wayland_socket()` -- the same scan that found the socket this
        backend is talking to -- for the directory that holds it, exactly as the own-connection path already
        does. In-session (uid is us) this is just `$XDG_RUNTIME_DIR`."""
        if self.uid is not None:
            return session.runtime_dir(uid=self.uid)
        hit = session.find_wayland_socket()
        if hit:
            return hit[1]
        return session.runtime_dir()

    def _labwc_geometry(self):
        """The per-view rectangles w11's labwc geometry shim writes to `$XDG_RUNTIME_DIR/w11-labwc-geometry`,
        keyed by `(app_id, title)` with any key two windows share dropped, or `{}` when there is no such file.

        AGENTS.md route 6 -- our own code loaded into an UNMODIFIED labwc from the outside, an LD_PRELOAD set
        by the w11-labwc session entry (packaging/labwc-shim/), the distro's labwc and libwlroots untouched.
        `zwlr_foreign_toplevel_management_v1` and `ext_foreign_toplevel_list_v1` carry no rectangle, so a
        NATIVE toplevel's geometry never reaches this backend over the wire and the X plane (route 5) reaches
        XWayland windows only. The shim closes the native half by interposing libwlroots'
        `wlr_scene_xdg_surface_create` (which ties a scene node to each xdg_surface) and reading the node's
        on-screen box with `wlr_scene_node_coords` plus `xdg_surface->geometry` -- the same rectangle
        `getwindowgeometry` prints for an XWayland view -- one line `pid\tx\ty\tw\th\tapp_id\ttitle` per
        toplevel view, rewritten each frame the scene changes. The join is `(app_id, title)`, the same pair
        `match_xids` pairs the X plane on, and a key two windows share is dropped so a tie keeps the floor
        rather than guess -- exactly the X-plane rule from the other end. A missing file (a plain labwc
        session without the shim, sway/Wayfire before their Xwayland, or river, whose rung is 3 not 6)
        yields `{}` and every native row keeps the floor."""
        try:
            path = os.path.join(self._session_runtime_dir(), GEOMETRY_FILE)
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except (OSError, CmdError):
            return {}
        seen: dict = {}
        dup: set = set()
        for ln in lines:
            parts = ln.split("\t", 6)
            if len(parts) != 7:
                continue
            try:
                pid, x, y, w, h = (int(parts[i]) for i in range(5))
            except ValueError:
                continue
            key = (parts[5], parts[6])
            if key in seen:
                dup.add(key)
            else:
                seen[key] = (pid, x, y, w, h)
        for key in dup:
            seen.pop(key, None)
        return seen

    def activate(self, wid: int):
        t = self._by_wid(wid)
        if self.seat is None:
            raise CmdError("wlr backend: %s" % NO_SEAT)
        self._act(t, _REQ_ACTIVATE, "activate", [("u", self.seat)],
                  check=self._has(_ST_ACTIVATED, True))

    def close(self, wid: int):
        t = self._by_wid(wid)
        before = set(t.states)
        # A client may take a moment to go, or refuse outright, and its own state may change on the way out:
        # anything at all having happened is enough to say the request was heard [M river.md §2a, where
        # nothing happened at all, twice, five seconds apart].
        self._act(t, _REQ_CLOSE, "close", reason=CLOSE_REASON % VERIFY_TIMEOUT,
                  check=lambda tt: tt.closed or tt.states != before)

    def minimize(self, wid: int):
        self._act(self._by_wid(wid), _REQ_SET_MINIMIZED, "set_minimized",
                  check=self._has(_ST_MINIMIZED, True))

    def map(self, wid: int):
        self._act(self._by_wid(wid), _REQ_UNSET_MINIMIZED, "unset_minimized",
                  check=self._has(_ST_MINIMIZED, False))

    def unmap(self, wid: int):
        self._act(self._by_wid(wid), _REQ_SET_MINIMIZED, "set_minimized",
                  check=self._has(_ST_MINIMIZED, True))

    def set_state(self, wid: int, state: str, action: int):
        t = self._by_wid(wid)
        if state == "FULLSCREEN":
            if self.mgr_ver < 2:
                self._not_yet("windowstate FULLSCREEN", NO_V2)
            on = action == 1 or (action == 2 and _ST_FULLSCREEN not in t.states)
            if on:
                return self._request(t, _REQ_SET_FULLSCREEN, [("u", 0)], name="set_fullscreen",
                                     check=self._has(_ST_FULLSCREEN, True))
            return self._request(t, _REQ_UNSET_FULLSCREEN, name="unset_fullscreen",
                                 check=self._has(_ST_FULLSCREEN, False))
        if state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            # the protocol only has all-or-nothing maximize
            on = action == 1 or (action == 2 and _ST_MAXIMIZED not in t.states)
            return self._request(t, _REQ_SET_MAXIMIZED if on else _REQ_UNSET_MAXIMIZED,
                                 name="set_maximized" if on else "unset_maximized",
                                 check=self._has(_ST_MAXIMIZED, on))
        if state == "HIDDEN":
            on = action == 1 or (action == 2 and _ST_MINIMIZED not in t.states)
            return self._request(t, _REQ_SET_MINIMIZED if on else _REQ_UNSET_MINIMIZED,
                                 name="set_minimized" if on else "unset_minimized",
                                 check=self._has(_ST_MINIMIZED, on))
        # The tail (SHADED, ABOVE, BELOW, SKIP_*, MODAL, ...): no member in the four-bit array. An XWayland
        # window takes the X plane -- the _NET_WM_STATE ClientMessage wmctrl sends (route 5) -- and a native
        # one keeps the refusal. wwmctl reaches the X plane through its own fallback for these; this arm is the
        # route for a wdotool caller, which has none.
        xid = self._xid_of(wid)
        if xid:
            return self._x_state(xid, state, action)
        self._not_yet("windowstate %s" % state, NO_SUCH_STATE)

    def unsupported_states(self) -> "set[str]":
        """`_NET_WM_STATE` names this backend has no Wayland setter for, so wwmctl reaches an XWayland window
        through the X server for them instead (`_x_set_state`) and agrees with the route `set_state` takes for
        a wdotool caller. FULLSCREEN, MAXIMIZED_* and HIDDEN are NOT here: the handle's array carries them."""
        return set(_TAIL_STATES)

    def _not_yet(self, op: str, why: str):
        """A capability gap, its cause and the route that would close it, as one CmdError.

        The prefix is left exactly as it was -- callers print it as `wwmctl: windowsize is not supported by
        the wlr backend; ignoring`, and vm/live-smoke.d/labwc.sh and river.sh grep for it -- and the
        sentence after the colon is the part AGENTS.md asks for: what is missing, and the lowest rung that
        would fetch it."""
        err = CmdError("%s is not supported by the %s backend: %s" % (op, self.name, why))
        err.unsupported = True
        raise err

    def _no_geometry(self, op: str):
        """The four geometry commands. The sentence after the colon replaces README note (c)'s tiling
        explanation, which is wrong on labwc: it is a stacking compositor and still cannot move a window,
        because the protocol has no request for it [M recon2/labwc.md §6c]."""
        self._not_yet(op, NO_GEOMETRY)

    def move_window(self, wid: int, x: int, y: int):
        xid = self._xid_of(wid)
        if xid:
            return self._x_configure(xid, "windowmove", x=x, y=y)
        self._no_geometry("windowmove")

    def resize(self, wid: int, w: int, h: int):
        xid = self._xid_of(wid)
        if xid:
            return self._x_configure(xid, "windowsize", width=w, height=h)
        self._no_geometry("windowsize")

    def raise_(self, wid: int):
        xid = self._xid_of(wid)
        if xid:
            return self._x_configure(xid, "windowraise", stack_mode=x11_mini.STACK_ABOVE)
        self._no_geometry("windowraise")

    def lower(self, wid: int):
        xid = self._xid_of(wid)
        if xid:
            return self._x_configure(xid, "windowlower", stack_mode=x11_mini.STACK_BELOW)
        self._no_geometry("windowlower")

    # -- desktops -----------------------------------------------------------

    def get_desktop(self) -> int:
        if self.ws is None:
            self._not_yet("get_desktop", NO_WORKSPACES)
        self._pump()
        return self.ws.active_index()

    def set_desktop(self, n: int):
        if self.ws is None:
            self._not_yet("set_desktop", NO_WORKSPACES)
        self._pump()
        if not self.ws.activate(n):
            raise CmdError("wlr backend: cannot activate workspace %d" % n)
        self._pump()

    def num_desktops(self) -> int:
        if self.ws is None:
            self._not_yet("get_num_desktops", NO_WORKSPACES)
        self._pump()
        return self.ws.count()

    def workspaces(self):
        if self.ws is None:
            return None
        self._pump()
        return self.ws.workspace_list()

    def display_size(self) -> tuple[int, int]:
        if not self.out_w or not self.out_h:
            raise CmdError("wlr backend: no wl_output mode seen")
        return self.out_w, self.out_h

    # -- XWayland ids -------------------------------------------------------
    # views(), the X connection and the matcher live in XPlaneViews above; only the state bits are the
    # backend's own.

    def _view_flags(self) -> "list[dict]":
        """The `View` state flags for each row of `list()`, in the same order -- the `XPlaneViews` hook.

        The four bits this protocol carries are the four `View` fields wxprop reads: `_node_from_view` builds
        `visible` from `minimized`/`hidden` and `fullscreen_mode` from `fullscreen`, and
        `_NET_WM_STATE_HIDDEN` comes off that node [wxprop/core.py:171-176, 545]. Leaving them at their
        defaults would drop `_NET_WM_STATE_HIDDEN` from a minimized native window -- which the listing
        fallback `{"visible": w.visible}` used to print, before this backend had a views() at all."""
        out = []
        for oid in self.order:
            t = self.tops[oid]
            if t.closed:
                continue
            mx = _ST_MAXIMIZED in t.states
            out.append({"minimized": _ST_MINIMIZED in t.states,
                        "fullscreen": _ST_FULLSCREEN in t.states,
                        "maximized_h": mx, "maximized_v": mx})
        return out
