"""The proxy's own connection to the upstream X server.

One more client of Xwayland, opened beside the per-client twins of
`xw11/client.py`, and the only one the proxy owns. It exists for four things a
forwarder cannot get from the byte stream it is forwarding:

* **resource ids.** The setup reply hands every connection a `resource-id-base`
  and a 21-bit mask, and the server promises no other client will ever be given
  an id in that range -- measured on the box's `:355`, three connections at once,
  0x00c00000 / 0x00e00000 / 0x01000000 stepping by mask+1 [recon/wire.md 7.1].
  Shadow ids are minted out of ours, so `BadIDChoice` for one is impossible and
  a shadow can never collide with a real Xwayland window.
* **atoms.** Atoms are server-global, not per connection: the same name interned
  on three connections came back 0x187 every time [recon/wire.md 7.2]. So the
  ~60 names of `policy.ATOMS` are interned once, here, and the ids handed to
  clients in synthesized properties are ids `GetAtomName` resolves upstream.
* **extension majors.** Per server, never per spec: RANDR is 139 on this
  Xwayland and 140 on Xvfb 21.1.22, XTEST 132 [recon/env.md 2.5, tools.md 7].
  Nothing in this package writes one down.
* **the root's own events.** `PropertyChange | SubstructureNotify` on the root,
  so the X plane's own creates, destroys, maps and property writes invalidate
  the cache of the shadow registry.

**Lazy, and held.** Nothing is opened at proxy start: Xwayland dies fifteen
seconds after its last client leaves and sway relaunches it lazily from the
`-listenfd`s it kept [recon/env.md 6], so a proxy that dialled at startup would
pin an X server nobody asked for. The connection opens at the first client
accept and is held while any client is connected and for `server._IDLE_SECONDS`
after the last one leaves -- which is the proxy's own lifetime, because a shadow
id is `rid_base | n` and a reopened connection re-bases every one of them
(design section 2.6). An upstream that goes away (an Xwayland restart) is an EOF
here: the connection closes, the next accept reopens it, and the registry is
reminted because ids are the one thing NOT stable across a restart -- root id,
atoms and extension majors all are [recon/env.md 2.5, 6].

**Asynchronous after startup.** `x11_mini.X11Conn` is a synchronous client with
one request in flight (`_wait_reply` blocks and drops stale replies,
x11_mini.py:418), which is the wrong shape for a socket that lives in the same
`selectors` loop as everything else. So the pieces are reused -- the cookie walk
of `_auth_candidates`, the setup parse (through `wire.parse_setup_reply`, the
same algorithm as `_handshake`'s), `_new_rid`'s allocator -- and the demux is
not: `send()` returns a sequence, a reply goes to that sequence's callback, and
events go to `on_event`. Only `open()` blocks, once, on its own five-step burst.
"""

import errno
import select
import socket
import struct
import time

from wdotool import x11_mini
from xw11 import policy, wire

#: The whole startup burst is pipelined and then waited for. Five seconds is
#: two and a half times the 2 s a client's own connect is retried for while sway
#: is between Xwaylands (wdotool/daemon.py:2053's shape), and the burst itself
#: is nothing like that: 65 requests in two round trips took **0.9-1.3 ms**
#: against the live X server on this box (:355, five opens, 2026-09-10; the
#: first open of a process is 13.3 ms with the imports in it). A deadline that
#: fires here means the server stopped answering, not that the burst is big.
OPEN_TIMEOUT = 5.0

#: `GetKeyboardMapping(first_keycode, count)`: the whole range the server
#: advertises, 8..255 [recon/env.md 2.5's "keycode range 8..255"]. Kept raw for
#: batch 6's keycode -> keysym table, which is the one thing XTEST cannot do
#: without (the daemon speaks evdev, not X keycodes [recon/seams.md 0.4]).
KEYMAP_FIRST = 8
KEYMAP_COUNT = 248

#: How long the dial is retried while the server is not there -- the daemon
#: client's own shape (wdotool/daemon.py:2053). Xwayland dies fifteen seconds
#: after its last client and sway relaunches it lazily from the `-listenfd`s it
#: kept [recon/env.md 6], so both this connection and a client's twin can arrive
#: in the gap; `xw11/server.py` imports these two for the twin's dial.
CONNECT_RETRY_SECONDS = 2.0
CONNECT_RETRY_SLEEP = 0.05

#: What the proxy selects on the root: every property write the X plane makes,
#: and every create/destroy/map/unmap/configure of a child of the root
#: (design section 2.4 step 1).
ROOT_EVENT_MASK = 0x00400000 | 0x00080000      # PropertyChange | SubstructureNotify

