#!/usr/bin/env python3
"""xw11/server.py: the loop, the buffers, the descriptors and the exits.

The four measurements this file exists to hold:

* **R10, backpressure.** `recon/env/fwd.py` is 61 lines and works until the
  reader is slower than the writer: with the peer socket non-blocking,
  `sendall()` raises BlockingIOError and the `except OSError` around it closes
  the pair, and 98 of 100 MB disappear with no error anywhere [recon/env.md 5.2].
  `NaiveForwarder` below is that shape, reproduced, so the passing test has a
  failing twin standing next to it.
* **R10, streaming.** A reply is never held whole: the 32-byte head goes out and
  the body passes through as it arrives, which is what makes xprop's 200 kB
  property [recon/tools.md 6] and the 400 MB every libX11 connection asks for on
  its fourth request [recon/tools.md 2] cost only the bytes actually sent.
* **R4, descriptors.** A plain `recv()` takes the payload and drops SCM_RIGHTS
  on the floor with no error at all [recon/env.md 2.6].
* **R15, the environment.** The proxy's own DISPLAY is the UPSTREAM's. A backend
  that reads the X plane itself must reach Xwayland; pointed at the proxy it
  would dial the loop it is running inside.
"""

import hashlib
import io
import os
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import ProxyRig, _recvn                                # noqa: E402
from w11common.errors import CmdError                               # noqa: E402
from wdotool import x11_mini                                        # noqa: E402
from xw11 import display as display_mod                             # noqa: E402
from xw11 import server as server_mod                               # noqa: E402
from xw11 import policy, wire                                       # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

HAVE_XVFB = subprocess.run(["sh", "-c", "command -v Xvfb"],
                           capture_output=True).returncode == 0

#: 100 replies of 1 MB. One 100 MB reply is not a thing a server may send: the
#: length FIELD of a reply is capped at 16 MiB (x11_mini._MAX_REPLY_WORDS), and
#: that cap is on the field and never on what a client asks for.
ONE_MEG = 1 << 20
HUNDRED_MEG = 100 * ONE_MEG


def reply_head(seq, words):
    return struct.pack("<BBHI", 1, 0, seq & 0xFFFF, words) + b"\0" * 24


class RigCase(unittest.TestCase):
    def rig(self, **kw):
        got = ProxyRig(**kw)
        self.addCleanup(got.stop)
        return got

    def logged(self, rig):
        """Point the server's log at a buffer and give back a reader for it."""
        buf = io.StringIO()
        rig.server._log = buf
        return buf


