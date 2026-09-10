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
import time

from w11common.errors import CmdError
from wdotool import backend as backend_mod
from wdotool import backend_detect
from wdotool import x11_mini
from wdotool.ctx import NoSessionError
from xw11 import upstream as upstream_mod

#: How long a snapshot stands before the next read re-lists (design section 4.8).
TTL = 0.020

#: What `entry.overlay[atom]` holds for a property a client deleted: X's own
#: "this name is absent", which is not the same as "this name was never
#: synthesized" -- the synthesis would put it straight back.
TOMBSTONE = object()

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
        """`views()` where the backend has one, else `list()` -- the richer
        record carries `xid`, the states and the WM_CLASS pair the read side
        answers with [recon/seams.md 2.1]. Returns (windows, views)."""
        self._lists += 1
        views = self._call("views")
        if views:
            return [v.window for v in views], list(views)
        wins = self._call("list", default=None)
        return (list(wins) if wins else []), None

    # -- the diff -------------------------------------------------------------

    def refresh(self):
        """Re-read the compositor and answer with what moved.

        Unconditional: the TTL is `snapshot()`'s rule, and the pump calls this
        one straight (design section 5.2 -- one re-list answers every token in
        a drain)."""
        wins, views = self._read()
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
                entry = self._add(win, view)
                changes.append(Change(NEW, entry.handle, entry))
                continue
            changes += self._update(entry, win, view)
        changes += self._departed(seen)
        if seen != self.order:
            if set(seen) == set(self.order):
                changes.append(Change(ORDER))
            self.order = seen
        changes += self._desktops()
        return changes

    def _add(self, win, view) -> Entry:
        entry = Entry(handle=win.id, window=win, view=view,
                      xid=int(getattr(view, "xid", 0) or 0),
                      seen=self.generation)
        if entry.xid:
            self.by_xid[entry.xid] = entry
        else:
            entry.shadow = self.mint()
            if entry.shadow:
                self.by_shadow[entry.shadow] = entry
        self.entries[entry.handle] = entry
        return entry

    def _update(self, entry, win, view):
        """One entry against its previous self, field by field. The order of the
        kinds here is the order of design section 5.3's table, so batch 5 can
        walk the list and write packets."""
        old_win, old_view = entry.window, entry.view
        changes = []
        xid = int(getattr(view, "xid", 0) or 0)
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
            entry.seen = self.generation
            changes.append(Change(NEW, entry.handle, entry))
            return changes
        if entry.shadow == 0 and entry.xid == 0:
            # A mint that failed while the range was spent; try again, because
            # a window that leaves frees nothing but a later one may arrive
            # after a rebase.
            entry.shadow = self.mint()
            if entry.shadow:
                self.by_shadow[entry.shadow] = entry
        entry.window, entry.view = win, view
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
