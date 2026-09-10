"""One downstream client and the upstream connection that is its twin.

Why a twin and not one shared connection: the server hands **each connection its
own `resource-id-base`**, stepping by `mask+1` -- 0x00c00000, 0x00e00000,
0x01000000 for three simultaneous connections (recon/wire.md 1.3). Two clients
behind one upstream connection would be handed the same base and would collide,
so every client gets its own, its setup is forwarded verbatim, and the base it
reads is really its own. The proxy's own pool is one more connection of its own
(design section 2.4), never a client's.

The state machine (design section 2.3):

    ACCEPTED  --12-byte head + padded strings-->  SETUP_SENT  --reply-->  ESTABLISHED

with one refusal: a client whose byte 0 is `'B'` (MSB-first) is answered a
`Failed` setup and closed. Every field but byte 0 of a setup request is in the
client's own byte order, so a 'B' client's lengths read here are wrong, and
mis-framing it silently is the alternative. Nothing on this box has ever sent one
-- all ~20 connections captured during recon were `'l'` (recon/wire.md 1.1) --
and `x11_mini`'s own docstring already says "byte order 'l' only". Byte-swapping
a 'B' client is AGENTS.md route 5 -- rung 5 is an X11 protocol proxy, which is
what this is -- and costs a swap of every field of every request and reply the
proxy reads; it is a row in the table, not a policy.

Sequence numbers are the thing that must never drift: every request costs one
upstream, `NoOperation` included, and an event carries the watermark of the last
request processed rather than its own number (recon/wire.md 3.2). So `seq` counts
requests RECEIVED from the client -- including the ones the proxy answers itself,
which go up as a substitute -- and equals upstream's count for ever.
"""

import collections
import struct
from typing import NamedTuple

from xw11 import policy, wire

#: What a 'B' client is told. A `Failed` reason is one byte of length, so it has
#: 255 bytes to say it in -- and it says the route and the cost, because
#: AGENTS.md's rule is that a missing feature is a gap of ours with a route,
#: never a policy. The rung is 5 because rung 5 IS an X11 protocol proxy: every
#: gap inside xw11 closes with more of xw11.
MSB_REASON = ("xw11: MSB-first clients are not supported -- not yet: the route "
              "is byte-swapping every field the proxy reads (AGENTS.md route 5, "
              "which is this proxy), at the cost of a swap on the byte path for "
              "a client nothing measured has ever sent")
assert len(MSB_REASON) < 256, "a Failed reason's length is one byte"

SETUP = "SETUP"
SETUP_SENT = "SETUP_SENT"
#: The upstream answered status 2, Authenticate: more authentication data is
#: owed, in a shape that belongs to the auth protocol and not to X. Nothing here
#: parses it -- every byte the client sends goes up verbatim and the upstream's
#: next setup reply decides -- because the only mechanism this project has ever
#: measured is MIT-MAGIC-COOKIE-1, which answers 0 or 1 and never 2.
AUTHENTICATING = "AUTHENTICATING"
ESTABLISHED = "ESTABLISHED"
CLOSED = "CLOSED"

#: An editor or a placeholder older than half the sequence space is one whose
#: reply is never coming (design section 2.3).
_SEQ_WINDOW = 32768


#: Where the window a request names lives, which is the axis design section 3.2
#: writes its table on. The three names are the three fields of `policy.Row`, so
#: the class is `getattr(row, target)` and there is no second table mapping one
#: to the other.
SHADOW = "shadow"
ROOT = "root"
OTHER = "other"


class Request(NamedTuple):
    """One request, decoded exactly as far as the policy needs it: the frame it
    came in, its opcode and second byte, the extension it belongs to (by NAME,
    never by major -- majors are per server [recon/env.md 2.5]), the window it
    names and where that window lives."""

    frame: bytes
    opcode: int
    byte1: int
    ext: str = None
    minor: int = 0
    target: str = OTHER
    xid: int = 0
    seq: int = 0

    @property
    def key(self):
        """What a handler is registered under: the opcode for a core request,
        `(extension name, minor)` for an extension one."""
        return self.opcode if self.ext is None else (self.ext, self.minor)


