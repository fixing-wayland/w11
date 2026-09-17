#!/usr/bin/env python3
"""xw11/client.py: one client's framing, its counters and its two buffers.

The three claims that matter, and the measurement behind each:

* **a 'B' client is refused, not mis-framed** (R12). Every field but byte 0 of a
  setup request is in the client's own byte order, so reading a 'B' client's
  lengths little-endian frames garbage. Nothing on this box has ever sent one --
  all ~20 connections captured during recon were 'l' [recon/wire.md 1.1].
* **BIG-REQUESTS is per connection, and a zero length before it is enabled gets
  the server's own answer** (R3, measured 2026-09-10 against Xvfb 21.1.22 and
  pinned as tests/fixtures/xw11/badlength-nobigreq.hex). xtrace treats any zero
  length as big; a real server does not, and the proxy is not xtrace.
* **the sequence delta is zero, for ever** [recon/wire.md 3.2a]. `client.seq`
  counts requests received from the client, and the upstream server's own count
  must equal it -- across the 16-bit wrap, and with the substitute that goes up
  in place of a request the proxy answered itself.

`ClientConn` reads and writes no socket: the server hands it bytes. So most of
this is the codec under a microscope, and `ProxyRig` is where the same claims are
made again with a kernel in the way.
"""

import binascii
import os
import struct
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import ProxyRig, _recvn                                # noqa: E402
from xw11 import client as client_mod                               # noqa: E402
from xw11 import wire                                               # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "xw11")

#: half the 16-bit sequence space, `client._SEQ_WINDOW`: how long an
#: unanswered sequence is remembered before the number can come round.
_SEQ_WINDOW = client_mod._SEQ_WINDOW

SETUP_L = struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0)
SETUP_B = struct.pack("<BxHHHHxx", 0x42, 11, 0, 0, 0)


def packets(name):
    out = []
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(binascii.unhexlify(line))
    return out


