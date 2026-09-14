#!/usr/bin/env python3
"""Regression tests from the wwmctl adversarial-review pass: hostile or
misbehaving X servers, degenerate properties, and core-level degradation.

Findings fixed and pinned here (wdotool/x11_mini.py unless noted):
- the GetProperty offset loop could be spun forever (or OOMed) by a server
  that always claims bytes_after > 0: now capped in size and iterations,
- a "success" setup reply with a lying body shape leaked struct.error or
  IndexError out of the handshake: now a clean XUnavailable refusal,
- a receive failure (timeout/EOF/OSError) left the connection open
  mid-packet, so a stalled-then-resuming server desynced reply framing:
  now ANY receive failure poisons the connection,
- a reply header alleging a multi-GB body made the client sit in recv
  until timeout: implausible lengths (>16MB) are rejected immediately,
- QueryTree's child count was trusted past the actual body length,
- an XAUTHORITY pointing at a FIFO blocked forever and /dev/zero OOMed:
  only regular files are read, bounded at 1MB,
- an oversized request (huge -N title) died of struct.error: now a clean
  XUnavailable naming the X11 maximum request length,
- a degenerate single-string WM_CLASS printed with a trailing dot
  ("solo." instead of the oracle's "solo") [also wwmctl/core.py],
- core._enrich paid one 5s timeout per call per X window against a hung
  XWayland: an XUnavailable now drops the X plane for the whole listing,
- core.windows() tracebacked if backend._nodes() drifted its tuple shape:
  now falls back to the generic backend.list() branch,
- -R slept 100ms even on sway, whose IPC round-trip is synchronous,
- :SELECT: blocked with no hint of what it was waiting for.
"""

import contextlib
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_wwmctl_cli import SPECS, FakeSwayBackend, FakeX11, run
from test_wwmctl_x11 import FakeXServer
from hacks.window.backend import Window
from hacks.window import x11_mini
from hacks.window import wmctl as core
from hacks.window.x11_mini import X11Conn, XUnavailable

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class HostileXServer(FakeXServer):
    """FakeXServer with one attack switched on; `mode` selects it.

    Everything a client needs to get connected is the base class's; what an
    attack overrides is the setup reply, or a single request arm. The base
    is the honest server these tests are measured against, so an attack that
    stops being an attack (a lying length the base would also send) cannot
    hide here."""

    ROOTS = [0x5A]                # one screen; nscreens is what one attack lies about

    def __init__(self, sockdir, mode, num=7):
        self.mode = mode
        self.getprop_count = 0
        super().__init__(sockdir, num=num)

    def _setup_reply(self):
        if self.mode == "tiny_body":
            # status=1 but the body is 1 word of zeros: nothing parses
            return struct.pack("<BxHHH", 1, 11, 0, 1) + b"\0" * 4
        reply = bytearray(super()._setup_reply())
        if self.mode == "lying_nscreens":
            # well-formed except nscreens=200 with a 1-screen body
            reply[8 + 20] = 200
        return bytes(reply)

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        if self.mode == "stall_resume":
            # 10 bytes of the reply, a stall past the client's timeout, then
            # the rest -- a resumed partial packet. Raising is what ends the
            # connection mid-packet, and it has to happen before any opcode
            # arm answers this request honestly.
            full = struct.pack("<BBHII20x", 1, 0, 1, 0, 7)
            conn.sendall(full[:10])
            time.sleep(1.2)
            with contextlib.suppress(OSError):
                conn.sendall(full[10:])
            raise EOFError
        if opcode == 16:  # InternAtom -> atom 5, whatever was asked for
            return conn.sendall(struct.pack("<BBHII20x", 1, 0, seq, 0, 5))
        if opcode == 20 and self.mode == "prop_loop":
            # GetProperty: always claim more bytes after this chunk
            self.getprop_count += 1
            data = b"ABCD" * 16
            return conn.sendall(struct.pack(
                "<BBHIIII12x", 1, 8, seq, len(data) // 4,
                6, 0xFFFF, len(data)) + data)
        if opcode == 20 and self.mode == "huge_reply":
            # a reply header alleging a ~4GB body, then nothing
            return conn.sendall(struct.pack(
                "<BBHIIII12x", 1, 8, seq, 0x3FFFFFFF, 6, 0, 4))
        if opcode == 15 and self.mode == "lying_tree":
            # QueryTree: claims 500 children, body carries 2
            body = struct.pack("<2I", 0x400001, 0x400002)
            return conn.sendall(struct.pack(
                "<BBHIIIH14x", 1, 0, seq, len(body) // 4,
                self.ROOTS[0], 0, 500) + body)
        return super()._dispatch(conn, opcode, dbyte, payload, seq)


