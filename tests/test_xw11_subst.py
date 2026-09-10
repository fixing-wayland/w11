#!/usr/bin/env python3
"""The two substitutes: what goes upstream when the proxy answers a request.

The whole file is R1 and its neighbours. A proxy that answers a request itself
still owes the upstream one sequence number for it, for ever, because a
divergence poisons replies AND events: an event carries the **watermark of the
last request the server processed** on that connection, not a number of its own
(measured 61/69/77 for seven NoOperations and a ChangeProperty
[recon/wire.md 3.2b]). So:

* a request that produces nothing for the client sends **`NoOperation`**
  (opcode 127, four bytes, no reply, no state);
* a request the proxy answers sends **`GetInputFocus`** (opcode 43), and the
  stored reply goes out where its 32-byte reply comes back -- **in stream
  order**.

The second one is what `Ordering` measures. libxcb sets `request_completed =
request_read - 1` on any reply or error it reads (`xcb_in.c: read_packet`), so a
local reply for request N+1 written before the forwarded reply for request N
arrives makes the client read NULL for N. `recon/wire.md 3.3` measured the other
end of the same rule: a proxy that sent NOTHING upstream for a locally answered
`QueryTree` hung `xdotool search --name .` for ever, and it took the 15 s
timeout to kill it. The negative twin here is written out and asserted, so the
positive test has a failing shape standing next to it.
"""

import os
import struct
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import FakeBackend, ProxyRig, _recvn, fake_window      # noqa: E402
from xw11 import policy, wire                                       # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

_MISSING = object()

#: A real X window id, in the shape Xwayland mints -- `(client << 21) | serial`,
#: client 2 being the first real client [recon/seams.md 3]. Nothing in the
#: registry knows it, so every request naming it passes.
REAL_WINDOW = 0x0040000C


