"""Hardening regressions: multi-output geometry origins, daemon request
robustness and DoS bounds, partial device creation retry, XF86 keysyms,
full keycode registration, and backend parsing fixes."""

import contextlib
import errno
import io
import json
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common.errors import CmdError
from support import RecorderDev
from wdotool import backend, daemon, keymap, uinput
from wdotool.keysyms import NAME_TO_KEYSYM

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")


def make_daemon(geom=(0, 0, 1920, 1080), rel_abs=False):
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = geom
    # Pin the relative-move mode so these tests never depend on whether a
    # sway socket happens to exist where they run (see B1 / _rel_absolute).
    d._rel_abs = rel_abs
    return d


# ---------------------------------------------------------------------------
# multi-output bounding boxes with non-zero/negative origins


class TestBBox(unittest.TestCase):
    def test_single_output_at_origin(self):
        self.assertEqual(daemon._bbox_of([(0, 0, 1280, 720)]), (0, 0, 1280, 720))

    def test_negative_x_origin(self):
        boxes = [(0, 0, 1280, 720), (-1920, 0, 1920, 1080)]
        self.assertEqual(daemon._bbox_of(boxes), (-1920, 0, 3200, 1080))

    def test_positive_offset_origin(self):
        boxes = [(100, 50, 800, 600)]
        self.assertEqual(daemon._bbox_of(boxes), (100, 50, 800, 600))

    def test_vertical_stack_negative_y(self):
        boxes = [(0, -1080, 1920, 1080), (0, 0, 1920, 1080)]
        self.assertEqual(daemon._bbox_of(boxes), (0, -1080, 1920, 2160))


def compositor_x(axis_value: int, span: int) -> int:
    """libinput scale_axis() + the compositor's pixel truncation: the pixel a
    tablet axis value lands on is floor(v * span / (max - min + 1))."""
    return axis_value * span // 32768