#: CWEventMask, the one value ChangeWindowAttributes is sent with here.
_CW_EVENT_MASK = 0x0800

#: The root events that mean the X plane's structure moved, so the pairing of
#: design section 4.2 may be stale: CreateNotify, DestroyNotify, UnmapNotify,
#: MapNotify, ConfigureNotify [recon/wire.md 4.4], plus MappingNotify (34),
#: which is the keyboard's and is kept for batch 6.
_STRUCTURE_EVENTS = frozenset((wire.EV_CREATE_NOTIFY, wire.EV_DESTROY_NOTIFY,
                               wire.EV_UNMAP_NOTIFY, wire.EV_MAP_NOTIFY,
                               wire.EV_CONFIGURE_NOTIFY, 34))


class UpstreamGone(Exception):
    """The own connection is not there: it was never opened, or the server went
    away. Never raised for a request that merely failed -- an X error goes to
    the callback like any other packet."""


def _sock_targets(num: int):
    """The two names of a display, abstract first -- libxcb's order, and the
    only one that still reaches a server whose filesystem socket was unlinked
    out from under it [recon/wire.md 9.1, 9.2]."""
    path = "%s/X%d" % (x11_mini._SOCK_DIR, num)
    return ("\0" + path, path)


class OwnConn:
    """The proxy's own upstream connection: ids, atoms, majors, the keymap and
    the root's events."""

    def __init__(self, display: str, xauthority: str = None, log=None,
                 open_timeout: float = OPEN_TIMEOUT):
        self.display = display
        self.num, _screen = x11_mini._parse_display(display)
        self.xauthority = xauthority
        self.open_timeout = open_timeout
        self._log = log
        self.sock = None
        self.setup = None
        self.root = 0
        #: name -> (major, first_event, first_error), from QueryExtension
        self.majors = {}
        #: both directions of the atom table; ids are the server's own
        self.atoms = {}
        self.atom_names = {}
        #: the raw GetKeyboardMapping reply, kept for batch 6
        self.keyboard_mapping = None
        #: upstream's own _NET_SUPPORTED, as atom ids (design section 4.6's union)
        self.upstream_supported = ()
        #: one packet at a time, from the selector
        self.on_event = None
        #: what the framer choked on, once it has; the connection is finished
        #: after one, and the caller does the closing
        self.framing_error = None
        self.seq = 0
        self._replies = {}
        self._in = bytearray()
        self._out = bytearray()
        self._raw_remaining = 0
        self._rid_base = 0
        self._rid_mask = 0
        self._rid_next = 0
        self.generation = 0
        #: why the last open failed, in the server's own words, and whether
        #: trying again could help: a connection-level error is transient (the
        #: server is starting, or resetting after its last client left), a
        #: `Failed` setup is not.
        self.refusal = None
        self._transient = True

    # -- the log --------------------------------------------------------------

    def say(self, text: str) -> None:
        if self._log is not None:
            self._log(text)

    # -- open, close ----------------------------------------------------------

    @property
    def open(self) -> bool:
        return self.sock is not None

    @property
    def rid_base(self) -> int:
        return self._rid_base

    @property
    def rid_mask(self) -> int:
        return self._rid_mask

    def fileno(self) -> int:
        return -1 if self.sock is None else self.sock.fileno()

    def connect(self) -> bool:
        """Dial the upstream and do the five steps of design section 2.4, in
        order, pipelined. True when the connection is up and every startup reply
        has arrived; False when the server is not there or refused us -- the
        caller stays in pass-through and says so, because a proxy without its
        own connection still forwards every byte.

        The whole open -- dial AND handshake -- is retried for
        `CONNECT_RETRY_SECONDS` while the failure is a **connection-level** one,
        which is the shape the daemon client already uses (daemon.py:2053) and
        which design section 2.6 asks for while sway is between Xwaylands. A
        server that answers a `Failed` setup is not retried: it said no, and
        saying it again changes nothing.

        The retry is not theoretical. **An X server resets when its last client
        disconnects**, and a connection that arrives during that reset is met
        with ECONNRESET mid-handshake -- measured against Xvfb 21.1.22 on
        2026-09-10, where a test class whose tests each leave the server
        clientless failed two runs in five without it. Xwayland's `-terminate`
        and sway's lazy relaunch [recon/env.md 6] are the same window, on
        purpose.
        """
        if self.sock is not None:
            return True
        deadline = time.monotonic() + CONNECT_RETRY_SECONDS
        while True:
            self._transient = True
            self.refusal = None
            self._one_open()
            if self.sock is not None:
                break
            if not self._transient or time.monotonic() >= deadline:
                self.say("the X server at %s refused the proxy's own connection "
                         "(%s): every request passes through untouched"
                         % (self.display, self.refusal or "not there"))
                return False
            time.sleep(CONNECT_RETRY_SLEEP)
        self.generation += 1
        try:
            self._startup()
        except (OSError, wire.WireError, UpstreamGone) as e:
            self.say("the proxy's own connection to %s failed during startup "
                     "(%s): every request passes through untouched"
                     % (self.display, e))
            self.close()
            return False
        self.sock.setblocking(False)
        return True

    def _one_open(self) -> None:
        """One walk of the cookie list, one socket per cookie, the way
        `X11Conn._connect` walks them (x11_mini.py:300): a refused setup leaves
        the socket spent, and the list always ends with the cookie-less attempt,
        which is what sway's Xwayland -- started with no `-auth` -- accepts
        [recon/env.md 2]."""
        sock = None
        for name, data in x11_mini._auth_candidates(self.num, self.xauthority):
            if sock is None:
                sock = self._dial_once()
                if sock is None:
                    self.refusal = "no socket at %s" % self.display
                    return
            if self._handshake(sock, name, data):
                self.sock = sock
                return
            sock.close()
            sock = None

    def _dial_once(self):
        for target in _sock_targets(self.num):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(target)
            except OSError:
                s.close()
                continue
            return s
        return None

    def _handshake(self, sock, name: bytes, data: bytes) -> bool:
        """One setup attempt, with the reply parsed by `wire.parse_setup_reply`
        rather than thrown away the way `x11_mini._handshake` throws it
        (x11_mini.py:317-365): the base and the mask in it are what every shadow
        id is minted from."""
        sock.settimeout(self.open_timeout)
        try:
            sock.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0,
                                     len(name), len(data))
                         + wire.pad4(name) + wire.pad4(data))
            head = self._recv_exact(sock, 8)
            (extra,) = struct.unpack_from("<H", head, 6)
            body = self._recv_exact(sock, extra * 4)
            setup = wire.parse_setup_reply(head, body)
        except (OSError, wire.WireError) as e:
            self.refusal = "%s" % e
            return False
        if setup.status != 1:
            self.refusal = (body[:head[1]] if setup.status == 0 else body)\
                .decode("latin-1", "replace").strip("\0")
            self._transient = False
            # Failed, or Authenticate -- nothing here speaks a second round of
            # any mechanism, and MIT-MAGIC-COOKIE-1, the only one measured
            # anywhere in this project, answers 0 or 1 [recon/wire.md 8]. The
            # reason is the server's own sentence and is what the log carries:
            # a client whose cookie the upstream refuses gets that same
            # sentence, so the two agree.
            return False
        self.setup = setup
        self.root = setup.roots[0]
        self._rid_base, self._rid_mask = setup.rid_base, setup.rid_mask
        self._rid_next = 0
        return True

    def _recv_exact(self, sock, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError(errno.EPIPE, "the upstream closed during setup")
            buf += chunk
        return buf

    def _startup(self) -> None:
        """Design section 2.4's five steps, in order, in two bursts.

        Pipelined because a round trip costs 70 us [recon/env.md 5.3]: the
        1 + 8 + len(ATOMS) + 1 requests of steps 1-4 go back to back and cost
        one wait instead of sixty. Step 5 is its own burst because it needs an
        answer from step 3 -- `GetProperty` names `_NET_SUPPORTED` by the id the
        server just handed back."""
        done = {"n": 0}
        want = 0
        self.select_root(ROOT_EVENT_MASK)
        for name in policy.WANTED_EXTENSIONS:
            self._query_extension(name, done)
            want += 1
        for name in policy.ATOMS:
            self._intern(name, done)
            want += 1
        self.send(wire.OP_GET_KEYBOARD_MAPPING, 0,
                  struct.pack("<BB2x", KEYMAP_FIRST, KEYMAP_COUNT),
                  on_reply=lambda pkt: self._took_keymap(pkt, done))
        want += 1
        self._drain_until(lambda: done["n"] >= want)
        for name, atom in policy.PREDEFINED_ATOMS.items():
            self.atoms.setdefault(name, atom)
            self.atom_names.setdefault(atom, name)
        supported = self.atom_id("_NET_SUPPORTED")
        if supported:
            self.send(wire.OP_GET_PROPERTY, 0,
                      struct.pack("<IIIII", self.root, supported, 0, 0, 0x3FFF),
                      on_reply=lambda pkt: self._took_supported(pkt, done))
            want += 1
            self._drain_until(lambda: done["n"] >= want)

    def _query_extension(self, name: str, done) -> None:
        raw = name.encode("latin-1")

        def took(pkt):
            done["n"] += 1
            if pkt[0] == 1 and pkt[8]:
                self.majors[name] = (pkt[9], pkt[10], pkt[11])
        self.send(wire.OP_QUERY_EXTENSION, 0,
                  struct.pack("<H2x", len(raw)) + wire.pad4(raw), on_reply=took)

    def _intern(self, name: str, done) -> None:
        raw = name.encode("latin-1")

        def took(pkt):
            done["n"] += 1
            if pkt[0] == 1:
                (atom,) = struct.unpack_from("<I", pkt, 8)
                if atom:
                    self.atoms[name] = atom
                    self.atom_names[atom] = name
        self.send(wire.OP_INTERN_ATOM, 0,
                  struct.pack("<H2x", len(raw)) + wire.pad4(raw), on_reply=took)

    def _took_keymap(self, pkt, done) -> None:
        done["n"] += 1
        if pkt[0] == 1:
            self.keyboard_mapping = pkt

    def _took_supported(self, pkt, done) -> None:
        done["n"] += 1
        if pkt[0] != 1:
            return
        (nitems,) = struct.unpack_from("<I", pkt, 16)
        got = pkt[32:32 + 4 * nitems]
        self.upstream_supported = struct.unpack("<%dI" % (len(got) // 4), got)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._replies.clear()
        del self._in[:]
        del self._out[:]
        self._raw_remaining = 0
        self.framing_error = None
        self.seq = 0

    # -- ids and atoms --------------------------------------------------------

    def new_xid(self) -> int:
        """One id out of our own range, through the allocator the tree already
        has: never the bare base, `XUnavailable` at wrap
        (x11_mini.py:552). Reused rather than written a second time, which is
        what recon/wire.md 7.1 asks for."""
        if self.sock is None:
            raise UpstreamGone("no upstream connection to mint ids from")
        return x11_mini.X11Conn._new_rid(self)

    def atom_id(self, name: str, default: int = 0) -> int:
        return self.atoms.get(name, default)

    def atom_name(self, atom: int):
        """The name of an atom id, from the table interned at open, else one
        synchronous `GetAtomName` upstream (cached). A client can intern
        anything at all -- `xprop -set _MY_THING` -- and the proxy has to be
        able to read what it wrote."""
        name = self.atom_names.get(atom)
        if name is not None:
            return name
        if self.sock is None:
            return None
        box = {}

        def took(pkt):
            box["name"] = None
            if pkt[0] == 1:
                (nlen,) = struct.unpack_from("<H", pkt, 8)
                box["name"] = pkt[32:32 + nlen].decode("latin-1", "replace")
        try:
            self.send(wire.OP_GET_ATOM_NAME, 0, struct.pack("<I", atom),
                      on_reply=took)
            self._drain_until(lambda: "name" in box)
        except UpstreamGone as e:
            # The one blocking call left after `open()`, and it is made from the
            # loop thread by a read handler. An upstream that went away here is
            # not that handler's death: the socket stays registered, the next
            # selector pass reads its EOF and `Server.close_own` does the
            # bookkeeping. None is the answer this method already has for "no
            # upstream to ask".
            self.say("asking the upstream for the name of atom %d failed (%s)"
                     % (atom, e))
            return None
        name = box.get("name")
        if name is not None:
            # Cached in both directions: a client that interned a name of its
            # own asks about it more than once, and a GetAtomName is a blocking
            # round trip on the loop thread.
            self.atoms.setdefault(name, atom)
            self.atom_names[atom] = name
        return name

    def randr_snapshot(self, timeout: float = None):
        """The upstream's RandR tables, read here and now (design section 7.4
        step 1).

        Ten round trips at 70 us [recon/env.md 5.3]: `GetScreenResourcesCurrent`
        first, because everything after it is keyed by the ids and the config
        timestamp that reply carries, then one `GetOutputInfo` per output, one
        `GetCrtcInfo` per crtc, `GetOutputPrimary` and `GetScreenInfo` in a
        single burst -- one wait instead of nine, the way `_startup` pipelines
        the atoms.

        `GetScreenResourcesCurrent` and not `GetScreenResources`: `Current` is
        what every xrandr write path asks for and it does not re-poll the
        hardware [recon/tools.md 7]. None when the extension is not there at
        all, which is a server the proxy has nothing to apply against."""
        from xw11 import randr as randr_mod
        entry = self.majors.get("RANDR")
        if not entry:
            return None
        op = entry[0]
        box = {}
        self.send(op, randr_mod.GET_SCREEN_RESOURCES_CURRENT,
                  struct.pack("<I", self.root),
                  on_reply=lambda pkt: box.__setitem__("res", pkt))
        self._drain_until(lambda: "res" in box, timeout)
        res = randr_mod.parse_screen_resources(box["res"])
        done = {"n": 0}
        want = 0

        def took(key):
            def fn(pkt):
                done["n"] += 1
                box[key] = pkt
            return fn
        for output in list(res.outputs):
            self.send(op, randr_mod.GET_OUTPUT_INFO,
                      struct.pack("<II", output, res.config_timestamp),
                      on_reply=took(("out", output)))
            want += 1
        for crtc in list(res.crtcs):
            self.send(op, randr_mod.GET_CRTC_INFO,
                      struct.pack("<II", crtc, res.config_timestamp),
                      on_reply=took(("crtc", crtc)))
            want += 1
        self.send(op, randr_mod.GET_OUTPUT_PRIMARY,
                  struct.pack("<I", self.root), on_reply=took("primary"))
        want += 1
        self.send(op, randr_mod.GET_SCREEN_INFO, struct.pack("<I", self.root),
                  on_reply=took("screen"))
        want += 1
        self._drain_until(lambda: done["n"] >= want, timeout)
        for output in list(res.outputs):
            res.outputs[output] = randr_mod.parse_output_info(
                box[("out", output)], output)
        for crtc in list(res.crtcs):
            res.crtcs[crtc] = randr_mod.parse_crtc_info(box[("crtc", crtc)],
                                                        crtc)
        res.primary = randr_mod.parse_output_primary(box["primary"])
        randr_mod.parse_screen_info(box["screen"], res)
        return res

    # -- requests -------------------------------------------------------------

    def select_root(self, mask: int) -> int:
        """ChangeWindowAttributes(root, CWEventMask, mask). No reply: what comes
        back is the root's own event stream."""
        return self.send(wire.OP_CHANGE_WINDOW_ATTRIBUTES, 0,
                         struct.pack("<III", self.root, _CW_EVENT_MASK, mask))

    def send(self, op: int, byte1: int = 0, body: bytes = b"",
             on_reply=None) -> int:
        """One request upstream. Returns the sequence it consumed, which is what
        a reply, an error and an event's watermark are all keyed by
        [recon/wire.md 3.2]."""
        if self.sock is None:
            raise UpstreamGone("the proxy's own connection is not open")
        body = wire.pad4(body)
        self._out += struct.pack("<BBH", op, byte1, 1 + len(body) // 4) + body
        self.seq = (self.seq + 1) & 0xFFFF
        if on_reply is not None:
            self._replies[self.seq] = on_reply
        self.flush()
        return self.seq

    def flush(self) -> bool:
        """Write what the kernel will take. False when the socket is gone --
        the caller closes and the next accept reopens."""
        while self._out:
            try:
                sent = self.sock.send(memoryview(self._out)[:1 << 16])
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            if not sent:
                return False
            del self._out[:sent]
        return True

    @property
    def wants_write(self) -> bool:
        return bool(self._out)

    # -- reading --------------------------------------------------------------

    def read_ready(self) -> bool:
        """Called from the selector when our socket is readable. False at EOF,
        at an error, or on a packet whose length word lies: the caller closes
        us -- it owns the selector registration and the fd table, and a socket
        this class shut by itself would leave both behind for the next
        connection to inherit the fd number of. The registry is reminted when we
        are opened again, because XIDs are the one thing an Xwayland restart
        does not keep [recon/env.md 2.5, 6]."""
        try:
            data = self.sock.recv(1 << 16)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            return False
        if not data:
            return False
        self._in += data
        return self._frame()

    def _frame(self) -> bool:
        while True:
            if self._raw_remaining:
                # A reply nobody asked to see: its head was already dropped and
                # the body goes the same way, without ever being held whole.
                take = min(self._raw_remaining, len(self._in))
                if not take:
                    return True
                del self._in[:take]
                self._raw_remaining -= take
                continue
            try:
                total = wire.split_server(self._in)
            except wire.WireError as e:
                self.framing_error = "%s" % e
                self.say("the proxy's own connection framed a lying packet "
                         "(%s); closing it, the next client reopens" % e)
                return False
            if total is None:
                return True
            code = self._in[0]
            (seq,) = struct.unpack_from("<H", self._in, 2)
            if code in (0, 1):
                cb = self._replies.get(seq)
                if cb is None:
                    del self._in[:32]
                    self._raw_remaining = total - 32
                    continue
                if len(self._in) < total:
                    # Not popped until the whole packet is here: a reply that
                    # arrives in two reads comes back through this loop, and a
                    # callback taken out on the first pass would leave the
                    # second pass treating its own reply as one nobody asked
                    # for. Measured against a live Xvfb, where the 6976-byte
                    # GetKeyboardMapping reply is the one that splits.
                    return True
                del self._replies[seq]
                pkt = bytes(self._in[:total])
                del self._in[:total]
                cb(pkt)
                continue
            if len(self._in) < total:
                return True
            pkt = bytes(self._in[:total])
            del self._in[:total]
            if self.on_event is not None and self.interesting(pkt):
                self.on_event(pkt)

    def interesting(self, pkt: bytes) -> bool:
        """Whether one packet on the root's own stream is worth a re-list.

        Every packet used to be: `Server.on_root_event` dropped the registry's
        cache for anything at all that arrived here. On a busy X plane that
        defeats the 20 ms TTL outright -- one `xprop -set` anywhere under the
        root costs the next read a whole `views()` -- so the set is narrowed to
        what design section 2.4 step 1 names.

        Create, Destroy, Map, Unmap and Configure are the structure of the X
        plane, and the X plane is where the pairing comes from: a window that
        appeared there may be the twin of a toplevel this registry is currently
        shadowing [design section 4.2]. `PropertyNotify` counts only for a name
        the proxy answers for itself (`policy.OVERRIDES`) -- `_NET_CLIENT_LIST`
        moving means the xwm just managed or dropped a window, and
        `_NET_ACTIVE_WINDOW` moving means the focus did. Every other property
        write under the root -- `RESOURCE_MANAGER`, a selection, a client's own
        state -- says nothing about which toplevels exist.

        `MappingNotify` (34) is here because batch 6's keycode table is fed
        from it; today `Server.on_root_event` only invalidates the registry,
        which costs one `list()` on a keymap change and is the cheapest correct
        thing until that batch branches on the code.
        """
        if len(pkt) < 32:
            return False
        code = pkt[0] & 0x7F
        if code in _STRUCTURE_EVENTS:
            return True
        if code != wire.EV_PROPERTY_NOTIFY:
            return False
        (atom,) = struct.unpack_from("<I", pkt, 8)
        return self.atom_names.get(atom) in policy.OVERRIDES

    def _drain_until(self, ready, timeout: float = None) -> None:
        """Block until `ready()` or the deadline. The two blocking reads after
        the handshake are here: `_startup`'s, once per open, and
        `randr_snapshot`'s, which happens at every `GrabServer` and costs
        `2 + outputs + crtcs` requests in two round trips of ~70 us
        [recon/env.md 5.3]. Everything else comes through the selector, and the
        RandR caller passes a deadline of its own
        (`randr.SNAPSHOT_TIMEOUT`, 1 s) rather than this connection's five so
        that a wedged upstream cannot stop the loop forwarding."""
        deadline = time.monotonic() + (self.open_timeout if timeout is None
                                       else timeout)
        while not ready():
            left = deadline - time.monotonic()
            if left <= 0:
                raise UpstreamGone("the upstream did not answer in %gs"
                                   % (self.open_timeout if timeout is None
                                      else timeout))
            # The write set too: after `open()` the socket is non-blocking, so
            # a `send()` that the kernel would not take whole leaves bytes in
            # `_out` -- and a request still in `_out` is a reply that never
            # comes, which would be this loop waiting out its whole deadline.
            want_w = [self.sock] if self.wants_write else []
            r, w, _x = select.select([self.sock], want_w, [], left)
            if w and not self.flush():
                raise UpstreamGone("the upstream stopped taking bytes")
            if not r:
                continue
            try:
                data = self.sock.recv(1 << 16)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as e:
                raise UpstreamGone("the upstream went away: %s" % e) from e
            if not data:
                raise UpstreamGone("the upstream closed the connection")
            self._in += data
            if not self._frame():
                raise UpstreamGone("the upstream framed a lying packet: %s"
                                   % self.framing_error)

