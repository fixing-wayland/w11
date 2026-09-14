#!/usr/bin/env python3
"""Wire-level hardening regressions: a compositor that stops answering, goes
away, or sends a short event must reach the user as one clear line and a
bounded wait -- never a hang and never a traceback.

Each test drives the real client against a mock that speaks the actual wire
format, so the guards are exercised, not mocked out."""

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import wl_fake` resolve only with the tests directory
# itself on sys.path: running this file by path puts it there for free,
# `python3 -m unittest tests/<file>.py` does not (tests/test_passthrough.py
# fails over a file that imports one of them without this line).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common.errors import CmdError
from w11common import wayland_mini
from w11common.wayland_mini import Cursor, WlConn
from support import env as support_env
from wl_fake import msg, wstr
from hacks.window import backend_sway


def _tmpsock(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    return os.path.join(d, "sock")


class _Server:
    """A listening AF_UNIX socket that runs `handler(conn)` per connection."""

    def __init__(self, handler):
        self.path = _tmpsock("wire-mock-")
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(self.path)
        self.s.listen(4)
        self.handler = handler
        self.conns = []
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            self.handler(c)
        except OSError:
            pass

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


# -- wayland --------------------------------------------------------------

class BrokenCompositor(_Server):
    """Enough of wl_display/wl_registry for WlrBackend's constructor.

    `mode` picks the failure: "silent" (accept, then never answer), "closes"
    (go away after the globals), "shortmode" (a wl_output.mode event whose
    payload is empty). Any other mode is no failure at all: the constructor
    gets every answer it asks for and no toplevel is announced.

    `GLOBALS` is what the registry advertises, so a subclass can be a
    compositor that is missing one (see NoToplevelManager)."""

    #: (name, interface, version) per wl_registry.global event
    GLOBALS = ((1, "wl_output", 2),
               (2, "zwlr_foreign_toplevel_manager_v1", 3))

    def __init__(self, mode):
        self.mode = mode
        super().__init__(self._serve)

    def _serve(self, c):
        buf = b""
        registry = None
        output_oid = None
        announced = False
        if self.mode == "silent":
            while True:
                time.sleep(0.5)
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 8:
                oid, so = struct.unpack_from("<II", buf)
                size, op = so >> 16, so & 0xFFFF
                if size < 8 or len(buf) < size:
                    break
                pay = buf[8:size]
                buf = buf[size:]
                if registry is not None and oid == registry and op == 0:
                    cur = Cursor(pay)          # bind(name, iface, ver, new_id)
                    cur.u32()
                    iface = cur.string()
                    cur.u32()
                    nid = cur.u32()
                    if iface == "wl_output":
                        output_oid = nid
                elif oid == 1 and op == 1:     # wl_display.get_registry
                    registry = struct.unpack_from("<I", pay)[0]
                    c.sendall(b"".join(
                        msg(registry, 0, struct.pack("<I", n) + wstr(iface)
                            + struct.pack("<I", ver))
                        for n, iface, ver in self.GLOBALS))
                    announced = True
                elif oid == 1 and op == 0:     # wl_display.sync
                    cb = struct.unpack_from("<I", pay)[0]
                    if self.mode == "closes" and announced:
                        c.close()
                        return
                    if self.mode == "shortmode" and output_oid:
                        c.sendall(msg(output_oid, 1, b""))
                    c.sendall(msg(cb, 0, struct.pack("<I", 0)))


class WlConnDeadline(unittest.TestCase):
    def test_a_compositor_that_never_answers_does_not_hang(self):
        """WlConn used to leave the socket blocking, so roundtrip() waited
        forever with no message and no exit code."""
        srv = BrokenCompositor("silent")
        self.addCleanup(srv.close)
        c = WlConn(srv.path, timeout=0.4)
        self.addCleanup(c.close)
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            c.get_registry()
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_the_default_is_a_deadline_not_blocking(self):
        srv = BrokenCompositor("silent")
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        self.assertEqual(c.sock.gettimeout(), WlConn.DEFAULT_TIMEOUT)

    def test_dispatch_restores_the_callers_timeout(self):
        """dispatch() used to restore None, leaving the connection blocking
        for every later read -- which kwin.py had to work around by hand."""
        srv = BrokenCompositor("silent")
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        c.sock.settimeout(3.0)
        self.assertFalse(c.dispatch(0.1))
        self.assertEqual(c.sock.gettimeout(), 3.0)


class CursorBounds(unittest.TestCase):
    def test_a_truncated_string_does_not_leak_the_terminator(self):
        """The slice was max(n - 1, 0) bytes with no check that they exist,
        so a short payload returned whatever followed, NUL included."""
        cur = Cursor(struct.pack("<I", 8) + b"abc\0")
        self.assertEqual(cur.string(), "abc")

    def test_a_string_that_runs_off_the_payload_is_clamped(self):
        cur = Cursor(struct.pack("<I", 64) + b"hi\0")
        self.assertEqual(cur.string(), "hi")


class NoToplevelManager(BrokenCompositor):
    """A compositor with no zwlr_foreign_toplevel_manager_v1 -- cosmic-comp's and Mutter's shape from here.

    The one registry the wlr constructor refuses over, which is what the connection-ownership guards below
    need: a refusal that happens *after* the connection was opened or handed in."""

    GLOBALS = ((1, "wl_output", 2),)


class WlrBackendGuards(unittest.TestCase):
    """`len(srv.conns)` is how many clients connected, which is what says whether a handed-in connection was
    reused or quietly duplicated."""

    def _backend(self, mode, cls=BrokenCompositor):
        srv = cls(mode)
        self.addCleanup(srv.close)
        old = os.environ.get("WAYLAND_DISPLAY")
        os.environ["WAYLAND_DISPLAY"] = srv.path
        self.addCleanup(lambda: os.environ.__setitem__("WAYLAND_DISPLAY", old)
                        if old is not None
                        else os.environ.pop("WAYLAND_DISPLAY", None))
        # The constructor raises with its connection half-built, so nothing
        # else will close it: an fd left to the collector prints a
        # ResourceWarning into whatever stderr a later test is capturing.
        from hacks.window import backend_wlr
        made = []
        real = backend_wlr.WlConn

        def tracking(*a, **kw):
            c = real(*a, **kw)
            made.append(c)
            return c

        backend_wlr.WlConn = tracking
        self._made = made

        def restore():
            backend_wlr.WlConn = real
            for c in made:
                try:
                    c.close()
                except OSError:
                    pass

        self.addCleanup(restore)
        return backend_wlr.WlrBackend

    def test_a_compositor_that_goes_away_is_one_line(self):
        """RuntimeError('wayland connection closed') used to escape
        backend_wlr and reach the user as a traceback."""
        WlrBackend = self._backend("closes")
        with self.assertRaises(CmdError) as cm:
            WlrBackend()
        self.assertIn("wlr backend:", str(cm.exception))

    def test_a_short_event_payload_is_one_line(self):
        """A wl_output.mode with an empty payload used to raise struct.error
        out of Cursor.u32()."""
        WlrBackend = self._backend("shortmode")
        with self.assertRaises(CmdError) as cm:
            WlrBackend()
        self.assertIn("wlr backend:", str(cm.exception))

    def test_a_handed_in_connection_is_used_and_not_a_second_one(self):
        """`WlrBackend(conn=...)` is the seam detection needs: it already pays one registry round trip to
        choose between the wlr and the COSMIC toplevel protocols, and a session should not then open a
        second connection to read the same registry again."""
        srv = BrokenCompositor("ok")
        self.addCleanup(srv.close)
        conn = WlConn(srv.path)
        self.addCleanup(conn.close)
        conn.get_registry()
        WlrBackend = self._backend("ok")
        b = WlrBackend(conn=conn)
        self.assertIs(b.c, conn)
        self.assertEqual(len(srv.conns), 1, "the handed-in connection is the only one")

    def test_a_handed_in_connection_survives_a_constructor_failure(self):
        """Whoever opened it still holds it, and detection's next arm reads the registry off it: closing
        somebody else's connection on the way out of a refusal would take that away."""
        srv = NoToplevelManager("ok")
        self.addCleanup(srv.close)
        conn = WlConn(srv.path)
        self.addCleanup(conn.close)
        conn.get_registry()
        WlrBackend = self._backend("ok", cls=NoToplevelManager)
        with self.assertRaises(CmdError) as cm:
            WlrBackend(conn=conn)
        self.assertIn("does not offer", str(cm.exception))
        self.assertNotEqual(conn.sock.fileno(), -1, "the socket must still be open")
        self.assertEqual(conn.find_global("wl_output")[0], 1,
                         "and the registry it holds is still readable")

    def test_detection_hands_its_own_connection_over_so_a_session_opens_one(self):
        """The other end of the seam, landed 2026-09-09 (requests-batch-8.md item 2, plan A 1.0 step 6):
        `session_registry()` used to close the connection in its `finally` and `_wlr()` constructed with no
        argument, so a wlr session really did open TWO connections -- detection's, to choose between the wlr
        and COSMIC toplevel protocols, and the backend's, to read the same registry again.  The compositor's
        own accept count is the only witness that says which."""
        from hacks.window import backend_detect
        srv = BrokenCompositor("ok")
        self.addCleanup(srv.close)
        backend_detect.reset()
        self.addCleanup(backend_detect.reset)
        with support_env(XDG_RUNTIME_DIR=os.path.dirname(srv.path),
                         WAYLAND_DISPLAY=os.path.basename(srv.path),
                         SWAYSOCK=None, I3SOCK=None, HYPRLAND_INSTANCE_SIGNATURE=None,
                         WAYFIRE_SOCKET=None, WDOTOOL_BACKEND=None,
                         DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-w11-bus"):
            b = backend_detect.detect()
            self.assertEqual(b.name, "wlr")
            self.assertIs(b.c, backend_detect.session_conn())
        self.assertEqual(len(srv.conns), 1, "detection's connection is the one the backend uses")

    def test_a_connection_this_constructor_opened_is_closed_on_failure(self):
        """The other half of the same rule, and the reason the flag exists: an fd left to the collector is
        a leak in a long-running process and a stray ResourceWarning in a shared test runner."""
        WlrBackend = self._backend("ok", cls=NoToplevelManager)
        with self.assertRaises(CmdError):
            WlrBackend()
        self.assertEqual([c.sock.fileno() for c in self._made], [-1])


# -- sway ------------------------------------------------------------------

_MAGIC = b"i3-ipc"


def iframe(mtype, payload):
    return _MAGIC + struct.pack("<II", len(payload), mtype) + payload


class FakeSway(_Server):
    """`mode` is "gone" (answer once, then the session ends), "badjson", or
    "wedged" (accept the connection and then never answer -- a compositor
    stuck in its own event loop; the kernel accepts for it)."""

    def __init__(self, mode):
        self.mode = mode
        super().__init__(self._serve)

    def _serve(self, c):
        n, buf = 0, b""
        if self.mode == "wedged":
            while True:
                time.sleep(0.5)
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 14:
                ln, mt = struct.unpack("<II", buf[6:14])
                if len(buf) < 14 + ln:
                    break
                buf = buf[14 + ln:]
                n += 1
                if self.mode == "badjson":
                    c.sendall(iframe(mt, b"{not json"))
                    continue
                c.sendall(iframe(mt, json.dumps(
                    [{"num": 3, "name": "3", "focused": True}]).encode()))
                if n == 1:
                    time.sleep(0.05)
                    c.close()
                    return


class SwayWireGuards(unittest.TestCase):
    def _backend(self, mode):
        srv = FakeSway(mode)
        self.addCleanup(srv.close)
        from hacks.window.backend_sway import SwayBackend
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        return b

    def test_a_session_that_ends_mid_chain_is_one_line(self):
        """_read_exact guarded only a clean EOF: a peer that has gone gives
        ECONNRESET on the read and EPIPE on the next write, and both used to
        reach the user as a traceback."""
        b = self._backend("gone")
        b.get_desktop()                    # the one answer the mock gives
        with self.assertRaises(CmdError) as cm:
            for _ in range(3):
                b.get_desktop()
        self.assertIn("sway backend: lost the connection", str(cm.exception))

    def test_a_reply_that_is_not_json_is_one_line(self):
        b = self._backend("badjson")
        with self.assertRaises(CmdError) as cm:
            b.get_desktop()
        self.assertIn("sway backend: lost the connection", str(cm.exception))

    def test_a_compositor_that_never_answers_does_not_hang(self):
        """The command socket had no deadline at all, so every tool waited on
        a wedged sway for ever -- no timeout, no message, nothing to Ctrl-C
        out of but the tool itself."""
        srv = FakeSway("wedged")
        self.addCleanup(srv.close)
        from hacks.window import backend_sway
        with mock.patch.object(backend_sway, "IPC_TIMEOUT", 0.4):
            b = backend_sway.SwayBackend(sockpath=srv.path)
            self.addCleanup(b.sock.close)
            self.assertEqual(b.sock.gettimeout(), 0.4)
            start = time.monotonic()
            with self.assertRaises(CmdError) as cm:
                b.get_desktop()
            waited = time.monotonic() - start
        self.assertIn("no answer from the compositor", str(cm.exception))
        self.assertIn("not responding", str(cm.exception))
        self.assertLess(waited, 5.0)

    def test_the_deadline_is_the_command_sockets_alone(self):
        """select_window() and wxprop's -spy subscribe on their own socket
        from _connect() and wait there for an event that may be minutes away:
        giving that one a deadline would break both."""
        srv = FakeSway("wedged")
        self.addCleanup(srv.close)
        from hacks.window import backend_sway
        b = backend_sway.SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(b.sock.gettimeout(), backend_sway.IPC_TIMEOUT)
        s = b._connect()
        self.addCleanup(s.close)
        self.assertIsNone(s.gettimeout())


    def test_a_full_backlog_is_the_wedged_message_not_a_hang(self):
        """A wedged compositor still *listens*: the kernel queues connections for
        it without waking it, so connecting looks fine until the backlog fills.

        Measured here against a raw AF_UNIX socket with listen(3) and no accept
        at all (not FakeSway, whose thread does accept): four connects succeed
        and the fifth is where it used to stop -- `_connect()` had no deadline
        on the socket, so the fifth connect() blocked in the kernel with nothing
        to time it out, and every tool that reached a wedged sway sat there. The
        thread is joined at 2.0 s so this test cannot hang before the fix, only
        fail."""
        d = tempfile.mkdtemp(prefix="wire-backlog-")
        path = os.path.join(d, "sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(3)
        self.addCleanup(srv.close)
        made, box = [], {}

        def build():
            try:
                for _ in range(5):
                    made.append(backend_sway.SwayBackend(sockpath=path))
            except BaseException as e:      # noqa: BLE001 -- reported below
                box["err"] = e

        with mock.patch.object(backend_sway, "IPC_TIMEOUT", 0.4):
            t = threading.Thread(target=build, daemon=True)
            start = time.monotonic()
            t.start()
            t.join(2.0)
            waited = time.monotonic() - start
        for b in made:
            self.addCleanup(b.sock.close)
        self.assertFalse(t.is_alive(),
                         "still inside connect() after %.1fs" % waited)
        self.assertIsInstance(box.get("err"), CmdError)
        self.assertIn("not responding", str(box["err"]))
        self.assertIn("0.4s", str(box["err"]))
        # backlog 3 -> the kernel holds four (Linux 7.0, measured); the fifth is
        # the one the deadline has to answer for
        self.assertEqual(len(made), 4)
        self.assertLess(waited, 2.0)

    def test_the_command_socket_keeps_its_deadline_after_connecting(self):
        """_connect() arms the socket for the connect() itself, so the timeout
        the caller asked for has to be put back afterwards -- otherwise the
        command socket would carry IPC_TIMEOUT only by accident and the events
        socket would carry it too (which would break -spy)."""
        srv = FakeSway("wedged")
        self.addCleanup(srv.close)
        b = backend_sway.SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(b.sock.gettimeout(), backend_sway.IPC_TIMEOUT)
        for want in (None, 0.25):
            s = b._connect(want)
            self.addCleanup(s.close)
            self.assertEqual(s.gettimeout(), want)


class SyncFailure(_Server):
    """A compositor whose answer to wl_display.sync is not a callback.

    `mode` is "error" (wl_display.error on object 1 -- what a compositor sends
    when it dislikes a request) or "drop" (the session ends where the callback
    should have been)."""

    def __init__(self, mode):
        self.mode = mode
        super().__init__(self._serve)

    def _serve(self, c):
        buf = b""
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 8:
                oid, so = struct.unpack_from("<II", buf)
                size, op = so >> 16, so & 0xFFFF
                if size < 8 or len(buf) < size:
                    break
                buf = buf[size:]
                if oid == 1 and op == 0:            # wl_display.sync(callback)
                    if self.mode == "drop":
                        c.close()
                        return
                    c.sendall(msg(1, 0, struct.pack("<II", 1, 7)
                                  + wstr("no such thing")))


class RoundtripGuard(unittest.TestCase):
    """`wayland_mini.roundtrip()` is what every virtual-device module calls
    after it sends anything, and the one place a protocol error can arrive.

    The daemon must never see a traceback out of vkbd/vptr (B5), so the two
    ways a roundtrip can fail -- the compositor objecting, and the compositor
    going away -- both come back as the caller's own exception class with the
    caller's own word for what it was doing."""

    class MyErr(Exception):
        pass

    def _conn(self, mode):
        srv = SyncFailure(mode)
        self.addCleanup(srv.close)
        c = WlConn(srv.path, timeout=2.0)
        self.addCleanup(c.sock.close)
        return c

    def test_a_protocol_error_is_the_callers_exception(self):
        c = self._conn("error")
        with self.assertRaises(self.MyErr) as cm:
            wayland_mini.roundtrip(c, "x", self.MyErr)
        self.assertTrue(str(cm.exception).startswith("x refused:"),
                        str(cm.exception))
        self.assertIn("no such thing", str(cm.exception))

    def test_a_dropped_socket_is_the_callers_exception(self):
        c = self._conn("drop")
        with self.assertRaises(self.MyErr) as cm:
            wayland_mini.roundtrip(c, "x", self.MyErr)
        self.assertTrue(str(cm.exception).startswith("x refused:"),
                        str(cm.exception))

    def test_now_ms_is_a_32_bit_monotonic_millisecond_clock(self):
        """The `time` argument every input protocol takes. Compositors compare
        it with their own clock and reject a float or a value that has wrapped
        differently, so the shape matters as much as the value."""
        a = wayland_mini.now_ms()
        self.assertIsInstance(a, int)
        self.assertNotIsInstance(a, bool)
        self.assertGreaterEqual(a, 0)
        self.assertLess(a, 2 ** 32)
        time.sleep(0.005)
        d = wayland_mini.now_ms() - a
        self.assertGreaterEqual(d, 3, d)
        self.assertLess(d, 200, d)


class EventSway(_Server):
    """A sway that takes a subscription and then pushes events at it.

    `events` are (i3-ipc message type, payload) pushed straight after the
    subscribe reply; `reply` is what the subscribe itself is answered with,
    and `reply_type` lets a test answer it with an *event* instead of a
    reply, which is the other way a subscription can fail."""

    def __init__(self, events=(), reply=b'{"success": true}',
                 reply_type=backend_sway.SUBSCRIBE):
        self.events = list(events)
        self.reply = reply
        self.reply_type = reply_type
        self.subscriptions = []      # the payload of every SUBSCRIBE seen
        super().__init__(self._serve)

    def _serve(self, c):
        buf = b""
        while True:
            try:
                data = c.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while len(buf) >= 14:
                ln, mt = struct.unpack("<II", buf[6:14])
                if len(buf) < 14 + ln:
                    break
                payload, buf = buf[14:14 + ln], buf[14 + ln:]
                if mt == backend_sway.SUBSCRIBE:
                    self.subscriptions.append(payload)
                    c.sendall(iframe(self.reply_type, self.reply))
                    for t, p in self.events:
                        c.sendall(iframe(t, json.dumps(p).encode()))
                else:
                    c.sendall(iframe(mt, b"[]"))


def wev(change, wid=None):
    node = {} if wid is None else {"container": {"id": wid}}
    return (backend_sway.EVENT_WINDOW, dict(node, change=change))


def wsev(change="focus"):
    return (backend_sway.EVENT_WORKSPACE, {"change": change})


class SwayEventStream(unittest.TestCase):
    """SwayBackend.events(): the stream `wdotool selectwindow`, `wwmctl
    -a :SELECT:` and `wxprop -spy` all sit on, and the one method of the
    backend with no test at all. Its contract is four sentences, and each
    is a way it has gone wrong: a connection of its own, sway's own change
    words passed through untranslated, workspace changes folded in as node
    0 only when asked, and a timeout that ends the iteration instead of
    waiting for ever."""

    def backend(self, **kw):
        srv = EventSway(**kw)
        self.addCleanup(srv.close)
        b = backend_sway.SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        return srv, b

    def test_window_events_come_out_in_sways_own_words(self):
        srv, b = self.backend(events=[wev("new", 5), wev("title", 5),
                                      wev("close", 7)])
        self.assertEqual(list(b.events(timeout=0.5)),
                         [(5, "new"), (5, "title"), (7, "close")])
        self.assertEqual(srv.subscriptions, [b'["window"]'])

    def test_workspace_events_are_dropped_unless_asked_for(self):
        _srv, b = self.backend(events=[wsev(), wev("focus", 5)])
        self.assertEqual(list(b.events(timeout=0.5)), [(5, "focus")])

    def test_workspaces_fold_in_as_node_zero(self):
        srv, b = self.backend(events=[wsev(), wev("focus", 5), wsev("init")])
        self.assertEqual(list(b.events(timeout=0.5, workspaces=True)),
                         [(0, "workspace"), (5, "focus"), (0, "workspace")])
        self.assertEqual(srv.subscriptions, [b'["window","workspace"]'])

    def test_an_event_with_no_container_is_skipped(self):
        # sway sends window events for containers it is no longer describing
        _srv, b = self.backend(events=[wev("close"), wev("focus", 9)])
        self.assertEqual(list(b.events(timeout=0.5)), [(9, "focus")])

    def test_a_payload_that_is_not_an_object_is_skipped(self):
        srv = EventSway(events=[(backend_sway.EVENT_WINDOW, [1, 2])])
        self.addCleanup(srv.close)
        b = backend_sway.SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(list(b.events(timeout=0.5)), [])

    def test_it_subscribes_on_a_connection_of_its_own(self):
        """A subscription and the command socket cannot share one: every
        reply the command socket waits for would arrive behind an unbounded
        queue of events."""
        srv, b = self.backend(events=[wev("focus", 5)])
        self.assertTrue(_eventually(lambda: len(srv.conns) == 1))
        self.assertEqual(list(b.events(timeout=0.5)), [(5, "focus")])
        self.assertTrue(_eventually(lambda: len(srv.conns) == 2))
        # and the command socket still answers afterwards
        self.assertEqual(b.num_desktops(), 0)

    def test_the_stream_socket_is_closed_when_the_iteration_ends(self):
        srv, b = self.backend(events=[wev("focus", 5)])
        it = b.events(timeout=5)
        self.assertEqual(next(it), (5, "focus"))
        it.close()                                   # the finally arm
        self.assertTrue(_eventually(
            lambda: srv.conns[1].recv(1, socket.MSG_DONTWAIT) == b""))

    def test_silence_ends_the_stream(self):
        _srv, b = self.backend()
        start = time.monotonic()
        self.assertEqual(list(b.events(timeout=0.3)), [])
        self.assertLess(time.monotonic() - start, 3.0)

    def test_a_refused_subscription_is_one_line(self):
        _srv, b = self.backend(reply=b'{"success": false}')
        with self.assertRaises(CmdError) as cm:
            list(b.events(timeout=0.5))
        self.assertIn("subscribe to window events failed", str(cm.exception))

    def test_an_event_where_the_reply_should_be_is_refused(self):
        """The first frame back has to be the subscribe *reply*. A stream
        that opened with an event would leave every later frame off by one."""
        _srv, b = self.backend(reply_type=backend_sway.EVENT_WINDOW,
                               reply=b'{"success": true}')
        with self.assertRaises(CmdError) as cm:
            list(b.events(timeout=0.5))
        self.assertIn("subscribe to window events failed", str(cm.exception))

    def test_select_window_returns_the_first_focus_change(self):
        _srv, b = self.backend(events=[wev("title", 5), wev("new", 6),
                                       wev("focus", 7), wev("focus", 8)])
        self.assertEqual(b.select_window(), 7)


def _eventually(pred, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


# -- D-Bus -----------------------------------------------------------------

from w11common import dbus_mini as D


def _raw_bus():
    """A Bus over a socketpair, built past authentication: the other end is
    a raw peer that can put any bytes on the wire."""
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    b = D.Bus.__new__(D.Bus)
    b.address = "unix:path=/dev/null"
    b.timeout = 5.0
    b.unique_name = ":1.99"
    b.guid = ""
    b.fds_ok = False
    b.auth_path = "direct"
    b.serve_calls = False
    b._serial = 0
    b._buf = bytearray()
    b._pending_fds = []
    b._waiting = set()
    b._replies = {}
    b._queue = __import__("collections").deque()
    b.sock = ours
    return b, theirs


def _reply_frame(to_serial, body_sig, body, endian=b"l"):
    m = D.Message(D.METHOD_RETURN, reply_serial=to_serial,
                  signature=body_sig, body=b"")
    out = bytearray(m.to_bytes(7)) + bytearray(body)
    struct.pack_into("<I", out, 4, len(body))
    if endian != b"l":
        out[0:1] = endian
    return bytes(out)


class DBusMalformedFrames(unittest.TestCase):
    """A peer that puts something impossible on the wire is a broken or
    hostile peer, not a bug in the caller: every consumer catches DBusError
    and nothing else, so a ValueError or a RecursionError out of the
    marshaller reached the user as a traceback."""

    def _peer_answers(self, make_body):
        bus, peer = _raw_bus()
        self.addCleanup(bus.close)
        self.addCleanup(peer.close)

        def serve():
            buf = b""
            while True:
                try:
                    d = peer.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                buf += d
                while True:
                    n = D.Message.frame_length(buf)
                    if n is None or len(buf) < n:
                        break
                    m = D.Message.from_bytes(buf[:n])
                    buf = buf[n:]
                    try:
                        peer.sendall(make_body(m.serial))
                    except OSError:
                        return

        threading.Thread(target=serve, daemon=True).start()
        return bus

    def test_a_body_that_contradicts_its_signature_is_a_dbus_error(self):
        bus = self._peer_answers(
            lambda serial: _reply_frame(serial, "s", b"\x10\x00"))
        with self.assertRaises(D.DBusError) as cm:
            bus.call("org.example", "/", "org.example", "Thing", timeout=5.0)
        self.assertIn("signature", str(cm.exception))

    def test_an_impossible_endianness_byte_closes_the_connection(self):
        """frame_length() raised before the frame was consumed, so the bad
        frame stayed in the buffer and the parse loop spun on it."""
        bus = self._peer_answers(
            lambda serial: _reply_frame(serial, "y", b"\x01", endian=b"Z"))
        with self.assertRaises(D.DBusError) as cm:
            bus.call("org.example", "/", "org.example", "Thing", timeout=5.0)
        self.assertIn("malformed message", str(cm.exception))
        self.assertIsNone(bus.sock, "a stream we cannot resynchronise stays shut")

    def test_absurd_type_nesting_is_refused_not_a_stack_overflow(self):
        """1000 nested variants is 3 KiB on the wire and legal type nesting;
        the specification's limit is 32 arrays and 32 variants deep."""
        body = b"\x01v\x00" * 2000 + b"\x01y\x00" + b"\x2a"
        with self.assertRaises(ValueError) as cm:
            D.unmarshal("v", body, "<", (), False)
        self.assertIn("nesting", str(cm.exception))
        bus = self._peer_answers(lambda serial: _reply_frame(serial, "v", body))
        with self.assertRaises(D.DBusError):
            bus.call("org.example", "/", "org.example", "Thing", timeout=5.0)

    def test_a_body_within_the_limit_still_parses(self):
        body = b"\x01v\x00" * 40 + b"\x01y\x00" + b"\x2a"
        self.assertEqual(D.unmarshal("v", body, "<", (), False), (42,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
