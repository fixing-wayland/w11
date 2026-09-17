"""Wayfire window backend over Wayfire's own JSON IPC (`plugins = ipc ipc-rules`).

Framing: a native-endian int32 length and a JSON body, both ways -- the recorded ping went out as the four
bytes `$\x00\x00\x00` followed by the 0x24-byte body `{"method": "stipc/ping", "data": {}}`
[M recon2/wayfire.md §1.2]. There is no message type and no magic, so a request is just its own method name.

Wayfire ships the IPC plugins in the base package and loads none of them by default: the stock plugin list
[M recon2/wayfire.md §1.1] carries `foreign-toplevel` but not `ipc`, so a stock Wayfire is a wlr-floor
compositor and the four tools land on `backend_wlr` there. What this backend buys over that floor is
everything the floor refuses: real geometry (the floor reported the output rectangle for every window),
pids, moving, resizing, lowering, desktops, `selectwindow` and the WM_CLASS *instance*
[M recon2/wayfire.md §2.3, §2.4].

Ids are the view's own `id` -- small, dense, and the shape sway's node ids have -- so nothing is minted here.

Desktops are Wayfire's 3x3 viewport grid per output, flattened `y * grid_width + x`
[M recon2/wayfire.md §1.2: `window-rules/list-outputs` carries `workspace {x, y, grid_width: 3,
grid_height: 3}`]. A view's geometry is expressed relative to the *current* viewport, not to the wset origin:
with the viewport at (1, 0), a view sitting on viewport (0, 0) of a 1280-wide output read back
`x: -882` (re-measured on this box against wayfire 0.10.0 headless), which is what the `_viewport()`
arithmetic below is for."""

import json
import select
import socket
import struct
import time

from w11common import session
from w11common.errors import CmdError
from hacks.window.backend import View, Window, WindowBackend, Workspace
from hacks.window.xid_match import match_xids

# Deadline for the command socket only, and the same 10 s the sway backend uses: every reply is built inside
# Wayfire's own event loop, so silence means the compositor is wedged rather than busy.
IPC_TIMEOUT = 10.0

#: the method the api gate demands. `ipc` alone answers `{"methods": ["list-methods"]}` and
#: `No such method found!` to everything else (measured on this box against wayfire 0.10.0 started with
#: `plugins = ipc`), and Wayfire 0.8 kept the view methods in `stipc` under other names
#: [R recon2/wayfire.md §3.3], so the presence of this one name is what says "0.9 or newer, with ipc-rules".
GATE_METHOD = "window-rules/list-views"

#: The gate's refusal.  Not a NOT-YET: nothing is missing that we would have to route around, because the
#: window half is already there and two words in wayfire.ini switch it on -- so the sentence names the rung
#: it is standing on (route 2, Wayfire's own IPC) and what it costs the user, which is one config line and a
#: restart.  Batch 19 declined to touch it for that reason and batch 23 kept the substance, naming the rung
#: so a reader does not have to work out which one this is.  `this Wayfire's IPC has no %s` stays first: no
#: step file greps it (checked against vm/live-smoke.d/ on 2026-09-09), but it is the half that says what
#: happened.
NO_IPC_RULES = ("wayfire backend: this Wayfire's IPC has no %s: the window half is Wayfire's own IPC "
                "(AGENTS.md route 2) and it is one config line away -- `plugins = ipc ipc-rules` in "
                "wayfire.ini on Wayfire 0.9 or newer -- at the cost of restarting Wayfire to load it")

#: wlroots' edge bits, as `tiled-edges` reports them: TOP 1, BOTTOM 2, LEFT 4, RIGHT 8. Measured against the
#: grid plugin on this box (wayfire 0.10.0): `grid/slot_l` -> 7, `slot_t` -> 13, `slot_r` -> 11, `slot_b` ->
#: 14, `slot_c` -> 15. So a full-height window has both of TILED_V and a full-width one both of TILED_H.
TILED_V = 3
TILED_H = 12
TILED_ALL = 15


#: Wayfire's answer to a method whose plugin is not loaded, byte for byte off the live socket
#: [M recon2/wayfire.md §1.2]. A Wayfire started with `plugins = ipc` answers exactly this to `list-methods`
#: in 0.8 and every window method in 0.9 (measured on this box against wayfire 0.10.0).
NO_METHOD = "No such method found!"


def _lost(e) -> CmdError:
    """Any wire-level failure of the Wayfire IPC socket, as one line."""
    return CmdError("wayfire backend: lost the connection to the compositor (%s)" % e)


def _wedged(timeout: "float | None" = None) -> CmdError:
    """Connected and not answering: the kernel accepts on a listening socket whose owner is stuck in its own
    event loop, so a wedged compositor looks exactly like a healthy one until the first read.

    The number in the sentence is the deadline that actually elapsed, so a reader running on 2.0 s
    (xkbmap.WayfireLayouts) does not tell the user it waited ten."""
    return CmdError("wayfire backend: no answer from the compositor within %gs "
                    "(it is not responding)" % (IPC_TIMEOUT if timeout is None else timeout))