def established(server=None):
    """A ClientConn past its handshake, with BIG-REQUESTS not yet enabled."""
    conn = client_mod.ClientConn(None, None, server)
    conn.feed_client(SETUP_L)
    body = _fake_setup_body()
    conn.feed_server(struct.pack("<BxHHH", 1, 11, 0, len(body) // 4) + body)
    conn.out_down.clear()          # the handshake's bytes are SetupReply's subject
    conn.out_up.clear()
    return conn


def _fake_setup_body():
    """The smallest well-formed success body: one screen, no formats, a
    four-byte vendor."""
    screen = (struct.pack("<5I6HI4B", 0x5A, 0x20, 0, 0, 0, 1280, 720, 300, 200,
                          1, 1, 0x21, 0, 0, 24, 1)
              + struct.pack("<BxH4x", 24, 0))
    body = struct.pack("<4IHH8B4x", 1, 0x400000, 0x1FFFFF, 256, 4, 0xFFFF,
                       1, 0, 0, 0, 32, 32, 8, 255) + b"FAKE" + screen
    return body


class MsbRefused(unittest.TestCase):
    """R12."""

    def test_the_failed_reply_is_exactly_these_bytes(self):
        conn = client_mod.ClientConn(None, None)
        conn.feed_client(SETUP_B)
        reason = client_mod.MSB_REASON.encode("latin-1")
        expect = (struct.pack("<BBHHH", 0, len(reason), 11, 0,
                              wire.padlen(len(reason)) // 4)
                  + wire.pad4(reason))
        self.assertEqual(bytes(conn.out_down), expect)
        self.assertEqual(conn.out_down[0], 0)              # status Failed
        self.assertEqual(conn.out_down[1], len(reason))    # the reason's length
        self.assertEqual(len(conn.out_down) % 4, 0)

    def test_the_reason_names_the_route_and_the_cost(self):
        """AGENTS.md's rule: a missing feature is a gap of ours with a route,
        never a policy. Rung 5 is an X11 protocol proxy, which is what this is."""
        self.assertIn("xw11: MSB-first clients are not supported",
                      client_mod.MSB_REASON)
        self.assertIn("not yet", client_mod.MSB_REASON)
        self.assertIn("AGENTS.md route 5", client_mod.MSB_REASON)
        self.assertIn("at the cost of", client_mod.MSB_REASON)
        self.assertLess(len(client_mod.MSB_REASON), 256)

    def test_nothing_is_forwarded_and_the_connection_is_closing(self):
        conn = client_mod.ClientConn(None, None)
        conn.feed_client(SETUP_B + b"\x2b\x00\x01\x00")
        self.assertEqual(bytes(conn.out_up), b"")
        self.assertTrue(conn.closing)
        self.assertEqual(conn.state, client_mod.CLOSED)

    def test_an_l_client_is_forwarded_verbatim_cookie_and_all(self):
        """The counter-case that makes the one above mean something."""
        conn = client_mod.ClientConn(None, None)
        cooked = (struct.pack("<BxHHHHxx", 0x6C, 11, 0, 18, 16)
                  + wire.pad4(b"MIT-MAGIC-COOKIE-1") + b"\xa5" * 16)
        conn.feed_client(cooked)
        self.assertEqual(bytes(conn.out_up), cooked)
        self.assertEqual(len(cooked), 48)                  # recon/wire.md 1.1
        self.assertFalse(conn.closing)


class MsbRefusedLive(unittest.TestCase):
    def test_a_real_socket_gets_the_reply_and_then_eof(self):
        rig = ProxyRig()
        self.addCleanup(rig.stop)
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect("\0" + rig.display.fs_path)
        self.addCleanup(s.close)
        s.sendall(SETUP_B)
        head = _recvn(s, 8)
        self.assertEqual(head[0], 0)
        body = _recvn(s, struct.unpack_from("<H", head, 6)[0] * 4)
        self.assertEqual(body[:head[1]].decode("latin-1"), client_mod.MSB_REASON)
        self.assertEqual(s.recv(4096), b"", "the proxy kept an MSB client open")


class BadLengthNoBigreq(unittest.TestCase):
    """R3, reproduced from the capture."""

    def test_the_answer_is_the_measured_packet(self):
        req, answer = packets("badlength-nobigreq.hex")
        conn = established()
        conn.feed_client(b"\x2b\x00\x01\x00")              # seq 1, as in the capture
        conn.out_up.clear()
        conn.feed_client(req)                              # seq 2, the bad one
        self.assertEqual(bytes(conn.out_down), answer)
        self.assertEqual(conn.out_down[1], wire.ERR_LENGTH)
        self.assertEqual(struct.unpack_from("<H", conn.out_down, 2)[0], 2)
        self.assertEqual(conn.out_down[10], 43)            # the request's own major

    def test_the_bad_request_still_costs_a_sequence_number(self):
        """The half the packet does not show and the measurement did: the
        request after the bad one came back as seq 3, not seq 2. Upstream gets a
        NoOperation so its count matches ours for ever."""
        req, _answer = packets("badlength-nobigreq.hex")
        conn = established()
        conn.feed_client(b"\x2b\x00\x01\x00")
        conn.out_up.clear()
        conn.feed_client(req)
        self.assertEqual(conn.seq, 2)
        self.assertEqual(bytes(conn.out_up), wire.NOOP)
        conn.out_up.clear()
        conn.feed_client(b"\x2b\x00\x01\x00")
        self.assertEqual(conn.seq, 3)
        self.assertEqual(bytes(conn.out_up), b"\x2b\x00\x01\x00")

    def test_only_the_four_byte_header_is_eaten_and_the_rest_is_framed(self):
        """The count the 4-byte capture cannot show, measured on the same Xvfb
        (scratchpad/b1/r3c.py, in the fixture's comment): a zero-length header
        plus 16 bytes of body drew one BadLength for seq 2 and the request after
        it was seq 7, so the server ate the header and framed the 16 bytes as
        four requests. Here: four NoOperations after the bad header land as four
        requests upstream, the sequence reaches 6, and the GetInputFocus after
        them is 7."""
        conn = established()
        conn.feed_client(b"\x2b\x00\x01\x00")               # seq 1
        conn.out_up.clear()
        conn.out_down.clear()
        conn.feed_client(b"\x14\x00\x00\x00" + wire.NOOP * 4)
        self.assertEqual(len(conn.out_down), 32, "more than one error came back")
        self.assertEqual(conn.out_down[10], 20, "the error did not name GetProperty")
        self.assertEqual(conn.seq, 6, "the four requests after the bad one were lost")
        # The substitute for the bad request, then the four the client sent.
        self.assertEqual(bytes(conn.out_up), wire.NOOP * 5)
        conn.out_up.clear()
        conn.feed_client(b"\x2b\x00\x01\x00")
        self.assertEqual(conn.seq, 7, "the next request is not the measured seq 7")
        self.assertEqual(len(conn.in_down), 0, "bytes were left unframed")

    def test_the_connection_carries_on(self):
        req, _answer = packets("badlength-nobigreq.hex")
        conn = established()
        conn.feed_client(req + b"\x2b\x00\x01\x00")
        self.assertFalse(conn.closing)
        self.assertEqual(conn.state, client_mod.ESTABLISHED)


class BadLengthBigreq(unittest.TestCase):
    """The three big forms a server refuses, cut off Xvfb 2:21.1.22-1ubuntu1 on
    2026-09-16 with scripts/xw11-probe-bigreq.py.

    Each probe ran on its own connection: setup, `QueryExtension`, the
    extension's `Enable` -- whose reply advertised 4194303 words -- and the
    malformed request as seq 3, with four `NoOperation`s and a `GetInputFocus`
    behind it so the answer says how many bytes the server ate. Xwayland was NOT
    measured; the fixture comments say so.
    """

    def armed(self):
        """A connection with the bit set and two requests behind it, so the
        probe is seq 3 here as it was on the wire."""
        conn = established()
        conn.bigreq = True
        conn.feed_client(b"\x2b\x00\x01\x00")            # seq 1
        conn.feed_client(b"\x2b\x00\x01\x00")            # seq 2
        conn.out_up.clear()
        conn.out_down.clear()
        return conn

    def test_a_short_big_form_eats_four_bytes_like_the_server(self):
        """A big form claiming ONE word. The server answered one BadLength for
        seq 3 and then never answered the GetInputFocus five words behind it, so
        it ate the 4-byte header and re-framed from the length word -- and so
        does this. The 24 bytes left over are that re-framing: `01000000` read
        as a zero-length CreateWindow and `7f000100` as its 32-bit big length,
        a frame that never completes, exactly as upstream is now waiting."""
        req, answer = packets("badlength-bigreq-short.hex")
        self.assertEqual(req, struct.pack("<BBHI", 20, 0, 0, 1))
        conn = self.armed()
        conn.feed_client(req + wire.NOOP * 4 + b"\x2b\x00\x01\x00")
        self.assertEqual(bytes(conn.out_down), answer)
        self.assertEqual(struct.unpack_from("<H", conn.out_down, 2)[0], 3)
        self.assertEqual(conn.out_down[10], 20)             # the request's own major
        self.assertEqual(conn.seq, 3)
        self.assertEqual(bytes(conn.out_up), wire.NOOP)     # the substitute, alone
        self.assertEqual(len(conn.in_down), 24)
        self.assertFalse(conn.closing)

    def test_a_big_form_over_the_ceiling_is_one_bad_length_and_nothing_is_hoarded(self):
        """4194304 words, one over the ceiling the Enable reply named. The
        server's answer is the short form's answer byte for byte; what the proxy
        owes on top of it is the refusal to buffer toward a length no server
        would have honoured (1.25 MiB of filler used to sit in `in_down`
        with nothing forwarded and the sequence still at zero)."""
        req, answer = packets("badlength-bigreq-over.hex")
        self.assertEqual(req, struct.pack("<BBHI", 20, 0, 0,
                                          wire.BIGREQ_DEFAULT_CEILING + 1))
        short_req, short_answer = packets("badlength-bigreq-short.hex")
        self.assertEqual(answer, short_answer)
        self.assertNotEqual(req, short_req)
        conn = self.armed()
        conn.feed_client(req + wire.NOOP * 4 + b"\x2b\x00\x01\x00")
        self.assertEqual(bytes(conn.out_down), answer, "more than the one BadLength")
        self.assertEqual(conn.seq, 3)
        for _ in range(20):
            conn.feed_client(b"A" * 65536)                  # 1.25 MiB of filler
        # Two bounds, and they are about two different things. The first is the
        # re-framing: the 24 bytes the refusal leaves over -- the length word
        # and the tail, spelled out in badlength-bigreq-short.hex -- are read as
        # a fresh request, so the filler behind them frames and is handed on
        # rather than accumulating, and `in_down` never holds more than the feed
        # it is in the middle of plus those 24 (65304 bytes at the peak, 40908
        # when the loop ends; measured here 2026-09-17). The second is the
        # refusal to buffer toward a length past the ceiling, and it is the one
        # the pre-fix splitter failed: with the ceiling raised to 0x0FFFFFFF all
        # 1310748 bytes sit in `in_down` with `seq` stuck at 2 and not a byte
        # forwarded. The old first bound here was 4 * wire.BIGREQ_DEFAULT_CEILING
        # -- 16777212 bytes against 1.25 MiB of input, a number no behaviour
        # reachable from this test could reach.
        self.assertLess(len(conn.in_down), 65536 + 24, "the stream never re-framed")
        self.assertLess(len(conn.in_down), 1310728, "the filler is being hoarded")

    def test_a_zero_word_big_form_closes_the_connection(self):
        """Zero words: the server sent NOTHING and hung up. So nothing is
        written down here either -- no BadLength, no NoOperation upstream -- and
        the connection goes out the door `Server._drained` opens for the MSB
        refusal."""
        (req,) = packets("badlength-bigreq-zero.hex")
        self.assertEqual(req, struct.pack("<BBHI", 20, 0, 0, 0))
        conn = self.armed()
        conn.feed_client(req + wire.NOOP * 4 + b"\x2b\x00\x01\x00")
        self.assertEqual(bytes(conn.out_down), b"")
        self.assertEqual(bytes(conn.out_up), b"")
        self.assertTrue(conn.closing)
        self.assertEqual(conn.state, client_mod.CLOSED)

    def test_the_ceiling_is_learned_from_the_enable_reply(self):
        """The number is the server's to name, and the `BigReqEnable` reply the
        proxy forwards is the only place it is said. A server naming 100 words
        holds that connection to 100 words."""
        conn = established()
        name = b"BIG-REQUESTS"
        conn.feed_client(struct.pack("<BBHH2x", 98, 0, 2 + len(name) // 4, len(name)) + name)
        conn.feed_server(wire.reply(conn.seq, 0, struct.pack("<BBBB20x", 1, 199, 0, 0)))
        conn.feed_client(bytes([199, 0, 1, 0]))             # Enable, on major 199
        self.assertTrue(conn.bigreq)
        self.assertEqual(conn.big_ceiling, wire.BIGREQ_DEFAULT_CEILING)
        conn.feed_server(wire.reply(conn.seq, 0, struct.pack("<I20x", 100)))
        self.assertEqual(conn.big_ceiling, 100)
        conn.out_down.clear()
        conn.feed_client(struct.pack("<BBHI", 20, 0, 0, 101))
        self.assertEqual(len(conn.out_down), 32)
        self.assertEqual(conn.out_down[1], wire.ERR_LENGTH)
        # Four bytes eaten, as on every other path here: the length word is left
        # to be re-framed, which is what the server does with it.
        self.assertEqual(len(conn.in_down), 4)

    def enabled_but_unanswered(self):
        """A connection that has asked for BIG-REQUESTS and is still waiting:
        the bit is set by the request, the ceiling waits on the reply."""
        conn = established()
        name = b"BIG-REQUESTS"
        conn.feed_client(struct.pack("<BBHH2x", 98, 0, 2 + len(name) // 4, len(name)) + name)
        conn.feed_server(wire.reply(conn.seq, 0, struct.pack("<BBBB20x", 1, 199, 0, 0)))
        conn.feed_client(bytes([199, 0, 1, 0]))             # Enable, on major 199
        self.assertTrue(conn.bigreq)
        self.assertEqual(conn.big_ceiling, wire.BIGREQ_DEFAULT_CEILING)
        return conn, conn.seq

    def test_an_error_for_the_enable_leaves_the_ceiling_and_drops_the_marker(self):
        """A reply is not the only answer an Enable can draw. When the server
        sends an error for it instead, the ceiling stays the default -- and the
        sequence stops being watched, because sequences are 16 bits: a marker
        left standing would be matched by whatever reply came round to that
        number later, and that reply's word at offset 8 would be read as a
        maximum request length no server ever named."""
        conn, enable = self.enabled_but_unanswered()
        conn.out_down.clear()
        conn.feed_server(wire.error(wire.ERR_REQUEST, enable, 0, 199, 0))
        self.assertEqual(conn.big_ceiling, wire.BIGREQ_DEFAULT_CEILING)
        self.assertIsNone(conn._bigreq_pending)
        self.assertEqual(len(conn.out_down), 32, "the error is forwarded, whole")
        # The number comes round again; the reply behind it belongs to somebody
        # else, and 100 is not this connection's ceiling.
        conn.feed_client(wire.NOOP)
        conn.feed_server(wire.reply(enable, 0, struct.pack("<I20x", 100)))
        self.assertEqual(conn.big_ceiling, wire.BIGREQ_DEFAULT_CEILING)

    def test_an_enable_nobody_answers_ages_out_with_the_other_books(self):
        """The other drift: no reply and no error, so nothing lands on that
        sequence at all. `_forget_stale` ages the marker out on the same
        half-wrap window that protects the editors and `_qext_pending`, well
        before the number can be reused."""
        conn, enable = self.enabled_but_unanswered()
        self.assertEqual(conn._bigreq_pending, enable)
        conn.feed_client(wire.NOOP * (_SEQ_WINDOW + 1))
        self.assertIsNone(conn._bigreq_pending)
        conn.feed_server(wire.reply(enable, 0, struct.pack("<I20x", 100)))
        self.assertEqual(conn.big_ceiling, wire.BIGREQ_DEFAULT_CEILING)


class BigReqBit(unittest.TestCase):
    def test_the_bit_is_set_by_this_connections_own_enable(self):
        """The major comes off this connection's QueryExtension reply -- never
        from a number written down, because RANDR is 140 on Xvfb and 139 on
        Xwayland and BIG-REQUESTS could move the same way."""
        conn = established()
        name = b"BIG-REQUESTS"
        conn.feed_client(struct.pack("<BBHH2x", 98, 0, 2 + len(name) // 4, len(name)) + name)
        self.assertFalse(conn.bigreq)
        conn.feed_server(wire.reply(conn.seq, 0, struct.pack("<BBBB20x", 1, 199, 0, 0)))
        self.assertEqual(conn.ext_major["BIG-REQUESTS"], 199)
        conn.feed_client(bytes([199, 0, 1, 0]))            # Enable, on major 199
        self.assertTrue(conn.bigreq)

    def test_an_enable_on_some_other_extensions_major_does_not_set_it(self):
        conn = established()
        conn.feed_client(bytes([140, 0, 1, 0]))            # a major nobody resolved
        self.assertFalse(conn.bigreq)

    def test_a_four_megabyte_request_is_forwarded_intact(self):
        conn = established()
        conn.bigreq = True
        payload = bytes(range(256)) * (4 << 12)            # 4 MiB
        words = (8 + len(payload)) // 4
        big = struct.pack("<BBHI", 18, 8, 0, words) + payload
        for at in range(0, len(big), 4096):                # in a real read's mouthfuls
            conn.feed_client(big[at:at + 4096])
        self.assertEqual(bytes(conn.out_up), big)
        self.assertEqual(conn.seq, 1)
        self.assertEqual(len(big), 4 * 1024 * 1024 + 8)


class BigReqLive(unittest.TestCase):
    """The same, with a kernel in the way and a server on the other end."""

    def test_a_four_megabyte_change_property_arrives_whole(self):
        rig = ProxyRig()
        self.addCleanup(rig.stop)
        s, _body = rig.raw()
        srv = rig.upstream
        name = b"BIG-REQUESTS"
        s.sendall(struct.pack("<BBHH2x", 98, 0, 2 + len(name) // 4, len(name)) + name)
        reply = _recvn(s, 32)
        major = reply[9]
        self.assertEqual(major, 133)
        s.sendall(bytes([major, 0, 1, 0]))
        self.assertEqual(struct.unpack_from("<I", _recvn(s, 32), 8)[0], 4194303)

        prop = srv.intern("_XW11_BIG")
        typ = srv.intern("STRING")
        data = bytes(range(256)) * (4 << 12)               # 4 MiB
        head = struct.pack("<BBHIIIBxxxI", 18, 0, 0, srv.ROOTS[0], prop, typ,
                           8, len(data))
        big = struct.pack("<BBHI", 18, 0, 0, (len(head) + len(data)) // 4 + 1) + head[4:] + data
        s.sendall(big)
        s.sendall(b"\x2b\x00\x01\x00")                     # a sync, so we know it landed
        _recvn(s, 32)
        stored = srv.props[(srv.ROOTS[0], "_XW11_BIG")]
        self.assertEqual(len(stored[2]), len(data))
        self.assertEqual(stored[2], data)


class SeqIsUpstreamSeq(unittest.TestCase):
    def test_seventy_thousand_requests_cross_the_wrap_in_step(self):
        """70000 > 65536, so this crosses the 16-bit wrap. The fake counts what
        it received; the proxy counts what it forwarded; they are the same
        number or the whole design is wrong [recon/wire.md 3.2a]."""
        rig = ProxyRig()
        self.addCleanup(rig.stop)
        s, _body = rig.raw()
        n = 70000
        s.sendall(wire.NOOP * n)
        srv = rig.upstream
        deadline = time.monotonic() + 60
        while srv.noops < n and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(srv.noops, n)
        self.assertEqual(srv.wires[0].seq, n & 0xFFFF)
        self.assertEqual(rig.server.conns[0].seq, n & 0xFFFF)
        self.assertEqual(rig.server.conns[0].seq, srv.wires[0].seq)

    def test_the_count_is_masked_to_sixteen_bits(self):
        conn = established()
        conn.seq = 0xFFFF
        conn.feed_client(wire.NOOP)
        self.assertEqual(conn.seq, 0)


class Streaming(unittest.TestCase):
    """R10's unit half: a reply with no editor is passed through as it arrives,
    never held whole."""

    def test_the_head_goes_out_before_the_body_has_arrived(self):
        conn = established()
        conn.feed_client(wire.NOOP)
        head = struct.pack("<BBHI", 1, 0, 1, 50000) + b"\0" * 24
        conn.feed_server(head)
        self.assertEqual(len(conn.out_down), 32)
        self.assertEqual(conn.raw_remaining, 200000)
        conn.out_down.clear()
        conn.feed_server(b"\xa5" * 1000)
        self.assertEqual(len(conn.out_down), 1000)
        self.assertEqual(conn.raw_remaining, 199000)
        self.assertEqual(len(conn.in_up), 0, "a streamed body was buffered")

    def test_the_packet_after_a_streamed_one_frames_correctly(self):
        conn = established()
        conn.feed_server(struct.pack("<BBHI", 1, 0, 1, 2) + b"\0" * 24
                         + b"\xa5" * 8 + wire.error(3, 2, 7, 15, 0))
        self.assertEqual(len(conn.out_down), 40 + 32)
        self.assertEqual(conn.raw_remaining, 0)

    def test_a_generic_event_streams_like_a_reply(self):
        (ge,) = packets("geprobe-136.hex")
        conn = established()
        conn.feed_server(ge[:32])
        self.assertEqual(conn.raw_remaining, 104)
        conn.feed_server(ge[32:])
        self.assertEqual(bytes(conn.out_down), ge)


class Editors(unittest.TestCase):
    """The EDIT slot design section 3.1 calls for. DRI3's `present` byte is its
    only user in this stage, and the server owns the function."""

    class FakeServer:
        def __init__(self):
            self.said = []

        def reply_editor(self, name):
            return (lambda pkt: pkt[:8] + b"\0" + pkt[9:]) if name == "DRI3" else None

        def say(self, text):
            self.said.append(text)

        def log_request(self, *a):
            pass

        def log_packet(self, *a):
            pass

    def query(self, conn, name):
        raw = name.encode()
        conn.feed_client(struct.pack("<BBHH2x", 98, 0, 2 + len(wire.pad4(raw)) // 4,
                                     len(raw)) + wire.pad4(raw))

    def test_the_editor_rewrites_only_the_extension_it_was_asked_for(self):
        conn = established(self.FakeServer())
        self.query(conn, "DRI3")
        self.query(conn, "XTEST")
        self.assertEqual(sorted(conn.editors), [1])
        conn.feed_server(wire.reply(1, 0, struct.pack("<BBBB20x", 1, 151, 0, 0)))
        conn.feed_server(wire.reply(2, 0, struct.pack("<BBBB20x", 1, 132, 0, 0)))
        self.assertEqual(conn.out_down[8], 0, "DRI3 still says present")
        self.assertEqual(conn.out_down[9], 151, "the major moved")
        self.assertEqual(conn.out_down[32 + 8], 1, "XTEST was edited too")
        self.assertEqual(conn.out_down[32 + 9], 132)
        self.assertEqual(len(conn.out_down), 64, "an edit changed a length")

    def test_an_error_for_the_sequence_drops_the_editor(self):
        conn = established(self.FakeServer())
        self.query(conn, "DRI3")
        conn.feed_server(wire.error(wire.ERR_REQUEST, 1, 0, 98, 0))
        self.assertEqual(conn.editors, {})
        self.assertEqual(len(conn.out_down), 32)

    def test_an_editor_nobody_answers_is_forgotten_after_half_the_sequence_space(self):
        conn = established(self.FakeServer())
        self.query(conn, "DRI3")
        self.assertEqual(sorted(conn.editors), [1])
        for _ in range(32770):
            conn.feed_client(wire.NOOP)
        self.assertEqual(conn.editors, {})


class SetupReply(unittest.TestCase):
    def test_the_reply_is_forwarded_verbatim_and_parsed(self):
        """Verbatim because the resource-id-base in it is the CLIENT's, handed
        out by the server for this connection [recon/wire.md 1.3]."""
        conn = client_mod.ClientConn(None, None)
        conn.feed_client(SETUP_L)
        body = _fake_setup_body()
        pkt = struct.pack("<BxHHH", 1, 11, 0, len(body) // 4) + body
        conn.feed_server(pkt)
        self.assertEqual(bytes(conn.out_down), pkt)
        self.assertEqual(conn.state, client_mod.ESTABLISHED)
        self.assertEqual(conn.setup.rid_base, 0x400000)
        self.assertEqual(conn.setup.roots, [0x5A])

    def test_a_refused_setup_is_forwarded_and_closes_the_pair(self):
        conn = client_mod.ClientConn(None, None)
        conn.feed_client(SETUP_L)
        reason = b"Invalid MIT-MAGIC-COOKIE-1 key"
        pkt = (struct.pack("<BBHHH", 0, len(reason), 11, 0,
                           wire.padlen(len(reason)) // 4) + wire.pad4(reason))
        conn.feed_server(pkt)
        self.assertEqual(bytes(conn.out_down), pkt)
        self.assertTrue(conn.closing)

    def test_an_authenticate_reply_continues_the_exchange(self):
        """Design section 2.3: status 0 and 2 are forwarded verbatim and the
        pair closes when the CLIENT does. Status 2 means the mechanism wants
        more data, in a shape that belongs to the mechanism; a proxy that closed
        here would be the one thing that ended an exchange the server is still
        having. MIT-MAGIC-COOKIE-1 is the only mechanism anything in this
        project has measured and it answers 0 or 1, so this is the continuation
        forwarded, not a capture reproduced."""
        conn = client_mod.ClientConn(None, None)
        conn.feed_client(SETUP_L)
        reason = b"more, please"
        pkt = struct.pack("<B5xH", 2, wire.padlen(len(reason)) // 4) + wire.pad4(reason)
        conn.feed_server(pkt)
        self.assertEqual(bytes(conn.out_down), pkt)
        self.assertFalse(conn.closing, "the pair was closed on an Authenticate")
        self.assertEqual(conn.state, client_mod.AUTHENTICATING)
        conn.out_up.clear()
        conn.feed_client(b"\x01\x02\x03")     # the mechanism's own bytes, unframed
        self.assertEqual(bytes(conn.out_up), b"\x01\x02\x03")
        conn.out_down.clear()
        body = _fake_setup_body()
        ok = struct.pack("<BxHHH", 1, 11, 0, len(body) // 4) + body
        conn.feed_server(ok)
        self.assertEqual(bytes(conn.out_down), ok)
        self.assertEqual(conn.state, client_mod.ESTABLISHED)
        self.assertEqual(conn.setup.rid_base, 0x400000)

    def test_the_setup_reply_arriving_in_pieces_is_still_verbatim(self):
        conn = client_mod.ClientConn(None, None)
        conn.feed_client(SETUP_L)
        body = _fake_setup_body()
        pkt = struct.pack("<BxHHH", 1, 11, 0, len(body) // 4) + body
        for byte in pkt:
            conn.feed_server(bytes([byte]))
        self.assertEqual(bytes(conn.out_down), pkt)

    def test_the_reserved_state_of_design_2_3_is_there_and_empty(self):
        """The fields the batches after this one fill. Named here so that a
        rename is a failing test rather than a surprise."""
        conn = established()
        self.assertEqual(conn.placeholders, {})
        self.assertEqual(conn.editors, {})
        self.assertEqual(conn.masks, {})
        self.assertIsNone(conn.batch)
        self.assertEqual(conn.held, set())
        self.assertEqual(conn.buttons, set())
        self.assertEqual(len(conn.deferred), 0)


if __name__ == "__main__":
    unittest.main()