class Backpressure(RigCase):
    """R10."""

    def test_a_hundred_megabytes_through_a_slow_reader_arrives_complete(self):
        """The reader stops reading altogether until the proxy's out-buffer is
        past the high-water mark, which is the state `recon/env/fwd.py` lost
        98 of 100 MB in, and then drains. Every byte arrives, in order."""
        rig = self.rig()
        s, _body = rig.raw(timeout=180)
        deadline = time.monotonic() + 5
        while not rig.server.conns and time.monotonic() < deadline:
            time.sleep(0.01)
        conn = rig.server.conns[0]
        peak, inner = [0], conn.feed_server

        def feed_server(data):                      # runs on the server thread
            inner(data)
            peak[0] = max(peak[0], len(conn.out_down))
        conn.feed_server = feed_server

        sock = rig.upstream.wires[0].sock
        sent, got = hashlib.sha256(), hashlib.sha256()
        chunk = bytes(range(256)) * 256             # 64 KiB
        body_chunks = (ONE_MEG - 32) // len(chunk)

        def write():
            for n in range(HUNDRED_MEG // ONE_MEG):
                head = reply_head(n + 1, (ONE_MEG - 32) // 4)
                sock.sendall(head)
                sent.update(head)
                for _ in range(body_chunks):
                    sock.sendall(chunk)
                    sent.update(chunk)
                tail = b"\0" * ((ONE_MEG - 32) % len(chunk))
                if tail:
                    sock.sendall(tail)
                    sent.update(tail)

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        stall = time.monotonic() + 20
        while peak[0] < server_mod.HIGH_WATER and time.monotonic() < stall:
            time.sleep(0.01)
        self.assertGreaterEqual(peak[0], server_mod.HIGH_WATER,
                                "the out-buffer never filled, so nothing here "
                                "exercised backpressure")
        total = 0
        while total < HUNDRED_MEG:
            data = s.recv(1 << 20)
            self.assertTrue(data, "the pair was closed with %d of %d bytes to go"
                            % (HUNDRED_MEG - total, HUNDRED_MEG))
            got.update(data)
            total += len(data)
        writer.join(timeout=60)
        self.assertEqual(total, HUNDRED_MEG)
        self.assertEqual(got.hexdigest(), sent.hexdigest())
        self.assertLessEqual(peak[0], server_mod.HIGH_WATER + server_mod.READ_SIZE)

    def test_the_naive_forwarders_shape_loses_bytes(self):
        """The negative twin. This is `recon/env/fwd.py`'s loop, and it is why
        every direction in xw11/server.py has an out-buffer instead: the sender
        got its bytes into the forwarder and the sink never saw them, with no
        error anywhere -- 1.9 of 100 MB was what recon measured."""
        delivered, accepted = NaiveForwarder(4 * ONE_MEG).run()
        self.assertGreater(accepted, 0, "the twin never got started")
        self.assertLess(delivered, accepted,
                        "the naive shape delivered everything it took, so this "
                        "twin proves nothing on this box any more")

    def test_reading_stops_over_the_high_water_and_resumes_under_the_low(self):
        """The registration itself, not a timing: over 4 MiB the side that feeds
        the deep buffer is not registered for reading; it comes back under
        1 MiB, so a slow reader costs one pause and not one per packet.

        On a Server with no loop running, so that nothing drains the buffer
        underneath the assertions."""
        server, conn = idle_server(self)
        up = conn.up.fileno()
        self.assertTrue(server._events[up] & selectors.EVENT_READ)
        conn.out_down.extend(b"\0" * (server_mod.HIGH_WATER + 1))
        server._interest(conn)
        self.assertFalse(server._events[up] & selectors.EVENT_READ,
                         "the upstream side is still being read into a full buffer")
        del conn.out_down[:ONE_MEG]                     # 3 MiB: still over LOW_WATER
        server._interest(conn)
        self.assertFalse(server._events[up] & selectors.EVENT_READ,
                         "reading resumed before the low-water mark")
        del conn.out_down[:3 * ONE_MEG]                 # now under it
        server._interest(conn)
        self.assertTrue(server._events[up] & selectors.EVENT_READ)

    def test_the_side_with_bytes_of_its_own_is_registered_for_writing(self):
        server, conn = idle_server(self)
        down = conn.down.fileno()
        self.assertFalse(server._events[down] & selectors.EVENT_WRITE)
        conn.out_down.extend(b"x")
        server._interest(conn)
        self.assertTrue(server._events[down] & selectors.EVENT_WRITE)

    def test_the_two_marks_are_the_measured_pair(self):
        self.assertEqual(server_mod.HIGH_WATER, 4 << 20)
        self.assertEqual(server_mod.LOW_WATER, 1 << 20)


class NaiveForwarder:
    """`recon/env/fwd.py`, reduced to the one thing that was wrong with it: a
    `sendall` onto a non-blocking peer inside an `except OSError` that closes
    the pair. Returns how many of `nbytes` reached the sink."""

    def __init__(self, nbytes):
        self.nbytes = nbytes

    def run(self):
        left, right = socket.socketpair()
        up_a, up_b = socket.socketpair()
        right.setblocking(False)
        left.setblocking(False)
        got = [0]

        def sink():
            up_b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
            while got[0] < self.nbytes:
                try:
                    data = up_b.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                got[0] += len(data)
                time.sleep(0.001)

        def relay():
            up_a.setblocking(False)
            while True:
                try:
                    data = right.recv(65536)
                except BlockingIOError:
                    time.sleep(0.001)
                    continue
                except OSError:
                    return
                if not data:
                    return
                try:
                    up_a.sendall(data)          # the bug, verbatim
                except OSError:
                    return                      # ...and the silence
        threads = [threading.Thread(target=sink, daemon=True),
                   threading.Thread(target=relay, daemon=True)]
        for t in threads:
            t.start()
        accepted = 0
        payload = bytes(65536)
        deadline = time.monotonic() + 3
        while accepted < self.nbytes and time.monotonic() < deadline:
            try:
                accepted += left.send(payload)
            except BlockingIOError:
                time.sleep(0.001)
            except OSError:
                break
        for t in threads:
            t.join(timeout=5)
        for s in (left, right, up_a, up_b):
            s.close()
        return got[0], accepted


def idle_server(case):
    """A Server with a real display and a real client pair, and NO loop running
    -- so that a test can put bytes in a buffer and ask what the selector was
    told, with nothing draining it underneath."""
    d = tempfile.mkdtemp(prefix="xw11-idle-")
    case.addCleanup(shutil.rmtree, d, ignore_errors=True)
    old_sock, old_lock = x11_mini._SOCK_DIR, display_mod._LOCK_DIR
    x11_mini._SOCK_DIR = d
    display_mod._LOCK_DIR = d
    case.addCleanup(_restore_dirs, old_sock, old_lock)
    display = display_mod.allocate()
    case.addCleanup(display.release)
    devnull = open(os.devnull, "w")
    case.addCleanup(devnull.close)
    server = server_mod.Server(display, ":7", log=devnull, idle=0.0, check=0.05,
                               watch_wayland=False)
    case.addCleanup(server.sel.close)
    down_a, down_b = socket.socketpair()
    up_a, up_b = socket.socketpair()
    for s in (down_a, down_b, up_a, up_b):
        case.addCleanup(s.close)
    from xw11 import client as client_mod
    conn = client_mod.ClientConn(down_a, up_a, server)
    server.conns.append(conn)
    server._want(down_a, selectors.EVENT_READ)
    server._want(up_a, selectors.EVENT_READ)
    return server, conn


class Streaming(RigCase):
    """R10's other half: peak buffer, not elapsed time."""

    def push_one_meg(self, rig, s, edited=False):
        """One 1 MB reply from the fake, read by a thread that is already
        waiting, with both of the proxy's buffers measured after every read it
        does.

        Measured rather than sampled: a sampling thread saw zero every time,
        because moving a megabyte through the loop takes less than one GIL
        switch interval. The wrapper below runs ON the server thread, once per
        `recvmsg`, so what it records is what the buffers really held."""
        deadline = time.monotonic() + 5
        while not rig.server.conns and time.monotonic() < deadline:
            time.sleep(0.01)
        conn = rig.server.conns[0]
        if edited:
            conn.editors[1] = lambda pkt: pkt
        peak_in, peak_out, inner = [0], [0], conn.feed_server

        def feed_server(data):
            inner(data)
            peak_in[0] = max(peak_in[0], len(conn.in_up))
            peak_out[0] = max(peak_out[0], len(conn.out_down))
        conn.feed_server = feed_server
        got = []
        reader = threading.Thread(target=lambda: got.append(_recvn(s, ONE_MEG)),
                                  daemon=True)
        reader.start()
        sock = rig.upstream.wires[0].sock
        sock.sendall(reply_head(1, (ONE_MEG - 32) // 4))
        sock.sendall(bytes(ONE_MEG - 32))
        reader.join(timeout=60)
        self.assertEqual(len(got), 1, "the reply never arrived")
        self.assertEqual(len(got[0]), ONE_MEG)
        return conn, peak_in[0], peak_out[0]

    def test_a_one_megabyte_reply_is_never_held_whole_to_be_framed(self):
        """The framing buffer is what says "streamed": 32 bytes decide the
        length and the body passes through, so `in_up` never holds more than one
        read's worth however big the reply is."""
        rig = self.rig()
        s, _body = rig.raw(timeout=60)
        conn, peak_in, peak_out = self.push_one_meg(rig, s)
        self.assertLess(peak_in, 128 << 10,
                        "the reply was held whole instead of streamed")
        self.assertEqual(conn.raw_remaining, 0)
        self.assertLessEqual(peak_out, server_mod.HIGH_WATER + server_mod.READ_SIZE,
                             "the write buffer grew past the high-water mark")

    def test_the_same_reply_with_an_editor_on_it_IS_held_whole(self):
        """The negative twin, and the reason the streaming path exists: an EDIT
        has to see the whole packet, so its buffer really does reach a megabyte.
        Without this, a `raw_remaining` that never fired would pass the test
        above by holding nothing at all."""
        rig = self.rig()
        s, _body = rig.raw(timeout=60)
        _conn, peak_in, _peak_out = self.push_one_meg(rig, s, edited=True)
        self.assertGreaterEqual(peak_in, ONE_MEG - server_mod.READ_SIZE,
                                "an edited reply was not buffered whole, so the "
                                "streaming test above is measuring nothing")

    def test_half_a_megabyte_of_property_arrives_whole(self):
        """xprop's own shape: `GetProperty` with long_length 125000 units, read
        back in chunks by the client [recon/tools.md 6]."""
        rig = self.rig()
        win = rig.upstream.big_property("_XW11_HALF_MEG", 500000)
        c = rig.conn()
        got = c.read_property(win, "_XW11_HALF_MEG")
        self.assertIsNotNone(got)
        self.assertEqual(len(got[2]), 500000)
        self.assertEqual(got[2], rig.upstream.props[(win, "_XW11_HALF_MEG")][2])


class EofAfterTheBytes(RigCase):
    """Design section 2.6: an EOF upstream reaches the client as EOF -- which
    means after the bytes that came before it. Closing the pair the instant one
    side stops writing throws away whatever the other side's out-buffer holds,
    and a megabyte does not fit in a unix socket's send buffer (212992 bytes
    here, /proc/sys/net/core/wmem_default), so the proxy really is holding most
    of it when the EOF lands."""

    def wait_for_conn(self, rig):
        deadline = time.monotonic() + 5
        while not rig.server.conns and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(rig.server.conns, "no connection reached the server")
        return rig.server.conns[0]

    def test_a_megabyte_written_just_before_the_upstream_goes_arrives_whole(self):
        rig = self.rig()
        s, _body = rig.raw(timeout=60)
        self.wait_for_conn(rig)
        sock = rig.upstream.wires[0].sock
        sock.sendall(reply_head(1, (ONE_MEG - 32) // 4))
        sock.sendall(bytes(ONE_MEG - 32))
        # The client is not reading yet, so the tail of the reply is in the
        # proxy's own buffer when the upstream stops writing.
        deadline = time.monotonic() + 5
        while not rig.server.conns[0].out_down and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(rig.server.conns[0].out_down,
                        "the proxy had nothing buffered, so this proves nothing")
        sock.shutdown(socket.SHUT_WR)
        got = _recvn(s, ONE_MEG)
        self.assertEqual(len(got), ONE_MEG, "the tail was dropped with the EOF")
        self.assertEqual(got[:8], reply_head(1, (ONE_MEG - 32) // 4)[:8])
        s.settimeout(10)
        self.assertEqual(s.recv(1), b"", "the EOF never reached the client")

    def test_the_pair_is_gone_once_both_buffers_are_empty(self):
        """The other half: an EOF that owes nobody anything closes at once, so
        a proxy holding half-dead connections is not what the fix above buys."""
        rig = self.rig()
        s, _body = rig.raw(timeout=60)
        conn = self.wait_for_conn(rig)
        s.close()
        deadline = time.monotonic() + 5
        while rig.server.conns and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(rig.server.conns, [], "the connection was never reaped")
        self.assertEqual(conn.out_down, bytearray())
        self.assertEqual(conn.out_up, bytearray())


class FdsClosed(RigCase):
    """R4. No box in this project has a /dev/dri, so the mechanism is exercised
    the way recon proved it: `socket.send_fds` around the exact read loop."""

    def test_the_payload_is_forwarded_and_the_descriptors_are_closed(self):
        rig = self.rig()
        log = self.logged(rig)
        s, _body = rig.raw()
        before = set(os.listdir("/proc/self/fd"))
        payload = wire.error(wire.ERR_WINDOW, 1, 0x2A, 15, 0)
        fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(3)]
        try:
            rig.upstream.send_fds(payload, fds)
        finally:
            for fd in fds:
                os.close(fd)
        self.assertEqual(_recvn(s, 32), payload)
        deadline = time.monotonic() + 5
        while "descriptor" not in log.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("closed 3 file descriptor(s)", log.getvalue())
        self.assertIn("AGENTS.md route 5", log.getvalue())
        self.assertIn("not yet", log.getvalue())
        after = set(os.listdir("/proc/self/fd"))
        self.assertEqual(after - before, set(),
                         "descriptors the proxy received are still open")

    def test_a_dozen_descriptors_in_one_message_are_all_closed(self):
        """One SCM_RIGHTS carrying twelve descriptors -- CMSG_SPACE(12 * 4) is
        exactly the 64 bytes the read asks the kernel for on this box, so this
        is the buffer's own ceiling and not a number read off the module. A
        buffer sized for one would take the first and leave eleven open in the
        proxy for the life of the process, which is the leak R4 is about."""
        self.assertLessEqual(socket.CMSG_SPACE(12 * 4), server_mod.ANCILLARY_SIZE)
        rig = self.rig()
        log = self.logged(rig)
        s, _body = rig.raw()
        before = set(os.listdir("/proc/self/fd"))
        payload = wire.error(wire.ERR_WINDOW, 1, 0x2A, 15, 0)
        fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(12)]
        try:
            rig.upstream.send_fds(payload, fds)
        finally:
            for fd in fds:
                os.close(fd)
        self.assertEqual(_recvn(s, 32), payload)
        deadline = time.monotonic() + 5
        while "descriptor" not in log.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("closed 12 file descriptor(s)", log.getvalue())
        self.assertEqual(set(os.listdir("/proc/self/fd")) - before, set(),
                         "descriptors the proxy received are still open")


class Dri3Hidden(RigCase):
    def query(self, sock, name):
        raw = name.encode()
        sock.sendall(struct.pack("<BBHH2x", 98, 0, 2 + len(wire.pad4(raw)) // 4,
                                 len(raw)) + wire.pad4(raw))
        return _recvn(sock, 32)

    def test_dri3_comes_back_absent_and_everything_else_untouched(self):
        rig = self.rig()
        s, _body = rig.raw()
        dri3 = self.query(s, "DRI3")
        self.assertEqual(dri3[8], 0, "DRI3 still says present")
        self.assertEqual(dri3[9], 151, "the fake's major moved")
        self.assertEqual(struct.unpack_from("<I", dri3, 4)[0], 0)
        self.assertEqual(len(dri3), 32)
        xtest = self.query(s, "XTEST")
        self.assertEqual((xtest[8], xtest[9]), (1, 132))
        randr = self.query(s, "RANDR")
        self.assertEqual((randr[8], randr[9], randr[10], randr[11]),
                         (1, 139, 88, 145))

    def test_the_fake_really_does_advertise_it(self):
        """Without this the test above passes against a fake that never had
        DRI3 in the first place."""
        self.assertEqual(support.FAKE_EXTENSIONS["DRI3"], (151, 0, 0))
        pkt = wire.reply(1, 0, struct.pack("<BBBB20x", 1, 151, 0, 0))
        self.assertEqual(pkt[8], 1)
        self.assertEqual(server_mod.hide_present(pkt)[8], 0)
        self.assertEqual(len(server_mod.hide_present(pkt)), len(pkt))

    def test_only_dri3_has_an_editor(self):
        rig = self.rig()
        self.assertIs(rig.server.reply_editor("DRI3"), server_mod.hide_present)
        for name in ("XTEST", "RANDR", "BIG-REQUESTS", "XWAYLAND"):
            self.assertIsNone(rig.server.reply_editor(name), name)


class TwoClientsTwoBases(RigCase):
    def test_each_client_reads_its_own_resource_id_base(self):
        """The server hands each connection its own, stepping by mask + 1
        [recon/wire.md 1.3]. Two clients behind one upstream connection would
        collide, which is why there is one upstream connection per client."""
        rig = self.rig()
        first, body1 = rig.raw()
        second, body2 = rig.raw()
        base1, mask1 = struct.unpack_from("<II", body1, 4)
        base2, mask2 = struct.unpack_from("<II", body2, 4)
        self.assertEqual(mask1, mask2)
        self.assertEqual(mask1, support.RID_MASK)
        self.assertNotEqual(base1, base2)
        self.assertEqual(base2 - base1, support.RID_STEP)
        self.assertEqual(base2 - base1, mask1 + 1)
        self.assertEqual(len(rig.upstream.wires), 2)
        for s in (first, second):
            s.close()


class Lifetime(RigCase):
    def wait_gone(self, rig, timeout=10):
        rig.thread.join(timeout=timeout)
        return not rig.thread.is_alive()

    def test_the_constants_are_the_daemons(self):
        """wdotool/daemon.py:177-178, constant for constant. Fifteen minutes,
        because a shadow id is `rid_base | n` and a reopened upstream connection
        re-bases every one of them, so two invocations of a shell one-liner have
        to fall inside one lifetime (design section 2.6)."""
        self.assertEqual(server_mod._IDLE_SECONDS, 900.0)
        self.assertEqual(server_mod._CHECK_SECONDS, 15.0)

    def test_idle_exit(self):
        rig = self.rig(idle=0.2, check=0.05)
        self.assertTrue(self.wait_gone(rig), "an idle proxy stayed up")
        self.assertIn("no client for 0.2s", rig.server.reason)
        self.assertFalse(os.path.exists(rig.display.fs_path))

    def test_a_connected_client_is_never_idled_out(self):
        """The one thing no exit may do: cut a request short."""
        rig = self.rig(idle=0.2, check=0.05)
        s, _body = rig.raw()
        self.addCleanup(s.close)
        time.sleep(0.8)
        self.assertTrue(rig.thread.is_alive())
        self.assertIsNone(rig.server.exit_reason())

    def test_the_wayland_socket_going_away_ends_it(self):
        rig = self.rig(idle=0.0, check=0.05)
        marker = os.path.join(rig.dir, "wayland-9")
        with open(marker, "w"):
            pass
        rig.server.wayland_socket = marker
        self.assertIsNone(rig.server.exit_reason())
        os.unlink(marker)
        self.assertTrue(self.wait_gone(rig))
        self.assertIn("wayland socket", rig.server.reason)

    def test_the_display_file_going_away_ends_it(self):
        """`xw11 --stop` unlinks the file and sends SIGTERM; the file check is
        the half that works on a proxy that is not reading its signals."""
        rig = self.rig(idle=0.0, check=0.05)
        rtdir = tempfile.mkdtemp(prefix="xw11-rt-")
        self.addCleanup(shutil.rmtree, rtdir, ignore_errors=True)
        with support.env(XDG_RUNTIME_DIR=rtdir):
            path = rig.display.write_display_file(":7")
        self.assertIsNone(rig.server.exit_reason())
        os.unlink(path)
        self.assertTrue(self.wait_gone(rig))
        self.assertIn(path, rig.server.reason)


class EnvironIsUpstream(RigCase):
    """R15."""

    def test_the_proxys_own_display_is_the_upstreams(self):
        rig = self.rig()
        with support.env(DISPLAY=rig.name, XAUTHORITY=None):
            rig.server.adopt_upstream_environment()
            self.assertEqual(os.environ["DISPLAY"], rig.upstream_name)
            self.assertNotEqual(os.environ["DISPLAY"], rig.name)

    def test_the_xauthority_it_exports_is_the_file_the_cookie_is_in(self):
        rig = self.rig()
        d = tempfile.mkdtemp(prefix="xw11-auth-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "up.xauth")
        support.write_xauth(path, [(256, x11_mini.hostname().encode(), b"7",
                                    b"MIT-MAGIC-COOKIE-1", b"K" * 16)])
        with support.env(DISPLAY=rig.name, XAUTHORITY=path):
            rig.server.adopt_upstream_environment()
            self.assertEqual(os.environ["XAUTHORITY"], path)


class Teardown(unittest.TestCase):
    """A real process and a real SIGTERM, because `display.release()` running
    out of a signal handler is the thing under test. The hazard is measured:
    `xtrace -D :1 -d :1` unlinked a LIVE socket and nothing anywhere reported
    it [recon/env.md 4]."""

    @unittest.skipUnless(HAVE_XVFB, "needs Xvfb")
    def test_sigterm_takes_exactly_what_the_proxy_made(self):
        upstream = display_mod.allocate()
        num = upstream.num
        upstream.release()
        xvfb = subprocess.Popen(["Xvfb", ":%d" % num, "-screen", "0", "640x480x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(_reap, xvfb)
        self.assertTrue(_wait_for_display(num), "Xvfb never came up")

        # A socket the proxy did not create, next door to the one it will.
        stranger = display_mod.allocate()
        self.addCleanup(stranger.release)

        rtdir = tempfile.mkdtemp(prefix="xw11-rt-")
        self.addCleanup(shutil.rmtree, rtdir, ignore_errors=True)
        os.chmod(rtdir, 0o700)
        env = dict(os.environ, XDG_RUNTIME_DIR=rtdir, W11_PASSTHROUGH="never",
                   XW11_LOG=os.path.join(rtdir, "log"))
        proxy = subprocess.Popen(
            [sys.executable, "-m", "xw11", "--upstream", ":%d" % num,
             "--foreground"], env=env, cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(_reap, proxy)
        dfile = os.path.join(rtdir, "xw11", "display")
        deadline = time.monotonic() + 20
        while not os.path.exists(dfile) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(dfile), "the proxy never wrote its display file")
        with open(dfile, encoding="utf-8") as f:
            name, pid, up = f.read().split()
        self.assertEqual(pid, str(proxy.pid))
        self.assertEqual(up, ":%d" % num)
        mine = int(name[1:])
        self.assertTrue(os.path.exists(display_mod.sock_path(mine)))
        self.assertTrue(os.path.exists(display_mod.lock_path(mine)))

        proxy.send_signal(signal.SIGTERM)
        proxy.wait(timeout=20)
        self.assertFalse(os.path.exists(display_mod.sock_path(mine)))
        self.assertFalse(os.path.exists(display_mod.lock_path(mine)))
        self.assertFalse(os.path.exists(dfile))
        self.assertFalse(os.path.exists(os.path.join(rtdir, "xw11",
                                                     "display.%d" % proxy.pid)))
        self.assertTrue(os.path.exists(stranger.fs_path),
                        "teardown unlinked a socket the proxy did not make")
        self.assertTrue(os.path.exists(stranger.lock_path))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        with self.assertRaises(OSError):
            s.connect("\0" + display_mod.sock_path(mine))
        s.close()


def _reap(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - a wedged child
            proc.kill()
            proc.wait(timeout=5)


def _wait_for_display(num, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect("\0%s/X%d" % (x11_mini._SOCK_DIR, num))
            return True
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.1)
    return False


class UpstreamMissing(unittest.TestCase):
    def test_a_client_is_told_which_display_is_not_there(self):
        """What X does when the server is not there is refuse the setup, so
        that is what happens here -- with the display named, because a proxy
        that answers for a server nobody can reach is the confusing case."""
        d = tempfile.mkdtemp(prefix="xw11-noupstream-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        old_sock, old_lock = x11_mini._SOCK_DIR, display_mod._LOCK_DIR
        x11_mini._SOCK_DIR = d
        display_mod._LOCK_DIR = d
        self.addCleanup(_restore_dirs, old_sock, old_lock)
        display = display_mod.allocate()
        self.addCleanup(display.release)
        devnull = open(os.devnull, "w")
        self.addCleanup(devnull.close)
        server = server_mod.Server(display, ":63", log=devnull, idle=0.0,
                                   check=0.05, watch_wayland=False)
        server_mod.CONNECT_RETRY_SECONDS, keep = 0.1, server_mod.CONNECT_RETRY_SECONDS
        self.addCleanup(setattr, server_mod, "CONNECT_RETRY_SECONDS", keep)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.stop)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect("\0" + display.fs_path)
        self.addCleanup(s.close)
        s.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0))
        head = _recvn(s, 8)
        self.assertEqual(head[0], 0)
        body = _recvn(s, struct.unpack_from("<H", head, 6)[0] * 4)
        self.assertEqual(body[:head[1]], b"xw11: no X server at :63")


def _restore_dirs(sock, lock):
    x11_mini._SOCK_DIR = sock
    display_mod._LOCK_DIR = lock


class TheLog(RigCase):
    def test_the_log_path_is_per_uid_under_tmp(self):
        with support.env(XW11_LOG=None):
            self.assertEqual(server_mod.log_path(), "/tmp/xw11-%d.log" % os.geteuid())
        with support.env(XW11_LOG="/tmp/elsewhere.log"):
            self.assertEqual(server_mod.log_path(), "/tmp/elsewhere.log")

    def test_a_log_that_is_a_symlink_is_not_written_through(self):
        """/tmp is world-writable and the log carries session diagnostics --
        the rule wdotool/daemon.py:2140 already applies to its own log. This is
        the O_NOFOLLOW half: the link is refused and the file it points at stays
        empty."""
        rig = self.rig()
        d = tempfile.mkdtemp(prefix="xw11-log-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        real = os.path.join(d, "real")
        with open(real, "w"):
            pass
        link = os.path.join(d, "link")
        os.symlink(real, link)
        rig.server._log = None
        with support.env(XW11_LOG=link):
            rig.server.say("hello")
        opened = rig.server._log
        self.addCleanup(opened.close)
        self.assertEqual(os.path.getsize(real), 0)

    def test_a_log_somebody_else_owns_is_not_appended_to(self):
        """The other half of daemon.py:2140's rule, the one a plain file cannot
        reach at a single uid: the fstat says the file belongs to somebody else.
        There is no second uid in this suite, so os.fstat is made to say so for
        exactly the one call the open makes, which is what a file planted by
        another user would look like from inside."""
        rig = self.rig()
        d = tempfile.mkdtemp(prefix="xw11-log-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "theirs")
        with open(path, "w"):
            pass
        real_fstat, seen = os.fstat, []

        def fstat(fd):
            st = real_fstat(fd)
            if not seen:
                seen.append(fd)
                return os.stat_result(tuple([st.st_mode, st.st_ino, st.st_dev,
                                             st.st_nlink, os.geteuid() + 4242,
                                             st.st_gid, st.st_size,
                                             int(st.st_atime), int(st.st_mtime),
                                             int(st.st_ctime)]))
            return st
        os.fstat = fstat
        self.addCleanup(setattr, os, "fstat", real_fstat)
        rig.server._log = None
        with support.env(XW11_LOG=path):
            rig.server.say("hello")
        os.fstat = real_fstat
        opened = rig.server._log
        self.addCleanup(opened.close)
        self.assertEqual(len(seen), 1, "the open never fstat'd the log it made")
        self.assertEqual(os.path.getsize(path), 0,
                         "a log owned by another uid was written to")

    def test_debug_off_writes_nothing_per_request(self):
        rig = self.rig()
        log = self.logged(rig)
        rig.server.debug = False
        s, _body = rig.raw()
        self.addCleanup(s.close)
        s.sendall(wire.NOOP)
        time.sleep(0.2)
        self.assertNotIn("req seq=", log.getvalue())

    def test_debug_on_names_the_request_and_its_sequence(self):
        rig = self.rig()
        log = self.logged(rig)
        rig.server.debug = True
        s, _body = rig.raw()
        self.addCleanup(s.close)
        s.sendall(b"\x2b\x00\x01\x00")                # GetInputFocus
        _recvn(s, 32)
        deadline = time.monotonic() + 5
        while "GetInputFocus" not in log.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("req seq=1 GetInputFocus len=4", log.getvalue())
        self.assertIn("s2c seq=1 reply len=32", log.getvalue())

    def test_an_extension_request_is_named_by_the_major_it_resolved(self):
        """Never by a number written down: RANDR is 140 on Xvfb and 139 on
        Xwayland, and this fake says 139."""
        rig = self.rig()
        log = self.logged(rig)
        rig.server.debug = True
        s, _body = rig.raw()
        self.addCleanup(s.close)
        name = b"RANDR"
        s.sendall(struct.pack("<BBHH2x", 98, 0, 2 + len(wire.pad4(name)) // 4,
                              len(name)) + wire.pad4(name))
        reply = _recvn(s, 32)
        self.assertEqual(reply[9], 139)
        s.sendall(bytes([139, 21, 1, 0]))             # RANDR.SetCrtcConfig, malformed
        deadline = time.monotonic() + 5
        while "RANDR." not in log.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("RANDR.SetCrtcConfig", log.getvalue())


class Sockets(RigCase):
    def test_the_proxy_answers_on_both_of_its_names(self):
        rig = self.rig()
        for target in ("\0" + rig.display.fs_path, rig.display.fs_path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(target)
            s.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0))
            self.assertEqual(_recvn(s, 8)[0], 1, target)
            s.close()

    def test_an_upstream_socket_is_tried_abstract_first(self):
        """With a listener on BOTH names, the abstract one gets the connection
        -- the order libxcb uses, re-measured for this batch against libxcb
        1.17.0, and the only order that reaches a server whose filesystem socket
        was unlinked out from under it [recon/wire.md 9.2]."""
        rig = self.rig()
        path = "%s/X%d" % (rig.dir, 61)
        listeners = {}
        for key, name in (("abstract", "\0" + path), ("filesystem", path)):
            ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ls.bind(name)
            ls.listen(4)
            ls.setblocking(False)
            self.addCleanup(ls.close)
            listeners[key] = ls
        sock = server_mod.upstream_socket(61)
        self.assertIsNotNone(sock)
        self.addCleanup(sock.close)
        sel = selectors.DefaultSelector()
        for key, ls in listeners.items():
            sel.register(ls, selectors.EVENT_READ, key)
        self.addCleanup(sel.close)
        ready = sel.select(2)
        self.assertEqual([k.data for k, _e in ready], ["abstract"])

    def test_only_the_filesystem_name_is_still_reached(self):
        """The fallback: the fake binds the path alone, and that is what the
        proxy's own upstream connection lands on."""
        rig = self.rig()
        sock = server_mod.upstream_socket(7)
        self.assertIsNotNone(sock)
        self.addCleanup(sock.close)
        self.assertEqual(sock.getpeername(), "%s/X7" % rig.dir)

    def test_no_upstream_at_all_is_none_rather_than_an_exception(self):
        self.rig()
        self.assertIsNone(server_mod.upstream_socket(63))

    def test_a_flush_onto_a_dead_socket_says_so(self):
        rig = self.rig()
        a, b = socket.socketpair()
        b.close()
        a.setblocking(False)
        out = bytearray(b"x" * 4096)
        for _ in range(64):
            if not rig.server._flush(a, out):
                break
        else:
            self.fail("_flush never reported the dead peer")
        a.close()


class UpstreamRestart(RigCase):
    """R16."""

    def shadow_rig(self):
        backend = support.FakeBackend(
            windows=[support.fake_window(1, "a"), support.fake_window(2, "b")])
        rig = self.rig(num=58, upstream_num=59, passthrough=False,
                       backend=backend, check=0.02)
        return rig

    def test_a_client_sees_the_eof_and_the_next_one_gets_a_reopened_connection(self):
        """An Xwayland restart under a live proxy reaches each client as EOF on
        its own upstream socket, which is what X does when the server goes. The
        proxy does not die with it: the next accept reopens the proxy's own
        connection, and root, atoms and extension majors come back the same --
        they are stable across a restart, and only XIDs are not
        [recon/env.md 2.5, 6]."""
        rig = self.shadow_rig()
        first, _body = rig.raw()
        own = rig.wait_own()
        before = {e.handle: e.shadow for e in rig.server.shadows.snapshot()}
        keep = (own.root, dict(own.atoms), dict(own.majors))
        rig.upstream.close_when_idle(0.15)
        self.assertEqual(first.recv(4096), b"")          # EOF, after the bytes
        rig.wait(lambda: not rig.server.own.open, timeout=10,
                 what="the upstream going away")
        rig.upstream._idle_seconds = None
        second, body = rig.raw()
        rig.wait(lambda: rig.server.own.open, timeout=10, what="the reopen")
        own = rig.server.own
        self.assertEqual((own.root, own.atoms, own.majors), keep)
        after = {e.handle: e.shadow for e in rig.server.shadows.snapshot()}
        self.assertEqual(sorted(after), sorted(before))
        self.assertEqual([], [h for h in after if after[h] == before[h]])
        base, _mask = struct.unpack_from("<II", body, 4)
        self.assertTrue(base)
        first.close()
        second.close()

    def test_the_proxy_keeps_its_display_through_the_restart(self):
        """The number, the sockets and the lock belong to the proxy and not to
        the X server behind it; a client that arrives during the gap is answered
        by the same listener."""
        rig = self.shadow_rig()
        first, _body = rig.raw()
        rig.wait_own()
        name = rig.display.name
        rig.upstream.close_when_idle(0.15)
        rig.wait(lambda: not rig.server.own.open, timeout=10, what="the gap")
        rig.upstream._idle_seconds = None
        self.assertEqual(rig.display.name, name)
        second, _body2 = rig.raw()
        second.sendall(bytes(wire.GET_INPUT_FOCUS))
        reply = _recvn(second, 32)
        self.assertEqual(reply[0], 1)
        first.close()
        second.close()


class HandlerTrouble(RigCase):
    """A handler that cannot do the thing, on the loop thread. `CmdError` is
    this compositor not doing it, which X answers with nothing at all when the
    window manager ignores a request (design section 3.3) -- and which may not
    take the loop, the client, or the other clients down with it."""

    def rig_with(self, cls, handler, num=66):
        backend = support.FakeBackend(windows=[support.fake_window(1, "a")])
        rig = self.rig(num=num, upstream_num=num + 1, passthrough=False,
                       backend=backend)
        old = policy.POLICY[wire.OP_QUERY_TREE]
        self.addCleanup(policy.POLICY.__setitem__, wire.OP_QUERY_TREE, old)
        policy.POLICY[wire.OP_QUERY_TREE] = policy.Row(root=cls)
        rig.server.handlers[wire.OP_QUERY_TREE] = handler
        return rig

    def refuse(self, _server, _conn, _request):
        raise CmdError("sway cannot do that")

    def test_a_consumed_request_whose_handler_refuses_is_silence_and_one_line(self):
        """The sequence is still consumed -- the client counted it, and X's
        sequence numbers are the client's own count [recon/wire.md 3.2] -- so
        the substitute goes upstream and the next reply comes back at 2."""
        rig = self.rig_with(policy.CONSUME, self.refuse)
        buf = self.logged(rig)
        sock, _body = rig.raw()
        rig.wait_own()
        root = support.FakeXServer.ROOTS[0]
        sock.sendall(struct.pack("<BBHI", wire.OP_QUERY_TREE, 0, 2, root))
        sock.sendall(bytes(wire.GET_INPUT_FOCUS))
        reply = _recvn(sock, 32)
        self.assertEqual(reply[0], 1)
        self.assertEqual(struct.unpack_from("<H", reply, 2)[0], 2)
        noops = [row for row in rig.upstream.log if row[0] == "NoOperation"]
        self.assertEqual(len(noops), 1)
        self.assertIn("sway cannot do that", buf.getvalue())
        self.assertIn("silence on the wire", buf.getvalue())
        self.assertFalse(rig.server.stopping)
        sock.close()

    def test_an_answered_request_whose_handler_refuses_is_forwarded_instead(self):
        """The fall-back is X itself: the upstream answers, which is exactly
        what this client would have read without the proxy in the way."""
        rig = self.rig_with(policy.ANSWER, self.refuse, num=68)
        buf = self.logged(rig)
        rig.upstream.children = [0x333]
        sock, _body = rig.raw()
        rig.wait_own()
        root = support.FakeXServer.ROOTS[0]
        sock.sendall(struct.pack("<BBHI", wire.OP_QUERY_TREE, 0, 2, root))
        head = _recvn(sock, 32)
        (words,) = struct.unpack_from("<I", head, 4)
        body = _recvn(sock, 4 * words)
        self.assertEqual(head[0], 1)
        self.assertEqual(struct.unpack_from("<H", head, 2)[0], 1)
        self.assertEqual(struct.unpack("<I", body)[0], 0x333)
        self.assertIn("sway cannot do that", buf.getvalue())
        self.assertIn("forwarded", buf.getvalue())
        self.assertFalse(rig.server.stopping)
        sock.close()


class OwnConnectionFraming(RigCase):
    def test_a_lying_length_closes_the_own_connection_and_leaves_nothing_behind(self):
        """A reply whose length field claims more than 16 MiB of body is a lying
        server (`xw11/wire.py`'s cap). The framer cannot go on -- it does not
        know where the next packet starts -- so the connection goes; and the
        SERVER does the closing, because a socket shut behind its back would
        leave a selector registration under a descriptor number the kernel hands
        straight back out."""
        backend = support.FakeBackend(windows=[support.fake_window(1, "a")])
        rig = self.rig(num=70, upstream_num=71, passthrough=False,
                       backend=backend)
        first, _b = rig.raw()
        own = rig.wait_own()
        fd = own.fileno()
        self.assertIn(fd, rig.server._events)
        lying = struct.pack("<BBHI24x", 1, 0, 1, 0xFFFFFFF)
        rig.upstream.push_event(lying, conn_index=0, stamp=False)
        rig.wait(lambda: not rig.server.own.open, timeout=10, what="the close")
        self.assertNotIn(fd, rig.server._events)
        self.assertNotIn(fd, rig.server.clients)
        self.assertFalse(rig.server.stopping)
        # And it comes back: the next accept reopens it and the registry mints
        # from the new base.
        first.close()
        second, _b2 = rig.raw()
        rig.wait(lambda: rig.server.own.open, timeout=10, what="the reopen")
        self.assertTrue(all(e.shadow for e in rig.server.shadows.snapshot()))
        second.close()


class HungBackend(RigCase):
    """R13."""

    def test_a_wedged_compositor_delays_another_client_and_loses_no_byte(self):
        """Every backend bounds itself at 10 s (`IPC_TIMEOUT`/`CALL_TIMEOUT`/
        `SCRIPT_TIMEOUT` [recon/seams.md 2.3]) and the loop makes its backend
        calls on the loop thread, so a wedged compositor costs every other
        client that much latency -- and nothing else. The bytes are in the
        out-buffers either way: the second client's reply arrives late, not
        never, and it is the right reply."""
        backend = support.FakeBackend(windows=[support.fake_window(1, "a")])
        rig = self.rig(num=60, upstream_num=61, passthrough=False,
                       backend=backend)
        old = policy.POLICY[wire.OP_QUERY_TREE]
        self.addCleanup(policy.POLICY.__setitem__, wire.OP_QUERY_TREE, old)
        policy.POLICY[wire.OP_QUERY_TREE] = policy.Row(root=policy.ANSWER)

        def handler(server, conn, request):
            server.shadows.snapshot()            # the wedged list() happens here
            return wire.reply(conn.seq, 0, b"\0" * 24)
        rig.server.handlers[wire.OP_QUERY_TREE] = handler
        slow, _b1 = rig.raw(timeout=30)
        other, _b2 = rig.raw(timeout=30)
        rig.wait_own()
        root = support.FakeXServer.ROOTS[0]
        calls = len(backend.calls)
        backend.wedge(0.5)
        began = time.monotonic()
        slow.sendall(struct.pack("<BBHI", wire.OP_QUERY_TREE, 0, 2, root))
        # Until the loop is INSIDE the wedged call: the claim is about a client
        # whose request arrives while the compositor is hung, and a second
        # client that merely raced the first one into the same `select()` would
        # prove nothing. Every call, not `counts["list"]`: the registry asks
        # `views()` first (seams 2.1) and it is that one the wedge lands on.
        rig.wait(lambda: len(backend.calls) > calls, what="the wedge")
        wedged = time.monotonic()
        # `other`'s replies are read on a thread of their own, started BEFORE
        # the request goes out: reading them after `slow`'s would pass even if
        # the loop had answered this client instantly, so the arrival TIME is
        # half of what is being pinned.
        arrived = []

        def read_four():
            try:
                for _n in range(4):
                    # The reply first and the clock after it: a tuple built the
                    # other way round is stamped when the READ BEGAN, which is
                    # before the wedge and would pass whatever the loop did.
                    reply = _recvn(other, 32)
                    arrived.append((time.monotonic(), reply))
            except OSError as e:                        # pragma: no cover
                arrived.append((time.monotonic(), e))
        reader = threading.Thread(target=read_four, daemon=True)
        reader.start()
        other.sendall(bytes(wire.GET_INPUT_FOCUS) * 4)
        answered = _recvn(slow, 32)
        self.assertEqual(answered[0], 1)
        self.assertGreaterEqual(time.monotonic() - began, 0.4)
        reader.join(timeout=10)
        self.assertFalse(reader.is_alive(), "the second client was never answered")
        self.assertEqual(len(arrived), 4)
        first_at, _first = arrived[0]
        # Late, and not never: the delay is the wedge and the bound is the
        # backend's own 10 s timeout [recon/seams.md 2.3].
        self.assertGreaterEqual(first_at - wedged, 0.4)
        self.assertLess(first_at - wedged, 10.0)
        for n, (_at, reply) in enumerate(arrived, start=1):
            self.assertEqual(reply[0], 1)
            self.assertEqual(struct.unpack_from("<H", reply, 2)[0], n)
        slow.close()
        other.close()


if __name__ == "__main__":
    unittest.main()
