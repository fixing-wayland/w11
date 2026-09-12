"""COSMIC window backend: `ext_foreign_toplevel_list_v1` + the two `zcosmic_toplevel_*` protocols.

cosmic-comp publishes no `zwlr_foreign_toplevel_manager_v1` at all, which is why every window command --
all of wwmctl, wxprop's native plane, every `wdotool window*` -- answered `no Wayland session found` on a
COSMIC session, with a sentence naming a protocol COSMIC is never going to have [M recon2/cosmic.md §2, §3].
The display half (`wxrandr`, `warandr`) already worked over `zwlr_output_manager_v1` v4, typing already
worked over `zwp_virtual_keyboard_v1`, and the screen-mirroring tool's `--check` already passed on
`ext_image_copy_capture_manager_v1`; this file is the window half and nothing else.

Everything here was driven by hand against a live cosmic-comp before it was written
(`recon2/cosmic/cosmic_probe.py`, `cosmic_act.py`): `set_maximized` came back as `state [0]`,
`unset_maximized` as `[]`, `activate` as `[2]`, the manager announced `capabilities [1,2,3,4,6]`, and the two
workspaces were named `1` and `2` with `coordinates` `[1]` and `[2]` [M cosmic.md §4]. Opcodes are read off
the XML saved at `recon2/cosmic/cosmic-toplevel-{info,management}-unstable-v1.xml`.

What the protocol does not carry, and what this backend therefore says instead of guessing:

* **No pid.** Neither `ext_foreign_toplevel_handle_v1` nor `zcosmic_toplevel_handle_v1` has one, so `pid` is
  0 and `kill` refuses with the default `no pid for window N`.
* **No move, resize, raise or lower.** `zcosmic_toplevel_manager_v1` has `set_rectangle` (a minimise-animation
  hint) and nothing else, so those four refuse per command, name the protocol, and say what would close the
  gap: a patched cosmic-comp, AGENTS.md route 6. Every refusal in this file carries that second half.
* **No numeric id.** `identifier` is 32 base62 characters, so ids are minted from it
  (`backend.mint_id`): `ID_BASE | 30 bits of blake2b`, i.e. at or ABOVE 0x40000000 and therefore above every
  id Xwayland gives its own clients ((client << 21) | serial), stable across processes for the life of the
  window, unlike the wlr floor's arrival order. This paragraph said "under 0x40000000" until 2026-09-11 and
  vm/live-smoke.d/cosmic.sh:120 checked for that, which failed on every correctly minted id
  [M goal2/recon/flavors.md §5: 1081277706 = 0x40730C0A].
* **Geometry, on a compositor that is asked to wait for it.** cosmic-comp sends `geometry` alongside
  `output_enter` or on a change [R `toplevel_info.rs`: `(outputs_changed || geometry_changed)`], out of the
  same rate-limited refresh as `state` (STATE_WAIT below). It never arrived in the nested rig, in 4 s or
  after a maximize [M cosmic.md §4] -- and on the fedora44-cosmic golden it arrives with the first state
  array, 152 ms in, which is why `wdotool getwindowgeometry` reads a real rectangle there
  (`762,201 696x532` [M vm/live-smoke.out/fedora44-cosmic-20260911-213252.log]) instead of the head's
  mode. Where it still does not come the
  listing falls back to the output rectangle, the way the wlr floor does, and sets `geometry_is_floor`
  rather than printing anything: `list()` runs on every window command, so a warning there is a warning on
  all of them."""

import struct
import time

from w11common import session
from w11common.errors import CmdError
from w11common.wayland_mini import WlConn
from wdotool import ext_workspace
from wdotool.backend import Window, WindowBackend, mint_map
from wdotool.backend_wlr import XPlaneViews

EXT_LIST = "ext_foreign_toplevel_list_v1"
INFO = "zcosmic_toplevel_info_v1"
MANAGER = "zcosmic_toplevel_manager_v1"

INFO_MAX = 3
MANAGER_MAX = 4

#: zcosmic_toplevel_handle_v1.state -- an array of these, not a bitfield
ST_MAXIMIZED, ST_MINIMIZED, ST_ACTIVATED, ST_FULLSCREEN, ST_STICKY = 0, 1, 2, 3, 4

#: zcosmic_toplelevel_management_capabilities_v1 (the XML's own spelling)
CAP_CLOSE, CAP_ACTIVATE, CAP_MAXIMIZE, CAP_MINIMIZE = 1, 2, 3, 4
CAP_FULLSCREEN, CAP_MOVE_TO_WORKSPACE, CAP_STICKY, CAP_MOVE_TO_EXT_WORKSPACE = 5, 6, 7, 8

