"""The registry: one entry per compositor toplevel, and the X ids it wears.

A **shadow** is an X window id the proxy invents for a native Wayland toplevel
that has no Xwayland window. It lives only in the proxy's replies and events --
upstream never hears it, because every request naming one is answered or
consumed here -- which is why it cannot collide and why an upstream error for it
is impossible (design section 4.1).

Three rules decide the whole file:

* **the id comes from the setup reply.** `rid_base | n`, minted through
  `x11_mini._new_rid`, out of the 21-bit range the server allocated exclusively
  to the proxy's own connection [recon/wire.md 7.1]. Not the tree's
  `mint_id`'s `0x40000000 | 30 bits` (backend.py:44-56), which is a guess about
  the server's allocator; the setup reply is the server saying it.
* **`View.xid` is trusted and no matching is repeated.** sway's tree node
  carries `"window"` (measured `window=4194316` on a live xterm), the GNOME
  bridge's `views()` carries `xid`, Cinnamon has `get_xwindow()`, and KWin,
  Hyprland, Wayfire, wlr and COSMIC have run `match_xids` before the proxy sees
  the answer [recon/seams.md 3]. An entry with an `xid` is real and gets no
  shadow.
* **the diff is the truth.** The pump's tokens are a hint about *when* to look;
  what a client is told changed is what two snapshots differ by (design
  section 5.2). A `move` token is "re-list and diff geometry" and nothing more
  [recon/seams.md 2.4].

The freshness rule is 20 ms (design section 4.8): a `search` walk of five
windows is served from one list, and a read 75 ms behind a write -- which is
what sway's own latency for `exec foot` was, `new`/`title`/`focus` all arriving
+75 ms [recon/seams.md 2.3] -- is what two back-to-back invocations would see
with no re-list at all. `list()` costs 0.08 ms on sway [recon/seams.md 2.3].
"""

import dataclasses
import struct
import time

from w11common.errors import CmdError
from wdotool import backend as backend_mod
from wdotool import backend_detect
from wdotool import x11_mini
from wdotool.ctx import NoSessionError
from xw11 import policy
from xw11 import upstream as upstream_mod

#: How long a snapshot stands before the next read re-lists (design section 4.8).
TTL = 0.020

#: What `entry.overlay[atom]` holds for a property a client deleted: X's own
#: "this name is absent", which is not the same as "this name was never
#: synthesized" -- the synthesis would put it straight back.
TOMBSTONE = object()

#: `ChangeProperty`'s three modes, at byte 1 of the request [recon/wire.md 4.2].
REPLACE, PREPEND, APPEND = 0, 1, 2

#: `PropertyNotify`'s `state` byte [recon/wire.md 4.4], which is what the
#: `on_property` hook below carries.
PROP_NEW_VALUE, PROP_DELETED = 0, 1

#: The kinds of change a refresh reports. Batch 5 turns each into the events of
#: design section 5.3; here they are computed, tested and handed out.
NEW = "new"
GONE = "gone"
GEOMETRY = "geometry"
TITLE = "title"
FOCUS = "focus"
VISIBLE = "visible"
PROPS = "props"
ORDER = "order"
DESKTOP = "desktop"


@dataclasses.dataclass
class Entry:
    """One compositor toplevel, as the proxy's X side sees it."""

    handle: int = 0                 # the backend's own Window.id
    xid: int = 0                    # the real X id, 0 for a native toplevel
    shadow: int = 0                 # the id we minted, 0 for a real window
    window: object = None           # backend.Window
    view: object = None             # backend.View, or None
    props: dict = dataclasses.field(default_factory=dict)
    overlay: dict = dataclasses.field(default_factory=dict)
    #: the `_NET_WM_STATE` bits a view-less backend carries on its tree node
    #: instead of on a `View` -- `{"fullscreen": bool, "sticky": bool}` on sway
    #: and i3, empty everywhere else. `_states` reads it when `view is None`.
    flags: dict = dataclasses.field(default_factory=dict)
    seen: int = 0                   # the list generation this was last in
    dead: bool = False

    @property
    def id(self) -> int:
        """What a client is shown: the real X id where there is one, else the
        shadow. Never the backend's handle -- a handle is a KWin uuid hash or a
        Hyprland pointer and means nothing to an X client."""
        return self.xid or self.shadow


@dataclasses.dataclass
class Change:
    """One difference between two snapshots. `names` carries the backend field
    names that moved for a PROPS change, which is what batch 5 maps onto
    property atoms through wxprop's own tables (wxprop/core.py:1031-1070)."""

    kind: str
    handle: int = 0
    entry: Entry = None
    names: tuple = ()


#: The `Window` fields a change in which is a property change and not one of the
#: kinds above. `desktop` is here as well as in the root's own pair: a window
#: that moved workspace publishes `_NET_WM_DESKTOP` and `_NET_WM_STATE`
#: [recon/seams.md 4's `_NATIVE_EVENT_PROPS["move"]`].
_WINDOW_PROPS = ("class_", "instance", "pid", "desktop", "window_type")

#: The tree-node fields a view-less backend (sway, i3) carries instead of a
#: `View`: the two `_NET_WM_STATE` bits design section 4.4 does not bracket as
#: rich. Named the way `_STATE_BITS` names them, so `_states` reads one key.
_NODE_PROPS = ("fullscreen", "sticky")


def node_flags(node) -> dict:
    """`{"fullscreen": bool, "sticky": bool}` off a sway/i3 tree node.

    `fullscreen_mode` is sway's own key and it is an INT (0 none, 1 output,
    2 global), which is why this is a `bool()` and not the value: `wxprop`
    reads the same two keys the same way (wxprop/core.py:544, 554) and the
    parity target is the clone's bytes."""
    return {"fullscreen": bool(node.get("fullscreen_mode")),
            "sticky": bool(node.get("sticky"))}


