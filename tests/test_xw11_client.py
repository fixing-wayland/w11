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
