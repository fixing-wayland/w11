"""sway/i3 window backend over raw i3-ipc (no external tools).

Framing: b"i3-ipc" + u32 payload length + u32 message type + JSON payload. Window ids are sway node ids;
commands address nodes with [con_id=N]. Desktops are workspaces: xdotool desktop N == workspace number N+1.

Two dialects on one wire. i3 speaks the same protocol and this backend is what `W11_PASSTHROUGH=never`
reaches on an i3 box, where it used to call itself sway and get four things wrong [M recon2/i3.md §2b]:
`dialect()` asks GET_VERSION once (sway is 1.x, i3 has been 4.x since 2011) and the four differences below hang
off it -- ids, floating, pid, visible. Everything else is one code path."""

import json
import select
import socket
import struct
import time

from w11common import session
from w11common.errors import CmdError
from hacks.window.backend import Window, WindowBackend, Workspace, warn
from wdotool.ctx import SoftCmdError

_MAGIC = b"i3-ipc"
RUN_COMMAND = 0
GET_WORKSPACES = 1
SUBSCRIBE = 2
GET_OUTPUTS = 3
GET_TREE = 4
GET_VERSION = 7
_EVENT_BIT = 0x80000000
EVENT_WORKSPACE = _EVENT_BIT | 0
EVENT_WINDOW = _EVENT_BIT | 3

SCRATCHPAD_WS = "__i3_scratch"

# Deadline for the command socket only (see __init__). Generous: every reply is built in the compositor's own
# event loop, and GET_TREE on a busy desktop is the slow one.
IPC_TIMEOUT = 10.0


def _lost(e) -> CmdError:
    """Any wire-level failure of the i3/sway IPC socket, as one clear line."""
    return CmdError("sway backend: lost the connection to the compositor (%s)" % e)


def _sel(node) -> str:
    """`[con_id=N]`, the criterion that addresses one view.

    Always the node's own `id` and never `Window.id`: on i3 the two differ (Window.id is the X id there), and
    `[con_id=<47-bit pointer>] focus` is what i3 itself wants -- measured working against the live 4.25.1
    [M recon2/i3.md §1]. A module function rather than a method because `move_to_current_desktop` below is
    borrowed by a double that has no other part of this class (tests/test_wwmctl_cli.py:RunCapableFake)."""
    return "[con_id=%d]" % node["id"]


def _wedged() -> CmdError:
    """Connected, and not answering: a compositor stuck in its own event loop
    accepts on the IPC socket (the kernel does that) and then says nothing."""
    return CmdError("sway backend: no answer from the compositor within %gs "
                    "(it is not responding)" % IPC_TIMEOUT)