#: what each capability is called in the refusal
CAP_NAMES = {
    CAP_CLOSE: "close", CAP_ACTIVATE: "activate", CAP_MAXIMIZE: "maximize",
    CAP_MINIMIZE: "minimize", CAP_FULLSCREEN: "fullscreen",
    CAP_MOVE_TO_WORKSPACE: "move_to_workspace", CAP_STICKY: "sticky",
    CAP_MOVE_TO_EXT_WORKSPACE: "move_to_ext_workspace",
}

# ext_foreign_toplevel_list_v1 / _handle_v1
_LIST_EV_TOPLEVEL = 0
_EXT_EV_CLOSED, _EXT_EV_DONE, _EXT_EV_TITLE, _EXT_EV_APP_ID, _EXT_EV_IDENTIFIER = 0, 1, 2, 3, 4

# zcosmic_toplevel_info_v1
_INFO_REQ_GET = 1
# zcosmic_toplevel_handle_v1 events
_CH_EV_CLOSED, _CH_EV_DONE = 0, 1
_CH_EV_OUTPUT_ENTER, _CH_EV_WORKSPACE_ENTER = 4, 6
_CH_EV_STATE, _CH_EV_GEOMETRY = 8, 9
_CH_EV_EXT_WORKSPACE_ENTER, _CH_EV_EXT_WORKSPACE_LEAVE = 10, 11
_CH_EV_WORKSPACE_LEAVE = 7

# zcosmic_toplevel_manager_v1 requests
_MGR_CLOSE, _MGR_ACTIVATE = 1, 2
_MGR_SET_MAXIMIZED, _MGR_UNSET_MAXIMIZED = 3, 4
_MGR_SET_MINIMIZED, _MGR_UNSET_MINIMIZED = 5, 6
_MGR_SET_FULLSCREEN, _MGR_UNSET_FULLSCREEN = 7, 8
_MGR_SET_STICKY, _MGR_UNSET_STICKY = 11, 12
_MGR_MOVE_TO_EXT_WORKSPACE = 13
_MGR_EV_CAPABILITIES = 0

#: `activate` takes a wl_seat and this backend will not send a null one (tests/test_backend_cosmic.py
#: proves no null goes on the wire).  Nobody has produced a COSMIC session without a wl_seat: cosmic-comp
#: builds its SeatState unfiltered [R recon2/cosmic/state.rs:694], and the sandbox filter it does apply
#: covers the toplevel manager and the workspaces [same file, 748-762], so a filtered client loses the
#: manager and fails the constructor's precondition long before it reaches here.  The branch exists
#: because the request takes a seat, and the rung is written for the session that turns up anyway: the
#: protocol names a seat in `activate`, so nothing below a patched cosmic-comp answers it.
NO_SEAT = ("compositor offers no wl_seat; cannot activate windows; not yet here, and the route is a "
           "patched cosmic-comp that activates a toplevel without naming a seat (AGENTS.md route 6)")

#: The refusal for the four commands with no request behind them.
NO_GEOMETRY = ("the COSMIC toplevel protocol has no move, resize, raise or lower; not yet here, "
               "and the route is a patched cosmic-comp (AGENTS.md route 6)")

#: The same, for a state name the handle's array has no member for. Five members [R the XML's `state`
#: enum], and SHADED, ABOVE, BELOW and the taskbar hints are not among them.
NO_SUCH_STATE = ("the COSMIC toplevel protocol carries maximized, minimized, activated, fullscreen and "
                 "sticky and no other state; not yet here, and the route is a patched cosmic-comp "
                 "(AGENTS.md route 6), one state member and one request each")

#: A version gap, which is the cheapest kind: the protocol already defines the request and this build is
#: older than it. `%s` is the request, `%d` the version it arrived in, `%d` the one this session offers.
OLD_MANAGER = ("this cosmic-comp's %s is version %d and %s arrived in version %d; not yet here, and the "
               "route is that version of the protocol it already speaks (AGENTS.md route 1), which "
               "costs a newer cosmic-comp and no code of ours")

