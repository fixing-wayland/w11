"""The loop: accept, connect upstream, move bytes, and know when to stop.

`selectors`, one process, one thread on the byte path. The numbers that decided
that (recon/env.md 5): a Python hop costs ~70 us per round trip and moves
549 MB/s; asyncio is 1.9x the latency for the same job; and a non-blocking
`sendall()` lost 98 of 100 MB silently, which is why every direction has an
out-buffer, an `EVENT_WRITE` registration and a high-water mark rather than a
`sendall`.

Three things here are measurements rather than taste:

* **Backpressure.** `HIGH_WATER` 4 MiB / `LOW_WATER` 1 MiB: while a peer's
  out-buffer is over the high mark the side that feeds it stops being read, and
  comes back under the low one. `recon/env/fwd.py`'s bug is the negative twin --
  with the peer socket non-blocking, `sendall` raised `BlockingIOError` and the
  `except OSError` around it closed the pair silently, and 98 of 100 MB
  disappeared with no error anywhere.
* **Descriptors.** Reads go through `recvmsg` with a 64-byte ancillary buffer.
  A plain `recv()` eats `SCM_RIGHTS` silently: the payload arrives and the
  descriptors are gone (recon/env.md 2.6, measured synthetically because no box
  in this project has a /dev/dri). Any descriptor that arrives is closed and one
  line says so.
* **DRI3 is answered honestly.** `QueryExtension("DRI3")`'s reply gets its
  `present` byte set to 0 on the way down -- a one-byte, length-preserving edit,
  so no sequence moves. Forwarding a DRI3 fd needs `sendmsg` at the right offset
  in the byte stream, which is stage 8 (AGENTS.md route 5 -- rung 5 is an X11
  protocol proxy, so every gap inside xw11 closes with more of xw11); until then
  a client that asks is told "not here" rather than handed a path that breaks
  silently. `XWAYLAND` (major 150) and everything else stay advertised.

Lifetime is the input daemon's, constant for constant (`wdotool/daemon.py`:177):
`_IDLE_SECONDS = 900.0` with a `_CHECK_SECONDS = 15.0` cadence, checked only
while no client is connected. Fifteen minutes because shadow ids are
`OwnConn.rid_base | n` and a reopened upstream connection re-bases every one of
them, so `W=$(xdotool search ...); ...; xdotool windowactivate $W` has to keep
meaning the same window between two invocations (design section 2.6). The other
two exits are the same shape: the session's Wayland socket is gone, or somebody
unlinked our display file (`xw11 --stop`).
"""

import array
import os
import selectors
import signal
import socket
import threading
import time

from w11common import session
from w11common.errors import CmdError
from wdotool import x11_mini
from xw11 import client as client_mod
from xw11 import display as display_mod
from xw11 import policy, wire
from xw11 import pump as pump_mod
from xw11 import shadow as shadow_mod
from xw11 import upstream as upstream_mod

HIGH_WATER = 4 << 20
LOW_WATER = 1 << 20
READ_SIZE = 1 << 16
#: 64 bytes: room for a dozen descriptors, which is more than the one a DRI3
#: reply carries and more than the zero the four tools ever send.
ANCILLARY_SIZE = 64

_IDLE_SECONDS = 900.0
_CHECK_SECONDS = 15.0

#: How long a client's upstream connect is retried while sway is between
#: Xwaylands -- the daemon client's shape (wdotool/daemon.py:2053). Xwayland
#: dies fifteen seconds after its last client and sway relaunches it lazily from
#: the `-listenfd`s it kept (recon/env.md 6). Defined beside the dial they
#: describe, in `xw11/upstream.py`, and named here because this is where a
#: client's twin is dialled and where the tests read them.
CONNECT_RETRY_SECONDS = upstream_mod.CONNECT_RETRY_SECONDS
CONNECT_RETRY_SLEEP = upstream_mod.CONNECT_RETRY_SLEEP

#: Extensions whose `present` byte is answered 0 on the way down.
HIDDEN_EXTENSIONS = ("DRI3",)

LOG_ENV = "XW11_LOG"
DEBUG_ENV = "XW11_DEBUG"


def log_path() -> str:
    return (os.environ.get(LOG_ENV)
            or ("/tmp/xw11.log" if os.geteuid() == 0
                else "/tmp/xw11-%d.log" % os.geteuid()))


