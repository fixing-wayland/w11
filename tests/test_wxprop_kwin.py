#!/usr/bin/env python3
"""wxprop on Plasma: the KWin backend driven through the CLI.

The counterpart of tests/test_wxprop_gnome.py for the other Wayland desktop.
What is here rather than there: KWin has no window signals on D-Bus at all,
so `-spy` runs on a script that stays loaded in the compositor and calls back
out for every Qt signal it hooked -- a different stream, with the same
contract (reprint when a watched property changed, exit when the window
goes). And Plasma 6 mints its window ids from KWin uuids while XWayland keeps
real X ids, so the two id spaces sit side by side in one listing and `-id`
has to say which one it was handed.

Measured on the rig (resolute-kde, Plasma 6.6 / KWin 6.6.6): `wxprop -id`
prints WM_CLASS and _NET_WM_STATE correctly for both planes, which is what
the dumps below are checked against."""

import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import wl_fake` resolve only with the tests directory
# itself on sys.path: running this file by path puts it there for free,
# `python3 -m unittest tests/<file>.py` does not (tests/test_passthrough.py
# fails over a file that imports one of them without this line).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

from test_backend_kwin import UU, WID, XTERM_XID, _Base, _FakeX
from test_wxprop_cli import _CapStdout
from wxprop import cli
from hacks.property import core

#: the xterm as Xwayland reports it, matching the fixture's XWayland row
XTERM_CLIENT = {"xid": XTERM_XID, "pid": 1201, "inst": "xterm",
                "cls": "XTerm", "name": "test@kde: ~",
                "geo": (101, 105, 638, 454)}


class KwinXpropBase(_Base):
    def setUp(self):
        self.b = self.backend()
        self.b._x = _FakeX([dict(XTERM_CLIENT)])

    def _patches(self, x=None, xwayland=False):
        os.environ.pop("WXPROP_NO_X", None)
        return [
            mock.patch.dict(os.environ, {"WXPROP_ARGV0": "xprop",
                                         "LC_ALL": "C"}),
            mock.patch.object(core, "_detect_backend", lambda: self.b),
            mock.patch.object(core, "_x11_connect",
                              lambda display=None, xauthority=None: x),
            mock.patch.object(core, "hostname", lambda: "testhost"),
            mock.patch.object(core, "_xwayland_running", lambda: xwayland),
        ]

    def run_cli(self, *args, x=None, xwayland=False):
        out, err = _CapStdout(), io.StringIO()
        ps = self._patches(x, xwayland) + [
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(sys, "stderr", err)]
        for p in ps:
            p.start()
        try:
            code = cli.main(list(args))
        finally:
            for p in reversed(ps):
                p.stop()
        return code, out.buffer.getvalue(), err.getvalue()


class RootTests(KwinXpropBase):
    def test_root_without_xwayland_is_the_synthesized_root(self):
        """A KWin session with no Xwayland at all -- the backend has no X
        connection of its own either (`_x = None`, as on a Plasma image
        built without xwayland), so no row in the payload carries an X id
        and every window is listed under the id KWin's uuid mints.
        _NET_SUPPORTING_WM_CHECK is 0x0 because there is no real check
        window to point at, which is the one honest way to say "this root
        does not exist on any X server"."""
        self.b._x = None
        code, out, err = self.run_cli("-root")
        self.assertEqual((code, err), (0, ""))
        lines = out.decode().splitlines()
        self.assertIn("_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x0",
                      lines)
        self.assertIn(
            "_NET_CLIENT_LIST(WINDOW): window id # 0x%08x, 0x%08x, 0x%08x, "
            "0x%08x" % (WID["desktop"], WID["kate"], WID["konsole"],
                        WID["xterm"]), lines)
        self.assertNotIn("0x%x" % XTERM_XID, out.decode())
        self.assertIn("_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 2", lines)
        self.assertIn('_NET_DESKTOP_NAMES(UTF8_STRING) = "Desktop 1", '
                      '"Desktop 2"', lines)
        self.assertIn("_NET_CURRENT_DESKTOP(CARDINAL) = 0", lines)

    def test_root_is_synthesized_when_only_wxprop_has_no_x_connection(self):
        """The mixed case, and the one the rig actually shows: Xwayland is
        running, the *backend* talked to it (setUp hands it `_FakeX` with
        the xterm's real 0x400005) and so the xterm is listed under that id
        -- but wxprop itself has no X connection (`_x11_connect` -> None,
        `_xwayland_running` false), so the root it prints is still the
        synthesized one. The client list is the proof that the two id
        spaces sit side by side in one listing: three KWin ids and one real
        X id under a root that has no X server behind it."""
        code, out, err = self.run_cli("-root")
        self.assertEqual((code, err), (0, ""))
        lines = out.decode().splitlines()
        self.assertIn("_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x0",
                      lines)
        self.assertIn(
            "_NET_CLIENT_LIST(WINDOW): window id # 0x%08x, 0x%08x, 0x%08x, "
            "0x%x" % (WID["desktop"], WID["kate"], WID["konsole"], XTERM_XID),
            lines)


class IdTests(KwinXpropBase):
    def test_a_kwin_id_and_an_x_id_each_reach_their_own_window(self):
        code, out, err = self.run_cli("-id", str(WID["kate"]), "WM_NAME")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b'WM_NAME(STRING) = "untitled - Kate"\n')
        code, out, err = self.run_cli("-id", str(XTERM_XID), "WM_CLASS")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_an_x_id_wins_over_a_native_id_of_the_same_number(self):
        """The two id spaces are disjoint by construction -- a KWin id is
        minted with its top bit set, above anything an X server hands out --
        but nothing stops a *test* (or a future Xwayland) from colliding
        them, and then the answer has to be one window, chosen by a rule and
        not by dict order. The X plane wins: an id that a real X server
        would answer for is the id the user typed."""
        collide = WID["kate"]
        self.b._x = _FakeX([dict(XTERM_CLIENT, xid=collide)])
        code, out, err = self.run_cli("-id", str(collide),
                                      "WM_CLASS", "WM_NAME")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n'
                              b'WM_NAME(STRING) = "test@kde: ~"\n')


class SpyTests(KwinXpropBase):
    """`-spy` runs the CLI on a thread; the test plays KWin's resident event
    script, which calls back over KWin's own bus connection (the backend
    ignores an Event from anyone else -- the token lives in a file KWin has
    to be able to read, so it is not proof of who is talking)."""

    def _spy(self, *args, **kw):
        result = {}
        ready = threading.Event()

        def target():
            out, err = _CapStdout(), io.StringIO()
            ps = self._patches(**kw) + [
                mock.patch.object(sys, "stdout", out),
                mock.patch.object(sys, "stderr", err)]
            for p in ps:
                p.start()
            ready.set()
            try:
                result["code"] = cli.main(list(args))
            finally:
                for p in reversed(ps):
                    p.stop()
                result["out"] = out.buffer.getvalue()
                result["err"] = err.getvalue()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        ready.wait(5)
        # the initial dump, then the events script loaded inside KWin
        self.assertTrue(self.kwin.events_ready.wait(10), "no events script")
        return t, result

    def emit(self, uuid, change):
        self.kwin.emit(self.kwin.bus, uuid, change)
        time.sleep(0.3)

    def test_spy_on_a_window_reprints_on_a_state_event_and_exits_on_close(self):
        """`fullscreen_mode` is KWin's own signal name, which the script
        forwards verbatim; the backend keeps sway's vocabulary so one spy
        loop serves every compositor. A `close` ends the loop with rc 0 --
        the window is gone, and xprop's -spy exits rather than erroring."""
        t, r = self._spy("-spy", "-id", str(WID["kate"]), "_NET_WM_STATE")
        self.kwin.find(UU["kate"]).update(fs=True, m=False, hi=False)
        self.emit(UU["kate"], "fullscreen_mode")
        self.emit(UU["konsole"], "fullscreen_mode")   # somebody else
        self.emit(UU["kate"], "close")
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["out"],
                         b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN\n"
                         b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n")

    def test_spy_on_the_root_reprints_on_a_workspace_event(self):
        """A workspace switch reaches the loop as the (0, "workspace") row
        the backends fold their own workspace stream into, so `-root -spy`
        needs one stream and not two."""
        self._limit_events()
        t, r = self._spy("-spy", "-root", "_NET_CURRENT_DESKTOP")
        self.kwin.current = 1
        self.emit("", "workspace")
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["out"],
                         b"_NET_CURRENT_DESKTOP(CARDINAL) = 0\n"
                         b"_NET_CURRENT_DESKTOP(CARDINAL) = 1\n")

    def _limit_events(self, seconds=2.0):
        """The root loop has no natural end (no window to close), so make the
        backend's stream stop after `seconds` of silence."""
        real = self.b.events

        def limited(timeout=None, workspaces=False):
            return real(timeout=seconds, workspaces=workspaces)

        self.b.events = limited


if __name__ == "__main__":
    unittest.main()