class TestMultiOutputPointer(unittest.TestCase):
    GEOM = (-1920, 0, 3200, 1080)  # HEADLESS-2 at -1920,0 + 1280x720... etc.

    def _abs_values(self, d):
        """(ABS_X, ABS_Y) of the last report the tablet emitted."""
        vals = {}
        for ev in d.tablet.events:
            if ev[0] == uinput.EV_ABS:
                vals[ev[1]] = ev[2]
        return vals[uinput.ABS_X], vals[uinput.ABS_Y]

    def test_abs_maps_layout_origin_to_zero(self):
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(-1920, 0, [])
        self.assertEqual((d.px, d.py), (-1920, 0))
        self.assertEqual(self._abs_values(d), (0, 0))

    def test_abs_maps_layout_max_to_last_pixel(self):
        # B7: the far edge need not be exactly 32767 -- what matters is that
        # the compositor's inverse maps it back to the last pixel.
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(1279, 1079, [])
        self.assertEqual((d.px, d.py), (1279, 1079))
        ax, ay = self._abs_values(d)
        self.assertEqual(compositor_x(ax, 3200), 3199)
        self.assertEqual(compositor_x(ay, 1080), 1079)

    def test_abs_round_trips_on_every_head(self):
        """B7: every global x must survive daemon -> tablet -> compositor.
        The old (x-gx)*32767//(w-1) floor map lost a pixel wherever the
        division was inexact (257 of 301 x values near the origin on the
        3-head rig)."""
        for geom in ((-1920, 0, 3200, 1080),      # two heads, negative origin
                     (0, 0, 5760, 1080),          # the 3x1920 stress rig
                     (0, 0, 1920, 1080),
                     (100, 50, 800, 600)):
            gx, gy, w, h = geom
            d = make_daemon(geom)
            xs = (list(range(gx, gx + 320))
                  + list(range(gx + w - 320, gx + w))
                  + list(range(gx + w // 2 - 40, gx + w // 2 + 40)))
            for x in xs:
                d.tablet.events.clear()
                d.op_mousemove_abs(x, gy, [])
                ax, _ay = self._abs_values(d)
                self.assertEqual(gx + compositor_x(ax, w), x,
                                 "geom %r x=%d axis=%d" % (geom, x, ax))

    def test_abs_zero_is_scaled_from_origin(self):
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(0, 0, [])
        ax, _ay = self._abs_values(d)
        self.assertEqual(ax, -((-1920 * 32768) // 3200))
        self.assertEqual(compositor_x(ax, 3200), 1920)

    def test_abs_clamps_at_negative_edge(self):
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(-99999, -99999, [])
        self.assertEqual((d.px, d.py), (-1920, 0))
        d.op_mousemove_abs(99999, 99999, [])
        self.assertEqual((d.px, d.py), (1279, 1079))

    def test_rel_clamps_at_negative_edge(self):
        d = make_daemon(self.GEOM)
        d.px, d.py = -1910, 5
        d.op_mousemove_rel(-100, -100, [])
        self.assertEqual((d.px, d.py), (-1920, 0))


# ---------------------------------------------------------------------------
# request validation / robustness (in-process serve_client over a socketpair)


class TestServeClient(unittest.TestCase):
    def serve(self, d):
        a, b = socket.socketpair()
        threading.Thread(target=d.serve_client, args=(b,), daemon=True).start()
        self.addCleanup(a.close)
        return a, a.makefile("r", encoding="utf-8")

    def test_malformed_requests_keep_serving(self):
        d = make_daemon()
        sock, rfile = self.serve(d)
        battery = [
            b"this is not json\n",
            b"5\n",
            b'"just a string"\n',
            b"[1, 2, 3]\n",
            b'{"op": "mousemove_abs", "x": null, "y": null}\n',
            b'{"op": "mousemove_abs", "x": "40", "y": []}\n',
            b'{"op": "key"}\n',
            b'{"op": "type", "text": 7}\n',
            b'{"op": "button", "btn": true, "down": true}\n',
            b'{"op": "click", "btn": 1, "repeat": "many"}\n',
        ]
        for req in battery:
            sock.sendall(req)
            resp = json.loads(rfile.readline())
            self.assertFalse(resp["ok"], f"{req!r} unexpectedly succeeded")
            self.assertIn("error", resp)
        # the connection (and daemon) must still serve after all of that
        sock.sendall(b'{"op": "ping"}\n')
        self.assertTrue(json.loads(rfile.readline())["ok"])
        sock.sendall(b'{"op": "mousemove_abs", "x": 10, "y": 20}\n')
        self.assertTrue(json.loads(rfile.readline())["ok"])
        self.assertEqual((d.px, d.py), (10, 20))

    def test_a_request_line_that_is_not_utf8_keeps_serving(self):
        """The connection's makefile decoded strict, so a byte that is not
        UTF-8 raised UnicodeDecodeError out of readline() -- where only
        OSError is caught. The connection thread died with a traceback in the
        daemon log and the client's next request got EPIPE."""
        d = make_daemon()
        sock, rfile = self.serve(d)
        sock.sendall(b'{"op": "type", "text": "\xff\xfe"}\n')
        resp = json.loads(rfile.readline())
        self.assertTrue(resp["ok"])
        self.assertTrue(any("\ufffd" in w for w in resp.get("warnings", [])),
                        resp)
        sock.sendall(b'{"op": "ping"\xff}\n')               # not JSON either
        self.assertFalse(json.loads(rfile.readline())["ok"])
        sock.sendall(b'{"op": "ping"}\n')                  # still serving
        self.assertTrue(json.loads(rfile.readline())["ok"])

    def test_dos_bounds(self):
        d = make_daemon()
        sock, rfile = self.serve(d)
        for req, field in [
            (b'{"op": "click", "btn": 1, "repeat": 999999999}\n', "repeat"),
            (b'{"op": "click", "btn": 1, "delay_ms": 999999999}\n', "delay_ms"),
            (b'{"op": "key", "spec": "a", "delay_ms": 400000}\n', "delay_ms"),
            (b'{"op": "type", "text": "a", "delay_ms": -5}\n', "delay_ms"),
            (b'{"op": "mousemove_abs", "x": 4000000000, "y": 0}\n', "x"),
        ]:
            sock.sendall(req)
            resp = json.loads(rfile.readline())
            self.assertFalse(resp["ok"], f"{req!r} unexpectedly succeeded")
            self.assertIn(field, resp["error"])
        # bounds are inclusive: the documented maxima still work
        sock.sendall(b'{"op": "click", "btn": 1, "repeat": 3, "delay_ms": 0}\n')
        self.assertTrue(json.loads(rfile.readline())["ok"])

    def test_oversized_request_line(self):
        d = make_daemon()
        with mock.patch.object(daemon, "_MAX_REQUEST", 64):
            sock, rfile = self.serve(d)
            sock.sendall(b'{"op": "ping", "pad": "' + b"x" * 500 + b'"}\n')
            resp = json.loads(rfile.readline())
            self.assertFalse(resp["ok"])
            self.assertIn("too large", resp["error"])
            sock.sendall(b'{"op": "ping"}\n')
            self.assertTrue(json.loads(rfile.readline())["ok"])

    def test_handle_rejects_non_dict(self):
        d = make_daemon()
        resp = d.handle(5)
        self.assertFalse(resp["ok"])

    def test_geometry_response_carries_origin(self):
        d = make_daemon((-1920, 0, 3200, 1080))
        resp = d.handle({"op": "geometry"})
        self.assertEqual((resp["x"], resp["y"], resp["w"], resp["h"]),
                         (-1920, 0, 3200, 1080))


# ---------------------------------------------------------------------------
# partial device creation: close what was made, retry on the next request


class TestDeviceRetry(unittest.TestCase):
    def test_partial_failure_closes_created_devices(self):
        d = daemon._Daemon()
        kb = RecorderDev()
        with mock.patch.object(uinput, "keyboard", return_value=kb), \
             mock.patch.object(uinput, "rel_mouse",
                               side_effect=OSError(errno.EPERM, "denied")), \
             contextlib.redirect_stderr(io.StringIO()):
            d.create_devices()
        self.assertTrue(kb.closed)
        self.assertIsNone(d.kb)
        self.assertIsNone(d.mouse)
        self.assertIsNone(d.tablet)
        self.assertIn("uinput", d.dev_error)

    def test_broken_daemon_retries_and_recovers(self):
        d = daemon._Daemon()
        with mock.patch.object(uinput, "keyboard",
                               side_effect=OSError(errno.EPERM, "denied")), \
             contextlib.redirect_stderr(io.StringIO()):
            d.create_devices()
        self.assertIsNotNone(d.dev_error)
        # next request retries device creation and succeeds this time
        kb, ms, tb = RecorderDev(), RecorderDev(), RecorderDev()
        with mock.patch.object(uinput, "keyboard", return_value=kb), \
             mock.patch.object(uinput, "rel_mouse", return_value=ms), \
             mock.patch.object(uinput, "abs_pointer", return_value=tb), \
             mock.patch.dict(os.environ, {"WDOTOOL_FAKE_UINPUT": "1"}):
            d._need_devices()
        self.assertIsNone(d.dev_error)
        self.assertIs(d.kb, kb)
        d.op_button(1, True)  # and it injects
        self.assertEqual(ms.events, [("KEY", uinput.BTN_LEFT, 1)])

    def test_still_broken_retry_raises_cleanly(self):
        d = daemon._Daemon()
        os.environ["WDOTOOL_UINPUT_PATH"] = "/nonexistent/wdotool-uinput"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        with contextlib.redirect_stderr(io.StringIO()):
            d.create_devices()
            first_err = d.dev_error
            with self.assertRaises(RuntimeError):
                d._need_devices()
        self.assertEqual(d.dev_error, first_err)


# ---------------------------------------------------------------------------
# XF86 keysyms


class TestXF86Keysyms(unittest.TestCase):
    REQUIRED = {
        "XF86AudioMute": 113, "XF86AudioLowerVolume": 114,
        "XF86AudioRaiseVolume": 115, "XF86AudioMicMute": 248,
        "XF86AudioPlay": 164, "XF86AudioPause": 201, "XF86AudioNext": 163,
        "XF86AudioPrev": 165, "XF86AudioStop": 166,
        "XF86MonBrightnessUp": 225, "XF86MonBrightnessDown": 224,
        "XF86Back": 158, "XF86Forward": 159, "XF86Refresh": 173,
        "XF86Stop": 128, "XF86Search": 217, "XF86HomePage": 172,
        "XF86Reload": 173, "XF86Mail": 155, "XF86Calculator": 140,
        "XF86Explorer": 144, "XF86Tools": 171, "XF86Favorites": 156,
        "XF86MyComputer": 157, "XF86PowerOff": 116, "XF86Sleep": 142,
        "XF86WakeUp": 143, "XF86ScreenSaver": 152, "XF86WWW": 150,
        "XF86Display": 227, "XF86KbdBrightnessUp": 230,
        "XF86KbdBrightnessDown": 229, "XF86Eject": 162,
    }

    def test_generated_table_has_xf86_names(self):
        self.assertEqual(NAME_TO_KEYSYM["XF86AudioMute"], 0x1008FF12)
        self.assertEqual(NAME_TO_KEYSYM["XF86AudioRaiseVolume"], 0x1008FF13)
        self.assertEqual(NAME_TO_KEYSYM["XF86MonBrightnessUp"], 0x1008FF02)
        self.assertGreater(
            sum(1 for n in NAME_TO_KEYSYM if n.startswith("XF86")), 300)

    def test_required_names_resolve_to_evdev_codes(self):
        for name, code in self.REQUIRED.items():
            self.assertEqual(keymap.resolve_token(name), (code, False), name)

    def test_evdevk_range_resolves_automatically(self):
        # XF86MediaPlayPause is _EVDEVK(0x0a4): keysym embeds KEY_PLAYPAUSE
        self.assertEqual(NAME_TO_KEYSYM["XF86MediaPlayPause"], 0x100810A4)
        self.assertEqual(keymap.resolve_token("XF86MediaPlayPause"), (164, False))

    def test_unknown_xf86_name_still_warns(self):
        hit = keymap.resolve_token("XF86NoSuchKeyEver")
        self.assertIsInstance(hit, str)
        self.assertIn("No such key name", hit)

    def test_parse_keyseq_with_xf86(self):
        keys, warnings = keymap.parse_keyseq("XF86AudioMute")
        self.assertEqual(keys, [(113, False)])
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# keyboard registers every keycode the keymap can emit (1..255)


class TestKeysymsAboveUnicode(unittest.TestCase):
    """The Unicode keysym space is `0x01000000 | codepoint`, 24 bits wide;
    Unicode is 21. `wdotool key 0x01ffffff` is therefore a well-formed
    keysym naming no character at all, and chr() answered it with a
    ValueError out of the middle of a key line."""

    def test_a_keysym_past_the_last_codepoint_is_just_unreachable(self):
        for ks in (0x01110000, 0x0117FFFF, 0x01FFFFFF):
            self.assertIsNone(keymap._keysym_value_to_key(ks), hex(ks))

    def test_the_key_command_warns_and_carries_on(self):
        msg = keymap.resolve_token("0x01ffffff")
        self.assertIsInstance(msg, str)          # the "ignoring it" warning
        self.assertIn("not reachable", msg)

    def test_the_last_real_codepoint_still_resolves_the_same(self):
        # the guard must not shave anything off the range that works
        self.assertEqual(keymap._keysym_value_to_key(0x01000061),
                         keymap._keysym_value_to_key(0x61))
        self.assertIsNotNone(keymap._keysym_value_to_key(0x01000061))


class TestWarningsNameTheRunningTool(unittest.TestCase):
    """The window backends are shared by three CLIs, and their warnings
    hard-coded "wdotool:". A `wmctrl -b` run on KWin answered in the name
    of a tool that was not on the command line, and `wwmctl -l` with no
    session said "wdotool: no Wayland session found". One warn(), and the
    name is whichever main() is running."""

    def setUp(self):
        self.addCleanup(backend.set_program, backend.program())

    def run_main(self, main, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main(list(argv))
            except SystemExit:
                pass
        return backend.program()

    def test_each_main_names_itself(self):
        from wdotool import cli as wdotool_cli
        from wwmctl import cli as wwmctl_cli
        from wxprop import cli as wxprop_cli

        backend.set_program("nobody")
        self.assertEqual(self.run_main(wwmctl_cli.main, ["--version"]),
                         "wwmctl")
        # wxprop answers to argv[0], like the xprop it replaces
        with mock.patch.dict(os.environ, {"WXPROP_ARGV0": "xprop"}):
            self.assertEqual(self.run_main(wxprop_cli.main, ["-version"]),
                             "xprop")
        self.assertEqual(
            self.run_main(wdotool_cli.main, ["wdotool", "version"]), "wdotool")

    def test_warn_writes_one_line_in_that_name(self):
        backend.set_program("wwmctl")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # A neutral sample, on purpose: this test is about the `wwmctl: ` prefix, and the literal it
            # used to carry (`windowlower: sway cannot lower windows; ignoring`) is the sentence
            # backend_sway retired on 2026-09-09 -- keeping a copy of it here left the tree holding the
            # wording the rule replaced, where a grep for it would find a live-looking hit in a test.
            backend.warn("windowlower: not yet here (AGENTS.md route 6); ignoring")
        self.assertEqual(err.getvalue(), "wwmctl: windowlower: not yet here "
                                         "(AGENTS.md route 6); ignoring\n")

    def test_no_backend_carries_a_tool_name_of_its_own(self):
        """The rule, not one instance of it: a new warning must not bring
        back a hard-coded prefix (three of them were spread over kwin, sway
        and gnome, plus the detect message all three tools print)."""
        pkg = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "wdotool")
        for name in sorted(n for n in os.listdir(pkg)
                           if n.startswith("backend") and n.endswith(".py")):
            with open(os.path.join(pkg, name)) as f:
                for i, line in enumerate(f, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    for tool in ("wdotool", "wwmctl", "wxprop"):
                        self.assertNotIn('"%s: ' % tool, line,
                                         "%s:%d" % (name, i))
                        self.assertNotIn("'%s: " % tool, line,
                                         "%s:%d" % (name, i))


class TestKeycodeRegistration(unittest.TestCase):
    def test_numeric_keycodes_up_to_255_accepted(self):
        self.assertEqual(keymap.resolve_token("263"), (255, False))
        self.assertEqual(keymap.resolve_token("264"),
                         "key '264' is not reachable on the US layout. Ignoring it.")

    def test_keyboard_registers_1_to_255_and_the_uploaded_extras(self):
        fd, path = tempfile.mkstemp(prefix="wdotool-kbd-")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        recorded = []

        def fake_ioctl(fd_, req, arg=0):
            recorded.append((req, arg))
            return 0

        with mock.patch.dict(os.environ, {"WDOTOOL_UINPUT_PATH": path}), \
             mock.patch.object(uinput.fcntl, "ioctl", side_effect=fake_ioctl):
            os.environ.pop("WDOTOOL_FAKE_UINPUT", None)
            dev = uinput.keyboard()
            dev.close()
        keybits = [arg for req, arg in recorded if req == uinput.UI_SET_KEYBIT]
        # 1..255, then every code the uploaded keymap binds above that block (the euro on
        # evdev 435): the character table answers those on the kernel sink too, and a code
        # that is not registered is an event the kernel drops silently
        from wdotool import keymap
        extra = sorted({code for code, _s in keymap.UPLOADED_EXTRA_KEYS.values()} - set(range(1, 256)))
        self.assertTrue(extra, "the uploaded keymap binds nothing above 255 any more")
        self.assertEqual(keybits, list(range(1, 256)) + extra)


# ---------------------------------------------------------------------------
# wayland_mini: malformed message must raise, not spin


class TestWaylandMalformed(unittest.TestCase):
    def test_short_size_raises(self):
        from w11common.wayland_mini import WlConn

        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        conn = WlConn.__new__(WlConn)
        conn.sock, conn.buf, conn.fds = b, b"", []
        conn.handlers, conn.dead = {}, None
        a.sendall(struct.pack("<II", 1, (4 << 16) | 0))  # size=4 < 8
        with self.assertRaises(RuntimeError):
            conn._dispatch_some()

    def _conn(self):
        from w11common.wayland_mini import WlConn

        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        conn = WlConn.__new__(WlConn)
        conn.sock, conn.buf, conn.fds = b, b"", []
        conn.handlers, conn.dead = {}, None
        return conn, a

    def test_dispatch_restores_the_callers_timeout(self):
        """dispatch() used to clear the socket timeout on the way out.  Every
        wxrandr backend arms a deadline before its apply loop so a compositor
        that goes quiet cannot hang the CLI; clearing it left the next read
        blocking in recvmsg with nothing to wake it."""
        conn, peer = self._conn()
        conn.sock.settimeout(10.0)
        self.assertFalse(conn.dispatch(timeout=0.05))     # nothing to read
        self.assertEqual(conn.sock.gettimeout(), 10.0)
        peer.sendall(struct.pack("<II", 1, (8 << 16) | 0))
        self.assertTrue(conn.dispatch(timeout=0.5))       # and on the way in
        self.assertEqual(conn.sock.gettimeout(), 10.0)

    def test_dispatch_keeps_a_blocking_socket_blocking(self):
        conn, peer = self._conn()
        conn.sock.settimeout(None)
        peer.sendall(struct.pack("<II", 1, (8 << 16) | 0))
        self.assertTrue(conn.dispatch(timeout=0.5))
        self.assertIsNone(conn.sock.gettimeout())


# ---------------------------------------------------------------------------
# sway display_size bounding box


class TestSwayDisplaySize(unittest.TestCase):
    def size_for(self, outputs):
        from wdotool.backend_sway import SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b._msg = lambda *a, **k: outputs
        return b.display_size()

    def rect(self, x, y, w, h, active=True):
        return {"active": active,
                "rect": {"x": x, "y": y, "width": w, "height": h}}

    def test_single(self):
        self.assertEqual(self.size_for([self.rect(0, 0, 1280, 720)]), (1280, 720))

    def test_negative_origin(self):
        outs = [self.rect(0, 0, 1280, 720), self.rect(-1920, 0, 1920, 1080)]
        self.assertEqual(self.size_for(outs), (3200, 1080))

    def test_inactive_ignored(self):
        outs = [self.rect(0, 0, 1280, 720),
                self.rect(-9999, 0, 1920, 1080, active=False)]
        self.assertEqual(self.size_for(outs), (1280, 720))



class TestSocketIsPrivate(unittest.TestCase):
    """With no XDG_RUNTIME_DIR -- every `sudo` run, `su -`, cron, a bare
    container -- the daemon socket used to be /tmp/wdotool-<uid>.sock in a
    world-writable directory. Another local user could bind it first and
    receive every request, the text of `type` included, answering {"ok":true}
    so the caller saw success."""

    def setUp(self):
        self.saved = os.environ.get("XDG_RUNTIME_DIR")
        os.environ.pop("XDG_RUNTIME_DIR", None)
        self.d = "/tmp/wdotool-%d" % os.getuid()

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.saved

    def test_fallback_socket_lives_in_a_private_directory(self):
        path = daemon.socket_path()
        self.assertEqual(os.path.dirname(path), self.d)
        st = os.lstat(self.d)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_mode & 0o077, 0)     # nobody else may enter
        # ...and the startup lock is inside it too, out of reach of a plant
        self.assertTrue((path + ".lock").startswith(self.d + "/"))

    def test_a_directory_someone_else_could_write_is_refused(self):
        with mock.patch.object(daemon.os, "lstat") as ls:
            ls.return_value = os.stat_result(
                (stat.S_IFDIR | 0o777, 0, 0, 1, os.getuid(), 0, 0, 0, 0, 0))
            with self.assertRaises(CmdError) as cm:
                daemon.socket_path()
        self.assertIn("private directory", str(cm.exception))

    def test_the_client_refuses_another_user_s_daemon(self):
        """A socket we did not create is not ours to type into: SO_PEERCRED
        says who is listening, and only our own euid may be."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, True))
        path = os.path.join(tmp, "sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)
        # our own daemon: accepted
        self.assertIsNotNone(daemon.DaemonClient._try_connect(path))
        with mock.patch.object(daemon.DaemonClient, "_peer_uid",
                               staticmethod(lambda s: os.geteuid() + 1)):
            with self.assertRaises(CmdError) as cm:
                daemon.DaemonClient._try_connect(path)
        self.assertIn("belongs to uid", str(cm.exception))

    def test_a_planted_lock_or_socket_is_an_error_not_a_traceback(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, True))
        path = os.path.join(tmp, "sock")
        os.symlink("/nonexistent/elsewhere", path + ".lock")   # O_NOFOLLOW
        err = io.StringIO()
        with mock.patch.object(daemon, "socket_path", lambda: path), \
                contextlib.redirect_stderr(err):
            self.assertEqual(daemon.daemon_main(), 1)
        self.assertIn("cannot open", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TestUinputGrantIsRechecked(unittest.TestCase):
    """logind's uaccess ACL is only consulted at open(), so a daemon that
    opened /dev/uinput while its user was at the seat kept injecting after a
    VT switch or a fast user switch handed the seat to somebody else."""

    def _daemon(self):
        d = daemon._Daemon()
        d.kb = d.mouse = d.tablet = RecorderDev()
        d.kb.fake = False          # pretend these came from the real node
        d.dev_error = None
        d._reader = None
        return d

    def test_the_devices_are_dropped_when_the_grant_is_gone(self):
        d = self._daemon()
        with mock.patch.object(uinput, "access_ok", lambda: True):
            d._need_devices()                       # still ours: fine
        err = io.StringIO()
        with mock.patch.object(uinput, "access_ok", lambda: False), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(RuntimeError) as cm:
                d._need_devices()
        self.assertIn("no longer accessible", str(cm.exception))
        self.assertIsNone(d.kb)                     # devices destroyed
        self.assertEqual(d.down, set())

    def test_root_and_fake_devices_are_never_probed(self):
        d = self._daemon()
        d.kb.fake = True                            # the test/fake path
        with mock.patch.object(uinput, "access_ok", lambda: False):
            d._need_devices()                       # not probed at all
        self.assertIsNotNone(d.kb)
        with mock.patch.object(uinput.os, "geteuid", lambda: 0):
            self.assertTrue(uinput.access_ok())     # root needs no grant


class TestClearModifiersReleases(unittest.TestCase):
    def test_keyup_clearmodifiers_leaves_the_modifier_up(self):
        """`keyup --clearmodifiers ctrl` used to press ctrl back down after
        releasing it -- and only the device holding a key can release it, so
        it stayed down for the daemon's lifetime, turning every later click
        into a ctrl-click."""
        d = daemon._Daemon()
        d.kb = d.mouse = d.tablet = RecorderDev()
        d.dev_error = None
        d._reader = None
        d.handle({"op": "key", "spec": "ctrl", "direction": "down",
                  "delay_ms": 0}, None)
        self.assertIn(keymap.KEY_LEFTCTRL, d.down)
        d.handle({"op": "key", "spec": "ctrl", "direction": "up",
                  "delay_ms": 0, "clearmods": True}, None)
        self.assertNotIn(keymap.KEY_LEFTCTRL, d.down)
        last = [v for (_t, c, v) in d.kb.events if c == keymap.KEY_LEFTCTRL]
        self.assertEqual(last[-1], 0)               # ends released
        # a modifier the op did NOT touch is still restored
        d.handle({"op": "key", "spec": "shift", "direction": "down",
                  "delay_ms": 0}, None)
        d.handle({"op": "key", "spec": "a", "direction": "press",
                  "delay_ms": 0, "clearmods": True}, None)
        self.assertIn(keymap.KEY_LEFTSHIFT, d.down)

if __name__ == "__main__":
    unittest.main()