def hide_present(pkt: bytes) -> bytes:
    """A QueryExtension reply with `present` (byte 8) cleared. Length-preserving
    on purpose: an in-place edit moves no sequence number and needs no
    `GetInputFocus` placeholder (recon/tools.md 1)."""
    if len(pkt) < 32 or pkt[0] != 1:
        return pkt
    return pkt[:8] + b"\0" + pkt[9:]


def upstream_socket(num: int):
    """A non-blocking connection to `:num`, abstract name first -- the order
    libxcb uses and the only one that reaches a server whose filesystem socket
    was unlinked out from under it (recon/wire.md 9.1, 9.2)."""
    path = "%s/X%d" % (x11_mini._SOCK_DIR, num)
    for target in ("\0" + path, path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(target)
        except OSError:
            s.close()
            continue
        s.setblocking(False)
        return s
    return None


def adopt_upstream_environment(upstream: str, upstream_num: int = None) -> None:
    """The proxy's own `DISPLAY` and `XAUTHORITY` are the UPSTREAM's, never its
    own (design section 2.4, R15). The backends that read the X plane themselves
    -- `XPlaneViews._x11()` at backend_wlr.py:199, KWin, Wayfire -- must reach
    Xwayland; a proxy that pointed them at itself would dial its own loop and
    wait for an answer it is the one that has to write.

    A module function and not only a method, because the environment has to be
    right BEFORE anything that reads it is constructed. Today nothing in
    `Server.__init__` opens a backend, so the order is invisible; the batch that
    adds `OwnConn` or a backend to the constructor would break R15 silently, and
    `cli.serve()` calls this before `Server(...)` so that it cannot.
    """
    os.environ["DISPLAY"] = upstream
    if upstream_num is None:
        upstream_num, _screen = x11_mini._parse_display(upstream)
    path, _cookie = display_mod.find_cookie(upstream_num)
    if path:
        os.environ["XAUTHORITY"] = path


class Server:
    """One proxy: one display, one selector, N clients."""

    def __init__(self, display, upstream, log=None, idle=_IDLE_SECONDS,
                 check=_CHECK_SECONDS, watch_wayland=True, passthrough=False,
                 backend=None):
        self.display = display
        self.upstream = upstream
        self.upstream_num, _screen = x11_mini._parse_display(upstream)
        self.sel = selectors.DefaultSelector()
        self.clients = {}                 # fileno -> (ClientConn, "down"|"up")
        self.conns = []
        self.idle = idle
        self.check = check
        self.debug = bool(os.environ.get(DEBUG_ENV))
        self._log = log
        self._own_log = log is None
        self._events = {}                 # fileno -> the events it is registered for
        self._paused = {}                 # fileno -> is its peer's buffer deep?
        self.stopping = False
        self.reason = None
        self.last_client_left = time.monotonic()
        self.live = 0
        self.wayland_socket = None
        self._wake_r = self._wake_w = None
        #: Every window-backend call in this process is made under this lock:
        #: from the loop thread (through `Shadows`) and from the pump's thread,
        #: which is what `wdotool/daemon.py` already does. The costs make it
        #: safe -- `list()` 0.08 ms and `get_desktop()` 0.02 ms on sway,
        #: `detect()` 0.7 ms [recon/seams.md 2.2, 2.3] -- and the bound on the
        #: worst case is the backend's own 10 s timeout, which is R13's row:
        #: a wedged compositor delays every client by at most that and loses no
        #: byte, because the bytes are in the out-buffers either way.
        self.block = threading.Lock()
        #: forward everything and synthesize nothing (`--passthrough`, and the
        #: mode a box with no Wayland session falls into by itself)
        self.passthrough = bool(passthrough)
        #: the proxy's own upstream connection, opened at the FIRST ACCEPT and
        #: never at startup: a proxy that dialled at startup would pin an
        #: Xwayland nobody asked for (design section 2.6, recon/env.md 6)
        self.own = None
        self.shadows = None
        self.events_pump = None
        self.backend = backend
        self._backend_tried = False
        #: the own connection's fd, remembered at registration: `fileno()` is
        #: -1 once the socket is closed, and a registration left behind under a
        #: number the kernel hands out again is a socket the loop never reads
        self._own_fd = None
        #: request handlers, keyed by `client.Request.key`: the opcode for a
        #: core request, `(extension name, minor)` for an extension one. Empty
        #: here -- every policy row is PASS in this batch -- and a class with no
        #: handler forwards, so an unwritten row behaves exactly like X.
        self.handlers = {}
        if watch_wayland:
            hit = session.find_wayland_socket()
            self.wayland_socket = hit[2] if hit else None

    # -- the log --------------------------------------------------------------

    def _open_log(self):
        path = log_path()
        try:
            # O_NOFOLLOW and an owner check: /tmp is world-writable and the log
            # carries session diagnostics -- the rule wdotool/daemon.py:2140
            # already applies to its own log.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
            if os.fstat(fd).st_uid != os.geteuid():
                os.close(fd)
                raise OSError("log file is not ours")
        except OSError:
            fd = os.open(os.devnull, os.O_WRONLY)
        self._log = os.fdopen(fd, "a", buffering=1, encoding="utf-8")

    def say(self, text: str) -> None:
        if self._log is None:
            self._open_log()
        try:
            self._log.write("%.3f xw11[%d] %s\n" % (time.time(), os.getpid(), text))
        except (OSError, ValueError):
            pass

    def debug_say(self, text: str) -> None:
        if self.debug:
            self.say(text)

    def log_request(self, conn, opcode, byte1, nbytes) -> None:
        """One line per request under `XW11_DEBUG=1`: the opcode's name, the
        sequence it consumed and its length. The extension major is resolved
        from what this connection's own QueryExtension replies said, never from
        a number written down here (RANDR is 140 on Xvfb and 139 on Xwayland)."""
        if not self.debug:
            return
        ext = conn.major_ext.get(opcode) if opcode >= 128 else None
        self.say("req seq=%d %s len=%d"
                 % (conn.seq, policy.request_name(opcode, byte1, ext), nbytes))

    def log_packet(self, conn, code, seq, nbytes) -> None:
        """One line per packet on the way down: reply, error or event."""
        if not self.debug:
            return
        if code == 1:
            what = "reply"
        elif code == 0:
            what = "error code=%d" % (conn.in_up[1] if len(conn.in_up) > 1 else 0)
        else:
            what = "event code=%d%s" % (code & 0x7F,
                                        " (SendEvent)" if code & wire.EV_SENT else "")
        self.say("s2c seq=%d %s len=%d" % (seq, what, nbytes))

    # -- the environment ------------------------------------------------------

    def adopt_upstream_environment(self) -> None:
        """This server's upstream, through the module function below."""
        adopt_upstream_environment(self.upstream, self.upstream_num)

    def reply_editor(self, name):
        """The editor a QueryExtension reply for `name` gets, or None. This is
        the seam design section 3.1 calls EDIT, and DRI3 is its only user in
        this stage."""
        return hide_present if name in HIDDEN_EXTENSIONS else None

    # -- the proxy's own connection, the registry, the pump ---------------------

    def ensure_upstream(self):
        """Open the proxy's own connection if it is not open, at the first
        accept and after an Xwayland restart. None in pass-through, and None
        when the upstream refused us -- every request still passes.

        The backend is detected FIRST, and **a proxy with nothing to shadow
        opens no connection at all**: with no Wayland session there are no ids
        to mint, no atoms to intern and no root events worth reading, because
        the policy table is not consulted in pass-through. That is also the
        difference between an X-only box seeing exactly the X server it would
        have seen without us and seeing one with an extra connection on it --
        which is not nothing, since a held connection is what keeps Xwayland
        from self-terminating [recon/env.md 6], and since the server hands out
        resource-id bases per connection [recon/wire.md 1.3]."""
        if self.passthrough:
            return None
        if self.own is not None and self.own.open:
            return self.own
        if self.ensure_backend() is None:
            return None
        if self.own is None:
            self.own = upstream_mod.OwnConn(self.upstream, log=self.say)
            self.own.on_event = self.on_root_event
        if not self.own.connect():
            return None
        self.say("own connection to %s: root 0x%x, ids 0x%x|n, %d atoms, %d "
                 "extensions" % (self.upstream, self.own.root,
                                 self.own.rid_base, len(self.own.atoms),
                                 len(self.own.majors)))
        self._want(self.own.sock, selectors.EVENT_READ)
        self._own_fd = self.own.fileno()
        self.clients[self._own_fd] = (self.own, "own")
        self.ensure_registry()
        return self.own

    def ensure_backend(self):
        """One backend for the whole proxy, detected once. `backend_detect`
        caches its bus, its `ListNames` and its registry connection
        [recon/seams.md 2.2], so "construct once and keep" is the shape the tree
        is already built for; `detect()` is 0.7 ms [recon/seams.md 2.2]. No
        session is not a refusal -- the proxy drops to pass-through and says
        so."""
        if self.backend is None and not self._backend_tried:
            self._backend_tried = True
            with self.block:
                self.backend = shadow_mod.detect_backend(self.say)
        if self.backend is None:
            self.passthrough = True
        return self.backend

    def ensure_registry(self):
        """The registry over that backend. Re-based rather than rebuilt when the
        upstream connection is, because the handles in it are the compositor's
        and only the X ids came from the connection that went away."""
        if self.passthrough or self.backend is None:
            return None
        if self.shadows is None:
            self.shadows = shadow_mod.Shadows(self.own, self.backend,
                                              log=self.say, block=self.block,
                                              on_backend=self.adopt_backend)
        else:
            self.shadows.own = self.own
            self.shadows.rebase()
        return self.shadows

    def adopt_backend(self, backend) -> None:
        """A re-detect replaced the backend: one for the whole proxy, so the
        loop's own reference and the pump's move with the registry's. Called
        with `block` held, from whichever thread noticed -- the registry's
        `NoSessionError` path on the loop thread, the pump's restart on its
        own."""
        self.backend = backend
        if self.shadows is not None:
            self.shadows.backend = backend
        if self.events_pump is not None:
            self.events_pump.backend = backend

    def close_own(self, why: str) -> None:
        """The upstream went away. Root id, atoms and extension majors survive
        an Xwayland restart; XIDs do not [recon/env.md 2.5, 6], so every shadow
        is forgotten here and reminted from the next connection's base."""
        if self.own is None:
            return
        # By the number it was registered under, never by `fileno()`: the framer
        # may have shut the socket already, and -1 would leave the selector
        # holding a registration the next connection inherits.
        fd = self._own_fd if self._own_fd is not None else self.own.fileno()
        self._own_fd = None
        if fd is not None and fd >= 0:
            self.clients.pop(fd, None)
            self._forget(fd)
        self.own.close()
        if self.shadows is not None:
            self.shadows.rebase()
        self.say("the proxy's own connection to %s is gone (%s): shadow ids "
                 "are reminted when the next client arrives" % (self.upstream, why))

    def on_root_event(self, pkt: bytes) -> None:
        """One packet on the proxy's own connection. Every create, destroy,
        map, unmap, configure and root property write in the X plane means the
        registry's idea of who is an Xwayland window may be out of date, so the
        cheapest correct thing is to drop the cache: the next read re-lists and
        the diff decides (design section 2.4 step 1)."""
        if self.shadows is not None:
            self.shadows.invalidate()

    def ensure_pump(self):
        """The pump, built once the wake pipe exists. It writes one byte per
        token onto the SAME pipe `stop()` uses: the loop only has to be told
        that something happened, and the deque says what."""
        if self.events_pump is not None or self.backend is None:
            return self.events_pump
        if self._wake_w is None:
            return None
        self.events_pump = pump_mod.Pump(self.backend, self._wake_w,
                                         log=self.say, block=self.block,
                                         on_backend=self.adopt_backend)
        return self.events_pump

    def drain_pump(self) -> int:
        """Every token since the last wake, in one go, and ONE invalidation for
        the lot: a `new`, a `title` and a `focus` for the same `exec foot`
        arrived within 75 ms of each other on sway [recon/seams.md 2.3], and one
        re-list answers all three (design section 5.2)."""
        if self.events_pump is None:
            return 0
        tokens = self.events_pump.drain()
        if tokens and self.shadows is not None:
            self.shadows.invalidate()
        return len(tokens)

    # -- what the policy asks ---------------------------------------------------

    @property
    def majors(self):
        """name -> (major, first_event, first_error), from the proxy's own
        connection. Never a constant: RANDR is 139 on Xwayland and 140 on Xvfb
        [recon/env.md 2.5, recon/tools.md 7]."""
        return {} if self.own is None else self.own.majors

    def ext_for_major(self, major: int):
        for name, (num, _ev, _err) in self.majors.items():
            if num == major:
                return name
        return None

    def is_shadow(self, xid: int) -> bool:
        return self.shadows is not None and self.shadows.is_shadow(xid)

    def handle(self, conn, req, cls) -> bool:
        """One non-PASS request. True when the frame must NOT be forwarded.

        The handler tables are empty in this batch and a class with no handler
        forwards, so the shape is exercised and nothing is answered locally --
        which is what keeps the parity oracle byte-identical while the machinery
        lands. A handler answers with the packet for ANSWER, the reply editor
        for EDIT, and None for CONSUME (it has already done the side effect).
        """
        fn = self.handlers.get(req.key)
        if fn is None:
            return False
        try:
            got = fn(self, conn, req)
        except CmdError as e:
            # This compositor not doing that thing. X gives a client no error
            # when the window manager ignores a request, and neither does this
            # (design section 3.3): the sequence is still consumed for a
            # CONSUME, because the client counted it, and an ANSWER or an EDIT
            # falls back to what the upstream would have said.
            self.say("the %s backend refused %s (%s): the request is %s"
                     % (getattr(self.backend, "name", "?"),
                        policy.request_name(req.opcode, req.byte1, req.ext), e,
                        "silence on the wire" if cls == policy.CONSUME
                        else "forwarded"))
            if cls != policy.CONSUME:
                return False
            got = None
        if cls == policy.CONSUME:
            conn.consume()
            return True
        if cls == policy.ANSWER:
            if got is None:
                return False
            conn.answer(got, req.seq)
            return True
        if cls == policy.EDIT and got is not None:
            conn.edit(got, req.seq)
        return False

    # -- accept ---------------------------------------------------------------

    def connect_upstream(self):
        """A fresh connection to the upstream display, retried while sway is
        between Xwaylands. None when the deadline passes, and the caller answers
        the client a `Failed` setup naming the display -- what X does when the
        server is not there."""
        deadline = time.monotonic() + CONNECT_RETRY_SECONDS
        while True:
            sock = upstream_socket(self.upstream_num)
            if sock is not None:
                return sock
            if time.monotonic() >= deadline:
                return None
            time.sleep(CONNECT_RETRY_SLEEP)

    def accept(self, listener) -> None:
        try:
            down, _addr = listener.accept()
        except OSError:
            return
        down.setblocking(False)
        # The proxy's own connection first, and only at the first accept: it is
        # opened here rather than at startup so that a proxy nobody has used
        # pins no Xwayland (design section 2.6, recon/env.md 6). Before the
        # client's twin rather than after, so that the order the two setups
        # reach the server in -- which is the order their resource-id bases are
        # handed out in [recon/wire.md 1.3] -- is one this code chose. Either
        # order works: a client's base is its own whichever it gets, and the
        # handshake below is synchronous, so the two never interleave.
        self.ensure_upstream()
        up = self.connect_upstream()
        if up is None:
            self.say("no X server at %s: refusing a client" % self.upstream)
            try:
                down.setblocking(True)
                down.sendall(client_mod.failed_setup(
                    "xw11: no X server at %s" % self.upstream))
            except OSError:
                pass
            down.close()
            return
        conn = client_mod.ClientConn(down, up, self)
        self.conns.append(conn)
        self.clients[down.fileno()] = (conn, "down")
        self.clients[up.fileno()] = (conn, "up")
        self._want(down, selectors.EVENT_READ)
        self._want(up, selectors.EVENT_READ)
        self.live += 1
        self.debug_say("accept: client fd %d, upstream fd %d"
                       % (down.fileno(), up.fileno()))

    def close_conn(self, conn) -> None:
        if conn not in self.conns:
            return
        self.conns.remove(conn)
        for sock in (conn.down, conn.up):
            if sock is None:
                continue
            fd = sock.fileno()
            self.clients.pop(fd, None)
            self._want(sock, 0)
            self._events.pop(fd, None)
            self._paused.pop(fd, None)
            try:
                sock.close()
            except OSError:
                pass
        conn.state = client_mod.CLOSED
        self.live -= 1
        if self.live <= 0:
            self.live = 0
            self.last_client_left = time.monotonic()

    # -- the byte path --------------------------------------------------------

    def _read(self, sock):
        """One read, with the control buffer a `recv()` would drop on the floor.
        `b""` when there was nothing to take, None at EOF or error."""
        try:
            data, ancdata, _flags, _addr = sock.recvmsg(READ_SIZE, ANCILLARY_SIZE)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            return None
        for level, typ, payload in ancdata:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(payload[:len(payload) - (len(payload) % fds.itemsize)])
                for fd in fds:
                    os.close(fd)
                self.say("closed %d file descriptor(s) passed over the X socket: "
                         "forwarding them is not yet done here, and the route is "
                         "one sendmsg at the right offset in the byte stream "
                         "(AGENTS.md route 5, which is this proxy), at the cost "
                         "of knowing which reply each descriptor belongs to"
                         % len(fds))
        if not data:
            return None
        return data

    def _flush(self, sock, out) -> bool:
        """Write what the kernel will take. False when the socket is gone."""
        while out:
            try:
                sent = sock.send(memoryview(out)[:READ_SIZE])
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            if not sent:
                return False
            del out[:sent]
        return True

    def _want(self, sock, want) -> None:
        fd = sock.fileno()
        cur = self._events.get(fd, 0)
        if cur == want:
            return
        try:
            if not cur:
                if want:
                    self.sel.register(sock, want)
            elif not want:
                self.sel.unregister(sock)
            else:
                self.sel.modify(sock, want)
        except (KeyError, ValueError, OSError):
            pass
        self._events[fd] = want

    def _forget(self, fd: int) -> None:
        """One descriptor out of the selector by NUMBER, for a socket that is
        already closed: `_want` reads `sock.fileno()`, which is -1 by then, so
        it would leave the registration behind and `_want`'s `cur == want`
        short-circuit would then skip re-registering the next socket that
        happens to get this number."""
        if not self._events.pop(fd, None):
            return
        try:
            self.sel.unregister(fd)
        except (KeyError, ValueError, OSError):
            pass

    def _interest(self, conn) -> None:
        """Each socket registered for exactly what it needs: reading unless its
        PEER's out-buffer is deep, writing while it has bytes of its own. The
        hysteresis is the measured pair -- stop over 4 MiB, resume under 1 MiB --
        so a slow reader costs one pause and not one per packet."""
        for sock, out, peer_out, eof in (
                (conn.down, conn.out_down, conn.out_up, conn.down_eof),
                (conn.up, conn.out_up, conn.out_down, conn.up_eof)):
            if sock is None:
                continue
            fd = sock.fileno()
            if fd < 0:
                continue
            paused = self._paused.get(fd, False)
            paused = len(peer_out) > LOW_WATER if paused else len(peer_out) >= HIGH_WATER
            self._paused[fd] = paused
            want = selectors.EVENT_WRITE if out else 0
            # A socket at EOF is readable for ever; asking for EVENT_READ again
            # would spin the loop at 100% until the buffers drained.
            if not paused and not eof:
                want |= selectors.EVENT_READ
            self._want(sock, want)

    def service(self, conn, which, events) -> None:
        sock = conn.down if which == "down" else conn.up
        if sock is None:
            return
        if events & selectors.EVENT_READ:
            data = self._read(sock)
            if data is None:
                self._eof(conn, which, sock)
            elif data:
                if which == "down":
                    conn.feed_client(data)
                else:
                    conn.feed_server(data)
        if conn.down is not None and not self._flush(conn.down, conn.out_down):
            self.close_conn(conn)
            return
        if conn.up is not None and not self._flush(conn.up, conn.out_up):
            self.close_conn(conn)
            return
        if self._drained(conn):
            self.close_conn(conn)
            return
        self._interest(conn)

    def _eof(self, conn, which, sock) -> None:
        """One side stopped writing. Closing the pair here would throw away
        whatever the OTHER side's out-buffer still holds -- Xwayland going away
        one packet after a 1 MB reply, or a Failed setup written to a client
        that has not read it yet -- and design section 2.6 says the EOF reaches
        the other side AFTER those bytes, the way it does through a real server.
        So the read side is shut down, the write side keeps flushing, and
        `_drained` closes the pair when there is nothing left to hand over.

        The client half is not a half-close in practice: libxcb closes the
        socket outright, so this flag means "gone" and the pair goes as soon as
        the bytes already accepted for the upstream are written."""
        if which == "down":
            conn.down_eof = True
        else:
            conn.up_eof = True
        try:
            sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _drained(self, conn) -> bool:
        """Is there anything left this connection owes anyone? `closing` is the
        refusal path (the Failed reply is owed to the client first)."""
        if not (conn.closing or conn.down_eof or conn.up_eof):
            return False
        return not conn.out_down and not conn.out_up

    def service_own(self, events) -> None:
        """The proxy's own socket: replies to its own requests and the root's
        event stream. An EOF here is an Xwayland that went away, which is not a
        client's business -- each client's own twin gets its own EOF and passes
        it down as X does, and this connection is reopened at the next accept
        with the registry reminted."""
        if self.own is None:
            return
        if events & selectors.EVENT_WRITE:
            if not self.own.flush():
                self.close_own("the write end failed")
                return
        if events & selectors.EVENT_READ and not self.own.read_ready():
            self.close_own("EOF")
            return
        if self.own.open:
            self._want(self.own.sock, selectors.EVENT_READ
                       | (selectors.EVENT_WRITE if self.own.wants_write else 0))

    def pump(self, conn) -> None:
        """Write out whatever a client's buffers hold, without waiting for the
        selector. The path a locally written packet takes."""
        self.service(conn, "down", 0)

    # -- lifetime -------------------------------------------------------------

    def exit_reason(self):
        """Why this proxy should stop, or None. Nothing here can end a proxy
        that is in use: a connected client is "in use" for all three checks, so
        no exit ever cuts a request short or drops a connection somebody holds."""
        if self.live:
            return None
        if self.display.file_path and not os.path.exists(self.display.file_path):
            return "%s is gone" % self.display.file_path
        if self.wayland_socket and not os.path.exists(self.wayland_socket):
            return "the session's wayland socket %s is gone" % self.wayland_socket
        if self.idle and (time.monotonic() - self.last_client_left) >= self.idle:
            return "no client for %gs" % self.idle
        return None

    def stop(self, reason=None) -> None:
        self.stopping = True
        if reason and not self.reason:
            self.reason = reason
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"\1")
            except OSError:
                pass

    def _signal(self, signum, _frame):
        self.stop("signal %d" % signum)

    def install_signal_handlers(self) -> None:
        """SIGTERM and SIGINT end the loop through `display.release()`, so the
        sockets, the lock, the xauth entry and the display file go with the
        process. A wake pipe rather than a bare flag: `select` would otherwise
        sit out the rest of its 15-second tick before noticing."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._signal)
            except (ValueError, OSError):          # not the main thread
                pass

    def _open_wake_pipe(self):
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self.sel.register(self._wake_r, selectors.EVENT_READ, "wake")

    def _close_wake_pipe(self):
        for fd in (self._wake_r, self._wake_w):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_r = self._wake_w = None

    # -- the loop -------------------------------------------------------------

    def serve_forever(self) -> int:
        for sock in self.display.sockets():
            sock.setblocking(False)
            self.sel.register(sock, selectors.EVENT_READ, "listen")
        self._open_wake_pipe()
        self.say("listening on %s (the abstract name and %s), upstream %s"
                 % (self.display.name, self.display.fs_path, self.upstream))
        tick = self.check or 1.0
        try:
            while not self.stopping:
                for key, events in self.sel.select(tick):
                    if key.data == "listen":
                        self.accept(key.fileobj)
                        continue
                    if key.data == "wake":
                        try:
                            os.read(self._wake_r, 4096)
                        except OSError:
                            pass
                        self.drain_pump()
                        continue
                    entry = self.clients.get(key.fd)
                    if entry is None:
                        continue
                    conn, which = entry
                    if which == "own":
                        self.service_own(events)
                        continue
                    self.service(conn, which, events)
                reason = self.exit_reason()
                if reason:
                    self.stop(reason)
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self.events_pump is not None:
            self.events_pump.stop()
            self.events_pump = None
        self.close_own("the proxy is exiting")
        for conn in list(self.conns):
            self.close_conn(conn)
        for sock in self.display.sockets():
            try:
                self.sel.unregister(sock)
            except (KeyError, ValueError, OSError):
                pass
        self.say("exiting: %s" % (self.reason or "asked to"))
        self.display.release()
        try:
            if self._wake_r is not None:
                self.sel.unregister(self._wake_r)
        except (KeyError, ValueError, OSError):
            pass
        self._close_wake_pipe()
        try:
            self.sel.close()
        except OSError:
            pass
        if self._own_log and self._log is not None:
            try:
                self._log.close()
            except (OSError, ValueError):
                pass
            self._log = None