#: How long a fresh connection waits for the first `state` array of every toplevel it attached to.
#:
#: cosmic-comp does NOT answer `get_cosmic_toplevel` with the window's info: the handle is created in the
#: request arm and stays empty
#: [R recon2/cosmic/src-cosmic-comp/src/wayland/protocols/toplevel_info.rs:186-215], and everything about
#: it -- `state` first of all -- is sent by `ToplevelInfoState::refresh`, which runs from
#: the event loop's post-dispatch callback [R src/lib.rs:210]. That callback is RATE LIMITED: `fn refresh` at
#: src/lib.rs:340-367 returns without sending anything when the last refresh was under 150 ms ago and arms a
#: 150 ms timer instead. A `wl_display.sync` is answered by the dispatch itself, so a roundtrip can come back
#: -- twice -- before the compositor has said a word about state, and the client then reads every window as
#: unfocused and unmaximized. That is exactly what CI measured on fedora44-cosmic (cosmic-comp 1.6.0):
#: `getactivewindow` -> `xdo_get_active_window reported an error` and `wxprop -id _NET_WM_STATE` empty right
#: after a maximize [M goal2/recon/flavors.md §5, goal2/ci/rig-fedora44-cosmic.log].
#:
#: So the connection waits for the events instead of assuming them, and the wait ends the moment the last
#: handle has its array. Measured on the live fedora44-cosmic golden (cosmic-comp 1.8.0-1.fc44 -- the
#: golden was rebuilt on 2026-09-11 and carries a build NEWER than arch-cosmic's 1.7.0, `rpm -q` in the
#: transcript; the flavor's own header still says the 1.6.0-3.fc44 it was written at), 2026-09-11: the
#: roundtrip after `get_cosmic_toplevel` came back in 0.1 ms with nothing, and every `state` array landed
#: 152.1 ms after the request -- the 150 ms window, once; 153.2, 151.9 and 152.8 ms on three re-runs (the
#: probe and its transcript: goal2/recon/cosmic-state-probe.py and cosmic-state-probe.txt, and what the
#: wait costs a command on that session: 0.15-0.30 s per tool, same file). 500 ms is three of them, the
#: margin for a compositor under load; a handle that never answers is given up on ONCE -- `_Top.waited`,
#: below -- so the budget is paid per silent handle and not per listing, and the window is still listed.
STATE_WAIT = 0.5

#: What the desktop commands say when the session publishes no workspace global. cosmic-comp does publish
#: one; this is the line for a build or a session that does not [M recon2/cosmic.md §3].
NO_WORKSPACES = ("this session publishes no ext_workspace_manager_v1; not yet here, and the route is that "
                 "protocol where the compositor grows it (AGENTS.md route 1), which costs a newer "
                 "cosmic-comp and no code of ours")


class _Top:
    __slots__ = ("ext", "cosmic", "identifier", "title", "app_id", "states", "have_state", "waited",
                 "geometry", "workspace", "closed")

    def __init__(self, ext):
        self.ext = ext
        self.cosmic = None
        self.identifier = ""
        self.title = ""
        self.app_id = ""
        self.states: set[int] = set()
        #: False until a `state` event has arrived for this handle at all -- which is not the same as an
        #: empty array, and is what `_await_state` waits on
        self.have_state = False
        #: set when a `_await_state` deadline passed with this handle still silent: the budget is spent
        #: once per handle, not once per listing (STATE_WAIT's paragraph)
        self.waited = False
        self.geometry = None        # (x, y, w, h) once a geometry event arrives
        self.workspace = None       # workspace handle oid from workspace_enter
        self.closed = False