class SwayBackend(WindowBackend):
    name = "sway"

    def __init__(self, sockpath: str | None = None):
        self.sockpath = sockpath or session.find_sway_socket()
        if not self.sockpath:
            raise CmdError(
                "sway backend: no sway/i3 IPC socket found (SWAYSOCK unset and "
                "no sway-ipc.* socket in any runtime dir)"
            )
        # Only the command socket gets a deadline: everything it ever waits for is the answer to a request we
        # have just sent, so silence means the compositor is wedged -- which used to hang every tool for ever.
        # The events socket is left blocking (_connect()'s default); select_window() and wxprop's -spy wait on
        # it for an event that may be minutes away.
        self.sock = self._connect(IPC_TIMEOUT)
        #: "sway" / "i3", filled by dialect() on first use
        self._dialect = None
        #: lazy X11Conn, i3 only (see _x_plane); "unset" means never tried
        self._x = "unset"
        #: X id -> pid, i3 only (see _fill_pids): an X id names one window for its whole life and this
        #: backend lives for one process, so the `_NET_WM_PID` round trip is paid once per window rather
        #: than once per window per _nodes() -- and _nodes() runs on every command, twice in resize().
        self._pids: dict[int, int] = {}

    # -- wire ---------------------------------------------------------------

    def _connect(self, timeout: float | None = None) -> socket.socket:
        """A fresh IPC socket, carrying `timeout` once it is up.

        The connect() itself always gets IPC_TIMEOUT, whatever the caller wants afterwards. A compositor wedged
        inside its own event loop still has a *listening* socket -- the kernel queues connections for it without
        waking it -- so connecting looks fine right up to the moment the backlog fills. Measured here against a
        raw AF_UNIX socket with listen(3) that never accepts: four connects succeed, and the fifth blocks in the
        kernel for ever with no deadline of its own. With one, the fifth is EAGAIN (Linux answers a full
        AF_UNIX backlog that way rather than making us wait), retried until the deadline, and the tool says the
        compositor is not responding instead of hanging."""
        deadline = time.monotonic() + IPC_TIMEOUT
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
                    raise _wedged() from None
                time.sleep(0.01)
            except OSError as e:
                s.close()
                raise CmdError("sway backend: cannot connect to %s: %s" % (self.sockpath, e)) from None
        s.settimeout(timeout)
        return s

    @staticmethod
    def _read_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise CmdError("sway backend: IPC connection closed")
            buf += chunk
        return buf

    @classmethod
    def _send(cls, sock, mtype: int, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            sock.sendall(_MAGIC + struct.pack("<II", len(payload), mtype) + payload)
        except TimeoutError:
            raise _wedged() from None
        except OSError as e:
            raise _lost(e) from None

    @classmethod
    def _recv(cls, sock):
        # A clean EOF is only the tidiest way for the compositor to go away: a session that ends mid-chain gives
        # ECONNRESET on the read and EPIPE on the next write, and a compositor that answers with something that
        # is not JSON gives ValueError. All three are the same event to the user -- one line, not a traceback
        # (B5). A compositor that is still there but not answering is a different event, and says so.
        try:
            hdr = cls._read_exact(sock, 14)
            if hdr[:6] != _MAGIC:
                raise CmdError("sway backend: bad IPC framing")
            length, mtype = struct.unpack("<II", hdr[6:])
            payload = cls._read_exact(sock, length) if length else b"null"
            return mtype, json.loads(payload.decode("utf-8", "replace"))
        except TimeoutError:
            # TimeoutError is an OSError: this arm has to come first, or a
            # compositor that is merely wedged reads as one that has gone.
            raise _wedged() from None
        except (OSError, struct.error, ValueError) as e:
            raise _lost(e) from None

    def _msg(self, mtype: int, payload=b""):
        self._send(self.sock, mtype, payload)
        while True:
            t, data = self._recv(self.sock)
            if not t & _EVENT_BIT:  # we never subscribe on this socket
                return data

    def run(self, command: str):
        results = self._msg(RUN_COMMAND, command)
        for r in results or []:
            if not r.get("success"):
                raise CmdError("%s: %s" % (self.dialect(),
                                           r.get("error") or "command failed: %s" % command))

    def dialect(self) -> str:
        """"i3" or "sway", from one GET_VERSION, cached for the life of the backend.

        sway is 1.x; i3 has been 4.x since 2011, and the live 4.25.1 answered `major` 4 [M recon2/i3.md §1,
        fixture get_version.json]. So `major >= 4` is the whole test, and a reply that is not a JSON object at
        all (an old sway, a stub) reads as sway, which is what this file did before there was a dialect.

        Asked lazily rather than in the constructor: a wedged compositor accepts the connection and answers
        nothing, and the message about that belongs to the first command the user typed, not to constructing a
        backend (tests/test_wire_hardening.py:SwayWireGuards builds one against such a socket)."""
        if self._dialect is None:
            v = self._msg(GET_VERSION)
            major = v.get("major") if isinstance(v, dict) else None
            self._dialect = "i3" if isinstance(major, int) and major >= 4 else "sway"
        return self._dialect

    def _x_plane(self):
        """An X11 connection for the i3 dialect, opened once and only if something needs it, or None.

        i3's tree carries no `pid` at all (i3ipc-python documents the field as sway-only) and `getwindowpid`
        failed on every window with `has no pid associated with it` [M recon2/i3.md §2b]. i3 *is* X11, so every
        view has an X id and `_NET_WM_PID` is one property read away. Never raises: a session whose X plane
        cannot be reached leaves the pids at 0, which is what they were before."""
        if self._x != "unset":
            return self._x
        self._x = None
        try:
            from hacks.window import x11_mini
            self._x = x11_mini.X11Conn()
        except Exception:                       # no X plane: pid stays 0
            self._x = None
        return self._x

    # -- tree ---------------------------------------------------------------

    def _nodes(self):
        """[(node, Window, floating, ws_name)] for every view in the tree."""
        # wwmctl.core.windows() unpacks this exact tuple shape; keep in sync.
        i3 = self.dialect() == "i3"
        # i3 nodes carry no `visible`, so `search --onlyvisible` matched nothing at all [M recon2/i3.md §2b].
        # Derived instead: a view is visible when its workspace is one of the visible ones. GET_WORKSPACES never
        # lists `__i3_scratch`, so "and not in the scratchpad" comes out of the same set for free.
        visible_ws = ({ws.get("name") or "" for ws in self._msg(GET_WORKSPACES) or []
                       if ws.get("visible")} if i3 else None)
        tree = self._msg(GET_TREE)
        out = []

        def walk(node, ws_num, ws_name, floating):
            ntype = node.get("type")
            if ntype == "workspace":
                ws_num = node.get("num", -1)
                ws_name = node.get("name") or ""
            is_view = ntype in ("con", "floating_con") and (
                node.get("app_id") is not None
                or node.get("window_properties") is not None
                or (node.get("pid") and not node.get("nodes"))
            )
            if is_view:
                wp = node.get("window_properties") or {}
                rect = node.get("rect") or {}
                num = ws_num if isinstance(ws_num, int) else -1
                # i3's own `floating` field, which sway does not send: "user_on"/"auto_on" when the view is
                # floating, "user_off"/"auto_off" when it is not [M recon2/i3.md §1].
                floating = floating or str(node.get("floating") or "").endswith("_on")
                out.append((
                    node,
                    Window(
                        # On i3 the X id, not the con id: i3's con ids are 47-bit pointers, and the one the
                        # tools handed out (97479943571072) truncated to 0x5168c680 in every X-shaped
                        # consumer -- `wxprop -id` answered BadWindow on it [M recon2/i3.md §2b]. Every i3
                        # window has `window`; commands still address the con id, through _sel().
                        id=(node.get("window") or node["id"]) if i3 else node["id"],
                        title=node.get("name") or "",
                        class_=node.get("app_id") or wp.get("class") or "",
                        # WM_CLASS instance of an X11 client under Xwayland; native Wayland views have none
                        # (search --classname then falls back to class_ = app_id).
                        instance=wp.get("instance") or "",
                        pid=node.get("pid") or 0,
                        x=rect.get("x", 0),
                        y=rect.get("y", 0),
                        w=rect.get("width", 0),
                        h=rect.get("height", 0),
                        focused=bool(node.get("focused")),
                        visible=(ws_name in visible_ws) if i3 else bool(node.get("visible")),
                        desktop=num - 1 if num > 0 else -1,
                    ),
                    floating,
                    ws_name,
                ))
            for ch in node.get("nodes") or []:
                # i3 wraps a floating view in a `floating_con` whose single child is the view, so the flag has
                # to survive that one hop down -- resetting it here is what made every floating i3 window read
                # as tiled and every `windowmove` refuse [M recon2/i3.md §2c].
                walk(ch, ws_num, ws_name, floating and ntype == "floating_con")
            for ch in node.get("floating_nodes") or []:
                walk(ch, ws_num, ws_name, True)

        walk(tree, -1, "", False)
        if i3:
            self._fill_pids(out)
        return out

    def _fill_pids(self, rows):
        """i3 only: fill in the pids its tree does not carry, off `_NET_WM_PID` on the X plane.

        Answers are remembered in `self._pids`: without that this is one X round trip per window on every
        `_nodes()`, and `_nodes()` is walked by every command, by every `_node(wid)` lookup and twice by
        `resize()`."""
        live = {node["window"] for node, _w, _f, _ws in rows if node.get("window")}
        if self._pids:
            # X ids are recycled: an id that has left the tree may come back on somebody else's window, so
            # its answer leaves the cache with it rather than outliving the window it was measured on.
            self._pids = {xid: pid for xid, pid in self._pids.items() if xid in live}
        if not any(not win.pid and node.get("window") for node, win, _f, _ws in rows):
            return
        x = self._x_plane()
        if x is None:
            return
        for node, win, _f, _ws in rows:
            if win.pid or not node.get("window"):
                continue
            xid = node["window"]
            if xid in self._pids:
                win.pid = self._pids[xid]
                continue
            try:
                win.pid = x.get_pid(xid)
            except Exception:                   # one unreadable window is not a failed listing
                continue
            if win.pid:                         # a 0 is "not answered yet", never a cached answer
                self._pids[xid] = win.pid

    def _node(self, wid: int):
        for node, win, floating, ws_name in self._nodes():
            if win.id == wid:
                return node, win, floating, ws_name
        raise CmdError("window %d not found" % wid)

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        return [win for _n, win, _f, _w in self._nodes()]

    def activate(self, wid: int):
        node = self._node(wid)[0]
        self.run("%s focus" % _sel(node))

    def close(self, wid: int):
        node = self._node(wid)[0]
        self.run("%s kill" % _sel(node))

    @staticmethod
    def _deco_h(node) -> int:
        """Titlebar height: sway's move/resize address the whole container
        (decoration included) while we report/take the content rect."""
        return (node.get("deco_rect") or {}).get("height", 0)

    def _refuse_fullscreen(self, node, what: str):
        """sway silently ignores move/resize of fullscreen containers, which
        would make --sync spin forever; refuse up front instead.

        Not a staticmethod any more: the refusal opens with `dialect()` so an i3 session is not told
        about sway."""
        if node.get("fullscreen_mode"):
            raise CmdError(
                "%s: cannot %s a fullscreen window "
                "(windowstate --remove FULLSCREEN first)" % (self.dialect(), what)
            )

    def move_window(self, wid: int, x: int, y: int):
        node, _w, floating, _ws = self._node(wid)
        if not floating:
            # the dialect, not the file's name: `dialect()` already answers i3 everywhere else here (the
            # label, the ids, the floating flag, the pids), and a refusal that opens `sway:` on an i3
            # session names a program the user is not running.  Measured on the first live resolute-i3 run,
            # 2026-09-09 [requests-batch-9.md, from batch 16].
            raise SoftCmdError(
                "%s: cannot move a tiled window to an absolute position "
                "(floating enable it first)" % self.dialect()
            )
        self._refuse_fullscreen(node, "move")
        self.run("%s move absolute position %d %d" % (_sel(node), x, y - self._deco_h(node)))

    def resize(self, wid: int, w: int, h: int):
        node, win, floating, _ws = self._node(wid)
        if not floating:
            # `resize set` on a tiled container moves the split ratio: the window ends up some other size on the
            # axis the layout owns and unchanged on the other, which read as a silent partial success. Refuse it
            # the way a tiled move is refused.
            raise SoftCmdError(
                "%s: cannot resize a tiled window to an absolute size "
                "(floating enable it first)" % self.dialect()
            )
        self._refuse_fullscreen(node, "resize")
        self.run("%s resize set %d px %d px" % (_sel(node), w, h + self._deco_h(node)))
        # sway resizes floating windows around their center; xdotool (X11)
        # keeps the top-left corner fixed. Move it back where it was.
        node2 = self._node(wid)[0]
        self.run("%s move absolute position %d %d"
                 % (_sel(node2), win.x, win.y - self._deco_h(node2)))

    def minimize(self, wid: int):
        self.unmap(wid)

    def map(self, wid: int):
        node, _win, _f, ws_name = self._node(wid)
        if ws_name == SCRATCHPAD_WS:
            self.run("%s scratchpad show" % _sel(node))

    def unmap(self, wid: int):
        node, _win, _f, ws_name = self._node(wid)
        if ws_name != SCRATCHPAD_WS:
            self.run("%s move scratchpad" % _sel(node))

    def is_mapped(self, wid: int) -> bool:
        """Mapped = not stashed in the scratchpad. A window on an unfocused workspace (or a background tab) is
        mapped but not visible; X11's map state is what windowmap/windowunmap --sync must wait on."""
        return self._node(wid)[3] != SCRATCHPAD_WS

    def raise_(self, wid: int):
        node, _win, floating, _ws = self._node(wid)
        if floating:
            self.run("%s focus" % _sel(node))
        else:
            warn("windowraise: tiled %s windows have no stacking order; not yet, and the route is a "
                 "patched compositor (AGENTS.md route 6), a restack its layout has no word for today; "
                 "ignoring" % self.dialect())

    def lower(self, wid: int):
        """XLowerWindow always worked, so this is a gap and not a policy: sway's command list has no `lower`
        (`swaymsg lower` is a parse error, the same one `output` is on i3), and neither does i3's. The
        cheapest rung that reaches it is the compositor's own command parser."""
        self._node(wid)
        warn("windowlower: %s has no lower command; not yet, and the route is a patched compositor "
             "(AGENTS.md route 6), one command in its parser; ignoring" % self.dialect())

    def set_state(self, wid: int, state: str, action: int):
        node, _win, _f, ws_name = self._node(wid)
        word = {0: "disable", 1: "enable", 2: "toggle"}[action]
        if state == "FULLSCREEN":
            self.run("%s fullscreen %s" % (_sel(node), word))
        elif state == "STICKY":
            self.run("%s sticky %s" % (_sel(node), word))
        elif state == "DEMANDS_ATTENTION":
            if action == 2:
                word = "disable" if node.get("urgent") else "enable"
            self.run("%s urgent %s" % (_sel(node), word))
        elif state == "HIDDEN":
            hidden = ws_name == SCRATCHPAD_WS
            if action == 2:
                action = 0 if hidden else 1
            if action == 1 and not hidden:
                self.run("%s move scratchpad" % _sel(node))
            elif action == 0 and hidden:
                self.run("%s scratchpad show" % _sel(node))
        else:
            # a plain CmdError, not an `unsupported` one: this refusal has always been a failure and
            # wwmctl's X-plane retry keys off that flag, so only the sentence changes here
            raise CmdError("windowstate %s is not supported by the %s backend: %s has no such window "
                           "state; not yet here, and the route is a patched compositor (AGENTS.md route "
                           "6), one command and one state bit each" % (state, self.name, self.dialect()))

    def window_desktop(self, wid: int) -> int:
        return self._node(wid)[1].desktop

    def set_window_desktop(self, wid: int, n: int):
        node = self._node(wid)[0]
        self.run("%s move container to workspace number %d" % (_sel(node), n + 1))

    def workspaces(self) -> "list[Workspace]":
        """One record per workspace, ascending. sway answers GET_WORKSPACES in creation order and numbers only
        the numbered ones, so the sort is on the raw `num` with the nameless-number workspaces last; the index
        is the desktop number the tools use (workspace number N+1 is desktop N), and a named workspace with no
        number is -1."""
        rows = sorted(self._msg(GET_WORKSPACES) or [],
                      key=lambda ws: (ws.get("num", -1) < 0,
                                      ws.get("num", -1),
                                      ws.get("name") or ""))
        out = []
        for ws in rows:
            num = ws.get("num", -1)
            rect = ws.get("rect") or {}
            out.append(Workspace(
                index=num - 1 if num > 0 else -1,
                name=ws.get("name") or "",
                active=bool(ws.get("focused")),
                work_area=(rect.get("x", 0), rect.get("y", 0),
                           rect.get("width", 0), rect.get("height", 0)),
            ))
        return out

    def move_to_current_desktop(self, wid: int) -> bool:
        # window_desktop() first and _nodes() after, rather than one _node(): this method is borrowed whole by
        # the sway-shaped double in tests/test_wwmctl_cli.py, which offers exactly `_nodes`, `window_desktop`
        # and `run` -- and the vanished-window case there is a window_desktop that raises.
        self.window_desktop(wid)               # gone -> CmdError, exit 1
        for node, win, _f, _ws in self._nodes():
            if win.id == wid:
                self.run("%s move container to workspace current" % _sel(node))
                return True
        raise CmdError("window %d not found" % wid)

    def get_desktop(self) -> int:
        for ws in self._msg(GET_WORKSPACES):
            if ws.get("focused"):
                num = ws.get("num", -1)
                return num - 1 if num > 0 else -1
        return -1

    def set_desktop(self, n: int):
        self.run("workspace number %d" % (n + 1))

    def num_desktops(self) -> int:
        return len(self._msg(GET_WORKSPACES))

    def events(self, timeout: float | None = None, workspaces: bool = False):
        """(id, change) for every sway window event, in sway's own vocabulary (new, close, focus, title,
        fullscreen_mode, move, floating, urgent), on a connection of its own: a subscription and the command
        socket cannot share one, since every reply we wait for would arrive behind an unbounded queue of events.

        With `workspaces` the workspace stream is folded in as (0, "workspace") -- no view has node id 0 -- the
        way the GNOME and KWin backends fold theirs, so a root-level watcher (wxprop -root -spy) needs one
        stream only. Stops after `timeout` seconds of silence; None waits for ever, which is what both callers
        do."""
        s = self._connect()
        try:
            self._send(s, SUBSCRIBE,
                       b'["window","workspace"]' if workspaces
                       else b'["window"]')
            t, reply = self._recv(s)
            if t & _EVENT_BIT or not (isinstance(reply, dict)
                                      and reply.get("success")):
                raise CmdError("sway backend: subscribe to window events failed")
            while True:
                if timeout is not None and not select.select([s], (), (),
                                                             timeout)[0]:
                    return
                t, data = self._recv(s)
                if not isinstance(data, dict):
                    continue
                if t == EVENT_WINDOW:
                    wid = self._event_wid(data.get("container") or {})
                    if wid is not None:
                        yield int(wid), str(data.get("change") or "")
                elif workspaces and t == EVENT_WORKSPACE:
                    yield 0, "workspace"
        finally:
            s.close()

    def _event_wid(self, con: dict):
        """The id an event names, in the same space `list()` hands out: the con id on sway, the X id on i3.

        The event payload carries the whole container node, so `window` is usually right there; a container
        that has just been closed may not carry it any more, and the tree is asked once for that one case
        (on the command socket -- the event socket is a separate connection)."""
        cid = con.get("id")
        if cid is None or self.dialect() != "i3":
            return cid
        if con.get("window"):
            return con["window"]
        for node, win, _f, _ws in self._nodes():
            if node.get("id") == cid:
                return win.id
        return cid

    select_window_hint = "focus the target window to select it"

    def select_window(self) -> int:
        """sway/i3: wait for the next window-focus event.

        Not xdotool's semantics (the window under the pointer at the next button press) and knowingly so: sway's
        IPC has no interactive picker, no pointer position and nothing that grabs a button press from outside
        the compositor, so there is nothing to click *with*. Clicking the window that already has focus
        therefore does not end this wait -- focus it from another window, or use another selector.

        The click-to-pick is not yet here rather than out of reach. Two rungs together do it: evdev sees the
        button press without asking the compositor (AGENTS.md route 4, at the cost of read access to
        /dev/input, which is the same cost the uinput typing path already pays), and the window under the
        pointer is a `get_tree` hit test once something says where the pointer is -- sway's IPC does not, so
        that half is route 6, a `get_seats` that carries the cursor position."""
        for wid, change in self.events():
            if change == "focus":
                return wid
        raise CmdError("sway backend: the window event stream ended")

    def display_size(self) -> tuple[int, int]:
        boxes = []
        for o in self._msg(GET_OUTPUTS):
            if not o.get("active"):
                continue
            rect = o.get("rect") or {}
            boxes.append((rect.get("x", 0), rect.get("y", 0), rect.get("width", 0), rect.get("height", 0)))
        if not boxes:
            raise CmdError("sway backend: no active outputs")
        # Bounding box of the full layout; origins can be non-zero/negative.
        minx = min(x for x, _y, _w, _h in boxes)
        miny = min(y for _x, y, _w, _h in boxes)
        w = max(x + w for x, _y, w, _h in boxes) - minx
        h = max(y + h for _x, y, _w, h in boxes) - miny
        if not w or not h:
            raise CmdError("sway backend: no active outputs")
        return w, h