def _error_line(reply: dict, method: str) -> CmdError:
    """Wayfire's two recorded error shapes as one line each [M recon2/wayfire.md §1.2].

    `{"error": "No such method found!", "method": "..."}` names the method in a field of its own; the handler
    shape (`Error during execution of the handler for method "window-rules/view-info": Missing "id"`) names it
    inside the sentence, so repeating it there would print it twice.

    The returned error carries `.no_method`, which is what the api gate reads: only this one shape means "the
    plugin behind that method is not loaded". Every other failure of `list-methods` -- a wedged compositor, a
    closed socket, a body that is not JSON -- already has a line of its own and must keep it."""
    err = str(reply.get("error"))
    named = reply.get("method")
    if named and str(named) not in err:
        e = CmdError("wayfire: %s (%s)" % (err, named))
    else:
        e = CmdError("wayfire: %s" % err)
    e.no_method = err == NO_METHOD
    return e


class _WayfireIPC:
    """The client. One connection for commands, another for every `watch()`.

    Two connections because `window-rules/events/watch` turns the connection it arrives on into an event
    stream: the subscribe is answered `{"result": "ok"}` and every event afterwards is written to that same
    socket (measured on this box: a foot window opening produced `view-set-output`, `view-title-changed`,
    `view-app-id-changed`, `view-mapped`, `view-focused`, `view-geometry-changed`, `view-unmapped` in that
    order). A command sharing it would have to read its reply out from behind an unbounded queue of those."""

    def __init__(self, sockpath: str, timeout: "float | None" = None):
        """`timeout` bounds the connect and every read on it; None is IPC_TIMEOUT, read at call time so the
        module attribute stays the knob a test can turn.  IPC_TIMEOUT (10.0 s) is right for a command a user
        is waiting on and wrong for `xkbmap.WayfireLayouts`, which runs inside `fetch()` while the daemon
        holds its lock: there a wedged Wayfire would stall every `type` for ten seconds, where KwinLayouts,
        GnomeInputSources, CinnamonInputSources and HyprLayouts all bound it at 2.0 s.  That reader passes
        WAYFIRE_TIMEOUT = 2.0 (requests-batch-6.md, from batch 10)."""
        self.sockpath = sockpath
        self.timeout = IPC_TIMEOUT if timeout is None else timeout
        self.sock = self._connect(self.timeout)

    def _connect(self, timeout: "float | None" = None) -> socket.socket:
        """A fresh connection, carrying `timeout` once it is up; the connect gets the same deadline.

        The retry loop is the sway backend's, for the reason measured there: a compositor wedged inside its
        event loop still has a listening socket, the kernel queues connections for it, and a connect with no
        deadline of its own blocks for ever once the backlog fills -- which is why the connect is bounded by
        the caller's number and not by IPC_TIMEOUT."""
        bound = IPC_TIMEOUT if timeout is None else timeout
        deadline = time.monotonic() + bound
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                s.connect(self.sockpath)
                break
            except (TimeoutError, BlockingIOError):
                # both are OSError subclasses: this arm has to come first
                s.close()
                if time.monotonic() >= deadline:
                    raise _wedged(bound) from None
                time.sleep(0.01)
            except OSError as e:
                s.close()
                raise CmdError("wayfire backend: cannot connect to %s: %s" % (self.sockpath, e)) from None
        s.settimeout(timeout)
        return s

    @staticmethod
    def frame(obj) -> bytes:
        """One message on the wire: the int32 length, then the JSON body."""
        body = json.dumps(obj).encode()
        return struct.pack("<i", len(body)) + body

    @staticmethod
    def _read_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise CmdError("wayfire backend: IPC connection closed")
            buf += chunk
        return buf

    @classmethod
    def _send(cls, sock, method: str, data: dict):
        try:
            sock.sendall(cls.frame({"method": method, "data": data}))
        except TimeoutError:
            # the socket carries the deadline the caller chose (2.0 s for xkbmap's reader, IPC_TIMEOUT for a
            # command), so it is the socket and not the module constant that says how long we waited
            raise _wedged(sock.gettimeout()) from None
        except OSError as e:
            raise _lost(e) from None

    @classmethod
    def _recv(cls, sock):
        # A session that ends mid-chain gives ECONNRESET on the read, a length prefix that is not followed by
        # its body gives the closed-connection line, and a body that is not JSON gives ValueError. All three
        # are one line to the user, not a traceback; a compositor that is there and silent is a different
        # event and says so.
        try:
            (length,) = struct.unpack("<i", cls._read_exact(sock, 4))
            if length < 0 or length > 64 << 20:
                raise CmdError("wayfire backend: bad IPC framing (length %d)" % length)
            return json.loads(cls._read_exact(sock, length).decode("utf-8", "replace"))
        except TimeoutError:
            # TimeoutError is an OSError: this arm has to come first, or a compositor that is merely wedged
            # reads as one that has gone.
            raise _wedged(sock.gettimeout()) from None
        except (OSError, struct.error, ValueError) as e:
            raise _lost(e) from None

    def call(self, method: str, data: "dict | None" = None, **kw):
        """One request, one reply; an `{"error": ...}` reply is a CmdError.

        `data` is a dict rather than only keywords because Wayfire's own argument names are not all valid
        Python identifiers and are not consistent either: `wm-actions/*` take `view_id` with an underscore,
        `vswitch/set-workspace` takes `output-id` and `vswitch/send-view` takes `view-id`, both with a hyphen
        -- `output_id` is answered `Missing "output-id"` [M recon2/wayfire.md §1.2, re-measured here for
        `send-view`]. Keywords stay for the many methods that take `id` and `state`."""
        payload = dict(data or {})
        payload.update(kw)
        self._send(self.sock, method, payload)
        reply = self._recv(self.sock)
        if isinstance(reply, dict) and "error" in reply:
            raise _error_line(reply, method)
        return reply

    def watch(self, timeout: "float | None" = None):
        """Wayfire's event stream on a connection of its own: yields each event dict until `timeout` seconds
        of silence (None waits for ever, which is what both callers do)."""
        s = self._connect()
        try:
            self._send(s, "window-rules/events/watch", {})
            reply = self._recv(s)
            if not (isinstance(reply, dict) and reply.get("result") == "ok"):
                raise CmdError("wayfire backend: subscribe to window events failed")
            while True:
                if timeout is not None and not select.select([s], (), (), timeout)[0]:
                    return
                event = self._recv(s)
                if isinstance(event, dict):
                    yield event
        finally:
            s.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class WayfireBackend(WindowBackend):
    name = "wayfire"
    #: what Wayfire's own Xwayland check window says: wlroots' string, which Wayfire never overwrites
    #: [M recon2/wayfire.md §2.4]. Only the no-X case reads it; with an X plane wwmctl -m reads the window.
    wm_name = "wlroots wm"

    def __init__(self, sockpath: "str | None" = None):
        self.sockpath = sockpath or session.find_wayfire_socket()
        if not self.sockpath:
            raise CmdError("wayfire backend: no Wayfire IPC socket found ($WAYFIRE_SOCKET unset and no "
                           "wayfire-*.socket in any runtime dir)")
        self.ipc = _WayfireIPC(self.sockpath)
        try:
            self.methods = self._methods()
        except CmdError:
            # a wire failure asking for the table is that failure's own line, and the socket goes back first
            self.ipc.close()
            raise
        if GATE_METHOD not in self.methods:
            # The gate, not a fall-through: a Wayfire whose ipc plugin is loaded and whose ipc-rules is not
            # answers every window method `No such method found!`, and letting that reach the caller one
            # command at a time would blame the tool for a two-word config line. The socket goes back
            # before the refusal.
            #
            # `.api_gate` is what makes detect() STOP here instead of falling through to the wlr floor,
            # which is plan A 1.2's sentence: "a socket that exists with no ipc-rules gives that line
            # rather than falling through to wlr".  Wayfire does advertise the wlr protocol, so the
            # fall-through worked -- it just left the user on the capability floor with no idea that two
            # words in wayfire.ini buy the window half.  A socket that is merely stale or unreachable
            # raises a wire error without this flag and is still swallowed, which is the case the
            # fall-through was protecting.
            self.ipc.close()
            err = CmdError(NO_IPC_RULES % GATE_METHOD)
            err.api_gate = True
            raise err
        self._x = "unset"     # the X11 connection, opened at most once per process

    def _methods(self) -> "frozenset[str]":
        """The method table, for the gate and for the two optional plugins (`stipc`, `grid`)."""
        try:
            reply = self.ipc.call("list-methods")
        except CmdError as e:
            # A socket that answers `No such method found!` to list-methods itself is not Wayfire 0.9's IPC,
            # and an empty table sends the caller into the gate's refusal. Nothing else may: a compositor
            # that is wedged, one that closed the socket and a body that is not JSON each carry their own
            # line, and swallowing those would tell a user whose wayfire.ini is right to go and edit it.
            if not getattr(e, "no_method", False):
                raise
            return frozenset()
        names = reply.get("methods") if isinstance(reply, dict) else None
        return frozenset(str(m) for m in names or ())

    def _need(self, method: str, plugin: str, what: str):
        """Refuse an operation whose plugin is not loaded, naming the plugin.

        Wayfire's `[core] plugins` line REPLACES the default list rather than adding to it, and the methods a
        socket answers are exactly the loaded plugins' own. Measured on this box against wayfire 0.10.0: with
        `plugins = ipc ipc-rules` the table is 23 methods -- every `window-rules/*` read and write, plus
        `wayfire/*` and `input/*` -- and carries no `grid/*`, no `vswitch/*` and no `wm-actions/*` at all.
        `wm-actions` is not even in the stock list (`/usr/share/wayfire/metadata/core.xml` line 10), so
        minimize on a default Wayfire is a missing plugin and not a missing feature; `No such method found!`
        is a true sentence about that and a useless one."""
        if method not in self.methods:
            raise CmdError("wayfire: %s needs Wayfire's `%s` plugin "
                           "(add it to the `plugins` line in wayfire.ini)" % (what, plugin))

    # -- views --------------------------------------------------------------

    def _raw_views(self) -> "list[dict]":
        """Every mapped toplevel, oldest-focused first.

        The sort is the stacking order this backend can honestly offer: `list-views` answers in view-id order
        and not in stacking order (measured here -- focusing either of two views left the order alone), and
        `last-focus-timestamp` is the only ordering Wayfire publishes at all. backend.hit_test() reads list()
        as bottom-to-top, so the most recently focused view has to come last."""
        rows = self.ipc.call("window-rules/list-views")
        if not isinstance(rows, list):
            raise CmdError("wayfire backend: window-rules/list-views came back malformed")
        out = [v for v in rows
               if isinstance(v, dict) and v.get("role") == "toplevel" and v.get("mapped")]
        out.sort(key=lambda v: (int(v.get("last-focus-timestamp") or 0), int(v.get("id") or 0)))
        return out

    def _view(self, wid: int) -> dict:
        """One view record, or the standard not-found line.

        Out of the listing rather than out of `window-rules/view-info`, which answers the same record for the
        same round trip: an id that is gone has to read the same whichever operation was asked for it, and
        Wayfire spells that five different ways depending on which plugin is asked -- `no such view` from
        window-rules, `toplevel view id not found!` from wm-actions, `Invalid view or view not toplevel!` from
        vswitch, `view not found` from configure-view, `view id not found!` from grid (all measured on this
        box against wayfire 0.10.0). So every operation checks the id here first, and the caller sees one
        sentence, the same one every other backend gives."""
        for view in self._raw_views():
            if int(view.get("id") or 0) == int(wid):
                return view
        raise CmdError("window %d not found" % wid)

    # -- outputs and the viewport grid --------------------------------------

    def _outputs(self) -> "list[dict]":
        rows = self.ipc.call("window-rules/list-outputs")
        if not isinstance(rows, list) or not rows:
            raise CmdError("wayfire backend: the compositor lists no outputs")
        return [o for o in rows if isinstance(o, dict)]

    def _focused_output(self) -> dict:
        """The output whose viewport grid the desktop commands address."""
        try:
            reply = self.ipc.call("window-rules/get-focused-output")
            info = reply.get("info") if isinstance(reply, dict) else None
            if isinstance(info, dict) and info.get("id") is not None:
                return info
        except CmdError:
            pass
        return self._outputs()[0]

    @staticmethod
    def _grid(out: dict) -> tuple[int, int, int, int]:
        """(grid_width, grid_height, current x, current y) of one output's wset."""
        ws = out.get("workspace") or {}
        return (max(1, int(ws.get("grid_width") or 1)), max(1, int(ws.get("grid_height") or 1)),
                int(ws.get("x") or 0), int(ws.get("y") or 0))

    @classmethod
    def _viewport(cls, view: dict, out: dict) -> int:
        """The flat desktop index of `view` on its own output, -1 for a sticky view (which is on all of them).

        Geometry is relative to the output's *current* viewport, so the view's own viewport is the current one
        plus however many screens away its centre sits. The centre and not the origin: a window straddling the
        boundary belongs to the viewport most of it is on, which is also what Wayfire's own expo shows.

        The screen the arithmetic steps by is the output's `geometry` and not its `workarea`, which is where
        this departs from the plan's wording. Two measured reasons: a viewport is the whole output and not
        the part a panel leaves free, so a workarea-sized step would drift by the panel's height per cell;
        and the two rectangles are not even in the same coordinates -- the recorded second head sits at
        `geometry.x: 1280` with `workarea.x: 0` [M recon2/wayfire.md §1.2], so subtracting the workarea
        origin on a multi-head layout would put every view on that head one screen to the left.
        `workspaces()` below does use `workarea`, which is what a work area means."""
        if view.get("sticky"):
            return -1
        gw, gh, cx, cy = cls._grid(out)
        geo = view.get("geometry") or {}
        rect = out.get("geometry") or {}
        ow, oh = int(rect.get("width") or 0), int(rect.get("height") or 0)
        if ow <= 0 or oh <= 0:
            return -1
        mx = int(geo.get("x") or 0) + int(geo.get("width") or 0) // 2 - int(rect.get("x") or 0)
        my = int(geo.get("y") or 0) + int(geo.get("height") or 0) // 2 - int(rect.get("y") or 0)
        vx = min(gw - 1, max(0, cx + mx // ow))
        vy = min(gh - 1, max(0, cy + my // oh))
        return vy * gw + vx

    # -- WindowBackend ------------------------------------------------------

    def _window(self, view: dict, outs: "dict[int, dict]") -> Window:
        out = outs.get(int(view.get("output-id") or -1)) or {}
        geo = view.get("geometry") or {}
        desktop = self._viewport(view, out) if out else -1
        gw, _gh, cx, cy = self._grid(out)
        current = cy * gw + cx
        return Window(
            id=int(view.get("id") or 0),
            title=view.get("title") or "",
            class_=view.get("app-id") or "",
            # Wayfire publishes no WM_CLASS instance on the view; views() fills the real pair in for the
            # XWayland windows it can match, and `search --classname` falls back to class_ for the rest.
            instance="",
            pid=int(view.get("pid") or 0),
            x=int(geo.get("x") or 0), y=int(geo.get("y") or 0),
            w=int(geo.get("width") or 0), h=int(geo.get("height") or 0),
            focused=bool(view.get("activated")),
            # minimized keeps `mapped` true (measured here: set-minimized left mapped 1, minimized 1), so
            # visibility is the three facts together -- mapped, not minimized, and on the viewport in front.
            visible=bool(view.get("mapped")) and not view.get("minimized")
            and (desktop == current or desktop < 0),
            desktop=desktop,
        )

    def list(self) -> "list[Window]":
        outs = {int(o.get("id") or -1): o for o in self._outputs()}
        return [self._window(v, outs) for v in self._raw_views()]

    def find(self, wid: int) -> Window:
        view = self._view(wid)
        outs = {int(o.get("id") or -1): o for o in self._outputs()}
        return self._window(view, outs)

    def activate(self, wid: int):
        self._view(wid)
        self.ipc.call("window-rules/focus-view", id=int(wid))

    def close(self, wid: int):
        self._view(wid)
        self.ipc.call("window-rules/close-view", id=int(wid))

    def move_window(self, wid: int, x: int, y: int):
        view = self._view(wid)
        geo = view.get("geometry") or {}
        self._configure(wid, x, y, int(geo.get("width") or 0), int(geo.get("height") or 0))

    def resize(self, wid: int, w: int, h: int):
        view = self._view(wid)
        geo = view.get("geometry") or {}
        self._configure(wid, int(geo.get("x") or 0), int(geo.get("y") or 0), w, h)

    def _configure(self, wid: int, x: int, y: int, w: int, h: int):
        """One `configure-view`, which is both the move and the resize.

        No "float it first" refusal, unlike sway and Hyprland: Wayfire's default plugin list carries no tiler,
        every window floats, and a view that the grid plugin *had* tiled still took the box -- a maximized
        foot (`tiled-edges` 15) moved to `40,50 500x400` on request and kept the tiled flag (measured here).
        The size that comes back is the client's own quantisation, not Wayfire's: a floating foot asked for
        500x400 settled at 498x390 [M recon2/wayfire.md §3.3], exactly as it does on sway."""
        self.ipc.call("window-rules/configure-view", id=int(wid),
                      geometry={"x": int(x), "y": int(y), "width": int(w), "height": int(h)})

    def minimize(self, wid: int):
        self._set_minimized(wid, True)

    def unmap(self, wid: int):
        self._set_minimized(wid, True)

    def map(self, wid: int):
        self._set_minimized(wid, False)

    def _set_minimized(self, wid: int, state: bool):
        self._need("wm-actions/set-minimized", "wm-actions", "minimizing")
        self._view(wid)
        self.ipc.call("wm-actions/set-minimized", view_id=int(wid), state=bool(state))

    def is_mapped(self, wid: int) -> bool:
        """X11 map state, as close as Wayfire tells it: not minimized. `mapped` itself stays true on a
        minimized view, so it is the wrong field to wait on (measured here)."""
        return not self._view(wid).get("minimized")

    def raise_(self, wid: int):
        """Wayfire's IPC has no plain raise yet, and `wm-actions/set-always-on-top` is a state and not a
        raise -- it
        would leave the window pinned above everything for the rest of the session. Focusing raises the view
        inside its layer, which is the sway backend's answer for a floating window and is honest here for
        every window, because a Wayfire with no tiler has nothing but floating ones."""
        self.activate(wid)

    def lower(self, wid: int):
        """A real lower, which sway has never had [M recon2/wayfire.md §3.3]."""
        self._need("wm-actions/send-to-back", "wm-actions", "lowering a window")
        self._view(wid)
        self.ipc.call("wm-actions/send-to-back", view_id=int(wid), state=True)

    def set_state(self, wid: int, state: str, action: int) -> "str | None":
        view = self._view(wid)
        if state in ("FULLSCREEN", "STICKY", "HIDDEN", "ABOVE"):
            return self._wm_action(wid, view, state, action)
        if state == "MAXIMIZED":
            return self._maximize(wid, view, action)
        if state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            # maximize_pair_state() folds the two axes into MAXIMIZED for every caller that is handed both;
            # one axis on its own has no expression here, because the grid plugin's slots are halves of the
            # screen and not axes (`grid/slot_t` is the top half, tiled-edges 13, not "maximized vertically").
            raise CmdError("wayfire: %s alone is not done yet (Wayfire's grid maximizes both axes at once; "
                           "ask for MAXIMIZED_VERT and MAXIMIZED_HORZ together); the route is "
                           "`window-rules/configure-view` on this same IPC (AGENTS.md route 2), at the "
                           "cost of a saved rectangle to restore, because a geometry is not a state and "
                           "the view would not report itself maximized" % state)
        raise CmdError("windowstate %s is not supported by the wayfire backend: no Wayfire plugin has a "
                       "method for it; not yet here, and the route is a patched Wayfire plugin "
                       "(AGENTS.md route 6), which is where `wm-actions` keeps the others" % state)

    _WM_ACTIONS = {"FULLSCREEN": ("wm-actions/set-fullscreen", "fullscreen"),
                   "STICKY": ("wm-actions/set-sticky", "sticky"),
                   "HIDDEN": ("wm-actions/set-minimized", "minimized"),
                   "ABOVE": ("wm-actions/set-always-on-top", None)}

    def _wm_action(self, wid: int, view: dict, state: str, action: int) -> None:
        method, field = self._WM_ACTIONS[state]
        self._need(method, "wm-actions", "windowstate %s" % state)
        if action == 2:
            if field is None:
                # Wayfire takes always-on-top and never reports it back: the view record's `layer` stayed
                # `workspace` on both sides of a set-always-on-top (measured here), so a toggle would have to
                # guess. Refuse rather than flip a state we cannot read.
                raise CmdError("wayfire: toggling ABOVE is not done yet (Wayfire does not report the "
                               "always-on-top state back; use --add or --remove); the route is a "
                               "patched Wayfire that puts the flag in the view record (AGENTS.md route "
                               "6), which is the one place a toggle could read it from")
            action = 0 if view.get(field) else 1
        self.ipc.call(method, view_id=int(wid), state=bool(action))
        return None

    def _maximize(self, wid: int, view: dict, action: int) -> None:
        """Both axes, through the grid plugin: `grid/slot_c` fills the workarea and `grid/restore` puts the
        window back where it was. Read back from `tiled-edges`, which is 15 for all four edges."""
        self._need("grid/slot_c", "grid", "maximizing")
        if action == 2:
            action = 0 if int(view.get("tiled-edges") or 0) == TILED_ALL else 1
        self.ipc.call("grid/slot_c" if action else "grid/restore", view_id=int(wid))
        return None

    def maximize_pair_state(self) -> str:
        """The pair is one call here: the grid plugin has a maximize and no per-axis state at all."""
        return "MAXIMIZED"

    # -- desktops -----------------------------------------------------------

    def get_desktop(self) -> int:
        gw, _gh, cx, cy = self._grid(self._focused_output())
        return cy * gw + cx

    def set_desktop(self, n: int):
        self._need("vswitch/set-workspace", "vswitch", "switching desktops")
        out = self._focused_output()
        gw, gh, _cx, _cy = self._grid(out)
        if not 0 <= n < gw * gh:
            raise CmdError("wayfire: desktop %d does not exist (this output's viewport grid is %dx%d)"
                           % (n, gw, gh))
        # `output-id`, with a hyphen: `output_id` is answered `Missing "output-id"`
        # [M recon2/wayfire.md §1.2].
        self.ipc.call("vswitch/set-workspace",
                      {"x": n % gw, "y": n // gw, "output-id": int(out.get("id") or 0)})

    def num_desktops(self) -> int:
        gw, gh, _cx, _cy = self._grid(self._focused_output())
        return gw * gh

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def set_window_desktop(self, wid: int, n: int):
        self._need("vswitch/send-view", "vswitch", "moving a window to another desktop")
        view = self._view(wid)
        outs = {int(o.get("id") or -1): o for o in self._outputs()}
        out = outs.get(int(view.get("output-id") or -1)) or self._focused_output()
        gw, gh, _cx, _cy = self._grid(out)
        if not 0 <= n < gw * gh:
            raise CmdError("wayfire: desktop %d does not exist (this output's viewport grid is %dx%d)"
                           % (n, gw, gh))
        # `view-id` here, with a hyphen, where wm-actions spells the same thing `view_id`: `view_id` is
        # answered `Missing "view-id"` (measured on this box).
        self.ipc.call("vswitch/send-view", {"view-id": int(wid), "x": n % gw, "y": n // gw})

    def workspaces(self) -> "list[Workspace]":
        """One row per viewport of the focused output's grid, in the flat order the tools use.

        Wayfire's viewports have no names -- `list-wsets` names the *wset*, not the cell -- so the name is
        empty and wwmctl prints the index, which is what it does for every nameless workspace. The work area
        is the output's, moved into layout coordinates: `workarea` is output-relative where `geometry` is
        absolute (the recorded second head sits at `x: 1280` with `workarea.x: 0`)
        [M recon2/wayfire.md §1.2]."""
        out = self._focused_output()
        gw, gh, cx, cy = self._grid(out)
        rect = out.get("geometry") or {}
        area = out.get("workarea") or {}
        ox, oy = int(rect.get("x") or 0), int(rect.get("y") or 0)
        work = (ox + int(area.get("x") or 0), oy + int(area.get("y") or 0),
                int(area.get("width") or 0), int(area.get("height") or 0))
        cur = cy * gw + cx
        return [Workspace(index=i, name="", active=(i == cur), work_area=work) for i in range(gw * gh)]

    def display_size(self) -> tuple[int, int]:
        boxes = []
        for o in self._outputs():
            rect = o.get("geometry") or {}
            boxes.append((int(rect.get("x") or 0), int(rect.get("y") or 0),
                          int(rect.get("width") or 0), int(rect.get("height") or 0)))
        minx = min(x for x, _y, _w, _h in boxes)
        miny = min(y for _x, y, _w, _h in boxes)
        w = max(x + w for x, _y, w, _h in boxes) - minx
        h = max(y + h for _x, y, _w, h in boxes) - miny
        if not w or not h:
            raise CmdError("wayfire backend: no active outputs")
        return w, h

    def pointer(self) -> "tuple[int, int] | None":
        """Wayfire's own cursor position, which no wlroots backend of ours could answer before: the recorded
        reply is `{"result": "ok", "pos": {"x": 640.0, "y": 360.0}}`, and a `wdotool mousemove 300 200`
        followed by this method came back 0 px out [M recon2/wayfire.md §1.2, §2.3]."""
        try:
            reply = self.ipc.call("window-rules/get_cursor_position")
        except CmdError:
            return None
        pos = reply.get("pos") if isinstance(reply, dict) else None
        if not isinstance(pos, dict) or pos.get("x") is None or pos.get("y") is None:
            return None
        return int(pos["x"]), int(pos["y"])

    # -- events -------------------------------------------------------------

    #: Wayfire's event names in the (id, change) vocabulary `WindowBackend.events()` documents -- the words
    #: sway's backend uses, and the ones select_window() reads. wxprop -spy reads them through its *bridge*
    #: tables rather than its sway ones, because `hacks/property/core.py:_is_sway()` gates on the backend name
    #: and this backend is not called sway; that fits what is emitted, since `move` here is a geometry change
    #: only (the bridge table maps it to no atom) and `minimized`/`workspace` are the bridge's own words.
    #: The first four names were recorded live [M recon2/wayfire.md §1.2] and the rest are in the plugin's
    #: own string table (`strings libipc-rules.so`); `view-geometry-changed` and `view-unmapped` were
    #: measured here too, opening and closing one foot window.
    _EVENTS = {"view-mapped": "new", "view-unmapped": "close", "view-focused": "focus",
               "view-title-changed": "title", "view-geometry-changed": "move",
               "view-fullscreen": "fullscreen_mode", "view-tiled": "floating",
               "view-minimized": "minimized", "view-sticky": "workspace"}

    #: the events that are about the desktop rather than about one window
    _WS_EVENTS = ("wset-workspace-changed", "view-workspace-changed", "output-wset-changed")

    def events(self, timeout: "float | None" = None, workspaces: bool = False):
        """(id, change) for every Wayfire view event, in sway's words.

        With `workspaces` the viewport stream is folded in as (0, "workspace") -- no view has id 0 -- the way
        the sway, GNOME and KWin backends fold theirs, so a root watcher needs one stream only."""
        for event in self.ipc.watch(timeout):
            name = str(event.get("event") or "")
            if workspaces and name in self._WS_EVENTS:
                yield 0, "workspace"
                continue
            change = self._EVENTS.get(name)
            if change is None:
                continue
            view = event.get("view")
            wid = (view or {}).get("id") if isinstance(view, dict) else None
            if wid is None:
                continue
            yield int(wid), change

    select_window_hint = "focus the target window to select it"

    def select_window(self) -> int:
        """The next view to take focus.

        Not xdotool's semantics (the window under the pointer at the next button press) and knowingly so, the
        same way the sway backend is not: Wayfire's IPC has a cursor position but no picker and nothing that
        grabs a button press from outside the compositor, so there is nothing to click with. Clicking the
        window that already has focus therefore does not end this wait.

        Not yet, rather than never: this IPC is the one that DOES publish the cursor (`wayfire/get-cursor`
        via stipc), so the missing half is only the press, and evdev has it (AGENTS.md route 4, at the cost
        of read access to /dev/input) -- the hit test against `list-views` geometry is then ours."""
        for wid, change in self.events():
            if change == "focus":
                return wid
        raise CmdError("wayfire backend: the window event stream ended")

    # -- the X plane --------------------------------------------------------

    def x_info(self) -> "tuple[str, str] | None":
        """(DISPLAY, XAUTHORITY) of Wayfire's Xwayland, when `stipc` is loaded.

        `stipc/get_xwayland_display` answers `{"result": "ok", "display": ":1"}`
        [M recon2/wayfire.md §1.2, the `stipc/get_display` capture]. Wayfire exports no cookie of its own, so
        the authority is the session scan; with no stipc this returns None and the callers fall back to
        session.find_x_display()."""
        if "stipc/get_xwayland_display" not in self.methods:
            return None
        try:
            reply = self.ipc.call("stipc/get_xwayland_display")
        except CmdError:
            return None
        display = str((reply or {}).get("display") or "") if isinstance(reply, dict) else ""
        if not display:
            return None
        return display, session.find_xauthority() or ""

    def _x11(self):
        """An X11 connection to Wayfire's Xwayland, or None; opened at most once per process.

        Nothing is opened unless an Xwayland is already running: Wayfire starts one on demand
        (`xwayland = true` is the default), and connecting merely to ask would start it."""
        if self._x != "unset":
            return self._x
        self._x = None
        if not session.xwayland_running():
            return None
        info = self.x_info() or ("", "")
        try:
            from hacks.window import x11_mini
            self._x = x11_mini.X11Conn(info[0] or None, xauthority=info[1] or None)
        except Exception:  # no X plane: every xid stays 0
            self._x = None
        return self._x

    @staticmethod
    def _x_clients(x) -> "list[dict]":
        """The X clients in _NET_CLIENT_LIST order, which the matcher reads as an order and not just a set."""
        out = []
        for xid in x.client_list():
            try:
                inst, cls = x.get_wm_class(xid)
                name = x.get_prop_string(xid, "_NET_WM_NAME") or x.get_prop_string(xid, "WM_NAME")
                geo = x.get_geometry(xid)
                pid = x.get_pid(xid)
            except Exception:  # a window that just died
                continue
            out.append({"xid": int(xid), "pid": int(pid), "inst": inst, "cls": cls,
                        "name": name, "geo": geo})
        return out

    def _x_ratio(self, x) -> "float | None":
        """X device pixels per Wayfire logical pixel, or None when the layout has no one answer.

        The same correction the KWin backend needs, for the same measured reason: on a scaled output every X
        rectangle is the logical one times the scale, so the distance between a window and *itself* exceeds
        the distance between the two windows of a tied pair, and the pair comes out swapped. A layout that
        mixes scales has no single ratio and the distance then says nothing at all."""
        try:
            rw, rh = x.get_geometry(x.root())[2:]
            vw, vh = self.display_size()
        except Exception:
            return 1.0
        if vw <= 0 or vh <= 0 or int(rw) <= 0 or int(rh) <= 0:
            return 1.0
        sx, sy = rw / vw, rh / vh
        if abs(sx - sy) > 0.01:
            return None
        return sx

    def _xids(self, raw: "list[dict]") -> "tuple[dict[str, int], dict[int, dict]]":
        """({view id as a string: X window id}, {X window id: its client row}).

        `list-views` carries no X window id at all [M recon2/wayfire.md §1.2], so the pairing is the one the
        KWin 6 backend makes against `_NET_CLIENT_LIST` -- and it has more to work with here, because Wayfire
        publishes the pid of every view where KWin 6 publishes none."""
        if not raw:
            return {}, {}
        x = self._x11()
        if x is None:
            return {}, {}
        try:
            clients = self._x_clients(x)
        except Exception:  # any X failure: no ids, no crash, and no second try
            self._x = None
            return {}, {}
        rows = []
        for i, v in enumerate(raw):
            geo = v.get("geometry") or {}
            rows.append({"u": str(v.get("id")), "p": int(v.get("pid") or 0),
                         "c": v.get("app-id") or "", "n": "", "t": v.get("title") or "",
                         "x": int(geo.get("x") or 0), "y": int(geo.get("y") or 0),
                         "w": int(geo.get("width") or 0), "h": int(geo.get("height") or 0), "ix": i})
        return match_xids(rows, clients, self._x_ratio(x)), {c["xid"]: c for c in clients}

    def views(self) -> "list[View]":
        raw = self._raw_views()
        rows = self._outputs()
        order = [int(o.get("id") or -1) for o in rows]
        outs = dict(zip(order, rows))
        xids, clients = self._xids(raw)
        out = []
        for v in raw:
            xid = int(xids.get(str(v.get("id")), 0))
            client = clients.get(xid) or {}
            app_id = v.get("app-id") or ""
            edges = int(v.get("tiled-edges") or 0)
            out.append(View(
                window=self._window(v, outs),
                xid=xid,
                # An XWayland window's real WM_CLASS pair, off the X server: Wayfire's `app-id` for an X
                # client is the WM_CLASS *class* alone, which is why the wlr floor printed `XTerm.XTerm`
                # where wmctrl prints `xterm.XTerm` [M recon2/wayfire.md §2.4].
                instance=client.get("inst") or app_id,
                cls=client.get("cls") or app_id,
                app_id="" if xid else app_id,
                fullscreen=bool(v.get("fullscreen")),
                maximized_h=(edges & TILED_H) == TILED_H,
                maximized_v=(edges & TILED_V) == TILED_V,
                sticky=bool(v.get("sticky")),
                minimized=bool(v.get("minimized")),
                hidden=bool(v.get("minimized")),
                floating=not edges,
                ws_name="",
                client_type="x11" if xid else "wayland",
                monitor=order.index(int(v.get("output-id") or -1))
                if int(v.get("output-id") or -1) in order else -1,
            ))
        return out
