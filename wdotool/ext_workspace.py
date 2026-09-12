"""`ext_workspace_manager_v1` v1 client: the desktops of a compositor that has no IPC socket.

The wlr floor reports no desktops at all -- `wwmctl -d` and `get_desktop` refuse -- on compositors whose own
panel is showing four workspaces. Three of them publish this protocol and were read by hand:

* labwc 0.9.3 with three configured names: one group, `one` (state 1 = active), `two`, `three`, each with
  capability 1 (activate); the default with no `<desktops>` block is one workspace `Workspace 1`
  [M recon2/labwc.md §6a].
* Budgie 10.10 over labwc with `<desktops number="4"/>`: `Workspace 1` active, `Workspace 2..4`
  [M recon2/budgie.md]. Xfce-on-Wayland's labwc has four the same way [M recon2/xfce-wayland.md].
* COSMIC: two workspaces named `1` and `2`, capability 1, carrying `coordinates` `[1]` and `[2]`
  [M recon2/cosmic.md §4]. labwc sends no `coordinates` at all.

sway 1.11 publishes no workspace protocol (it has an IPC socket instead) and Wayfire 0.10 publishes none
either, so a backend binds this only when the global is there and keeps its old refusal otherwise
[M recon2/labwc.md §2, recon2/wayfire.md §1.1].

Ordering is the one rule that has to cover both shapes, and it is two comparisons because the protocol has
two axes. Within a group, workspaces sort by their coordinates when they carry any and by arrival order when
they do not -- `(coordinates, arrival)`, `_live()`. Across groups they sort by the group's own first arrival,
because a compositor may publish one `ext_workspace_group_handle_v1` PER OUTPUT: cosmic-comp does, and on the
fedora44-cosmic golden with two heads on 2026-09-11 that was groups 4278190084 (workspaces `1` [1] and `2`
[2]) and 4278190087 (`1` [1]), both `1`s active. Sorting those three by coordinates alone interleaves the
heads into `1, 1, 2`, so desktop 1 was the other head's already-active workspace, `wdotool set_desktop 1`
activated something that was already active and `get_desktop` answered 0 -- the fedora44-cosmic CI failure
[M goal2/recon/flavors.md 5, goal2/ci/rig-fedora44-cosmic.log, goal2/requests-batch-5.md]. `_rows()` is that
order and it is what every public method below counts in; it changes nothing for labwc, Budgie,
Xfce-on-Wayland or LXQt-on-Wayland, which publish a single group. Nothing here dispatches on its own; the
caller owns the connection and its round trips, the same contract `backend_wlr.py` has with `wayland_mini`."""

import struct

from wdotool.backend import Workspace

INTERFACE = "ext_workspace_manager_v1"
VERSION = 1

# ext_workspace_manager_v1
_MGR_COMMIT = 0
_MGR_STOP = 1
_MGR_EV_GROUP = 0
_MGR_EV_WORKSPACE = 1
_MGR_EV_DONE = 2
_MGR_EV_FINISHED = 3

# ext_workspace_group_handle_v1 events
_GRP_EV_CAPABILITIES = 0
_GRP_EV_OUTPUT_ENTER = 1
_GRP_EV_OUTPUT_LEAVE = 2
_GRP_EV_WORKSPACE_ENTER = 3
_GRP_EV_WORKSPACE_LEAVE = 4
_GRP_EV_REMOVED = 5

# ext_workspace_handle_v1 requests
_WS_DESTROY = 0
_WS_ACTIVATE = 1
_WS_DEACTIVATE = 2
_WS_ASSIGN = 3
_WS_REMOVE = 4

# ext_workspace_handle_v1 events
_WS_EV_ID = 0
_WS_EV_NAME = 1
_WS_EV_COORDINATES = 2
_WS_EV_STATE = 3
_WS_EV_CAPABILITIES = 4
_WS_EV_REMOVED = 5

#: ext_workspace_handle_v1.state -- a bitfield, not the array of enums the
#: foreign-toplevel protocols use
STATE_ACTIVE = 1
STATE_URGENT = 2
STATE_HIDDEN = 4

