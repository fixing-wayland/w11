"""Hyprland window backend over the compositor's own IPC (`wdotool/hypr_ipc.py`).

Detection used to land Hyprland on the generic wlroots floor, which is honest for reading and wrong about
three things that were measured on a live 0.53.3 [M recon2/hyprland.md §3]:

* ids shift under the caller's feet -- two foots listed as `0x000f4240`/`0x000f4241`, and closing the first
  renamed the survivor to `0x000f4240`;
* `windowminimize` was accepted and did nothing (`hyprctl` still said `mapped: 1 hidden: 0`) -- Hyprland has
  no minimize at all, so the floor's silent success was a lie;
* every geometry was the whole output where `hyprctl -j clients` had the real rectangle (`at: 22,61
  size: 1876,997`).

`hyprctl -j clients` publishes everything the protocol does not: `at`, `size`, `pid`, `class`, `initialClass`,
`workspace`, `monitor`, `floating`, `mapped`, `hidden`, `fullscreen`, `pinned`, `xwayland`, `focusHistoryID`
[M recon2/hyprland.md §2, the recorded fixture is tests/fixtures/hypr/clients.json].

Ids are minted from `address` with `backend.mint_id()` -- the KWin scheme, `0x40000000 | 30 bits` -- so they
are stable for the life of a window and identical in two processes reading the same session, and they sit
outside the range Xwayland hands out ((client << 21) | serial) so a minted id is never read as an X id in the
same listing. `stableId` is not the key: it is a decimal string on 0.56 [M recon2/arch.md] and does not exist
on 0.53 (it is absent from every row of the recorded fixture).

What Hyprland does not do yet is refused by name, with the reason and the rung of AGENTS.md's ladder that
would close it, which is the whole point of the backend replacing the floor: a refusal a script can read
beats a success that did not happen."""

import os

from w11common import session
from w11common.errors import CmdError
from wdotool.backend import View, Window, WindowBackend, Workspace, mint_id, mint_map, warn
from wdotool.ctx import SoftCmdError
from wdotool.hypr_ipc import HyprIPC
from wdotool.xid_match import match_xids

#: `fullscreen` in `hyprctl -j clients`: 0 none, 1 maximize, 2 fullscreen.
#: Measured: `windowstate --add FULLSCREEN` took the field from 0 to 2, with
#: the window at 0,0 sized to the whole output [M recon2/hyprland.md §3].
FS_NONE = 0
FS_MAXIMIZED = 1
FS_FULLSCREEN = 2

#: `dispatch fullscreen <n>`: 0 is full, 1 is "maximize" (both axes). Both are
#: toggles, which is why every set_state below reads the current value first
#: and sends nothing when it already holds.
DISPATCH_FULLSCREEN = 0
DISPATCH_MAXIMIZE = 1