def req(opcode, byte1=0, body=b""):
    """One request, in the ordinary (non-BIG-REQUESTS) form."""
    body = wire.pad4(body)
    return struct.pack("<BBH", opcode, byte1, 1 + len(body) // 4) + body


def get_geometry(win):
    return req(wire.OP_GET_GEOMETRY, 0, struct.pack("<I", win))


def query_tree(win):
    return req(wire.OP_QUERY_TREE, 0, struct.pack("<I", win))


def empty_tree_reply(seq, root):
    """QueryTree answered as design section 3.2 answers it on a shadow: root is
    the root, parent is the root, no children."""
    return wire.reply(seq, 0, struct.pack("<IIH14x", root, root, 0))


class SubstCase(unittest.TestCase):
    """A rig with the shadow side on, and a way to put one row in the policy
    table for the length of one test.

    The rows are installed here and not in `xw11/policy.py` on purpose: every
    row in this batch is PASS, so that the parity oracle through the proxy stays
    byte-identical while the machinery lands. What is under test is the
    machinery, and it needs exactly one row that is not PASS to be visible."""

    def rig(self, windows=(1,), **kw):
        kw.setdefault("backend", FakeBackend(
            windows=[fake_window(w, "w%d" % w) for w in windows]))
        kw.setdefault("passthrough", False)
        kw.setdefault("num", 34)
        kw.setdefault("upstream_num", 35)
        got = ProxyRig(**kw)
        self.addCleanup(got.stop)
        return got

    def install(self, rig, key, row, fn):
        table = policy.EXT_POLICY if isinstance(key, tuple) else policy.POLICY
        old = table.get(key, _MISSING)

        def restore():
            if old is _MISSING:
                table.pop(key, None)
            else:
                table[key] = old
        self.addCleanup(restore)
        table[key] = row
        rig.server.handlers[key] = fn

    def ready(self, rig, **kw):
        """A raw client, the proxy's own connection up, and one shadow minted.
        Returns (sock, shadow id)."""
        sock, _body = rig.raw(**kw)
        rig.wait_own()
        entries = rig.server.shadows.snapshot()
        self.assertTrue(entries and entries[0].shadow)
        return sock, entries[0].shadow

    def twin(self, rig):
        """The upstream connection belonging to the client, which is the second
        the fake accepted: the proxy's own is opened at the first accept, before
        the client's, so that neither depends on kernel scheduling."""
        rig.wait(lambda: len(rig.upstream.wires) >= 2, what="the client's twin")
        return rig.upstream.wires[1]


class Ordering(SubstCase):
    """R1."""

    def answer_query_tree(self, rig):
        def handler(server, conn, request):
            return empty_tree_reply(conn.seq, conn.setup.roots[0])
        self.install(rig, wire.OP_QUERY_TREE,
                     policy.Row(shadow=policy.ANSWER), handler)

    def pipelined(self, rig, sock, shadow):
        """A GetGeometry the proxy forwards, then a QueryTree it answers, sent
        back to back and never read in between -- which is what
        `xdotool search` does (`GetWindowAttributes` and `GetGeometry`
        pipelined, [recon/tools.md 4.1])."""
        twin = self.twin(rig)
        rig.upstream.hold_reply(1)
        sock.sendall(get_geometry(REAL_WINDOW) + query_tree(shadow))
        rig.wait(lambda: twin.seq >= 2, what="both requests upstream")
        time.sleep(0.05)
        rig.upstream.release()
        first = _recvn(sock, 32)
        second = _recvn(sock, 32)
        return first, second

    def test_the_forwarded_reply_comes_out_first_however_late_it_is(self):
        """The forwarded GetGeometry reply is held back by the server; the
        locally answered QueryTree must still not overtake it, because the
        client's libxcb would read the GetGeometry as "completed, no reply"."""
        rig = self.rig()
        self.answer_query_tree(rig)
        sock, shadow = self.ready(rig)
        first, second = self.pipelined(rig, sock, shadow)
        self.assertEqual(struct.unpack_from("<H", first, 2)[0], 1)
        self.assertEqual(first[0], 1)                       # a reply
        self.assertEqual(struct.unpack_from("<H", second, 2)[0], 2)
        self.assertEqual(second, empty_tree_reply(2, support.FakeXServer.ROOTS[0]))
        sock.close()

    def test_the_twin_that_writes_the_local_reply_at_once_reorders_them(self):
        """The negative twin, run: a handler that writes the reply into the
        client's buffer at dispatch time and consumes the request instead of
        placing it produces the two replies in the wrong order. Same bytes, same
        rig, one line different -- and the assertion above fails on it."""
        rig = self.rig()

        def handler(server, conn, request):
            conn.out_down += empty_tree_reply(conn.seq, conn.setup.roots[0])
            return None                                     # CONSUME: NoOperation
        self.install(rig, wire.OP_QUERY_TREE,
                     policy.Row(shadow=policy.CONSUME), handler)
        sock, shadow = self.ready(rig)
        first, second = self.pipelined(rig, sock, shadow)
        self.assertEqual(struct.unpack_from("<H", first, 2)[0], 2)
        self.assertEqual(struct.unpack_from("<H", second, 2)[0], 1)
        sock.close()


class NoOpConsumes(SubstCase):
    def test_fifty_consumed_requests_leave_the_fifty_first_at_sequence_51(self):
        """`recon/wire.md 3.2a` replayed through the proxy: 50 NoOperations then
        a GetInputFocus came back as sequence 51 against the real server, and it
        has to come back as 51 through this one -- every consumed request costs
        a sequence number upstream."""
        rig = self.rig()
        self.install(rig, wire.OP_NO_OPERATION, policy.Row(other=policy.CONSUME),
                     lambda server, conn, request: None)
        sock, _shadow = self.ready(rig)
        before = rig.upstream.noops
        sock.sendall(bytes(wire.NOOP) * 50 + bytes(wire.GET_INPUT_FOCUS))
        reply = _recvn(sock, 32)
        self.assertEqual(struct.unpack_from("<H", reply, 2)[0], 51)
        self.assertEqual(rig.upstream.noops - before, 50)
        twin = self.twin(rig)
        noops = [row for row in rig.upstream.log if row[0] == "NoOperation"]
        self.assertEqual([row[1] for row in noops[-50:]], list(range(1, 51)))
        self.assertEqual(twin.seq, 51)
        sock.close()

    def test_the_substitute_is_the_four_bytes_the_protocol_spells_it_with(self):
        self.assertEqual(wire.NOOP, b"\x7f\x00\x01\x00")
        self.assertEqual(wire.GET_INPUT_FOCUS, b"\x2b\x00\x01\x00")


class ZeroDelta(SubstCase):
    def test_an_event_carries_the_clients_own_request_count(self):
        """The sequence in an event is a watermark of the request stream
        [recon/wire.md 3.2b]. With substitution the delta between what the
        client counted and what the server counted is permanently zero, so the
        number in the event is the client's own count -- after a mix of
        consumed, answered and forwarded requests."""
        rig = self.rig()
        self.install(rig, wire.OP_NO_OPERATION, policy.Row(other=policy.CONSUME),
                     lambda server, conn, request: None)
        self.install(rig, wire.OP_QUERY_TREE, policy.Row(shadow=policy.ANSWER),
                     lambda server, conn, request:
                     empty_tree_reply(conn.seq, conn.setup.roots[0]))
        sock, shadow = self.ready(rig)
        twin = self.twin(rig)
        sock.sendall(bytes(wire.NOOP) * 2                # consumed
                     + query_tree(shadow)                # answered
                     + bytes(wire.GET_INPUT_FOCUS))      # forwarded
        answered = _recvn(sock, 32)
        forwarded = _recvn(sock, 32)
        self.assertEqual(struct.unpack_from("<H", answered, 2)[0], 3)
        self.assertEqual(struct.unpack_from("<H", forwarded, 2)[0], 4)
        rig.wait(lambda: twin.seq == 4, what="four requests upstream")
        rig.upstream.push_event(b"\x1c" + b"\0" * 31, conn_index=1)
        event = _recvn(sock, 32)
        self.assertEqual(event[0], wire.EV_PROPERTY_NOTIFY)
        self.assertEqual(struct.unpack_from("<H", event, 2)[0], 4)
        conn = rig.server.conns[0]
        self.assertEqual(conn.seq, 4)
        sock.close()


class ErrorPlaceholder(SubstCase):
    def test_a_synthesized_error_arrives_in_stream_order_with_the_live_layout(self):
        """An error the proxy makes has the same shape as one the server makes
        -- `00 09 0400 efbeadde 0000 0e` for a live BadDrawable
        [recon/wire.md 3.1] -- so Xlib's default handler prints the same lines
        it prints for a server error [recon/tools.md 9]."""
        rig = self.rig()

        def handler(server, conn, request):
            return wire.error(wire.ERR_WINDOW, conn.seq, request.xid,
                              request.opcode, 0)
        self.install(rig, wire.OP_QUERY_TREE, policy.Row(shadow=policy.ANSWER),
                     handler)
        sock, shadow = self.ready(rig)
        twin = self.twin(rig)
        rig.upstream.hold_reply(1)
        sock.sendall(get_geometry(REAL_WINDOW) + query_tree(shadow))
        rig.wait(lambda: twin.seq >= 2, what="both requests upstream")
        time.sleep(0.05)
        rig.upstream.release()
        first = _recvn(sock, 32)
        second = _recvn(sock, 32)
        self.assertEqual(first[0], 1)                       # the forwarded reply
        self.assertEqual(second[0], 0)                      # an error
        self.assertEqual(second[1], wire.ERR_WINDOW)
        self.assertEqual(struct.unpack_from("<H", second, 2)[0], 2)
        self.assertEqual(struct.unpack_from("<I", second, 4)[0], shadow)
        self.assertEqual(struct.unpack_from("<H", second, 8)[0], 0)      # minor
        self.assertEqual(second[10], wire.OP_QUERY_TREE)                 # major
        self.assertEqual(len(second), 32)
        sock.close()


class OneAnswerOnePlaceholder(SubstCase):
    def test_each_answer_sends_exactly_one_get_input_focus_and_swallows_its_reply(self):
        """One 32-byte round trip per locally answered request and not two, and
        the substitute's own reply never reaches the client: it is dropped where
        it stands and the stored packet is written in its place."""
        rig = self.rig()
        self.install(rig, wire.OP_QUERY_TREE, policy.Row(shadow=policy.ANSWER),
                     lambda server, conn, request:
                     empty_tree_reply(conn.seq, conn.setup.roots[0]))
        sock, shadow = self.ready(rig)
        sock.sendall(query_tree(shadow) * 3)
        got = [_recvn(sock, 32) for _ in range(3)]
        focus = [row for row in rig.upstream.log if row[0] == "GetInputFocus"]
        self.assertEqual([row[1] for row in focus], [1, 2, 3])
        for n, pkt in enumerate(got, start=1):
            self.assertEqual(pkt, empty_tree_reply(n, support.FakeXServer.ROOTS[0]))
        conn = rig.server.conns[0]
        self.assertEqual(conn.placeholders, {})
        sock.close()

    def test_a_placeholder_is_forgotten_after_half_the_sequence_space(self):
        """A reply that never came must not keep 32 bytes of state for ever
        (design section 2.3)."""
        from xw11 import client as client_mod
        conn = client_mod.ClientConn(None, None)
        conn.seq = 5
        conn.answer(b"x" * 32)
        self.assertEqual(list(conn.placeholders), [5])
        conn.seq = (5 + client_mod._SEQ_WINDOW + 1) & 0xFFFF
        conn._forget_stale()
        self.assertEqual(conn.placeholders, {})


class EditChangesLength(SubstCase):
    def test_an_editor_that_grows_a_reply_rewrites_its_length_word(self):
        """The EDIT class of design section 3.1: the reply is rewritten on the
        way back, the length word follows the new body and the SEQUENCE never
        moves -- which is what makes an edit free where an answer costs a
        placeholder."""
        rig = self.rig()
        rig.upstream.children = [0x111, 0x222]

        def handler(server, conn, request):
            def grow(pkt):
                (children,) = struct.unpack_from("<H", pkt, 16)
                body = pkt[32:] + struct.pack("<II", 0x333, 0x444)
                return (pkt[:16] + struct.pack("<H", children + 2) + pkt[18:32]
                        + body)
            return grow
        self.install(rig, wire.OP_QUERY_TREE, policy.Row(root=policy.EDIT),
                     handler)
        sock, _shadow = self.ready(rig)
        root = support.FakeXServer.ROOTS[0]
        sock.sendall(query_tree(root))
        head = _recvn(sock, 32)
        (words,) = struct.unpack_from("<I", head, 4)
        self.assertEqual(words, 4)                      # two children, then two more
        self.assertEqual(struct.unpack_from("<H", head, 2)[0], 1)
        self.assertEqual(struct.unpack_from("<H", head, 16)[0], 4)
        body = _recvn(sock, 4 * words)
        self.assertEqual(struct.unpack("<4I", body), (0x111, 0x222, 0x333, 0x444))
        sock.close()

    def test_an_editor_that_keeps_the_length_leaves_the_word_alone(self):
        """DRI3's `present` byte is the measured case: a one-byte edit moves no
        sequence and rewrites no length (recon/tools.md 1)."""
        from xw11 import client as client_mod
        pkt = wire.reply(7, 0, b"\1" + b"\0" * 23)
        out = client_mod.ClientConn._apply_editor(
            lambda p: p[:8] + b"\0" + p[9:], pkt)
        self.assertEqual(len(out), len(pkt))
        self.assertEqual(out[:8], pkt[:8])
        self.assertEqual(out[8], 0)


if __name__ == "__main__":
    unittest.main()
