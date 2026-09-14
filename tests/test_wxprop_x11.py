#!/usr/bin/env python3
"""tests for the wdotool.x11_mini extensions wxprop needs
(list_properties, get_atom_name, read_property, delete_property,
generalized change_property, select_input/next_event).

Reuses the programmable FakeXServer from test_wwmctl_x11, extended with
the new opcodes and event injection. The wwmctl-era behavior of the module
is covered by test_wwmctl_x11 and must stay green — these tests only add
coverage for the new surface (including the hardening: bounded event
queue, timeout semantics, error mapping)."""

import os
import shutil
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

from hacks.window import x11_mini
from hacks.window.x11_mini import X11Conn, X11Error, XUnavailable

from test_wwmctl_x11 import FakeXServer

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class FakeXServerExt(FakeXServer):
    """Adds ListProperties/GetAtomName/DeleteProperty/
    ChangeWindowAttributes and lets tests inject events."""

    def __init__(self, *a, **kw):
        self.prop_order = {}       # win -> [names] for ListProperties
        self.event_masks = {}      # win -> last CWEventMask value
        self.inject_on_cwa = []    # 32-byte packets sent after a CWA req
        self._conns = []
        super().__init__(*a, **kw)

    def _serve(self, conn):
        self._conns.append(conn)
        super()._serve(conn)

    def push_event(self, pkt: bytes):
        for c in list(self._conns):
            try:
                c.sendall(pkt)
            except OSError:
                pass

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        if opcode == 21:  # ListProperties
            (win,) = struct.unpack("<I", payload)
            if win in self.error_windows:
                return self._error(conn, seq, 3, 21, bad=win)
            names = self.prop_order.get(
                win, [n for (w, n) in self.props if w == win])
            atoms = [self.intern(n) for n in names]
            body = struct.pack("<%dI" % len(atoms), *atoms)
            conn.sendall(struct.pack("<BBHIH22x", 1, 0, seq,
                                     len(body) // 4, len(atoms)) + body)
        elif opcode == 17:  # GetAtomName
            (atom,) = struct.unpack("<I", payload)
            name = self._names.get(atom)
            if name is None:
                return self._error(conn, seq, 5, 17, bad=atom)  # BadAtom
            nb = name.encode("latin-1")
            body = nb + b"\0" * (-len(nb) % 4)
            conn.sendall(struct.pack("<BBHIH22x", 1, 0, seq,
                                     len(body) // 4, len(nb)) + body)
        elif opcode == 19:  # DeleteProperty
            win, prop = struct.unpack("<II", payload)
            pname = self._names.get(prop, "?")
            self.log.append(("DeleteProperty", win, pname))
            self.props.pop((win, pname), None)
        elif opcode == 2:  # ChangeWindowAttributes
            win, mask, value = struct.unpack("<III", payload)
            self.log.append(("ChangeWindowAttributes", win, mask, value))
            self.event_masks[win] = value
            for pkt in self.inject_on_cwa:
                conn.sendall(pkt)
        else:
            super()._dispatch(conn, opcode, dbyte, payload, seq)


def property_notify(seq, window, atom, state=0, tstamp=12345) -> bytes:
    return struct.pack("<BxHIIIB15x", 28, seq, window, atom, tstamp, state)


def destroy_notify(seq, window) -> bytes:
    return struct.pack("<BxHII20x", 17, seq, window, window)


class X11ExtTest(unittest.TestCase):
    DPY = ":7"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="x11ext-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._old_dir = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", self._old_dir)
        patcher = mock.patch.dict(os.environ, {
            "XAUTHORITY": os.path.join(self.dir, "no-such-authority")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = FakeXServerExt(self.dir, num=7)
        self.addCleanup(self.server.stop)

    def connect(self) -> X11Conn:
        conn = X11Conn(self.DPY)
        self.addCleanup(conn.close)
        return conn

    # -- new request wrappers -------------------------------------------------

    def test_list_properties_order(self):
        srv = self.server
        srv.set_prop(0x10, "WM_NAME", "STRING", 8, b"x")
        srv.set_prop(0x10, "WM_CLASS", "STRING", 8, b"a\0b\0")
        srv.prop_order[0x10] = ["WM_CLASS", "WM_NAME"]
        conn = self.connect()
        atoms = conn.list_properties(0x10)
        names = [conn.get_atom_name(a) for a in atoms]
        self.assertEqual(names, ["WM_CLASS", "WM_NAME"])

    def test_list_properties_bad_window(self):
        self.server.error_windows.add(0x99)
        conn = self.connect()
        with self.assertRaises(X11Error) as cm:
            conn.list_properties(0x99)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(cm.exception.major, 21)
        self.assertGreater(getattr(cm.exception, "sequence", 0), 0)

    def test_get_atom_name_caches_and_bad_atom(self):
        srv = self.server
        a = srv.intern("SOME_ATOM")
        conn = self.connect()
        self.assertEqual(conn.get_atom_name(a), "SOME_ATOM")
        before = len(srv.log)
        self.assertEqual(conn.get_atom_name(a), "SOME_ATOM")  # cached
        self.assertEqual(len(srv.log), before)
        self.assertIsNone(conn.get_atom_name(0xDEAD))

    def test_read_property_returns_type_name(self):
        self.server.set_prop(0x10, "WM_CLASS", "STRING", 8, b"in\0cls\0")
        conn = self.connect()
        self.assertEqual(conn.read_property(0x10, "WM_CLASS"),
                         ("STRING", 8, b"in\0cls\0"))
        self.assertIsNone(conn.read_property(0x10, "NOPE"))

    def test_delete_property(self):
        srv = self.server
        srv.set_prop(0x10, "DOOMED", "STRING", 8, b"x")
        conn = self.connect()
        self.assertTrue(conn.delete_property(0x10, "DOOMED"))
        self.assertNotIn((0x10, "DOOMED"), srv.props)
        # unknown atom: no request is sent at all
        before = len(srv.log)
        self.assertFalse(conn.delete_property(0x10, "NEVER_INTERNED_XYZ"))
        self.assertEqual([e for e in srv.log[before:]
                          if e[0] == "DeleteProperty"], [])

    def test_change_property_formats(self):
        srv = self.server
        conn = self.connect()
        conn.change_property(0x10, "P32", "CARDINAL", 32,
                             struct.pack("<2I", 1, 0xFFFFFFFF))
        self.assertEqual(srv.props[(0x10, "P32")],
                         ("CARDINAL", 32, struct.pack("<2I", 1,
                                                      0xFFFFFFFF)))
        conn.change_property(0x10, "P8", "STRING", 8, b"hello")
        self.assertEqual(srv.props[(0x10, "P8")], ("STRING", 8, b"hello"))

    # -- events ---------------------------------------------------------------

    def test_select_input_sets_mask(self):
        conn = self.connect()
        conn.select_input(0x10, 0x420000)
        self.assertEqual(self.server.event_masks.get(0x10), 0x420000)

    def test_event_queued_during_reply_wait_then_popped(self):
        srv = self.server
        atom = srv.intern("PINGED")
        # the event arrives BEFORE the sync reply of select_input's void
        # request: _wait_reply must queue it, next_event must pop it
        srv.inject_on_cwa.append(property_notify(1, 0x10, atom, state=0))
        conn = self.connect()
        conn.select_input(0x10, 0x420000)
        ev = conn.next_event(timeout=2)
        self.assertEqual(ev, {"type": "PropertyNotify", "window": 0x10,
                              "atom": atom, "time": 12345, "state": 0})

    def test_next_event_timeout_returns_none(self):
        conn = self.connect()
        conn.select_input(0x10, 0x420000)
        t0 = time.monotonic()
        self.assertIsNone(conn.next_event(timeout=0.2))
        self.assertLess(time.monotonic() - t0, 2)

    def test_next_event_zero_timeout_drains_buffered(self):
        # timeout=0 is a true non-blocking poll: it must still drain an
        # event already sitting in the socket buffer. Regression: the old
        # code returned None before ever polling when wait <= 0.
        srv = self.server
        atom = srv.intern("BUF")
        conn = self.connect()
        conn.select_input(0x10, 0x420000)
        srv.push_event(property_notify(5, 0x10, atom, state=1))
        time.sleep(0.3)  # let the bytes reach our socket buffer
        ev = conn.next_event(timeout=0)
        self.assertIsNotNone(ev)
        self.assertEqual((ev["type"], ev["atom"]),
                         ("PropertyNotify", atom))
        # and with nothing buffered, timeout=0 returns None promptly
        self.assertIsNone(conn.next_event(timeout=0))

    def test_next_event_wire_delivery(self):
        srv = self.server
        atom = srv.intern("LIVE")
        conn = self.connect()
        conn.select_input(0x10, 0x400000)
        threading.Timer(
            0.2, lambda: srv.push_event(
                property_notify(2, 0x10, atom, state=1))).start()
        ev = conn.next_event(timeout=5)
        self.assertIsNotNone(ev)
        self.assertEqual((ev["type"], ev["atom"], ev["state"]),
                         ("PropertyNotify", atom, 1))

    def test_destroy_notify_parses(self):
        srv = self.server
        conn = self.connect()
        conn.select_input(0x10, 0x420000)
        srv.push_event(destroy_notify(3, 0x10))
        ev = conn.next_event(timeout=5)
        self.assertEqual(ev, {"type": "DestroyNotify", "window": 0x10})

    def test_event_queue_is_bounded(self):
        srv = self.server
        atom = srv.intern("FLOOD")
        srv.inject_on_cwa.extend(
            property_notify(1, 0x10, atom) for _ in range(50))
        conn = self.connect()
        with mock.patch.object(x11_mini, "_MAX_EVENT_QUEUE", 10):
            conn.select_input(0x10, 0x420000)
            conn.atom("SYNC_ROUNDTRIP")  # forces a _wait_reply drain
            time.sleep(0.3)
            conn.atom("SYNC_ROUNDTRIP2")
            self.assertLessEqual(len(conn._events), 10)

    def test_wwmctl_paths_untouched_without_select_input(self):
        # no select_input -> no _events attribute is ever created, so the
        # wwmctl behavior (skip events) is bit-identical
        srv = self.server
        srv.set_prop(0x10, "WM_CLASS", "STRING", 8, b"a\0b\0")
        conn = self.connect()
        self.assertEqual(conn.get_wm_class(0x10), ("a", "b"))
        self.assertIsNone(getattr(conn, "_events", None))

    def test_next_event_on_closed_connection(self):
        conn = self.connect()
        conn.close()
        with self.assertRaises(XUnavailable):
            conn.next_event(timeout=0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