class HyprBackend(WindowBackend):
    name = "hypr"
    #: What `wwmctl -m` prints on a session with no X plane. Hyprland's own X check window says `Hyprland :D`
    #: [M recon2/hyprland.md §3]; this is the answer for the no-Xwayland case, where there is no check window
    #: to read and the compositor's name is all there is to say.
    wm_name = "Hyprland"

    #: sway's wording, and sway's reason: Hyprland's IPC has no interactive picker and nothing that grabs a
    #: button press from outside the compositor, so there is nothing to click *with*. The next
    #: `activewindowv2` is the answer, which means focusing the window that already has focus does not end
    #: the wait. xdotool's click-to-pick is not yet here: the routes are evdev, which sees the press without
    #: the compositor's help (AGENTS.md route 4, at the cost of read access to /dev/input), plus something
    #: that says which window is under the pointer -- `j/cursorpos` gives the point and `j/clients` the
    #: rectangles, so route 2 finishes it.
    select_window_hint = "focus the target window to select it"

    def __init__(self, sockpath: "str | None" = None, ipc: "HyprIPC | None" = None):
        self.ipc = ipc if ipc is not None else HyprIPC(sockpath)
        #: {minted id: address} for the last listing, so an id the caller is holding still names a window
        self._addrs: "dict[int, str]" = {}
        #: the X connection, opened at most once per process ("unset" = not tried yet, None = no X plane)
        self._x = "unset"

    @property
    def sockpath(self) -> str:
        return self.ipc.sockpath

    # -- the client list ----------------------------------------------------

    def _rows(self) -> "list[dict]":
        """Every mapped client, bottom of the stack first, each row carrying its minted `_wid`.

        Hyprland publishes no stacking order. `focusHistoryID` is the nearest thing it has -- 0 is the window
        with focus, and the number grows with staleness -- so the list is that order reversed, because
        `backend.hit_test()` reads list() as bottom-to-top and takes the last hit as the topmost. (Reversed is
        also the order `hyprctl -j clients` itself printed the recorded session in.)"""
        rows = [r for r in self.ipc.json("clients") or [] if r.get("mapped")]
        rows.sort(key=lambda r: -int(r.get("focusHistoryID") or 0))
        ids = mint_map([str(r.get("address") or "") for r in rows])
        self._addrs = {}
        for r in rows:
            r["_wid"] = ids.get(str(r.get("address") or ""), 0)
            self._addrs[r["_wid"]] = str(r.get("address") or "")
        return rows

    def _row(self, wid: int) -> dict:
        for r in self._rows():
            if r["_wid"] == wid:
                return r
        raise CmdError("window %d not found" % wid)

    @staticmethod
    def _addr(row: dict) -> str:
        return "address:%s" % row.get("address")

    def _active_address(self) -> str:
        """`j/activewindow.address`, or "" when nothing is focused."""
        cur = self.ipc.json("activewindow")
        if not isinstance(cur, dict):
            return ""
        return str(cur.get("address") or "")

    def _visible_workspaces(self) -> "set[int]":
        """Workspace ids that are on screen: one per enabled monitor, its `activeWorkspace`."""
        out = set()
        for m in self.ipc.json("monitors") or []:
            if m.get("disabled"):
                continue
            ws = m.get("activeWorkspace") or {}
            if ws.get("id") is not None:
                out.add(int(ws["id"]))
        return out

    @staticmethod
    def _desktop_of(row: dict) -> int:
        """wmctrl's 0-based desktop from Hyprland's workspace id.

        Workspace ids are 1-based and dense, so desktop N is workspace N+1 -- the sway mapping. Special
        workspaces (the scratchpad family) carry NEGATIVE ids and are on no numbered desktop at all, which is
        -1, the same answer a named sway workspace gets."""
        wsid = int(((row.get("workspace") or {}).get("id")) or 0)
        return wsid - 1 if wsid > 0 else -1

    def _window(self, row: dict, focused_addr: str, visible_ws: "set[int]") -> Window:
        at = row.get("at") or [0, 0]
        size = row.get("size") or [0, 0]
        wsid = int(((row.get("workspace") or {}).get("id")) or 0)
        return Window(
            id=row["_wid"],
            title=row.get("title") or "",
            class_=row.get("class") or "",
            # `initialClass` is the app id the client started with; for an XWayland client it is the WM_CLASS
            # instance, which is what `search --classname` matches on
            instance=row.get("initialClass") or "",
            pid=int(row.get("pid") or 0),
            x=int(at[0]), y=int(at[1]),
            w=int(size[0]), h=int(size[1]),
            focused=bool(focused_addr) and row.get("address") == focused_addr,
            visible=not row.get("hidden") and wsid in visible_ws,
            desktop=self._desktop_of(row),
        )

    def list(self) -> "list[Window]":
        rows = self._rows()
        focused = self._active_address()
        visible = self._visible_workspaces()
        return [self._window(r, focused, visible) for r in rows]

    # -- window actions -----------------------------------------------------

    def activate(self, wid: int):
        self.ipc.dispatch("focuswindow %s" % self._addr(self._row(wid)))

    def close(self, wid: int):
        self.ipc.dispatch("closewindow %s" % self._addr(self._row(wid)))

    @staticmethod
    def _refuse_tiled(row: dict, what: str, how: str):
        """A tiled window ignores an absolute move or resize until it is floated -- measured: the dispatcher
        answers `ok` and `hyprctl clients` shows the window where it was [M recon2/arch.md, Hyprland IPC].
        Refuse it up front, the way the sway backend refuses the same thing, so `--sync` does not spin on a
        move that will never happen."""
        if not row.get("floating"):
            raise SoftCmdError("hypr: cannot %s a tiled window to an absolute %s "
                               "(setfloating it first)" % (what, how))

    def move_window(self, wid: int, x: int, y: int):
        row = self._row(wid)
        self._refuse_tiled(row, "move", "position")
        self.ipc.dispatch("movewindowpixel exact %d %d,%s" % (x, y, self._addr(row)))

    #: hyprctl --batch's own prefix on the wire, and `;` between the requests.
    BATCH = "[[BATCH]]"
    #: What Hyprland puts BETWEEN the answers of a batch: one reply per request, joined by three newlines.
    #: Measured on the arch-hypr golden (0.56.2, 2026-09-11) over a raw socket: two dispatches that took
    #: answered `b'ok\n\n\nok'`, and a batch whose second verb does not exist answered
    #: `b'ok\n\n\nInvalid dispatcher'`. Not `okok`, which is what this code first assumed and what the
    #: live run corrected.
    BATCH_SEP = "\n\n\n"

    def _batch(self, dispatches: "list[str]") -> str:
        """Several dispatches in ONE round trip, insisting on `ok` for every one of them.

        `HyprIPC.dispatch` is a connection per verb, and two connections are two moments: Hyprland re-lays
        the floating window between them, which is exactly what `resize` below must not allow.

        Hyprland answers a batch whatever happened -- the refusal of one verb does not stop the others --
        so the first answer that is not `ok` is the one relayed, in its own words, the way a single
        dispatch's is."""
        text = self.BATCH + ";".join("dispatch " + d for d in dispatches)
        reply = self.ipc.request(text).decode("utf-8", "replace")
        parts = [p.strip() for p in reply.split(self.BATCH_SEP)]
        bad = [p for p in parts if p != "ok"]
        if bad or len(parts) != len(dispatches):
            raise CmdError("hypr: %s" % (bad[0] if bad else reply.strip()))
        return reply

    def resize(self, wid: int, w: int, h: int):
        """`resizewindowpixel exact`, and then the origin again, in one batch.

        Hyprland 0.56.2 resizes a floating window about its CENTRE, so the size moves the window: measured
        with `hyprctl` alone on a fresh 0.56.2 session, a window at 100,100 800x600 sent
        `resizewindowpixel exact 400 300` landed at 300,250 400x300 -- both rectangles have the centre
        (500,400) [M goal2/recon/flavors.md 3a]. The smoke's move-then-size therefore came out at
        660,340 800x600 from 100,100 1920x1080 (100+560, 100+240), the bytes CI printed twice
        (`hypr.sh` windows phase, runs 34372382621 and 103360601210). 0.53.3 did not do it.

        X is the oracle and unambiguous: `xdotool windowsize` changes width and height and never the
        origin. So the position `row["at"]` already carries -- fetched here for `_refuse_tiled` -- is
        re-issued after the resize, both dispatches in one `--batch` so no repaint sits between them
        (measured: two separate dispatches also land, the batch lands 100,100 800x600 in one round
        trip)."""
        row = self._row(wid)
        self._refuse_tiled(row, "resize", "size")
        addr = self._addr(row)
        at = self._origin(row)
        if at is None:
            # No origin to put back: the resize goes out ALONE rather than with a `movewindowpixel exact
            # 0 0` after it. Every `j/clients` row measured here carries a two-number `at` (the recorded
            # fixture's four rows, and the live arch-hypr golden), so this is not a case seen on the wire
            # -- but a move to 0,0 is the one thing this method exists to prevent, and guessing it from a
            # row we could not read would be doing it on purpose.
            self.ipc.dispatch("resizewindowpixel exact %d %d,%s" % (w, h, addr))
            return
        self._batch(["resizewindowpixel exact %d %d,%s" % (w, h, addr),
                     "movewindowpixel exact %d %d,%s" % (at[0], at[1], addr)])

    @staticmethod
    def _origin(row: dict) -> "tuple[int, int] | None":
        """`row["at"]` as two ints, or None when the row does not carry one.

        None is not 0,0 here: the caller sends nothing rather than a move to the corner."""
        at = row.get("at")
        if not isinstance(at, list) or len(at) != 2:
            return None
        try:
            return int(at[0]), int(at[1])
        except (TypeError, ValueError):
            return None

    def _no(self, op: str, why: str, route: str):
        """A capability gap with its reason and its route attached. `_unsupported()` names the operation and
        the backend; on Hyprland the interesting half is *why*, because the wlr floor answered these
        silently -- and after the why, what would close the gap, which is what AGENTS.md asks a refusal for.

        The parenthesis stays closed around the reason alone: vm/live-smoke.d/hypr.sh:169 greps for
        `(Hyprland has no minimize)` contiguous, and the route goes after it."""
        err = CmdError("%s is not supported by the hypr backend (%s); %s" % (op, why, route))
        err.unsupported = True
        raise err

    #: The reason the three minimize-shaped gaps share.
    NO_MINIMIZE = "Hyprland has no minimize"
    #: And the rung that would close it. A special workspace is a stash, not a minimize: the window keeps
    #: its size and stays in `j/clients`, so the bookkeeping to bring it back is ours to write. The verb is
    #: the `movetoworkspacesilent special:<name>` that `set_desktop_for_window` below already sends for a
    #: numbered move; no report has run it against a stash and back, which is the rest of the cost [R].
    MINIMIZE_ROUTE = ("not yet here, and the route is Hyprland's own IPC (AGENTS.md route 2), a special "
                      "workspace to stash the window in, at the cost of the bookkeeping that brings it "
                      "back and a listing that keeps showing it while it is stashed")
    #: What `j/clients` does and does not carry. Only what was measured: the rows have `focusHistoryID` and
    #: nothing that orders them front to back [M recon2/hyprland.md §2, the recorded fixture's keys].
    NO_STACKING = "Hyprland publishes no stacking order in j/clients"
    #: The dispatcher that would be the lower is already in the IPC and has been run: `dispatch alterzorder
    #: top,address:0x...` on a floating window answers `ok` [M requests-batch-5.md, "Read by batch 20", item
    #: 8]. What is missing is not the run, it is the read-back -- every other verb here verifies itself
    #: against `j/clients` and this one cannot -- which is why it is a route with a cost and not a patch.
    STACKING_ROUTE = ("not yet here, and the route is `alterzorder bottom` over the IPC we already speak "
                      "(AGENTS.md route 2), measured to answer ok on a floating window; the cost is "
                      "sending it unverified, because j/clients publishes no order to read a lower back "
                      "from")
    #: Everything else about a window state. Hyprland's dispatcher list is the whole surface, so a state it
    #: has no dispatcher for is Hyprland's own code away.
    STATE_ROUTE = "not yet here, and the route is a patched Hyprland (AGENTS.md route 6), one dispatcher each"

    def minimize(self, wid: int):
        self._row(wid)
        self._no("windowminimize", self.NO_MINIMIZE, self.MINIMIZE_ROUTE)

    def unmap(self, wid: int):
        self._row(wid)
        self._no("windowunmap", self.NO_MINIMIZE, self.MINIMIZE_ROUTE)

    def map(self, wid: int):
        self._row(wid)
        self._no("windowmap", self.NO_MINIMIZE + ", so there is nothing to restore", self.MINIMIZE_ROUTE)

    def raise_(self, wid: int):
        """Focus, for a floating window; sway's warning for a tiled one -- the shape the sway backend has
        (wdotool/backend_sway.py:391) and the one the plan asks for here.

        Focusing is as close to a raise as this gets on a floating window, and it is what was measured.
        `alterzorder top,address:...` answers ok [M requests-batch-5.md, "Read by batch 20", item 8] and
        changes nothing anyone can read back, so it is not an improvement on a verb that demonstrably
        focuses; a tiled window sits in a layout with no z at all, so it gets sway's warning and its rung."""
        row = self._row(wid)
        if row.get("floating"):
            self.ipc.dispatch("focuswindow %s" % self._addr(row))
        else:
            warn("windowraise: tiled Hyprland windows have no stacking order; not yet, and the route is a "
                 "patched Hyprland (AGENTS.md route 6), a restack its layout has no word for today; "
                 "ignoring")

    def lower(self, wid: int):
        self._row(wid)
        self._no("windowlower", self.NO_STACKING, self.STACKING_ROUTE)

    def maximize_pair_state(self) -> "str | None":
        """`dispatch fullscreen 1` takes both axes at once and there is no per-axis dispatcher, so the pair is
        folded into one call and a lone axis is refused rather than half-applied."""
        return "MAXIMIZED"

    def set_state(self, wid: int, state: str, action: int):
        row = self._row(wid)
        if state == "FULLSCREEN":
            self._toggle_fullscreen(row, action, FS_FULLSCREEN, DISPATCH_FULLSCREEN)
        elif state == "MAXIMIZED":
            self._toggle_fullscreen(row, action, FS_MAXIMIZED, DISPATCH_MAXIMIZE)
        elif state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            self._no("windowstate %s" % state,
                     "Hyprland maximizes both axes at once; ask for both", self.STATE_ROUTE)
        elif state == "STICKY":
            if not row.get("floating"):
                raise CmdError("hypr: only floating windows can be pinned (setfloating it first)")
            on = action == 1 or (action == 2 and not row.get("pinned"))
            if on != bool(row.get("pinned")):
                self.ipc.dispatch("pin %s" % self._addr(row))
        else:
            self._no("windowstate %s" % state, "Hyprland has no such window state",
                     self.STATE_ROUTE)
        return None

    #: which `dispatch fullscreen <n>` clears each state
    _CLEAR = {FS_FULLSCREEN: DISPATCH_FULLSCREEN, FS_MAXIMIZED: DISPATCH_MAXIMIZE}

    def _toggle_fullscreen(self, row: dict, action: int, want_value: int, mode: int):
        """`dispatch fullscreen <mode>` is a toggle and takes no address at all -- it acts on whatever has
        focus -- so the window is focused first and the current `fullscreen` value decides whether anything is
        sent. Sending a toggle for a state that already holds turns it off, which is how a `--add` that
        arrived twice would undo itself; and going straight from maximized to fullscreen needs the standing
        one cleared first, because one toggle would only clear it."""
        now = int(row.get("fullscreen") or FS_NONE)
        on = action == 1 or (action == 2 and now != want_value)
        target = want_value if on else FS_NONE
        if now == target:
            return
        self.ipc.dispatch("focuswindow %s" % self._addr(row))
        if now != FS_NONE:
            self.ipc.dispatch("fullscreen %d" % self._CLEAR[now])
        if target != FS_NONE:
            self.ipc.dispatch("fullscreen %d" % mode)

    # -- desktops -----------------------------------------------------------

    def _active_workspace_id(self) -> int:
        """`j/activeworkspace.id`: the ONE workspace that has focus, 0 when the compositor will not say.

        Not the set `_visible_workspaces()` builds. On the recorded two-head session workspace 1 is on
        Virtual-1 and workspace 2 on HEADLESS-2, so two workspaces are *on screen* and exactly one is
        *current* -- and `Workspace.active` is the current one, because wwmctl/core.py takes the first active
        row as the current desktop and prints `*` on every active row (sway's backend fills it from the
        node's `focused` for the same reason)."""
        cur = self.ipc.json("activeworkspace")
        return int((cur or {}).get("id") or 0) if isinstance(cur, dict) else 0

    def get_desktop(self) -> int:
        wsid = self._active_workspace_id()
        return wsid - 1 if wsid > 0 else -1

    def set_desktop(self, n: int):
        self.ipc.dispatch("workspace %d" % (int(n) + 1))

    def num_desktops(self) -> int:
        return len(self.ipc.json("workspaces") or [])

    def set_window_desktop(self, wid: int, n: int):
        row = self._row(wid)
        # `movetoworkspacesilent` and not `movetoworkspace`: wmctrl -r -t moves the window and leaves the
        # focus where it is, and the loud dispatcher follows the window to the other workspace.
        self.ipc.dispatch("movetoworkspacesilent %d,%s" % (int(n) + 1, self._addr(row)))

    def workspaces(self) -> "list[Workspace]":
        """One record per workspace, ascending by id, with the work area of the monitor it lives on.

        Hyprland's `reserved` is the monitor's [left, top, right, bottom] gap for layer-shell panels, which is
        exactly what `_NET_WORKAREA` means, so the work area is the monitor rectangle minus it."""
        mons = {}
        for m in self.ipc.json("monitors") or []:
            mons[str(m.get("name") or "")] = m
        active = self._active_workspace_id()
        out = []
        for ws in sorted(self.ipc.json("workspaces") or [],
                         key=lambda w: int(w.get("id") or 0)):
            wsid = int(ws.get("id") or 0)
            m = mons.get(str(ws.get("monitor") or ""))
            out.append(Workspace(
                index=wsid - 1 if wsid > 0 else -1,
                name=str(ws.get("name") or ""),
                active=wsid == active,
                work_area=self._work_area(m),
            ))
        return out

    @staticmethod
    def _logical_box(m: "dict | None") -> tuple:
        """One `j/monitors` row as its layout rectangle (x, y, w, h), in logical pixels.

        `x`/`y` are already logical. `width`/`height` are the MODE's pixels, so a rotated head has them the
        wrong way round: `--rotate left` left the mode at 1920x1080 and put `transform: 3` in
        `hyprctl monitors`, and the layout box is 1080x1920 [M recon2/hyprland.md §4]. The wl_output enum's
        odd values (1, 3, 5, 7) are the quarter turns -- the same `% 2` test wdotool/layoutbox.py:85 and the
        daemon's output tracker make, and what core.transform_swaps() says on the wxrandr side. Then divide by
        `scale` and truncate, as wlroots does: the recorded second head is a 1920x1080 HEADLESS at scale 2.0,
        which is 960x540 of layout [M recon2/hyprland.md fixtures].

        One helper for both readers, because `wdotool getdisplaygeometry` and `wwmctl -d`'s work area have to
        agree with each other and with `wxrandr --query` over the same rows."""
        if not m:
            return (0, 0, 0, 0)
        w, h = int(m.get("width") or 0), int(m.get("height") or 0)
        if int(m.get("transform") or 0) % 2:
            w, h = h, w
        scale = float(m.get("scale") or 1.0) or 1.0
        return (int(m.get("x") or 0), int(m.get("y") or 0), int(w / scale), int(h / scale))

    @classmethod
    def _work_area(cls, m: "dict | None") -> tuple:
        if not m:
            return (0, 0, 0, 0)
        res = list(m.get("reserved") or [0, 0, 0, 0]) + [0, 0, 0, 0]
        x, y, w, h = cls._logical_box(m)
        return (x + int(res[0]), y + int(res[1]),
                w - int(res[0]) - int(res[2]), h - int(res[1]) - int(res[3]))

    # -- the rest of the contract -------------------------------------------

    def display_size(self) -> tuple:
        """The bounding box of the whole layout, in logical pixels: `_logical_box()` over every enabled head,
        so a rotated one contributes 1080x1920 and not the mode's 1920x1080."""
        boxes = [self._logical_box(m) for m in self.ipc.json("monitors") or [] if not m.get("disabled")]
        if not boxes:
            raise CmdError("hypr backend: no enabled monitors")
        minx = min(x for x, _y, _w, _h in boxes)
        miny = min(y for _x, y, _w, _h in boxes)
        w = max(x + w for x, _y, w, _h in boxes) - minx
        h = max(y + h for _x, y, _w, h in boxes) - miny
        if not w or not h:
            raise CmdError("hypr backend: no enabled monitors")
        return w, h

    def pointer(self) -> "tuple[int, int] | None":
        """The compositor's real pointer, from `j/cursorpos`. `hyprctl cursorpos` answered `640, 360` exactly
        after a `mousemove 640 360`, twice [M recon2/hyprland.md §3]."""
        try:
            d = self.ipc.json("cursorpos")
        except CmdError:
            return None
        if not isinstance(d, dict) or "x" not in d or "y" not in d:
            return None
        return int(d["x"]), int(d["y"])

    #: Hyprland's event names, mapped onto the vocabulary sway's backend
    #: publishes, which is the one wxprop -spy and select_window read
    #: [M recon2/hyprland.md §2 recorded five of these lines].
    EVENT_NAMES = {
        "openwindow": "new",
        "closewindow": "close",
        "activewindowv2": "focus",
        "windowtitlev2": "title",
        "movewindowv2": "move",
    }

    def events(self, timeout: "float | None" = None, workspaces: bool = False):
        """(window id, change) for every Hyprland window event, in sway's words.

        The payload's first field is the address with no `0x` (`activewindowv2>>59daae69e360`), so it is
        minted the same way the listing is and the ids agree. `fullscreen>>` carries only the new state and no
        address at all -- it is always about the focused window -- so that one costs a `j/activewindow`; every
        other event is free. With `workspaces` the workspace stream folds in as (0, "workspace"), as the sway
        and KWin backends do, so a root-level watcher needs one stream only."""
        for name, payload in self.ipc.events(timeout):
            if name == "workspacev2":
                if workspaces:
                    yield 0, "workspace"
                continue
            if name == "fullscreen":
                addr = self._active_address()
                if addr:
                    yield self._mint(addr), "fullscreen_mode"
                continue
            change = self.EVENT_NAMES.get(name)
            if change is None:
                continue
            field = payload.split(",", 1)[0].strip()
            if field:
                yield self._mint(field), change

    @staticmethod
    def _mint(addr: str) -> int:
        """The id of the window at `addr`, from the event stream's spelling (no `0x`) or the listing's.

        `mint_id` and not `mint_map`: an event carries one address and no list to re-mint a collision against,
        so the ~1e-6 pair that a listing would have separated is reported here under the id the first of them
        holds. A stream of ids is a hint (something changed about this window); the listing is the truth."""
        return mint_id(addr if addr.startswith("0x") else "0x" + addr)

    def select_window(self) -> int:
        for wid, change in self.events():
            if change == "focus":
                return wid
        raise CmdError("hypr backend: the window event stream ended")

    # -- XWayland ids -------------------------------------------------------

    def views(self) -> "list[View]":
        """list() with the extras wwmctl and wxprop print, X ids included.

        `hyprctl -j clients` says `xwayland: true` and gives the pid and the exact rectangle but no X id, so
        the id comes from `_NET_CLIENT_LIST` through the shared matcher -- the same pairing KWin 6 needs, with
        more evidence than KWin gives it: the recorded xmessage row carries pid 11920, class Xmessage and
        at/size 300,200 400x300, which is three filters and a distance [M recon2/hyprland.md §2, §3]."""
        rows = self._rows()
        focused = self._active_address()
        visible = self._visible_workspaces()
        xinfo = self._xinfo(rows)
        names = {}
        for r in rows:
            ws = r.get("workspace") or {}
            names[r["_wid"]] = str(ws.get("name") or "")
        out = []
        for r in rows:
            win = self._window(r, focused, visible)
            xid, xinst, xcls = xinfo.get(str(r.get("address") or ""), (0, "", ""))
            x11 = bool(r.get("xwayland"))
            # WM_CLASS as the X server holds it wins for a matched XWayland client: Hyprland's `class` is the
            # WM_CLASS *class* and `initialClass` is what the window started as, neither of which is the
            # instance an `xterm` needs to print as `xterm.XTerm` [M recon2/hyprland.md §3].
            cls = xcls or r.get("class") or ""
            inst = xinst or r.get("initialClass") or cls
            fs = int(r.get("fullscreen") or FS_NONE)
            out.append(View(
                window=win, xid=xid,
                instance=inst, cls=cls,
                app_id="" if x11 else cls,
                fullscreen=fs == FS_FULLSCREEN,
                maximized_h=fs == FS_MAXIMIZED,
                maximized_v=fs == FS_MAXIMIZED,
                sticky=bool(r.get("pinned")),
                floating=bool(r.get("floating")),
                ws_name=names[r["_wid"]],
                client_type="x11" if x11 else "wayland",
                # `or -1` would read monitor 0 -- every row of the recorded session -- as "unknown"
                monitor=r["monitor"] if isinstance(r.get("monitor"), int) else -1,
            ))
        return out

    def _xinfo(self, rows: "list[dict]") -> "dict[str, tuple]":
        """{address: (X11 window id, WM_CLASS instance, WM_CLASS class)} for the XWayland rows.

        Empty for every row when Xwayland is not running, when the X plane cannot be read, or when the matcher
        could not tell two windows apart -- an unknown id beats a wrong one, and that rule lives in
        wdotool/xid_match.py."""
        want = [r for r in rows if r.get("xwayland")]
        if not want:
            return {}
        x = self._x11()
        if x is None:
            return {}
        try:
            clients = self._x_clients(x)
        except Exception:  # any X failure: no ids, no crash
            self._x = None
            return {}
        raw = []
        for i, r in enumerate(want):
            at = r.get("at") or [0, 0]
            size = r.get("size") or [0, 0]
            raw.append({"u": str(r.get("address") or ""), "p": int(r.get("pid") or 0),
                        "c": r.get("class") or "", "n": "", "t": r.get("title") or "",
                        "x": int(at[0]), "y": int(at[1]),
                        "w": int(size[0]), "h": int(size[1]), "ix": i})
        # ratio 1.0: Hyprland reports a window's rectangle in the same layout pixels the X root is sized in on
        # every unscaled head, and no report measured a scaled Xwayland here.
        by_xid = {c["xid"]: c for c in clients}
        out = {}
        for addr, xid in match_xids(raw, clients, 1.0).items():
            c = by_xid.get(xid) or {}
            out[addr] = (xid, c.get("inst") or "", c.get("cls") or "")
        return out

    def _x11(self):
        """The session's X connection, or None. Nothing is opened unless Xwayland is already running:
        connecting to the socket would start it."""
        if self._x != "unset":
            return self._x
        self._x = None
        if not session.xwayland_running():
            return None
        display = session.find_x_display() or os.environ.get("DISPLAY") or None
        xauth = session.find_xauthority() or None
        try:
            from wdotool import x11_mini
            self._x = x11_mini.X11Conn(display, xauthority=xauth)
        except Exception:  # no X plane: xid stays 0
            self._x = None
        return self._x

    @staticmethod
    def _x_clients(x) -> "list[dict]":
        """The X clients in `_NET_CLIENT_LIST` order, which the matcher reads as an order and not just a set."""
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