class HostileServerTest(unittest.TestCase):
    DPY = ":7"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wwx-hard-")
        self._old_dir = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", self._old_dir)
        patcher = mock.patch.dict(os.environ, {
            "XAUTHORITY": os.path.join(self.dir, "no-such-authority")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.stop()

    def serve(self, mode) -> HostileXServer:
        self.server = HostileXServer(self.dir, mode)
        return self.server

    # -- finding 1: unbounded GetProperty offset loop -----------------------

    def test_prop_loop_terminates_with_xunavailable(self):
        srv = self.serve("prop_loop")
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        with self.assertRaises(XUnavailable) as cm:
            conn._read_property(1, "EVIL_PROP")
        self.assertIn("property too large or server misbehaving",
                      str(cm.exception))
        # bounded: the iteration cap, not luck, ended the loop
        self.assertLessEqual(srv.getprop_count, x11_mini._MAX_PROP_CHUNKS)
        # the lying server is distrusted for good
        self.assertIsNone(conn._sock)

    # -- finding 2: malformed "success" setup replies -----------------------

    def test_tiny_setup_body_is_clean_refusal(self):
        self.serve("tiny_body")
        with self.assertRaises(XUnavailable) as cm:
            X11Conn(self.DPY)
        self.assertIn("malformed setup reply", str(cm.exception))

    def test_lying_nscreens_is_clean_refusal(self):
        self.serve("lying_nscreens")
        with self.assertRaises(XUnavailable) as cm:
            X11Conn(self.DPY)
        self.assertIn("malformed setup reply", str(cm.exception))

    # -- finding 3: framing desync after a stalled-then-resuming server -----

    def test_receive_failure_poisons_the_connection(self):
        self.serve("stall_resume")
        with mock.patch.object(x11_mini, "_TIMEOUT", 0.4):
            conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        with self.assertRaises(XUnavailable):
            conn.atom("FIRST")  # server stalls mid-reply past the timeout
        # ANY receive failure must kill the connection so the resumed
        # partial packet can never be misread as a fresh reply
        self.assertIsNone(conn._sock)
        with self.assertRaises(XUnavailable) as cm:
            conn.atom("SECOND")
        self.assertIn("closed", str(cm.exception))

    # -- finding 4: lying reply lengths -------------------------------------

    def test_implausible_reply_length_fails_fast(self):
        self.serve("huge_reply")
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        t0 = time.monotonic()
        with self.assertRaises(XUnavailable) as cm:
            conn._read_property(1, "EVIL_PROP")
        self.assertIn("implausible reply length", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 2.0)  # no 5s recv timeout
        self.assertIsNone(conn._sock)

    def test_querytree_count_clamped_to_body(self):
        self.serve("lying_tree")
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        # no _NET_CLIENT_LIST (atom interns but property reads come back
        # empty-typed) -> QueryTree fallback with the lying count
        self.assertEqual(conn.client_list(), [0x400001, 0x400002])


class XauthorityHardeningTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wwx-auth-")

    def test_fifo_xauthority_does_not_block(self):
        fifo = os.path.join(self.dir, "auth-fifo")
        os.mkfifo(fifo)
        result = {}
        done = threading.Event()

        def go():
            with mock.patch.dict(os.environ, {"XAUTHORITY": fifo}):
                result["cands"] = x11_mini._auth_candidates(0)
            done.set()

        threading.Thread(target=go, daemon=True).start()
        self.assertTrue(done.wait(2.0),
                        "_auth_candidates blocked on a FIFO XAUTHORITY")
        self.assertEqual(result["cands"], [(b"", b"")])

    def test_dev_zero_xauthority_is_rejected_not_oomed(self):
        with self.assertRaises(OSError):
            x11_mini._read_xauth("/dev/zero")  # char device: not regular
        with mock.patch.dict(os.environ, {"XAUTHORITY": "/dev/zero"}):
            self.assertEqual(x11_mini._auth_candidates(0), [(b"", b"")])

    def test_oversized_xauthority_read_is_bounded(self):
        big = os.path.join(self.dir, "auth-big")
        entry = (struct.pack(">H", 256)
                 + struct.pack(">H", 4) + b"host"
                 + struct.pack(">H", 1) + b"0"
                 + struct.pack(">H", 18) + b"MIT-MAGIC-COOKIE-1"
                 + struct.pack(">H", 16) + b"\xAB" * 16)
        with open(big, "wb") as f:
            f.write(entry * ((2 << 20) // len(entry)))  # ~2MB of entries
        entries = x11_mini._read_xauth(big)
        # parsed cleanly, but never past the 1MB ceiling
        self.assertTrue(entries)
        self.assertLessEqual(len(entries) * len(entry),
                             x11_mini._MAX_XAUTH_BYTES + len(entry))


class MaxRequestLengthTest(unittest.TestCase):
    DPY = ":7"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wwx-maxreq-")
        self._old_dir = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", self._old_dir)
        patcher = mock.patch.dict(os.environ, {
            "XAUTHORITY": os.path.join(self.dir, "no-such-authority")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = FakeXServer(self.dir, num=7)
        self.addCleanup(self.server.stop)

    def test_oversized_request_is_clean_xunavailable(self):
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        with self.assertRaises(XUnavailable) as cm:
            conn.set_name(0x400001, "x" * 300000, icon=False, long_=True)
        self.assertIn("maximum request length", str(cm.exception))
        # nothing was put on the wire: the connection stays usable
        self.assertGreater(conn.atom("_STILL_ALIVE"), 0)

    def test_rid_range_and_max_req_parsed(self):
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        # the setup reply's resource-id range: the only ids we allocate are
        # the font ids `wxprop -font` needs, and they come from here
        self.assertEqual((conn._rid_base, conn._rid_mask),
                         (0x400000, 0x3FFFFF))
        self.assertEqual(conn._new_rid() & ~conn._rid_mask, conn._rid_base)
        self.assertNotEqual(conn._new_rid(), conn._new_rid())
        self.assertEqual(conn._max_req_words, 0xFFFF)  # from the fake setup

    def test_core_set_title_degrades_on_oversized_name(self):
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        rc, _o, err, _b = run(["-r", "Mail", "-N", "x" * 300000], x11=conn)
        self.assertEqual(rc, 0)
        self.assertIn("cannot set the window title", err)
        self.assertIn("; ignoring", err)


class WmClassDegenerateTest(unittest.TestCase):
    DPY = ":7"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wwx-class-")
        self._old_dir = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", self._old_dir)
        patcher = mock.patch.dict(os.environ, {
            "XAUTHORITY": os.path.join(self.dir, "no-such-authority")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = FakeXServer(self.dir, num=7)
        self.addCleanup(self.server.stop)

    def test_wm_class_shapes(self):
        win = 0x400001
        for i, (raw, want) in enumerate([
            (b"inst\0cls\0", ("inst", "cls")),
            (b"solo", ("solo", "")),          # no NUL at all
            (b"solo\0", ("solo", "")),        # no second string
            (b"inst\0\0", ("inst", "")),      # second string present, empty
            (b"inst\0cls", ("inst", "cls")),  # unterminated second string
            (b"\0cls\0", ("", "cls")),        # empty instance is real
        ]):
            self.server.set_prop(win + i, "WM_CLASS", "STRING", 8, raw)
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        self.assertEqual(conn.get_wm_class(win), ("inst", "cls"))
        self.assertEqual(conn.get_wm_class(win + 1), ("solo", ""))
        self.assertEqual(conn.get_wm_class(win + 2), ("solo", ""))
        self.assertEqual(conn.get_wm_class(win + 3), ("inst", ""))
        self.assertEqual(conn.get_wm_class(win + 4), ("inst", "cls"))
        self.assertEqual(conn.get_wm_class(win + 5), ("", "cls"))

    def test_dot_class_empty_class_prints_no_trailing_dot(self):
        self.assertEqual(core._dot_class("solo", ""), "solo")
        self.assertEqual(core._dot_class("a", "B"), "a.B")
        self.assertEqual(core._dot_class("", "cls"), ".cls")
        self.assertEqual(core._dot_class(None, None), None)

    def test_lx_single_string_class_has_no_trailing_dot(self):
        class SoloX11(FakeX11):
            def get_wm_class(self, win):
                return ("solo", "")

        rc, out, _e, _b = run(["-lx"], x11=SoloX11())
        self.assertEqual(rc, 0)
        row = out.splitlines()[0]  # the XWayland window
        self.assertEqual(row.split()[2], "solo")  # was "solo." before
        self.assertNotIn("solo.", out)


class EnrichDropTest(unittest.TestCase):
    """A hung/vanished XWayland (XUnavailable) must cost the listing at
    most ONE failed X call, not 4 calls x N windows."""

    class DeadX11:
        def __init__(self):
            self.calls = []
            self.closed = 0

        def _die(self, name):
            self.calls.append(name)
            raise XUnavailable("X server timed out")

        def get_wm_class(self, win):
            self._die("get_wm_class")

        def get_client_machine(self, win):
            self._die("get_client_machine")

        def get_geometry(self, win):
            self._die("get_geometry")

        def get_pid(self, win):
            self._die("get_pid")

        def close(self):
            self.closed += 1

    def _specs(self):
        return [
            dict(node=5, xid=0x40000C, title="A", pid=1,
                 wp={"class": "XTerm", "instance": "xterm"},
                 x=0, y=0, w=640, h=360, desktop=0),
            dict(node=6, xid=0x40000D, title="B", pid=2,
                 wp={"class": "XTerm", "instance": "xterm"},
                 x=0, y=360, w=640, h=360, desktop=0),
            dict(node=7, xid=0x40000E, title="C", pid=3,
                 wp={"class": "XTerm", "instance": "xterm"},
                 x=640, y=0, w=640, h=720, desktop=0),
        ]

    def test_xunavailable_drops_x_plane_once(self):
        x = self.DeadX11()
        rc, out, _e, _b = run(["-lGpx"],
                              backend=FakeSwayBackend(self._specs()), x11=x)
        self.assertEqual(rc, 0)
        # exactly one X call for the whole 3-window listing
        self.assertEqual(x.calls, ["get_wm_class"])
        self.assertEqual(x.closed, 1)
        # compositor data stands
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines[0],
            "0x0040000c  0 1      0    0    640  360  "
            "xterm.XTerm           testhost A")

    def test_non_connection_errors_still_degrade_per_field(self):
        # X11Error-ish per-window failures keep trying (window died racily,
        # the others are fine) - pinned already in torture, re-asserted here
        class BadWindowX11(FakeX11):
            def get_wm_class(self, win):
                raise RuntimeError("BadWindow")

        rc, out, _e, _b = run(["-l"], x11=BadWindowX11())
        self.assertEqual(rc, 0)
        # machine still enriched even though the class read failed
        self.assertIn("xhost", out.splitlines()[0])


class NodesShapeGuardTest(unittest.TestCase):
    """core.windows() must survive a drifted backend._nodes() shape by
    falling back to the generic backend.list() branch."""

    GENERIC_ROW = "0x0000002a  0 testhost G"

    class _Base(FakeSwayBackend):
        def list(self):
            return [Window(id=42, title="G", class_="app", pid=1,
                           x=1, y=2, w=3, h=4, desktop=0)]

    def _check(self, backend):
        rc, out, err, _b = run(["-v", "-l"], backend=backend)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [self.GENERIC_ROW])
        self.assertIn("generic", err)

    def test_wrong_tuple_arity(self):
        class B(self._Base):
            def _nodes(self):
                return [(1, 2)]
        self._check(B([dict(s) for s in SPECS]))

    def test_non_iterable(self):
        class B(self._Base):
            def _nodes(self):
                return 7
        self._check(B([dict(s) for s in SPECS]))

    def test_key_error(self):
        class B(self._Base):
            def _nodes(self):
                raise KeyError("num")
        self._check(B([dict(s) for s in SPECS]))

    def test_intact_nodes_still_win(self):
        rc, out, _e, _b = run(["-l"])
        self.assertEqual(out.splitlines()[0],
                         "0x0040000c  0 testhost Mail inbox")


class NoSleepOnSwayTest(unittest.TestCase):
    def test_R_does_not_sleep_on_sway(self):
        with mock.patch.object(core.time, "sleep") as sl:
            rc, _o, _e, b = run(["-R", "FootWin"])
        self.assertEqual(rc, 0)
        sl.assert_not_called()
        self.assertEqual(b.calls, [("set_window_desktop", 6, 0),
                                   ("activate", 6)])

    def test_R_keeps_grace_period_on_generic_backends(self):
        class GenericBackend:
            name = "wlr"

            def __init__(self):
                self.calls = []

            def list(self):
                return [Window(id=9, title="FootWin", class_="app", pid=3,
                               x=1, y=2, w=3, h=4, desktop=0)]

            def move_to_current_desktop(self, wid):
                return False  # not sway: move it by number

            def get_desktop(self):
                return 2

            def set_window_desktop(self, wid, n):
                self.calls.append(("set_window_desktop", wid, n))

            def activate(self, wid):
                self.calls.append(("activate", wid))

        be = GenericBackend()
        with mock.patch.object(core.time, "sleep") as sl:
            rc, _o, _e, _b = run(["-R", "FootWin"], backend=be)
        self.assertEqual(rc, 0)
        sl.assert_called_once_with(0.1)
        self.assertEqual(be.calls, [("set_window_desktop", 9, 2),
                                    ("activate", 9)])


class SelectHintTest(unittest.TestCase):
    HINT = "wwmctl: focus the target window to select it\n"

    def test_select_prints_hint_before_blocking(self):
        outer = self

        class B(FakeSwayBackend):
            def select_window(self):
                # the hint must already be on stderr when we block
                self.stderr_at_select = sys.stderr.getvalue()
                return self._select

        b = B([dict(s) for s in SPECS])
        b._select = 5
        rc, _o, err, b = run(["-a", ":SELECT:"], backend=b)
        outer.assertEqual(rc, 0)
        outer.assertIn(outer.HINT, err)
        outer.assertIn(outer.HINT, b.stderr_at_select)
        outer.assertEqual(b.calls, [("activate", 5)])

    def test_no_hint_without_select_magic(self):
        rc, _o, err, _b = run(["-a", "FootWin"])
        self.assertNotIn("focus the target window", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