#: ext_workspace_handle_v1.workspace_capabilities
CAP_ACTIVATE = 1
CAP_DEACTIVATE = 2
CAP_REMOVE = 4
CAP_ASSIGN = 8


class _Workspace:
    __slots__ = ("oid", "arrival", "ws_id", "name", "coords", "state", "caps", "group", "removed")

    def __init__(self, oid, arrival):
        self.oid = oid
        self.arrival = arrival
        self.ws_id = ""
        self.name = ""
        self.coords: tuple[int, ...] = ()
        self.state = 0
        self.caps = 0
        self.group = None
        self.removed = False


class WorkspaceClient:
    """The manager, its groups and its workspaces, kept up to date by the caller's round trips.

    `conn` is a live `w11common.wayland_mini.WlConn` whose registry has already been fetched. `bind()` returns
    None when the compositor does not offer the protocol, so a caller can say `ws = WorkspaceClient.bind(conn)`
    and keep its own refusal for None rather than catching anything."""

    def __init__(self, conn, name: int, version: int = VERSION):
        self.c = conn
        self.version = min(version, VERSION)
        self.workspaces: dict[int, _Workspace] = {}   # handle oid -> record
        self.groups: dict[int, int] = {}              # group oid -> capabilities
        self.arrival = 0
        #: bumped by every `done`; a caller that wants "has anything changed"
        #: compares it across a round trip rather than re-listing
        self.serial = 0
        self.finished = False
        self.mgr = self.c.bind(name, INTERFACE, self.version)
        self.c.on(self.mgr, self._on_mgr)

    @classmethod
    def bind(cls, conn) -> "WorkspaceClient | None":
        """The client, or None when `ext_workspace_manager_v1` is not advertised."""
        g = conn.find_global(INTERFACE)
        if g is None:
            return None
        return cls(conn, g[0], g[1])

    # -- events ---------------------------------------------------------------

    def _on_mgr(self, op, cur, fds):
        if op == _MGR_EV_WORKSPACE:
            oid = cur.u32()
            rec = _Workspace(oid, self.arrival)
            self.arrival += 1
            self.workspaces[oid] = rec
            self.c.on(oid, lambda o, c, f, r=rec: self._on_ws(r, o, c))
        elif op == _MGR_EV_GROUP:
            oid = cur.u32()
            self.groups[oid] = 0
            self.c.on(oid, lambda o, c, f, g=oid: self._on_group(g, o, c))
        elif op == _MGR_EV_DONE:
            self.serial += 1
        elif op == _MGR_EV_FINISHED:
            self.finished = True

    def _on_group(self, group, op, cur):
        if op == _GRP_EV_CAPABILITIES:
            self.groups[group] = cur.u32()
        elif op == _GRP_EV_WORKSPACE_ENTER:
            rec = self.workspaces.get(cur.u32())
            if rec is not None:
                rec.group = group
        elif op == _GRP_EV_WORKSPACE_LEAVE:
            rec = self.workspaces.get(cur.u32())
            if rec is not None and rec.group == group:
                rec.group = None
        elif op == _GRP_EV_REMOVED:
            self.groups.pop(group, None)

    def _on_ws(self, rec: _Workspace, op, cur):
        if op == _WS_EV_ID:
            rec.ws_id = cur.string()
        elif op == _WS_EV_NAME:
            rec.name = cur.string()
        elif op == _WS_EV_COORDINATES:
            # an array of uint32, however many dimensions the group has; COSMIC
            # sends one ([1], [2]), labwc sends the event not at all
            raw = cur.array()
            rec.coords = struct.unpack("<%dI" % (len(raw) // 4), raw[:len(raw) // 4 * 4])
        elif op == _WS_EV_STATE:
            rec.state = cur.u32()
        elif op == _WS_EV_CAPABILITIES:
            rec.caps = cur.u32()
        elif op == _WS_EV_REMOVED:
            rec.removed = True

    # -- what a backend asks --------------------------------------------------

    def _live(self) -> list[_Workspace]:
        """Every workspace still there, in the order ONE group runs in.

        `(coordinates, arrival)`: COSMIC's `[1]`/`[2]` decide there, labwc and Budgie send no coordinates and
        fall back to the order the compositor announced them in, which is the order their own panels show.
        This is not the desktop order on a session with a group per output -- `_rows()` is, and every public
        method counts in that one. The two are kept apart because the difference is the whole of the
        fedora44-cosmic fix: `tests/test_backend_cosmic.py::GroupedWorkspaces` reads this flat order to prove
        that the grouped one really is a different list, which is the guard on the sort key below."""
        rows = [r for r in self.workspaces.values() if not r.removed]
        rows.sort(key=lambda r: (r.coords, r.arrival))
        return rows

    def _rows(self) -> list[_Workspace]:
        """Every workspace still there, in DESKTOP order: every group's workspaces contiguous, coordinates
        within the group, the groups themselves in the order the compositor first announced one of their
        workspaces (the module docstring's measurement).

        The group key is the group's EARLIEST arrival and not the first one this list happens to meet, so it
        does not depend on the coordinate sort above having put the group's own rows in arrival order."""
        rows = self._live()
        first: dict = {}
        for r in rows:
            got = first.get(r.group)
            if got is None or r.arrival < got:
                first[r.group] = r.arrival
        rows.sort(key=lambda r: (first.get(r.group, r.arrival), r.coords, r.arrival))
        return rows

    def count(self) -> int:
        return len(self._rows())

    def workspace_list(self) -> list[Workspace]:
        """The `backend.Workspace` rows `wwmctl -d` prints. `work_area` stays (0,0,0,0): the protocol carries
        no geometry, and a workspace's group may cover several outputs."""
        return [Workspace(index=i, name=r.name, active=bool(r.state & STATE_ACTIVE))
                for i, r in enumerate(self._rows())]

    def handles(self) -> list[int]:
        """The `ext_workspace_handle_v1` object ids, in the same desktop-number order `workspace_list()`
        numbers its rows -- so `handles()[n]` is the handle of desktop `n`.

        The rows themselves are private (`_live()`), and the public `backend.Workspace` deliberately carries
        no protocol object: it is what `wwmctl -d` prints.  COSMIC needs the oids and nothing else --
        `zcosmic_toplevel_handle_v1.workspace_enter` names a workspace by object id, which is the only way a
        window gets a desktop number there, and `move_to_ext_workspace` (opcode 13) takes one
        [requests-batch-8.md item 1]."""
        return [r.oid for r in self._rows()]

    def active_handles(self) -> set[int]:
        """The oids of the workspaces whose state carries `active`; empty when none of them says so.  A
        window is visible when it sits on one of these, which is a set membership and not an index."""
        return {r.oid for r in self._rows() if r.state & STATE_ACTIVE}

    def active_index(self) -> int:
        """The active workspace's 0-based index, or -1 when none of them says it is active."""
        for i, r in enumerate(self._rows()):
            if r.state & STATE_ACTIVE:
                return i
        return -1

    def can_activate(self, index: int) -> bool:
        rows = self._rows()
        return 0 <= index < len(rows) and bool(rows[index].caps & CAP_ACTIVATE)

    def activate(self, index: int) -> bool:
        """Ask for workspace `index` and commit. False when the index is out of range or the compositor did not
        advertise the activate capability for it -- the caller turns that into its own refusal rather than
        sending a request the protocol says is not there.

        `activate` alone changes nothing: ext-workspace is a double-buffered protocol and the manager's
        `commit` is what applies the batch [M recon2/labwc.md §6a: activate + commit moved the active bit]."""
        rows = self._rows()
        if not (0 <= index < len(rows)):
            return False
        rec = rows[index]
        if not rec.caps & CAP_ACTIVATE:
            return False
        self.c.send(rec.oid, _WS_ACTIVATE)
        self.c.send(self.mgr, _MGR_COMMIT)
        return True