def failed_setup(reason: str) -> bytes:
    """A `Failed` setup reply: status 0, the reason's length in byte 1, the
    protocol version, the padded reason (recon/wire.md 1.2)."""
    raw = reason.encode("latin-1", "replace")
    return (struct.pack("<BBHHH", 0, len(raw), 11, 0, wire.padlen(len(raw)) // 4)
            + wire.pad4(raw))


class ClientConn:
    """The framing, the counters and the two out-buffers for one client.

    No socket is read or written here: `xw11/server.py` owns the selector and
    hands bytes in, which is what makes every rule below testable without a
    kernel in the way.
    """

    def __init__(self, down, up, server=None):
        self.down = down
        self.up = up
        self.server = server
        self.state = SETUP
        #: bytes read and not yet framed
        self.in_down = bytearray()
        self.in_up = bytearray()
        #: bytes framed and not yet written
        self.out_down = bytearray()
        self.out_up = bytearray()
        #: requests received; equal to upstream's count, for ever
        self.seq = 0
        #: this connection's BIG-REQUESTS bit -- per connection, never global
        self.bigreq = False
        #: bytes of a reply/GenericEvent still to pass through untouched
        self.raw_remaining = 0
        #: bytes of a SUBSTITUTED reply still to drop on the floor
        self.raw_drop = 0
        #: the parsed setup reply (status 1 only)
        self.setup = None
        #: design section 2.3's reserved fields; the batches after this one fill them
        self.placeholders = {}
        self.editors = {}
        self.masks = {}
        self.batch = None
        self.held = set()
        self.buttons = set()
        self.deferred = collections.deque()
        #: extension name -> major, learned from this connection's own
        #: QueryExtension replies. Majors are server-global but are learned per
        #: connection here because that is where they go past.
        self.ext_major = {}
        self.major_ext = {}
        self._qext_pending = {}
        #: set when a refusal owes the client its last bytes and then the door
        self.closing = False
        #: one side read EOF. The bytes already accepted for the OTHER side are
        #: still owed to it -- design section 2.6: an EOF upstream reaches the
        #: client as EOF, which means AFTER the bytes that came before it.
        self.down_eof = False
        self.up_eof = False

    # -- the client's side ----------------------------------------------------

    def feed_client(self, data: bytes) -> None:
        """Bytes from the client. Whatever can be framed is framed; the rest
        waits, however small the trickle."""
        self.in_down += data
        while not self.closing:
            if self.state == SETUP:
                if not self._setup_request():
                    return
            elif self.state == CLOSED:
                return
            elif self.state == AUTHENTICATING:
                # Not request-framed: this is the auth protocol's own
                # continuation, and it goes up exactly as it came in.
                if not self.in_down:
                    return
                self.out_up += self.in_down
                del self.in_down[:]
                return
            else:
                if not self._one_request():
                    return

    def _setup_request(self) -> bool:
        got = wire.parse_setup_request(self.in_down)
        if got is None:
            return False
        order, _nlen, _dlen, total = got
        if order != 0x6C:
            self.out_down += failed_setup(MSB_REASON)
            self.closing = True
            self.state = CLOSED
            return False
        if len(self.in_down) < total:
            return False
        self.out_up += self.in_down[:total]         # verbatim, cookie and all
        del self.in_down[:total]
        self.state = SETUP_SENT
        return True

    def _one_request(self) -> bool:
        got = wire.split_request(self.in_down, self.bigreq)
        if got is None:
            return False
        if got == wire.BAD_LENGTH:
            self._bad_length()
            return True
        nbytes, opcode, byte1 = got
        frame = bytes(self.in_down[:nbytes])
        del self.in_down[:nbytes]
        self.seq = (self.seq + 1) & 0xFFFF
        self.dispatch(frame, opcode, byte1)
        return True

    def _bad_length(self) -> None:
        """A zero 16-bit length from a connection that never enabled
        BIG-REQUESTS, answered the way the server answers it.

        Measured against Xvfb 21.1.22 from a raw socket (R3, 2026-09-10,
        tests/fixtures/xw11/badlength-nobigreq.hex): one 32-byte BadLength
        naming the request's own major with `bad` 0, the connection kept open,
        and -- the part the packet does not show -- the request still counted:
        the next request came back as seq 3, not seq 2. The same probe with an
        extension major measured `minor` echoed from byte 1 (BIG-REQUESTS' major
        133, minor 0 -> BadLength minor=0; minor 7, which that extension does
        not have, -> BadRequest minor=7 -- the server checks the minor's
        validity first, which the proxy cannot do without every extension's
        minor table and which is a row in the table, not a policy).

        Four bytes are consumed, because that is what the server consumed --
        measured, not inferred: a zero-length GetProperty header followed by 16
        bytes of body (four NoOperations) drew ONE BadLength for seq 2 and the
        GetInputFocus after it came back as seq 7, so the other 16 bytes were
        framed as four ordinary requests (scratchpad/b1/r3c.py, the same Xvfb,
        2026-09-10; it is in the fixture's comment). And a NoOperation goes
        upstream in its place, because the sequence delta must be zero for ever.
        """
        opcode = self.in_down[0]
        byte1 = self.in_down[1]
        del self.in_down[:4]
        self.seq = (self.seq + 1) & 0xFFFF
        minor = byte1 if opcode >= 128 else 0
        self.out_down += wire.error(wire.ERR_LENGTH, self.seq, 0, opcode, minor)
        self.out_up += wire.NOOP
        if self.server is not None:
            self.server.say("seq=%d zero 16-bit length with BIG-REQUESTS not "
                            "enabled: BadLength, major %d" % (self.seq, opcode))

    def dispatch(self, frame: bytes, opcode: int, byte1: int) -> None:
        """The policy, as a lookup and three ways to answer it.

        Two requests are watched on the way past whatever the table says,
        because both change how the bytes after them are framed or read: this
        connection's `QueryExtension` replies name the majors, and BIG-REQUESTS'
        `Enable` arms the splitter.

        Every row is PASS in this batch, so what runs below is the shape and not
        yet the behaviour: a row that is not PASS asks the server for the
        handler registered under `Request.key`, and **a class with no handler
        forwards**. PASS is the floor of this proxy, not a special case -- the
        splitter needs no opcode to forward [recon/wire.md 2], so a request
        nobody has written a handler for behaves exactly like X.

        A proxy in pass-through -- `--passthrough`, or a box with no Wayland
        session for the registry to read -- never leaves that floor: the table
        is not consulted at all, which is what makes the parity oracle's answer
        through the proxy the same bytes as its answer without one.
        """
        if opcode == wire.OP_QUERY_EXTENSION:
            self._watch_query_extension(frame)
        elif (opcode >= 128 and byte1 == wire.BIGREQ_ENABLE
                and self.major_ext.get(opcode) == "BIG-REQUESTS"):
            # From here on a zero 16-bit length is an 8-byte header. libX11 sends
            # this as request 2 of every connection, before anything else
            # (recon/wire.md 2, recon/tools.md 2), so it is not an edge case.
            self.bigreq = True
        req = self.decode(frame, opcode, byte1)
        cls = getattr(policy.lookup(opcode, req.ext, req.minor), req.target)
        if self.server is not None:
            self.server.log_request(self, opcode, byte1, len(frame))
        if cls != policy.PASS and self.server is not None \
                and not self.server.passthrough:
            if self.server.handle(self, req, cls):
                self._forget_stale()
                return
        self.out_up += frame
        self._forget_stale()

    def decode(self, frame: bytes, opcode: int, byte1: int) -> Request:
        """Which extension a major belongs to, which window the request names,
        and where that window lives.

        The extension is resolved from this connection's own QueryExtension
        replies first and from the proxy's own connection second: a client that
        asked gets the name it asked about, and one that inherited a major from
        somewhere else (a library with a cached table) still resolves, because
        majors are server-global [recon/wire.md 5.1].
        """
        ext = minor = None
        if opcode >= 128:
            minor = byte1
            ext = self.major_ext.get(opcode)
            if ext is None and self.server is not None:
                ext = self.server.ext_for_major(opcode)
        offset = policy.WINDOW_FIELD.get(opcode)
        xid = 0
        if offset is not None and len(frame) >= offset + 4:
            (xid,) = struct.unpack_from("<I", frame, offset)
        return Request(frame, opcode, byte1, ext, minor or 0,
                       self.where(xid), xid, self.seq)

    def where(self, xid: int) -> str:
        """`SHADOW` for an id the registry minted, `ROOT` for the setup's root,
        `OTHER` for everything else -- a real X window, a pixmap, a dead shadow.
        An unknown id passes and upstream answers `BadWindow` itself, with the
        serial the tools print [recon/tools.md 9]: inventing that error here
        would be more code for the same bytes (design section 3.1)."""
        if not xid:
            return OTHER
        if self.server is not None and self.server.is_shadow(xid):
            return SHADOW
        if self.setup is not None and xid in self.setup.roots:
            return ROOT
        return OTHER

    # -- the two substitutes (design section 3.1) ------------------------------

    def consume(self) -> None:
        """The request produces nothing for the client, and upstream gets a
        `NoOperation` in its place.

        Not "nothing goes up": a request the proxy eats still costs one sequence
        number upstream for ever, and a divergence poisons replies AND events,
        because an event carries the watermark of the last request the server
        processed rather than its own number (recon/wire.md 3.2). Measured
        against a real client: with nothing sent upstream, `xdotool search`
        wedges for ever -- libxcb waits for reply N+1 while the server's next
        reply carries N (recon/wire.md 3.3's `swallow` row). Four bytes,
        no state."""
        self.out_up += wire.NOOP

    def answer(self, packet: bytes, seq: int = None) -> None:
        """The proxy has the reply (or the error); upstream gets a
        `GetInputFocus` whose own 32-byte reply comes back **in stream order**
        carrying this sequence, and is replaced by `packet` on the way down.

        The placeholder is what keeps ordering exact with no opcode-has-reply
        table anywhere: libxcb sets `request_completed = request_read - 1` on
        any reply or error it reads (`xcb_in.c: read_packet`), so a local reply
        for request N+1 written before the forwarded reply for request N arrives
        makes the client read NULL for N. Cost: one 32-byte round trip per
        locally answered request, 70 us a trip [recon/env.md 5.3].
        `tests/test_xw11_subst.py::Ordering` is the measurement (R1)."""
        self.placeholders[self.seq if seq is None else seq] = bytes(packet)
        self.out_up += wire.GET_INPUT_FOCUS

    def edit(self, fn, seq: int = None) -> None:
        """The request is forwarded and its reply rewritten on the way back. The
        sequence never moves; a length-changing edit rewrites the length word
        (`_apply_editor`). An error for that sequence passes untouched and drops
        the editor -- the reply it belonged to is not coming."""
        self.editors[self.seq if seq is None else seq] = fn

    def _watch_query_extension(self, frame: bytes) -> None:
        try:
            (nlen,) = struct.unpack_from("<H", frame, 4)
            name = bytes(frame[8:8 + nlen]).decode("latin-1")
        except (struct.error, UnicodeDecodeError):
            return
        self._qext_pending[self.seq] = name
        fn = self.server.reply_editor(name) if self.server is not None else None
        if fn is not None:
            self.editors[self.seq] = fn

    def _forget_stale(self) -> None:
        if not self.editors and not self.placeholders and not self._qext_pending:
            return
        for book in (self.editors, self.placeholders, self._qext_pending):
            for seq in [s for s in book if (self.seq - s) % 0x10000 > _SEQ_WINDOW]:
                del book[seq]

    # -- the server's side ----------------------------------------------------

    def feed_server(self, data: bytes) -> None:
        """Bytes from upstream. A packet whose sequence has no editor and no
        placeholder is streamed: the 32-byte head goes out at once and the rest
        passes through as it arrives, so a 200 kB property (recon/tools.md 6) and
        the 400 MB every libX11 connection asks for on its fourth request
        (recon/tools.md 2) cost only the bytes actually sent."""
        self.in_up += data
        while True:
            if self.raw_drop:
                before = self.raw_drop
                self._drop_raw()
                if self.raw_drop and self.raw_drop == before:
                    return
                continue
            if self.raw_remaining:
                take = min(self.raw_remaining, len(self.in_up))
                if not take:
                    return
                self.out_down += self.in_up[:take]
                del self.in_up[:take]
                self.raw_remaining -= take
                continue
            if self.state in (SETUP_SENT, AUTHENTICATING):
                if not self._setup_reply():
                    return
                continue
            if not self._one_packet():
                return

    def _setup_reply(self) -> bool:
        if len(self.in_up) < 8:
            return False
        head = bytes(self.in_up[:8])
        (extra,) = struct.unpack_from("<H", head, 6)
        total = 8 + extra * 4
        if len(self.in_up) < total:
            return False
        body = bytes(self.in_up[8:total])
        self.out_down += self.in_up[:total]         # verbatim: the base is the client's
        del self.in_up[:total]
        if head[0] == 1:
            self.setup = wire.parse_setup_reply(head, body)
            self.state = ESTABLISHED
        elif head[0] == 2:
            # Authenticate: the exchange continues, and design section 2.3 says
            # status 0 and 2 are forwarded verbatim. Both directions stay open
            # -- the client's next bytes go up as they stand and the upstream's
            # next setup reply is read here again -- because a proxy that closed
            # here would be the one thing that ended an exchange a real server
            # is still having.
            self.state = AUTHENTICATING
        else:
            # Failed: the upstream says why and hangs up. The reason is already
            # in out_down and reaches the client before the door does.
            self.state = CLOSED
            self.closing = True
        return True

    def _one_packet(self) -> bool:
        total = wire.split_server(self.in_up)
        if total is None:
            return False
        (seq,) = struct.unpack_from("<H", self.in_up, 2)
        code = self.in_up[0]
        placeholder = self.placeholders.pop(seq, None) if code in (0, 1) else None
        editor = self.editors.get(seq) if code == 1 else None
        if editor is not None and len(self.in_up) < total:
            # An edited packet is held whole (design section 2.2). The log line
            # waits for the whole of it too: logging here would print the same
            # head again on every read until the tail arrived.
            return False
        if self.server is not None:
            self.server.log_packet(self, code, seq, total)
        if placeholder is not None:
            # The substitute's own reply, dropped where it stands, and ours
            # written in its place. A GetInputFocus reply is 32 bytes exactly,
            # so `total - 32` is zero here for every packet this proxy
            # substitutes; it is subtracted anyway so that a server answering
            # something longer for that sequence cannot leak a body into the
            # client's stream.
            del self.in_up[:32]
            self.raw_drop = total - 32
            self.out_down += placeholder
            self.editors.pop(seq, None)
            if self.raw_drop:
                self._drop_raw()
            return True
        if code == 0:
            # An error for a sequence drops its editor: the reply is not coming.
            self.editors.pop(seq, None)
        if editor is None:
            head = bytes(self.in_up[:32])
            if code == 1 and seq in self._qext_pending:
                # A QueryExtension reply is 32 bytes exactly, so the whole of it
                # is here even on the streaming path -- which is the path it
                # takes, because only DRI3's reply carries an editor. Learning
                # BIG-REQUESTS' major here is what arms the splitter.
                self._learn_extension(self._qext_pending.pop(seq), head)
            self.out_down += head
            del self.in_up[:32]
            self.raw_remaining = total - 32
            return True
        pkt = bytes(self.in_up[:total])
        del self.in_up[:total]
        del self.editors[seq]
        name = self._qext_pending.pop(seq, None)
        if name is not None:
            self._learn_extension(name, pkt)
        self.out_down += self._apply_editor(editor, pkt)
        return True

    def _drop_raw(self) -> None:
        """Bytes of a substituted reply's body that are here already. Whatever
        is not here yet is dropped by `feed_server` as it arrives."""
        take = min(self.raw_drop, len(self.in_up))
        del self.in_up[:take]
        self.raw_drop -= take

    @staticmethod
    def _apply_editor(editor, pkt: bytes) -> bytes:
        """The editor's answer, with the reply's length word rewritten when the
        edit changed the length (design section 3.1). The body is padded to the
        4-byte boundary the protocol counts in, and bytes 0-3 -- code, the
        reply-specific byte and the SEQUENCE -- are the editor's own, so an
        editor that moved a sequence is visible rather than silently corrected.
        """
        out = bytes(editor(pkt))
        if len(out) == len(pkt) or len(out) < 32 or out[0] != 1:
            return out
        body = wire.pad4(out[32:])
        return out[:4] + struct.pack("<I", len(body) // 4) + out[8:32] + body

    def _learn_extension(self, name: str, pkt: bytes) -> None:
        if len(pkt) >= 32 and pkt[0] == 1 and pkt[8]:
            self.ext_major[name] = pkt[9]
            self.major_ext[pkt[9]] = name