#: The `View` fields that are `_NET_WM_STATE` bits or the WM_CLASS pair.
_VIEW_PROPS = ("cls", "instance", "app_id", "fullscreen", "maximized_h",
               "maximized_v", "above", "below", "sticky", "urgent",
               "minimized", "hidden", "skip_taskbar", "skip_pager",
               "window_type", "transient_for", "ws_name")


def detect_backend(log=None):
    """One backend for the whole proxy, or None when there is no session.

    `set_program("xw11")` first so every `backend.warn()` line carries the name
    of the thing that made it (backend.py:26-33). No session is not a refusal
    here: the proxy still forwards every byte, and the client talks to Xwayland
    exactly as it would have without us -- one log line says the shadow side is
    off (design section 4.1)."""
    backend_mod.set_program("xw11")
    try:
        return backend_detect.detect()
    except NoSessionError as e:
        if log is not None:
            log("no Wayland session for the shadow registry (%s): every "
                "request passes through untouched, and the route back is a "
                "session to detect -- this proxy keeps serving X either way"
                % e)
        return None


class Shadows:
    """The registry, its ids, its freshness and its diff."""

    def __init__(self, own, backend, log=None, block=None, ttl=TTL,
                 on_backend=None):
        self.own = own
        self.backend = backend
        self.ttl = ttl
        self._log = log
        self._block = block
        #: Told when a re-detect replaced the backend, because there is ONE
        #: backend in this process and the loop and the pump hold references to
        #: it too. Called with the lock held.
        self.on_backend = on_backend
        self.entries = {}               # handle -> Entry
        self.by_shadow = {}             # minted id -> Entry
        self.by_xid = {}                # real X id -> Entry
        self.order = []                 # handles, bottom to top, as list() gave them
        self.generation = 0
        self.current_desktop = -1
        self.num_desktops = 0
        self.exhausted = False
        self._stamp = 0.0
        self._dirty = True
        self._lists = 0                 # list()/views() calls made, for the tests
        self._warned = set()
        #: the root's synthesized set and the generation it was built from
        self._root_props = None
        self._root_gen = -1

    # -- the log --------------------------------------------------------------

    def say(self, text: str) -> None:
        if self._log is not None:
            self._log(text)

    def say_once(self, key: str, text: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.say(text)

    # -- ids ------------------------------------------------------------------

    def mint(self) -> int:
        """One shadow id, or 0 when the range is spent.

        Never reused while the proxy lives (design section 4.3): 2^21 ids is
        24 days at one window per second [recon/wire.md 7.1, arithmetic on the
        measured mask], and the proxy idles out fifteen minutes after its last
        client, so a free list would be bookkeeping for a case a session cannot
        reach. Exhaustion is `BadAlloc` on the request that needed the id, plus
        this one line (R7)."""
        if self.own is None or not self.own.open or self.exhausted:
            # `open` and not merely `own is not None`: `Server.close_own` (an
            # Xwayland restart) calls `rebase()`, and a read from a handler in
            # the same selector iteration -- a client whose own twin's EOF has
            # not been serviced yet -- reaches here with the socket already
            # shut. 0 is the shape that path already has for "not now".
            #
            # `exhausted` is latched: `_new_rid`'s check fires at the wrap and
            # then the counter walks on, so an allocator asked a fifth time
            # after a four-id range ran out would hand back the first id again
            # (x11_mini.py:552 raises only when `n & mask` is zero). An id that
            # named a window never names another one while this base lives, so
            # the first refusal is the last answer under it.
            return 0
        try:
            return self.own.new_xid()
        except upstream_mod.UpstreamGone:
            # The connection went between the guard and the call. Not
            # exhaustion -- nothing is latched -- and the next accept reopens
            # it and `rebase()` remints the lot.
            return 0
        except x11_mini.XUnavailable:
            self.exhausted = True
            self.say_once("exhausted",
                          "the proxy's 2^21 shadow ids are spent: the request "
                          "that needed a new one is answered BadAlloc. Reusing "
                          "the ids of windows that are gone is not yet done "
                          "here, and the route is a free list with a grace "
                          "period on this registry (AGENTS.md route 5, which "
                          "is this proxy), at the cost of an id that names two "
                          "windows in one script's lifetime -- 2^21 ids is 24 "
                          "days at one window a second [recon/wire.md 7.1]")
            return 0

    def rebase(self) -> None:
        """Every minted id forgotten, because the connection they were minted
        from is gone. Bases are handed out in connection order and reused --
        0x600000 then 0x400000 on two consecutive connections [recon/env.md 2.5]
        -- so a reopened `OwnConn` re-bases the whole registry, and an id a
        script saved before an Xwayland restart names nothing (design
        section 2.6's row)."""
        for entry in self.entries.values():
            entry.shadow = 0
        self.by_shadow.clear()
        # The latch belongs to the base that ran out, not to the registry: a
        # restarted Xwayland hands the next connection a whole 2^21 range, and
        # a proxy that kept answering 0 after one would never mint again.
        self.exhausted = False
        self._dirty = True

    def is_shadow(self, xid: int) -> bool:
        return xid in self.by_shadow

    def entry_for(self, xid: int):
        """The entry an X id names, shadow or real, or None."""
        return self.by_shadow.get(xid) or self.by_xid.get(xid)

    # -- freshness ------------------------------------------------------------

    def invalidate(self) -> None:
        """The pump saw something. The next read re-lists, whatever the clock
        says."""
        self._dirty = True

    def stale(self) -> bool:
        return self._dirty or (time.monotonic() - self._stamp) >= self.ttl

    def snapshot(self):
        """The entries, bottom to top, re-listed if the last read is older than
        the TTL or the pump has invalidated it."""
        if self.stale():
            self.refresh()
        return [self.entries[h] for h in self.order if h in self.entries]

    # -- the backend ----------------------------------------------------------

    def _call(self, name, *args, default=None):
        """One backend call, under the server's lock, with the two failures the
        backends actually raise handled the way the tree handles them:
        `NoSessionError` is a compositor that went away, and nothing in the tree
        re-detects on its own [recon/seams.md 2.2], so this does it once;
        `CmdError` is this compositor not doing that thing, which is a log line
        and a default, never an X error (design section 3.3)."""
        for attempt in (0, 1):
            fn = getattr(self.backend, name, None)
            if fn is None:
                return default
            try:
                if self._block is not None:
                    with self._block:
                        return fn(*args)
                return fn(*args)
            except NoSessionError as e:
                if attempt:
                    self.say("the compositor is still not there (%s): the "
                             "registry is empty and every request passes "
                             "through untouched" % e)
                    return default
                self.say("the compositor went away (%s): re-detecting" % e)
                new = self._redetect()
                if new is None:
                    return default
            except CmdError as e:
                self.say_once("cmderr:%s" % name,
                              "the %s backend cannot %s (%s): the read side "
                              "answers what it has" % (self.backend.name, name, e))
                return default
        return default

    def _redetect(self):
        """`reset()` and detect again, under the server's lock: `reset()`
        closes the cached bus, the cached `ListNames` and the registry
        connection (`backend_detect`), and the pump's thread may be inside a
        call on exactly those [recon/seams.md 2.2]. The answer goes to everyone
        holding a reference, not only to this registry -- there is one backend
        in this process."""
        if self._block is not None:
            with self._block:
                return self._redetect_locked()
        return self._redetect_locked()

    def _redetect_locked(self):
        backend_detect.reset()
        new = detect_backend(self._log)
        if new is None:
            return None
        self.backend = new
        if self.on_backend is not None:
            self.on_backend(new)
        return new

    def _read(self):
        """`views()` where the backend has one, else the tree, else `list()`.
        Returns `(windows, views, {handle: xid}, {handle: flags})`.

        Six of the eight backends have `views()`, and its `xid` is the pairing
        design section 4.2 trusts: the GNOME bridge and Cinnamon read it off
        the compositor, and KWin, Hyprland, Wayfire and the wlr floor have run
        `match_xids` before the proxy sees the answer [recon/seams.md 3].

        **sway and i3 have no `views()` at all** -- and they are the two that
        need none, because the tree node carries `"window"`, the X id itself
        (measured `window=4194316` on a live xterm [recon/seams.md 3]).
        `list()` drops that field on the floor (`backend_sway.py:312` maps
        `_nodes()` straight to its `Window`s), so the pairing is read from
        `_nodes()` here -- the same private seam `wxprop.core` already reads
        through `sess.nodes()`, and one GET_TREE rather than two, since
        `list()` is that walk with the nodes thrown away.

        The node carries a second thing no `Window` has: `fullscreen_mode` and
        `sticky`, the two `_NET_WM_STATE` bits design section 4.4 does NOT
        bracket as rich. `wxprop` reads them straight off the node
        (wxprop/core.py:544, 554), so a `foot` that sway has fullscreened
        prints `_NET_WM_STATE_FULLSCREEN` through the clone; without carrying
        them the proxy printed `_NET_WM_STATE(ATOM) =` for the same window
        [M 2026-09-10, headless sway on this box].

        Without this every Xwayland window on sway gets a shadow of its own and
        is listed TWICE: measured 2026-09-10 on the rig, where `wmctrl -lp`
        printed `0x00600001 WXL-Xterm` and `0x00600002 WXL-Foot` and the
        xterm's real 0x40000c was nowhere, and `search --class xterm` printed
        the real id and a shadow.
        """
        self._lists += 1
        views = self._call("views")
        if views:
            return [v.window for v in views], list(views), {}, {}
        nodes = self._tree_nodes()
        if nodes is not None:
            return ([win for _n, win, _f, _w in nodes], None,
                    {win.id: int(node.get("window") or 0)
                     for node, win, _f, _w in nodes},
                    {win.id: node_flags(node) for node, win, _f, _w in nodes})
        wins = self._call("list", default=None)
        return (list(wins) if wins else []), None, {}, {}

    def _tree_nodes(self):
        """`backend._nodes()` where the backend has one -- sway and i3, and
        nothing else in the tree -- else None.

        Keyed on the METHOD and not on `backend.name`, because that is the
        thing this needs, and because a backend that grows a `views()` never
        reaches this line at all: the caller tries `views()` first."""
        if not hasattr(self.backend, "_nodes"):
            return None
        return self._call("_nodes")

    # -- the diff -------------------------------------------------------------

    def refresh(self):
        """Re-read the compositor and answer with what moved.

        Unconditional: the TTL is `snapshot()`'s rule, and the pump calls this
        one straight (design section 5.2 -- one re-list answers every token in
        a drain)."""
        wins, views, xids, flags = self._read()
        self._stamp = time.monotonic()
        self._dirty = False
        self.generation += 1
        changes = []
        seen = []
        for i, win in enumerate(wins):
            view = views[i] if views is not None else None
            seen.append(win.id)
            entry = self.entries.get(win.id)
            if entry is None:
                entry = self._add(win, view, xids.get(win.id, 0),
                                  flags.get(win.id))
                changes.append(Change(NEW, entry.handle, entry))
                continue
            changes += self._update(entry, win, view, xids.get(win.id, 0),
                                    flags.get(win.id))
        changes += self._departed(seen)
        if seen != self.order:
            if set(seen) == set(self.order):
                changes.append(Change(ORDER))
            self.order = seen
        changes += self._desktops()
        return changes

    def _add(self, win, view, xid=0, flags=None) -> Entry:
        entry = Entry(handle=win.id, window=win, view=view,
                      xid=int(getattr(view, "xid", 0) or 0) or int(xid or 0),
                      flags=dict(flags or {}), seen=self.generation)
        if entry.xid:
            self.by_xid[entry.xid] = entry
        else:
            entry.shadow = self.mint()
            if entry.shadow:
                self.by_shadow[entry.shadow] = entry
        self.entries[entry.handle] = entry
        return entry

    def _update(self, entry, win, view, paired=0, flags=None):
        """One entry against its previous self, field by field. The order of the
        kinds here is the order of design section 5.3's table, so batch 5 can
        walk the list and write packets."""
        old_win, old_view, old_flags = entry.window, entry.view, entry.flags
        flags = dict(flags or {})
        changes = []
        xid = int(getattr(view, "xid", 0) or 0) or int(paired or 0)
        if xid != entry.xid:
            # A pairing that settled late -- a title arriving after the window
            # did, on the backends that match on title [recon/seams.md 3]. The
            # shadow is destroyed and the real id takes its place; the reverse
            # never happens, because an id the compositor once reported does not
            # become unknown again (design section 4.2).
            changes.append(Change(GONE, entry.handle, self._snapshot_of(entry)))
            self.by_shadow.pop(entry.shadow, None)
            self.by_xid.pop(entry.xid, None)
            entry.xid, entry.shadow = xid, 0
            if xid:
                self.by_xid[xid] = entry
            else:
                entry.shadow = self.mint()
                if entry.shadow:
                    self.by_shadow[entry.shadow] = entry
            entry.window, entry.view, entry.props = win, view, {}
            entry.flags, entry.seen = flags, self.generation
            changes.append(Change(NEW, entry.handle, entry))
            return changes
        if entry.shadow == 0 and entry.xid == 0:
            # A mint that failed while the range was spent; try again, because
            # a window that leaves frees nothing but a later one may arrive
            # after a rebase.
            entry.shadow = self.mint()
            if entry.shadow:
                self.by_shadow[entry.shadow] = entry
        entry.window, entry.view, entry.flags = win, view, flags
        entry.seen = self.generation
        if (old_win.x, old_win.y, old_win.w, old_win.h) != (win.x, win.y, win.w, win.h):
            changes.append(Change(GEOMETRY, entry.handle, entry))
        if old_win.title != win.title:
            changes.append(Change(TITLE, entry.handle, entry))
        if old_win.visible != win.visible:
            changes.append(Change(VISIBLE, entry.handle, entry))
        if old_win.focused != win.focused:
            changes.append(Change(FOCUS, entry.handle, entry))
        names = tuple(n for n in _WINDOW_PROPS
                      if getattr(old_win, n, None) != getattr(win, n, None))
        if view is not None and old_view is not None:
            names += tuple(n for n in _VIEW_PROPS
                           if getattr(old_view, n, None) != getattr(view, n, None))
        elif view is None:
            # The tree node's two state bits, which no `View` and no `Window`
            # carries: a sway `fullscreen enable` that does not move the rect
            # (a window already the size of the output) is otherwise a change
            # nothing here sees, and the cached `_NET_WM_STATE` would keep the
            # old answer.
            names += tuple(n for n in _NODE_PROPS
                           if bool(old_flags.get(n)) != bool(flags.get(n)))
        if names:
            changes.append(Change(PROPS, entry.handle, entry, names))
        if changes:
            # Rebuilt on ANY change and not only on a PROPS one (design section
            # 4.1): `_NET_WM_NAME` and `WM_NAME` are the title, `_NET_WM_STATE`
            # carries FOCUSED and HIDDEN and `WM_STATE` is the visibility, so a
            # cache kept across a rename would serve the old name. The 20 ms
            # TTL bounds what rebuilding costs.
            entry.props = {}
        return changes

    def _departed(self, seen):
        changes = []
        alive = set(seen)
        for handle in [h for h in self.entries if h not in alive]:
            entry = self.entries.pop(handle)
            entry.dead = True
            self.by_shadow.pop(entry.shadow, None)
            self.by_xid.pop(entry.xid, None)
            changes.append(Change(GONE, handle, entry))
        return changes

    def _desktops(self):
        """The root's own pair. `max(num_desktops(), get_desktop() + 1)` is
        wxprop's rule for a workspace sway does not count [recon/seams.md 4]."""
        cur = self._call("get_desktop", default=-1)
        num = self._call("num_desktops", default=0)
        cur = -1 if cur is None else int(cur)
        num = 0 if num is None else int(num)
        num = max(num, cur + 1)
        if (cur, num) == (self.current_desktop, self.num_desktops):
            return []
        self.current_desktop, self.num_desktops = cur, num
        return [Change(DESKTOP)]

    @staticmethod
    def _snapshot_of(entry) -> Entry:
        """A copy of an entry as it was, for a change that reports what it
        stopped being. The events of design section 5.3 name the id that is
        going away, which is not the id the entry wears afterwards."""
        return dataclasses.replace(entry, dead=True)

    # -- what the read side asks -----------------------------------------------

    def client_list(self):
        """`_NET_CLIENT_LIST`: every entry's id, real or shadow, in `list()`
        order -- bottom to top, which is `WindowBackend.list`'s contract
        (backend.py:295)."""
        out = []
        for handle in self.order:
            entry = self.entries.get(handle)
            if entry is not None and entry.id:
                out.append(entry.id)
        return out

    def stacking(self):
        """`_NET_CLIENT_LIST_STACKING`: a copy of `client_list()`.

        sway's tree is not a stacking order and no backend in the tree reports
        one, so the two lists are the same list and this method exists to say
        so: `tests/test_xw11_shadow.py::RootLists` pins the copy, and a backend
        that starts reporting stacking changes a test rather than sliding a
        wrong order past (R13, design section 4.6)."""
        return self.client_list()

    def focused(self) -> int:
        """`_NET_ACTIVE_WINDOW`: the focused entry's id, 0 for none."""
        for handle in self.order:
            entry = self.entries.get(handle)
            if entry is not None and entry.window is not None and entry.window.focused:
                return entry.id
        return 0

    def focused_entry(self):
        """The focused entry, or None -- what `GetInputFocus` needs, which is
        not the same question as `focused()`: the id answers `0` both for
        "nothing is focused" and for "the focused entry has no id yet"."""
        for handle in self.order:
            entry = self.entries.get(handle)
            if entry is not None and entry.window is not None and entry.window.focused:
                return entry
        return None

    def resolve(self, handle: int) -> int:
        """The id a client is shown for a backend handle, 0 for one this
        registry has never listed. `WM_TRANSIENT_FOR` is the only reader:
        `View.transient_for` is a handle, and a handle in that field would be a
        lie in the one number a dialog-placing client trusts."""
        entry = self.entries.get(handle)
        return entry.id if entry is not None else 0

    def workspace_names(self):
        """`[name]` for `_NET_DESKTOP_NAMES`, or None where the backend has no
        `workspaces()`. A nameless workspace prints its index, which is what
        `wwmctl -d` does (wwmctl/core.py:790)."""
        got = self._call("workspaces")
        if not got:
            return None
        return [ws.name or "%d" % ws.index for ws in got]

    def display_size(self):
        """`(w, h)` for `_NET_DESKTOP_GEOMETRY`, or None where the backend will
        not say -- `_call` turns that `CmdError` into one log line and a
        default (design section 3.3)."""
        got = self._call("display_size")
        if not got:
            return None
        return int(got[0]), int(got[1])

    # -- what the read side reads ----------------------------------------------

    def props_for(self, entry):
        """One entry's properties with the overlay applied (design sections
        4.4 and 4.7), cached on the entry until anything about it moves."""
        return visible_props(entry, self.own, self.resolve)

    def root_props(self):
        """The root's synthesized set, rebuilt once per re-list.

        Cached on the generation and not on a clock: `snapshot()` already
        honours the 20 ms TTL, and `wmctrl -d` reads six root names in one
        breath [recon/tools.md 5] -- six calls to `workspaces()` and
        `display_size()` for one answer would be six IPC round trips where one
        will do, and on KWin each of them is a `loadScript` (R9)."""
        self.snapshot()
        if self._root_props is None or self._root_gen != self.generation:
            self._root_props = root_props(self, self.own)
            self._root_gen = self.generation
        return self._root_props

    # -- what the write side writes (design section 4.7) ------------------------

    def write(self, entry, atom, type_atom, fmt, data, mode=REPLACE):
        """One client's `ChangeProperty` on a shadow, into the overlay.

        The three modes are honoured against the CURRENT value -- overlaid or
        synthesized, whichever `props_for` answers -- because that is the value
        the same client just read back and the one X would have appended to.
        `xdotool set_window --name` writes `WM_NAME` and then `_NET_WM_NAME`,
        both `Replace`, both typed `STRING` [recon/tools.md 4.3]; the type is
        stored exactly as it came, `_NET_WM_NAME(STRING)` and all, because that
        bug is xdotool's and a proxy that corrected it would answer bytes no X
        server would have answered (AGENTS.md: bugs are features).

        True when the overlay moved. A `Prepend`/`Append` whose type or format
        disagrees with what is there is where a server answers `BadMatch`; this
        answers False and one log line, and the error is NOT YET -- the seam is
        `Server.handle`, which today can consume a request or answer it and not
        both, and the cost of the fix is one class in `xw11/policy.py` plus its
        arm in that method (AGENTS.md rung 5: rung 5 is this proxy, so a gap
        inside it closes with more of it). Nothing measured sends one: xprop
        `-set` and `set_window --name` are `Replace` [recon/tools.md 4.3, 6].
        """
        fmt = int(fmt)
        data = bytes(data)
        if mode != REPLACE:
            current = self.props_for(entry).get(atom)
            if current is not None:
                have_type, have_fmt, have = current
                if have_type != type_atom or have_fmt != fmt:
                    self.say("ChangeProperty mode %d on atom %d of shadow 0x%x "
                             "with type %d format %d over a value typed %d "
                             "format %d: X answers BadMatch here and this "
                             "proxy has no seam to answer an error on a "
                             "request it consumes -- not yet; the route is one "
                             "more class in xw11/policy.py and its arm in "
                             "Server.handle (AGENTS.md rung 5). The write is "
                             "dropped."
                             % (mode, atom, entry.shadow, type_atom, fmt,
                                have_type, have_fmt))
                    return False
                data = data + have if mode == PREPEND else have + data
        entry.overlay[atom] = (type_atom, fmt, data)
        self.on_property(entry, atom, PROP_NEW_VALUE)
        return True

    def delete(self, entry, atom):
        """`xprop -remove`, and `GetProperty(delete = 1)` on a full read.

        A synthesized value cannot be deleted -- the next re-list builds it
        again -- so the overlay carries a `TOMBSTONE` for the life of the
        entry, which is what X does for a property a window manager keeps
        rewriting. False when there was nothing to delete: a real server sends
        no `PropertyNotify` for a `DeleteProperty` on a name the window does
        not have, and the tombstone would otherwise hide the synthesis on the
        strength of a request that did nothing.
        """
        if self.props_for(entry).get(atom) is None:
            return False
        entry.overlay[atom] = TOMBSTONE
        self.on_property(entry, atom, PROP_DELETED)
        return True

    def on_property(self, entry, atom, state) -> None:
        """A property of a shadow moved, because a CLIENT wrote it: `state` is
        `PropertyNotify`'s own byte, 0 NewValue and 1 Deleted
        [recon/wire.md 4.4].

        A no-op here and the seam batch 5 fills: design section 5.3's last row
        sends the `PropertyNotify` to every `PropertyChange` selector on that
        window at once, the way the server does, and this is where it learns
        that it must. An instance attribute of the same name shadows this
        method, which is how `OwnConn.on_event` is already wired
        (xw11/upstream.py) -- so batch 5 assigns and never edits this file.
        """


# -- the properties a shadow wears (design section 4.4) -----------------------
#
# A port of `NativeViewTarget._props()` (wxprop/core.py:529-597) with three
# changes, all of them design section 4.4's: the atom ids are the ones `OwnConn`
# interned upstream and not `NativeAtoms`' 0x40000000 fakes [recon/seams.md 1,
# 4], the dict is cached on the entry and rebuilt whenever anything about the
# entry moved (`Shadows._update` clears it), and three names wxprop does not
# synthesize are here: `WM_STATE` on EVERY backend rather than on a `views()`
# one, `_NET_FRAME_EXTENTS` and `WM_PROTOCOLS`.
#
# The ORDER of this table is the order `ListProperties(shadow)` answers in.
# xprop prints server order, and a real server's order is creation order
# reversed -- the live xterm on the rig answers `_NET_WM_STATE, WM_STATE,
# WM_PROTOCOLS, _NET_WM_PID, WM_CLIENT_LEADER, WM_LOCALE_NAME, WM_CLASS,
# WM_HINTS, WM_NORMAL_HINTS, WM_CLIENT_MACHINE, WM_COMMAND, WM_ICON_NAME,
# WM_NAME` [M 2026-09-10, headless sway on this box, scratchpad b3/xterm_props].
# A shadow has no creation order to reverse, so the order is fixed here and the
# fixture pins it.

#: `View.window_type` / `Window.window_type` (a Mutter type name) -> the one
#: `_NET_WM_WINDOW_TYPE` atom. wxprop's own `_WINDOW_TYPES` (wxprop/core.py:348),
#: repeated here for the same reason `policy.ATOMS` is: `import xw11` must not
#: pull wxprop.core's 10 ms in.
WINDOW_TYPES = {
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

#: `_NET_WM_STATE`, in Mutter's own order (window-x11.c `set_net_wm_state`),
#: with FOCUSED last. `(name, View field, needs a views() backend)`: the
#: bracketed ones of wxprop's table are the `rich` ones -- a `list()`-only
#: backend has no field to read them from [recon/seams.md 4].
#:
#: The two that are NOT rich and are not the window's own -- FULLSCREEN and
#: STICKY -- have a second source: on sway and i3 they are keys of the tree
#: node (`Entry.flags`, filled by `Shadows._read`), which is where wxprop reads
#: them from too (wxprop/core.py:544, 554). Without that a foot sway had
#: fullscreened printed an empty `_NET_WM_STATE` through the proxy while the
#: clone printed `_NET_WM_STATE_FULLSCREEN` [M 2026-09-10, headless sway].
_STATE_BITS = (
    ("_NET_WM_STATE_SKIP_PAGER", "skip_pager", True),
    ("_NET_WM_STATE_SKIP_TASKBAR", "skip_taskbar", True),
    ("_NET_WM_STATE_MAXIMIZED_HORZ", "maximized_h", True),
    ("_NET_WM_STATE_MAXIMIZED_VERT", "maximized_v", True),
    ("_NET_WM_STATE_FULLSCREEN", "fullscreen", False),
    ("_NET_WM_STATE_HIDDEN", None, False),          # the window's own `visible`
    ("_NET_WM_STATE_ABOVE", "above", True),
    ("_NET_WM_STATE_BELOW", "below", True),
    ("_NET_WM_STATE_DEMANDS_ATTENTION", "urgent", True),
    ("_NET_WM_STATE_STICKY", "sticky", False),
    ("_NET_WM_STATE_FOCUSED", None, True),          # the window's own `focused`
)


def _p_string(text: str):
    """A latin-1 property, or UTF8_STRING when the text will not fit in one.

    wxprop's `_p_string` (wxprop/core.py:408) exactly: typing UTF-8 bytes as
    STRING is not a legibility trade-off but wrong -- xprop's STRING-to-locale
    rule decodes them as latin-1 and re-encodes for the locale, so every
    character above U+00FF prints as mojibake."""
    try:
        return ("STRING", 8, text.encode("latin-1"))
    except UnicodeEncodeError:
        return ("UTF8_STRING", 8, text.encode("utf-8"))


def _p_cardinal(vals):
    return ("CARDINAL", 32,
            struct.pack("<%dI" % len(vals), *[v & 0xFFFFFFFF for v in vals]))


def _p_window(vals):
    return ("WINDOW", 32,
            struct.pack("<%dI" % len(vals), *[v & 0xFFFFFFFF for v in vals]))


def _p_atoms(atoms, names):
    ids = [atoms.atom_id(n) for n in names]
    return ("ATOM", 32, struct.pack("<%dI" % len(ids), *ids))


def _states(entry):
    """The `_NET_WM_STATE` names for one entry, in `_STATE_BITS`' order."""
    win, view = entry.window, entry.view
    out = []
    for name, field, rich in _STATE_BITS:
        if field is None:
            if name.endswith("_HIDDEN"):
                got = not getattr(win, "visible", True)
            else:
                got = bool(getattr(win, "focused", False))
        elif view is None and field in entry.flags:
            # A view-less backend: the bit is on the tree node the registry
            # kept beside the window (`Shadows._read`), read the way wxprop
            # reads it. Without this a `foot` sway had fullscreened printed an
            # EMPTY `_NET_WM_STATE` through the proxy while `wxprop` printed
            # `_NET_WM_STATE_FULLSCREEN` for the same window [M 2026-09-10].
            got = bool(entry.flags[field])
        else:
            got = bool(getattr(view, field, False))
        if got and (view is not None or not rich):
            out.append(name)
    return out


def build_props(entry, atoms, resolve=None):
    """Design section 4.4's table for one entry: `{atom id: (type atom id,
    format, value bytes)}`, in the table's order.

    `atoms` is anything with `atom_id(name)` -- `OwnConn` in the proxy. A name
    whose atom is 0 (an intern that failed, or an `OwnConn` that never opened)
    is left out rather than written as atom 0, which is `None` on the wire and
    would make xprop print a property nobody can name.

    `resolve(handle) -> id` maps a backend handle onto what a client is shown,
    for `WM_TRANSIENT_FOR`: the parent's real X id where it has one and its
    shadow otherwise (design section 4.4). Without it the parent is left out --
    a `WM_TRANSIENT_FOR` naming a backend handle would be a lie in the one
    field a dialog-placing client trusts.
    """
    win, view = entry.window, entry.view
    named = {}

    def put(name, packed):
        atom = atoms.atom_id(name)
        type_atom = atoms.atom_id(packed[0])
        if atom and type_atom:
            named[atom] = (type_atom, packed[1], packed[2])

    title = getattr(win, "title", None)
    if title is not None:
        put("_NET_WM_NAME", ("UTF8_STRING", 8, title.encode("utf-8")))
        put("WM_NAME", _p_string(title))
    # `app_id` is BOTH halves for a native toplevel and empty for an X client,
    # which is wxprop's own rule (wxprop/core.py:576): a Wayland app id has no
    # instance/class pair to split. The last fallback -- instance = class -- is
    # for the ONE backend with no `views()` and therefore no `app_id` field to
    # read: sway's `list()` carries `class_ = app_id or WM_CLASS class` and
    # `instance = WM_CLASS instance or ""`, so a native toplevel arrives with an
    # empty instance, and `Window.instance`'s own docstring says
    # `search --classname` falls back to `class_` for exactly that reason
    # (backend.py:136-139). Without it python-xlib's `get_wm_class()` answered
    # `('', 'footw')` on the live rig [M 2026-09-10].
    app_id = getattr(view, "app_id", "") or ""
    cls = app_id or getattr(view, "cls", "") or getattr(win, "class_", "")
    instance = (app_id or getattr(view, "instance", "")
                or getattr(win, "instance", "") or cls)
    if instance or cls:
        # STRING by ICCCM whatever the app id looks like, never routed through
        # `_p_string`'s UTF8_STRING escape hatch: wmctrl names the type STRING
        # explicitly and reads a mismatch as absent [recon/tools.md 5], and an
        # X twin's WM_CLASS is STRING too.
        data = ((instance or "").encode("latin-1", "replace") + b"\0"
                + (cls or "").encode("latin-1", "replace") + b"\0")
        put("WM_CLASS", ("STRING", 8, data))
    pid = int(getattr(win, "pid", 0) or 0)
    if pid:
        put("_NET_WM_PID", _p_cardinal([pid]))
    host = x11_mini.hostname()
    if host:
        put("WM_CLIENT_MACHINE", _p_string(host))
    desktop = int(getattr(win, "desktop", -1))
    put("_NET_WM_DESKTOP", _p_cardinal([desktop if desktop >= 0 else 0xFFFFFFFF]))
    put("_NET_WM_STATE", _p_atoms(atoms, _states(entry)))
    wtype = "_NET_WM_WINDOW_TYPE_NORMAL"
    if view is not None:
        wtype = WINDOW_TYPES.get(getattr(view, "window_type", None) or "NORMAL",
                                 wtype)
    put("_NET_WM_WINDOW_TYPE", _p_atoms(atoms, [wtype]))
    parent = int(getattr(view, "transient_for", 0) or 0)
    if parent and resolve is not None:
        shown = resolve(parent)
        if shown:
            put("WM_TRANSIENT_FOR", _p_window([shown]))
    # ICCCM WM_STATE, on EVERY backend and not only a views() one (design
    # section 4.4's decided row), and the REASON that row gives is wrong --
    # measured, so it is written down here rather than repeated. design 4.4
    # says `xdotool getwindowfocus` climbs from the focus window until it finds
    # WM_STATE and would answer the root for a shadow without it. It does read
    # the property (GetInputFocus, InternAtom WM_STATE, GetProperty WM_STATE
    # [recon/tools.md 4.2]) and it does NOT climb on an absent one: with
    # WM_STATE tombstoned out of a live shadow, the pinned xdotool
    # 4.20260303.1 printed the same shadow id and exited 0, exactly as it did
    # with the property there [M 2026-09-10, headless sway, scratchpad
    # b3/wmstate.py]. What it does on a MISSING WINDOW is the measured failure
    # of recon/env.md 3, which is a different thing.
    #
    # The property stays on every backend anyway, for the readers that do care:
    # Mutter writes it on every X11 window it manages, so a native toplevel
    # that answers "is this minimized?" the same way keeps a script working
    # across the two planes, and `tests/test_xw11_parity.py::NativeParity` has
    # the clone printing it too. `tests/test_xw11_live.py` pins the
    # measurement, so an xdotool that starts climbing changes a test.
    put("WM_STATE", ("WM_STATE", 32,
                     struct.pack("<II", 1 if getattr(win, "visible", True) else 3, 0)))
    # The rect every backend reports is the whole toplevel and GetGeometry
    # answers that same rect, so zero is the true number and not a placeholder.
    # Real decoration sizes are a row (route 2, per-backend frame geometry).
    put("_NET_FRAME_EXTENTS", _p_cardinal([0, 0, 0, 0]))
    # A polite closer sends the ClientMessage batch 4 routes to `close`;
    # without this the fallback is XKillClient, which is SIGKILL.
    put("WM_PROTOCOLS", _p_atoms(atoms, ["WM_DELETE_WINDOW"]))
    return named


def visible_props(entry, atoms, resolve=None):
    """`build_props` with the overlay applied: what `ListProperties` lists and
    `GetProperty` reads (design section 4.7).

    The overlay is what a client WROTE -- `xprop -set`, `xdotool set_window
    --name` -- shared by every client the way X shares a property, and it
    beats the synthesis for the same name. `TOMBSTONE` hides a synthesized
    name for as long as the entry lives; a name only the overlay has is
    appended after the table, in write order, which is the order a real server
    would answer them in for a window whose properties arrived in that order.
    """
    base = entry.props
    if not base:
        base = entry.props = build_props(entry, atoms, resolve)
    if not entry.overlay:
        return base
    out = {}
    for atom, packed in base.items():
        got = entry.overlay.get(atom)
        if got is TOMBSTONE:
            continue
        out[atom] = packed if got is None else got
    for atom, packed in entry.overlay.items():
        if packed is not TOMBSTONE and atom not in base:
            out[atom] = packed
    return out


# -- the root (design section 4.6) --------------------------------------------


def root_props(shadows, atoms):
    """The root names the proxy answers from the compositor: `{atom id: (type
    atom id, format, value bytes)}` in `policy.OVERRIDES` order.

    Synthesized whether or not upstream has the name. sway **deletes**
    `_NET_CLIENT_LIST` outright when no X client is mapped rather than emptying
    it, and keeps `_NET_ACTIVE_WINDOW` set to `None` [recon/env.md 2.7] -- so
    the read side has to invent a property the server says does not exist, not
    merely rewrite one that does.

    `_NET_DESKTOP_NAMES` is absent where the backend has no `workspaces()` and
    `_NET_DESKTOP_GEOMETRY` where it will not say its size, which is what a
    real EWMH window manager that does not publish a name does: `wmctrl -d`
    prints `N/A` for an absent column [wwmctl/core.py:780] rather than a zero
    that reads as a real answer.
    """
    named = {}

    def put(name, packed):
        atom = atoms.atom_id(name)
        type_atom = atoms.atom_id(packed[0])
        if atom and type_atom:
            named[atom] = (type_atom, packed[1], packed[2])

    ids = shadows.client_list()
    put("_NET_CLIENT_LIST", _p_window(ids))
    put("_NET_CLIENT_LIST_STACKING", _p_window(shadows.stacking()))
    put("_NET_ACTIVE_WINDOW", _p_window([shadows.focused()]))
    put("_NET_NUMBER_OF_DESKTOPS", _p_cardinal([max(shadows.num_desktops, 1)]))
    put("_NET_CURRENT_DESKTOP", _p_cardinal([max(shadows.current_desktop, 0)]))
    names = shadows.workspace_names()
    if names is not None:
        put("_NET_DESKTOP_NAMES",
            ("UTF8_STRING", 8,
             b"".join(n.encode("utf-8") + b"\0" for n in names)))
    size = shadows.display_size()
    if size is not None:
        # `wwmctl -d` prints its `DG:` column from `display_size()`
        # (wwmctl/core.py:780) and the parity target is the clone's bytes.
        put("_NET_DESKTOP_GEOMETRY", _p_cardinal([size[0], size[1]]))
    return named


def supported_union(upstream, atoms, names=None):
    """`_NET_SUPPORTED`: upstream's own list, then every atom the proxy answers
    for that upstream did not name, in `policy.SUPPORTED` order.

    Upstream's half comes first and unreordered because it is upstream's
    answer and the proxy is adding to it, not replacing it: wlroots' Xwayland
    root names 19 atoms [recon/env.md 2.1] and its xwm handles every one of
    them itself. Ours are appended, deduplicated against upstream by atom id --
    the same name interned twice is the same id, server-global
    [recon/wire.md 7.2].
    """
    out = list(upstream)
    have = set(out)
    for name in (policy.SUPPORTED if names is None else names):
        atom = atoms.atom_id(name)
        if atom and atom not in have:
            have.add(atom)
            out.append(atom)
    return out