class CosmicBackend(XPlaneViews, WindowBackend):
    name = "cosmic"
    #: What cosmic-comp's own X window manager puts on the `_NET_SUPPORTING_WM_CHECK` window -- read off the
    #: live session's Xwayland, where `wwmctl -m` already printed it [M cosmic.md §3 wwmctl]. So a run with
    #: no X plane says the same thing as a run with one.
    wm_name = "Smithay X WM"

    def __init__(self, conn=None):
        """`conn` is detection's live `WlConn` when it has one; without it this opens and owns its own."""
        self._own_conn = conn is None
        self.uid = None
        if conn is None:
            hit = session.find_wayland_socket()
            if not hit:
                raise CmdError("cosmic backend: no Wayland socket found")
            self.uid, _rd, sockpath = hit
            try:
                self.c = WlConn(sockpath)
            except OSError as e:
                raise CmdError("cosmic backend: cannot connect to %s: %s" % (sockpath, e)) from None
        else:
            self.c = conn
        try:
            reg = self.c.get_registry()
            glist = self.c.find_global(EXT_LIST)
            ginfo = self.c.find_global(INFO)
        except (OSError, RuntimeError, struct.error) as e:
            self._close_own()
            raise CmdError("cosmic backend: %s" % e) from None
        if not glist or not ginfo:
            self._close_own()
            # Not a full stop: cosmic-comp builds BOTH behind `client_not_sandboxed`
            # [R recon2/cosmic/state.rs:647, 748], so the commonest way to meet this line is to be the
            # sandboxed client rather than to be on a compositor that lacks the protocol.
            raise CmdError("cosmic backend: compositor does not offer %s and %s; not yet here -- "
                           "cosmic-comp speaks both and hides them from a sandboxed client, so the route "
                           "is an unsandboxed run of the protocol it already has (AGENTS.md route 1), and "
                           "on any other compositor a backend over its own IPC (route 2)"
                           % (EXT_LIST, INFO))

        self.tops: dict[int, _Top] = {}       # ext handle oid -> record
        self.order: list[int] = []            # ext handle oids, arrival order
        self.by_cosmic: dict[int, _Top] = {}  # zcosmic handle oid -> record
        self.caps: set[int] = set()
        #: set once a listing had to report an output rectangle because no `geometry` event arrived
        self.geometry_is_floor = False

        self.list_oid = self.c.bind(glist[0], EXT_LIST, 1)
        self.c.on(self.list_oid, self._on_list)
        self.info_ver = min(ginfo[1], INFO_MAX)
        self.info = self.c.bind(ginfo[0], INFO, self.info_ver)
        self.c.on(self.info, lambda op, cur, fds: None)

        gmgr = self.c.find_global(MANAGER)
        self.mgr = None
        self.mgr_ver = 0
        if gmgr:
            self.mgr_ver = min(gmgr[1], MANAGER_MAX)
            self.mgr = self.c.bind(gmgr[0], MANAGER, self.mgr_ver)
            self.c.on(self.mgr, self._on_mgr)

        self.seat = None
        sg = self.c.find_global("wl_seat")
        if sg:
            self.seat = self.c.bind(sg[0], "wl_seat", min(sg[1], 2))
            self.c.on(self.seat, lambda op, cur, fds: None)

        self.out_w = self.out_h = 0
        self.outputs: list[int] = []
        for gname, (iface, ver) in list(reg.items()):
            if iface == "wl_output":
                oid = self.c.bind(gname, "wl_output", min(ver, 2))
                self.outputs.append(oid)
                self.c.on(oid, self._on_output)

        self.ws = ext_workspace.WorkspaceClient.bind(self.c)
        self._x = "unset"

        self._pump()   # the toplevel announcements and their ext events
        self._attach()  # one get_cosmic_toplevel per handle
        self._pump()   # state / geometry / workspace for each, when the compositor is quick
        self._await_state()   # ...and the wait for them when it is not (STATE_WAIT)

    def _close_own(self):
        if self._own_conn:
            try:
                self.c.close()
            except OSError:
                pass

    # -- events ---------------------------------------------------------------

    def _on_list(self, op, cur, fds):
        if op == _LIST_EV_TOPLEVEL:
            oid = cur.u32()
            rec = _Top(oid)
            self.tops[oid] = rec
            self.order.append(oid)
            self.c.on(oid, lambda o, c, f, r=rec: self._on_ext(r, o, c))
        # op 1 = finished

    def _on_ext(self, rec: _Top, op, cur):
        if op == _EXT_EV_TITLE:
            rec.title = cur.string()
        elif op == _EXT_EV_APP_ID:
            rec.app_id = cur.string()
        elif op == _EXT_EV_IDENTIFIER:
            rec.identifier = cur.string()
        elif op == _EXT_EV_CLOSED:
            rec.closed = True
        # op _EXT_EV_DONE: the batch boundary; every field is read as it arrives

    def _on_cosmic(self, rec: _Top, op, cur):
        if op == _CH_EV_STATE:
            arr = cur.array()
            rec.states = set(struct.unpack("<%dI" % (len(arr) // 4), arr[:len(arr) // 4 * 4]))
            rec.have_state = True
        elif op == _CH_EV_GEOMETRY:
            cur.u32()   # output
            rec.geometry = (cur.i32(), cur.i32(), cur.i32(), cur.i32())
        elif op in (_CH_EV_WORKSPACE_ENTER, _CH_EV_EXT_WORKSPACE_ENTER):
            # v3 renamed the pair: `workspace_enter` carries a zcosmic_workspace_handle_v1 (deprecated) and
            # `ext_workspace_enter` an ext_workspace_handle_v1. Only the latter is an object this client
            # created, so both are recorded as an oid and the lookup against WorkspaceClient decides.
            rec.workspace = cur.u32()
        elif op in (_CH_EV_WORKSPACE_LEAVE, _CH_EV_EXT_WORKSPACE_LEAVE):
            if rec.workspace == cur.u32():
                rec.workspace = None
        elif op == _CH_EV_CLOSED:
            rec.closed = True
        # _CH_EV_DONE, output_enter/leave: nothing to keep

    def _on_mgr(self, op, cur, fds):
        if op == _MGR_EV_CAPABILITIES:
            arr = cur.array()
            self.caps = set(struct.unpack("<%dI" % (len(arr) // 4), arr[:len(arr) // 4 * 4]))

    def _on_output(self, op, cur, fds):
        if op == 1:  # mode(flags, width, height, refresh)
            flags, w, h = cur.u32(), cur.i32(), cur.i32()
            if flags & 1:
                self.out_w = max(self.out_w, w)
                self.out_h = max(self.out_h, h)

    # -- plumbing -------------------------------------------------------------

    def _pump(self):
        """One roundtrip; the wire's three failure shapes become one line, as on the wlr floor."""
        try:
            self.c.roundtrip()
        except CmdError:
            raise
        except (OSError, RuntimeError, struct.error) as e:
            raise CmdError("cosmic backend: %s" % e) from None

    def _attach(self):
        """`get_cosmic_toplevel` for every handle that has not got one: the ext protocol carries the title,
        the app id and the identifier, and everything else -- state, geometry, workspace -- arrives only on
        the zcosmic handle this creates."""
        for oid in self.order:
            rec = self.tops[oid]
            if rec.cosmic is not None or rec.closed:
                continue
            new_id = self.c.alloc()
            try:
                self.c.send(self.info, _INFO_REQ_GET, [("u", new_id), ("u", rec.ext)])
            except OSError as e:
                raise CmdError("cosmic backend: %s" % e) from None
            rec.cosmic = new_id
            self.by_cosmic[new_id] = rec
            self.c.on(new_id, lambda o, c, f, r=rec: self._on_cosmic(r, o, c))

    def _await_state(self):
        """Read until every attached handle has had a `state` event, or `STATE_WAIT` runs out ON IT.

        The wait is the whole reason `have_state` exists: an empty array is an answer (a window that is
        neither maximized nor activated) and no array at all is silence from a compositor whose refresh is
        still 150 ms away (see STATE_WAIT). Without this a `wdotool getactivewindow` on an idle COSMIC
        session read every window as unfocused and answered `xdo_get_active_window reported an error`.

        The budget is spent per HANDLE and not per call. A `get_cosmic_toplevel` whose
        `ext_foreign_toplevel_handle_v1` cosmic-comp could not resolve gets an EMPTY handle state and no
        events at all [R protocols/toplevel_info.rs:213-219]; `_refresh` runs on every `list()` and every
        `window*` command, so waiting the full STATE_WAIT for that handle again each time would put 0.5 s
        on every listing of that session and 0.5 s on every poll of a `--sync` loop (measured over
        NeverRefresh in tests/test_backend_cosmic.py: 0.504 s, then 0.501 s, then 0.501 s, before the
        give-up flag). So a deadline that passes marks the still-silent handles `waited` and later calls
        skip them; a `state` that turns up afterwards still lands through `_on_cosmic`, and a handle
        attached later -- a window that opened since -- has its own first wait.

        Returns True when every handle answered, False when some handle has been given up on: not an
        error, a listing that gave up on one handle is still a listing, with that window's states left at
        the empty set it started with."""
        deadline = time.monotonic() + STATE_WAIT
        while True:
            silent = [r for r in self.tops.values()
                      if r.cosmic is not None and not r.closed and not r.have_state]
            if not silent:
                return True
            if all(r.waited for r in silent):
                return False
            left = deadline - time.monotonic()
            if left <= 0:
                for r in silent:
                    r.waited = True
                return False
            try:
                self.c.dispatch(timeout=left)
            except (OSError, RuntimeError, struct.error) as e:
                raise CmdError("cosmic backend: %s" % e) from None

    def _refresh(self):
        self._pump()
        self._attach()
        self._pump()
        self._await_state()

    def _ids(self) -> "dict[int, int]":
        """{ext handle oid: window id}, minted from `identifier` in arrival order.

        `mint_map` rather than a comprehension so that the ~1e-6 chance of two live windows hashing to the
        same 30 bits re-mints the second one instead of dropping it out of the listing entirely."""
        live = [self.tops[o] for o in self.order if not self.tops[o].closed]
        minted = mint_map([r.identifier for r in live])
        return {r.ext: minted.get(r.identifier, 0) for r in live}

    def _by_wid(self, wid: int) -> _Top:
        self._refresh()
        for ext, mid in self._ids().items():
            if mid == wid:
                return self.tops[ext]
        raise CmdError("window %d not found" % wid)

    def _need(self, cap: int, op: str):
        """Refuse an operation the manager did not advertise, naming the capability.

        Decision C5.15: gate on the array rather than send the request anyway. `set_fullscreen` did work on
        the live compositor although 5 was not in `[1,2,3,4,6]`, but a client that ignores a capability array
        is a client that will be wrong the first time the array is right."""
        if self.mgr is None:
            raise CmdError("cosmic backend: compositor offers no %s; cannot %s; not yet here, and the "
                           "route is an unsandboxed run of the protocol cosmic-comp already speaks "
                           "(AGENTS.md route 1): the manager is built behind the same sandbox filter as "
                           "the list [R recon2/cosmic/state.rs:749]" % (MANAGER, op))
        if cap not in self.caps:
            self._not_yet(op, "cosmic-comp does not advertise the %s capability; not yet here, and the "
                              "route is a patched cosmic-comp (AGENTS.md route 6), which is where that "
                              "array is built [R recon2/cosmic/state.rs:752-756]"
                              % CAP_NAMES.get(cap, cap))

    def _mgr_send(self, opcode: int, args):
        try:
            self.c.send(self.mgr, opcode, args)
        except OSError as e:
            raise CmdError("cosmic backend: %s" % e) from None
        self._pump()

    def _act(self, rec: _Top, opcode: int, cap: int, op: str, extra=()):
        self._need(cap, op)
        self._mgr_send(opcode, [("u", rec.cosmic)] + list(extra))

    # -- WindowBackend --------------------------------------------------------

    def _rect(self, rec: _Top) -> "tuple[int, int, int, int]":
        """This window's rectangle, or the output's when no `geometry` event ever arrived.

        Silently, the way the wlr floor reports the same fallback: `list()` is what every `wdotool search`,
        `getactivewindow` and `wwmctl -l` runs, and no `geometry` event arrived at all in the nested rig
        [M cosmic.md §4] -- so a warning here would be one line of stderr on every window command of every
        COSMIC session. `geometry_is_floor` is the flag for a caller that wants to say so."""
        if rec.geometry is not None:
            return rec.geometry
        self.geometry_is_floor = True
        return (0, 0, self.out_w, self.out_h)

    def list(self) -> list[Window]:
        self._refresh()
        ids = self._ids()
        active = self._active_workspaces()
        wins = []
        for oid in self.order:
            rec = self.tops[oid]
            if rec.closed:
                continue
            x, y, w, h = self._rect(rec)
            desktop = self._desktop_of(rec)
            wins.append(Window(
                id=ids.get(oid, 0),
                title=rec.title,
                class_=rec.app_id,
                pid=0,                       # the protocol carries none
                x=x, y=y, w=w, h=h,
                focused=ST_ACTIVATED in rec.states,
                # On another workspace is invisible -- but only where some workspace claims to be the active
                # one. The live cosmic-comp published both of its workspaces with an EMPTY state array
                # [M cosmic.md §4: `ws {'name': '1', ... 'state': []}` twice], and filtering on that would
                # empty the whole listing on the one session this was measured against.
                visible=(ST_MINIMIZED not in rec.states
                         and (not active or rec.workspace is None or rec.workspace in active)),
                desktop=desktop,
            ))
        return wins

    def _ws_handles(self) -> "list[int]":
        """The `ext_workspace_handle_v1` oids in desktop-number order, or [].

        The order is `ext_workspace.WorkspaceClient._rows()`: every group's workspaces contiguous, the
        groups in the order the compositor first announced one of their workspaces. cosmic-comp publishes
        one `ext_workspace_group_handle_v1` PER OUTPUT, which is the session that ordering exists for --
        measured on the fedora44-cosmic golden with two heads, 2026-09-11: three workspaces, `1`
        (coordinates [1]) and `2` ([2]) in group 4278190084 and `1` ([1]) in group 4278190087, with BOTH
        ones active, one per head [M goal2/recon/flavors.md 5, goal2/ci/rig-fedora44-cosmic.log]. It lived
        here as a COSMIC-local copy until the client took it (goal2/requests-batch-5.md, batch 17); labwc
        and Budgie publish a single group and nothing changed for them either way.

        The oid is what this backend needs and the public `backend.Workspace` deliberately does not carry:
        `workspace_enter` names a workspace by oid, and `move_to_ext_workspace` takes one."""
        return [] if self.ws is None else self.ws.handles()

    def _active_workspaces(self) -> "set[int]":
        """The handle oids of the workspaces that say they are active; empty when none does.

        A set and not an index: with one group per output, several workspaces are on screen at once."""
        return set() if self.ws is None else self.ws.active_handles()

    def _desktop_of(self, rec: _Top) -> int:
        if rec.workspace is None:
            return -1
        handles = self._ws_handles()
        return handles.index(rec.workspace) if rec.workspace in handles else -1

    def activate(self, wid: int):
        rec = self._by_wid(wid)
        if self.seat is None:
            raise CmdError("cosmic backend: %s" % NO_SEAT)
        self._act(rec, _MGR_ACTIVATE, CAP_ACTIVATE, "windowactivate", [("u", self.seat)])

    def close(self, wid: int):
        self._act(self._by_wid(wid), _MGR_CLOSE, CAP_CLOSE, "windowclose")

    def minimize(self, wid: int):
        self._act(self._by_wid(wid), _MGR_SET_MINIMIZED, CAP_MINIMIZE, "windowminimize")

    def map(self, wid: int):
        self._act(self._by_wid(wid), _MGR_UNSET_MINIMIZED, CAP_MINIMIZE, "windowmap")

    def unmap(self, wid: int):
        self._act(self._by_wid(wid), _MGR_SET_MINIMIZED, CAP_MINIMIZE, "windowunmap")

    def set_state(self, wid: int, state: str, action: int):
        rec = self._by_wid(wid)
        if state == "FULLSCREEN":
            on = action == 1 or (action == 2 and ST_FULLSCREEN not in rec.states)
            self._need(CAP_FULLSCREEN, "windowstate FULLSCREEN")
            if on:
                # set_fullscreen(toplevel, output) -- a null output means "wherever it is"
                self._mgr_send(_MGR_SET_FULLSCREEN, [("u", rec.cosmic), ("u", 0)])
            else:
                self._mgr_send(_MGR_UNSET_FULLSCREEN, [("u", rec.cosmic)])
            return None
        if state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            on = action == 1 or (action == 2 and ST_MAXIMIZED not in rec.states)
            self._act(rec, _MGR_SET_MAXIMIZED if on else _MGR_UNSET_MAXIMIZED,
                      CAP_MAXIMIZE, "windowstate %s" % state)
            return None
        if state == "STICKY":
            on = action == 1 or (action == 2 and ST_STICKY not in rec.states)
            if self.mgr_ver < 3:
                self._not_yet("windowstate STICKY",
                              OLD_MANAGER % (MANAGER, self.mgr_ver, "set_sticky", 3))
            self._act(rec, _MGR_SET_STICKY if on else _MGR_UNSET_STICKY,
                      CAP_STICKY, "windowstate STICKY")
            return None
        if state == "HIDDEN":
            on = action == 1 or (action == 2 and ST_MINIMIZED not in rec.states)
            self._act(rec, _MGR_SET_MINIMIZED if on else _MGR_UNSET_MINIMIZED,
                      CAP_MINIMIZE, "windowstate HIDDEN")
            return None
        self._not_yet("windowstate %s" % state, NO_SUCH_STATE)

    def _not_yet(self, op: str, why: str):
        """A capability gap, its cause and the route that would close it, as one CmdError.

        The prefix is what `_unsupported` has always printed and what vm/live-smoke.d/cosmic.sh greps for;
        the sentence after the colon is the half AGENTS.md asks for -- what is missing, and the lowest rung
        that would fetch it."""
        err = CmdError("%s is not supported by the cosmic backend: %s" % (op, why))
        err.unsupported = True
        raise err

    def _no_geometry(self, op: str):
        self._not_yet(op, NO_GEOMETRY)

    def move_window(self, wid: int, x: int, y: int):
        self._no_geometry("windowmove")

    def resize(self, wid: int, w: int, h: int):
        self._no_geometry("windowsize")

    def raise_(self, wid: int):
        self._no_geometry("windowraise")

    def lower(self, wid: int):
        self._no_geometry("windowlower")

    # -- desktops -------------------------------------------------------------

    def get_desktop(self) -> int:
        """The first active workspace in `_ws_handles()` order, or -1.

        The first and not the only one: with a group per output, every head's current workspace says it is
        active, and wwmctl/core.py takes the first active row as the current desktop and prints `*` on each
        (backend_hypr's `_active_workspace_id` carries the same sentence for Hyprland's per-monitor
        workspaces). `WorkspaceClient.active_index()` is that same first-active scan over the same rows."""
        if self.ws is None:
            self._not_yet("get_desktop", NO_WORKSPACES)
        self._pump()
        return self.ws.active_index()

    def set_desktop(self, n: int):
        """`activate` on desktop `n`'s own handle, then the manager's `commit`.

        ext-workspace is double-buffered: `activate` alone changes nothing and `commit` applies the batch
        [M recon2/labwc.md §6a]. `WorkspaceClient.activate` sends exactly that pair and counts in the same
        rows `_ws_handles()` does, so this is one call again; it went out by hand only while the grouped
        order lived in this file. False is both refusals the client has -- an index off the end and a
        workspace whose capabilities do not carry `activate` -- and both are the one sentence
        `vm/live-smoke.d/cosmic.sh` greps for. The activate is pumped twice: once to put the requests on
        the wire, once to read the `state` the compositor sends back, which is up to 150 ms later
        (STATE_WAIT's paragraph)."""
        if self.ws is None:
            self._not_yet("set_desktop", NO_WORKSPACES)
        self._pump()
        handles = self._ws_handles()
        try:
            ok = 0 <= n < len(handles) and self.ws.activate(n)
        except OSError as e:
            raise CmdError("cosmic backend: %s" % e) from None
        if not ok:
            raise CmdError("cosmic backend: cannot activate workspace %d" % n)
        self._pump()
        self._await_workspace(handles[n])

    def _await_workspace(self, oid: int):
        """Read until the workspace `oid` says it is active, or `STATE_WAIT` runs out.

        The same wait as `_await_state` and for the same reason: cosmic-comp answers the commit by
        switching the shell and sends the new `state` from its rate-limited refresh, so a `set_desktop`
        that returned on the roundtrip alone left the next `get_desktop` -- a second process -- reading the
        workspace bits from before the switch."""
        deadline = time.monotonic() + STATE_WAIT
        while True:
            rec = self.ws.workspaces.get(oid)
            if rec is None or rec.state & ext_workspace.STATE_ACTIVE:
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                self.c.dispatch(timeout=left)
            except (OSError, RuntimeError, struct.error) as e:
                raise CmdError("cosmic backend: %s" % e) from None

    def num_desktops(self) -> int:
        if self.ws is None:
            self._not_yet("get_num_desktops", NO_WORKSPACES)
        self._pump()
        return self.ws.count()

    def workspaces(self):
        """The `wwmctl -d` rows, in `_ws_handles()` order. `work_area` stays (0,0,0,0): the protocol carries
        no geometry, and a group may cover several outputs."""
        if self.ws is None:
            return None
        self._pump()
        return self.ws.workspace_list()

    def set_window_desktop(self, wid: int, n: int):
        """`move_to_ext_workspace`, which is the v4 spelling of the capability the manager advertises as 6.

        cosmic-comp's own handler for the deprecated `move_to_workspace` (opcode 10) is an empty arm --
        `zcosmic_toplevel_manager_v1::Request::MoveToWorkspace { .. } => {}` -- while `MoveToExtWorkspace`
        resolves the handle and moves the window [R recon2/cosmic/tlmgmt.rs:251-263]. Its capability array
        still says 6 and not 8 [R state.rs:752-756], so the gate is 6 and the request is the one that works;
        below manager v4 there is no request this client can send, because the deprecated one wants a
        `zcosmic_workspace_handle_v1` from a workspace protocol this backend does not bind.

        Any `wl_output` will do, and `outputs[0]` is not a multi-head guess: cosmic-comp's dispatcher
        refuses the request unless `Output::from_resource(&output)` is Some
        [R recon2/cosmic/tlmgmt.rs:255-262] and its handler then ignores the value (`_output: Output`,
        R toplevel_management.rs:144-149) -- the
        workspace handle is what decides where the window lands."""
        if self.ws is None:
            self._not_yet("set_desktop_for_window", NO_WORKSPACES)
        rec = self._by_wid(wid)
        self._need(CAP_MOVE_TO_WORKSPACE, "set_desktop_for_window")
        if self.mgr_ver < 4:
            self._not_yet("set_desktop_for_window",
                          OLD_MANAGER % (MANAGER, self.mgr_ver, "move_to_ext_workspace", 4))
        if not self.outputs:
            # Not a rung on the ladder: `wl_output` is core and every COSMIC session has one, so this is
            # our registry pass (above) having bound none, which is ours to look at. The sentence says
            # that and does not invent a route for it.
            self._not_yet("set_desktop_for_window",
                          "this client bound no wl_output during its registry pass and cosmic-comp's "
                          "dispatcher drops the request without one [R recon2/cosmic/tlmgmt.rs:255-262]")
        handles = self._ws_handles()
        if not 0 <= n < len(handles):
            raise CmdError("cosmic backend: no workspace %d" % n)
        self._mgr_send(_MGR_MOVE_TO_EXT_WORKSPACE,
                       [("u", rec.cosmic), ("u", handles[n]), ("u", self.outputs[0])])

    def display_size(self) -> tuple[int, int]:
        if not self.out_w or not self.out_h:
            raise CmdError("cosmic backend: no wl_output mode seen")
        return self.out_w, self.out_h

    # -- XWayland ids ---------------------------------------------------------
    # `views()`, the X connection and the matcher are `XPlaneViews`, shared with the wlr floor: neither
    # protocol carries an X id, a pid or a rectangle, so the join is the same code with the same rules.
    # Only the state bits below are COSMIC's own -- it has a fifth, STICKY, which wlr has no word for.

    def _view_flags(self) -> "list[dict]":
        """The `View` state flags for each row of `list()`, in the same order.

        wxprop reads exactly these off the View: `visible` from `minimized`/`hidden`, `fullscreen_mode` from
        `fullscreen`, and `_NET_WM_STATE_HIDDEN`/`_NET_WM_STATE_STICKY` off that node [wxprop/core.py:171-176,
        545]. `minimized` is the state bit rather than `not w.visible`, because a window can also be
        invisible here for being on another workspace, and that is not what HIDDEN means."""
        out = []
        for oid in self.order:
            rec = self.tops[oid]
            if rec.closed:
                continue
            mx = ST_MAXIMIZED in rec.states
            out.append({"minimized": ST_MINIMIZED in rec.states,
                        "fullscreen": ST_FULLSCREEN in rec.states,
                        "maximized_h": mx, "maximized_v": mx,
                        "sticky": ST_STICKY in rec.states})
        return out
