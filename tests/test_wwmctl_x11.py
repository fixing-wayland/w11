#!/usr/bin/env python3
"""tests for wdotool.x11_mini, the pure-stdlib X11 wire client.

Two layers:

- FakeXServerTest: a programmable in-process fake X server (real unix
  socket, real setup handshake with MIT-MAGIC-COOKIE-1 checking, real
  32-byte reply/error framing) exercises the wire client offline —
  auth-file parsing and fallback order, DISPLAY forms, socket probing,
  stale sockets, GetProperty type/format/long-offset handling, WM_CLASS
  splitting, ChangeProperty (set_name), SendEvent framing, QueryTree
  fallback, and X-error mapping.
- X11LiveTest: the same client against a REAL XWayland under headless sway,
  cross-checked byte-for-byte against xprop/xwininfo and the sway tree.
  Skips cleanly when the testbed tools are absent (run in `nix develop`).
"""

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
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

# FakeXServer and its three helpers moved to tests/support.py with the rest
# of the doubles (batch 1 of the xw11 proxy); the names are imported back
# because six other test files reach them through this module.
from support import (FakeXServer, HeadlessSway, _pad4,     # noqa: F401
                     _recvn, write_xauth)

from wdotool import x11_mini
from wdotool.x11_mini import X11Conn, X11Error, XUnavailable

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class FakeXServerTest(unittest.TestCase):
    DPY = ":7"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="x11mini-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._old_dir = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", self._old_dir)
        # keep any real ~/.Xauthority out of the picture by default
        patcher = mock.patch.dict(os.environ, {
            "XAUTHORITY": os.path.join(self.dir, "no-such-authority")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.stop()

    def serve(self, **kw) -> FakeXServer:
        self.server = FakeXServer(self.dir, num=7, **kw)
        return self.server

    def connect(self, display=DPY) -> X11Conn:
        conn = X11Conn(display)
        self.addCleanup(conn.close)
        return conn

    # -- connection setup / auth --------------------------------------------

    def test_cookieless_connect_no_auth_file(self):
        srv = self.serve()
        conn = self.connect()
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])
        self.assertEqual(srv.setup_attempts, [(b"", b"")])

    def test_cookie_exact_hostname_match(self):
        cookie = bytes(range(16))
        srv = self.serve(cookie=cookie, accept_empty=False)
        auth = os.path.join(self.dir, "authority")
        host = socket.gethostname().encode()
        write_xauth(auth, [
            (256, b"elsewhere", b"7", b"MIT-MAGIC-COOKIE-1", b"\xEE" * 16),
            (256, host, b"9", b"MIT-MAGIC-COOKIE-1", b"\xEE" * 16),
            (256, host, b"7", b"MIT-MAGIC-COOKIE-1", cookie),
        ])
        with mock.patch.dict(os.environ, {"XAUTHORITY": auth}):
            conn = self.connect()
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])
        # the exact-host entry was tried first and immediately accepted
        self.assertEqual(srv.setup_attempts,
                         [(b"MIT-MAGIC-COOKIE-1", cookie)])

    def test_cookie_wildcard_and_empty_number(self):
        cookie = b"\x42" * 16
        srv = self.serve(cookie=cookie, accept_empty=False)
        auth = os.path.join(self.dir, "authority")
        write_xauth(auth, [(0xFFFF, b"", b"", b"MIT-MAGIC-COOKIE-1", cookie)])
        with mock.patch.dict(os.environ, {"XAUTHORITY": auth}):
            conn = self.connect()
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])
        self.assertEqual(srv.setup_attempts,
                         [(b"MIT-MAGIC-COOKIE-1", cookie)])

    def test_wrong_cookie_falls_back_to_empty_auth(self):
        srv = self.serve(cookie=b"\x01" * 16, accept_empty=True)
        auth = os.path.join(self.dir, "authority")
        write_xauth(auth, [(256, socket.gethostname().encode(), b"7",
                            b"MIT-MAGIC-COOKIE-1", b"\xBB" * 16)])
        with mock.patch.dict(os.environ, {"XAUTHORITY": auth}):
            conn = self.connect()
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])
        self.assertEqual(len(srv.setup_attempts), 2)
        self.assertEqual(srv.setup_attempts[1], (b"", b""))

    def test_all_auth_rejected_raises_xunavailable(self):
        self.serve(cookie=b"\x01" * 16, accept_empty=False)
        with self.assertRaises(XUnavailable) as cm:
            self.connect()
        self.assertIn("Authentication rejected", str(cm.exception))

    def test_garbage_auth_file_still_connects(self):
        self.serve()
        auth = os.path.join(self.dir, "authority")
        with open(auth, "wb") as f:
            f.write(b"\x01\x00\x03ab")  # truncated mid-entry
        with mock.patch.dict(os.environ, {"XAUTHORITY": auth}):
            conn = self.connect()
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])

    # -- DISPLAY parsing / socket probing -----------------------------------

    def test_display_forms(self):
        self.serve()
        for dpy in (":7", ":7.0", "unix:7", "localhost:7", " :7 "):
            conn = self.connect(dpy)
            self.assertEqual(conn.root(), FakeXServer.ROOTS[0], dpy)
        conn = self.connect(":7.1")  # second screen
        self.assertEqual(conn.root(), FakeXServer.ROOTS[1])
        conn = self.connect(":7.9")  # out of range: clamps
        self.assertEqual(conn.root(), FakeXServer.ROOTS[1])

    def test_display_from_environment(self):
        self.serve()
        with mock.patch.dict(os.environ, {"DISPLAY": ":7"}):
            conn = self.connect(None)
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])

    def test_probe_sockets_without_display(self):
        self.serve()
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            conn = self.connect(None)
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])

    def test_no_display_no_sockets(self):
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(XUnavailable):
                X11Conn(None)
        with self.assertRaises(XUnavailable):
            X11Conn(":9")  # no such socket
        with self.assertRaises(XUnavailable):
            X11Conn("remotehost:0")  # non-local
        with self.assertRaises(XUnavailable):
            X11Conn("garbage")  # unparseable

    def test_stale_socket(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(self.dir, "X3"))
        s.close()  # dead socket file, nobody listening
        with self.assertRaises(XUnavailable):
            X11Conn(":3")
        # ...and the probe tries X3 first (lowest number), skips the corpse,
        # and reaches the live server on X7
        self.serve()
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            conn = self.connect(None)
        self.assertEqual(conn.root(), FakeXServer.ROOTS[0])

    # -- atoms / properties -------------------------------------------------

    def test_atom_intern_and_cache(self):
        srv = self.serve()
        conn = self.connect()
        a = conn.atom("_NET_WM_PID")
        self.assertGreater(a, 0)
        self.assertEqual(conn.atom("_NET_WM_PID"), a)  # cached, no roundtrip
        self.assertEqual(
            [e for e in srv.log if e[0] == "InternAtom"],
            [("InternAtom", "_NET_WM_PID", 0)])
        self.assertEqual(conn.atom("_NET_NOPE", only_if_exists=True), 0)
        self.assertGreater(conn.atom("_NET_NOPE"), 0)  # now created

    def test_get_prop_ints_and_missing(self):
        srv = self.serve()
        win = 0x400001
        srv.set_prop(win, "_NET_WM_PID", "CARDINAL", 32,
                     struct.pack("<I", 4242))
        srv.set_prop(win, "_NET_WM_DESKTOP", "CARDINAL", 32,
                     struct.pack("<I", 0xFFFFFFFF))
        srv.intern("_NET_EXISTS_ELSEWHERE")
        conn = self.connect()
        self.assertEqual(conn.get_prop_ints(win, "_NET_WM_PID"), [4242])
        self.assertEqual(conn.get_pid(win), 4242)
        # EWMH -1 comes back as unsigned 32-bit
        self.assertEqual(conn.get_prop_ints(win, "_NET_WM_DESKTOP"),
                         [0xFFFFFFFF])
        # atom unknown to the server -> absent
        self.assertEqual(conn.get_prop_ints(win, "_NET_UNKNOWN_ATOM"), [])
        # atom known, property not set on this window -> absent
        self.assertEqual(conn.get_prop_ints(win, "_NET_EXISTS_ELSEWHERE"), [])
        self.assertEqual(conn.get_pid(win + 1), 0)
        self.assertEqual(conn.get_prop_string(win + 1, "_NET_WM_PID"), "")

    def test_get_prop_string_types(self):
        srv = self.serve()
        win = 0x400001
        srv.set_prop(win, "_NET_WM_NAME", "UTF8_STRING", 8,
                     "café ☕".encode("utf-8"))
        srv.set_prop(win, "WM_NAME", "STRING", 8, b"caf\xe9")
        srv.set_prop(win, "WM_ICON_NAME", "STRING", 8, b"head\0tail")
        conn = self.connect()
        self.assertEqual(conn.get_prop_string(win, "_NET_WM_NAME"), "café ☕")
        self.assertEqual(conn.get_prop_string(win, "WM_NAME"), "café")
        # printf("%s") semantics: truncated at the first NUL
        self.assertEqual(conn.get_prop_string(win, "WM_ICON_NAME"), "head")

    def test_wm_class_and_client_machine(self):
        srv = self.serve()
        win = 0x400001
        srv.set_prop(win, "WM_CLASS", "STRING", 8, b"xterm\0XTerm\0")
        srv.set_prop(win, "WM_CLIENT_MACHINE", "STRING", 8, b"testhost")
        srv.set_prop(win + 1, "WM_CLASS", "STRING", 8, b"solo")
        conn = self.connect()
        self.assertEqual(conn.get_wm_class(win), ("xterm", "XTerm"))
        self.assertEqual(conn.get_client_machine(win), "testhost")
        self.assertEqual(conn.get_wm_class(win + 1), ("solo", ""))
        self.assertEqual(conn.get_wm_class(win + 2), ("", ""))

    def test_long_property_offset_loop(self):
        srv = self.serve()
        srv.max_chunk_units = 4  # force multiple GetProperty roundtrips
        win = 0x400001
        values = list(range(1000, 1012))
        srv.set_prop(win, "_NET_LONG", "CARDINAL", 32,
                     struct.pack("<12I", *values))
        blob = bytes(range(256)) * 4  # 1024 bytes, format 8
        srv.set_prop(win, "LONG_BYTES", "STRING", 8, blob)
        conn = self.connect()
        self.assertEqual(conn.get_prop_ints(win, "_NET_LONG"), values)
        gets = [e for e in srv.log if e[0] == "GetProperty"
                and e[2] == "_NET_LONG"]
        self.assertEqual([g[3] for g in gets], [0, 4, 8])  # advancing offset
        got = conn._read_property(win, "LONG_BYTES")
        self.assertEqual(got, (srv.intern("STRING"), 8, blob))

    def test_client_list_and_querytree_fallback(self):
        srv = self.serve()
        ids = [0x400001, 0x600002]
        srv.set_prop(FakeXServer.ROOTS[0], "_NET_CLIENT_LIST", "WINDOW", 32,
                     struct.pack("<2I", *ids))
        conn = self.connect()
        self.assertEqual(conn.client_list(), ids)
        # absent _NET_CLIENT_LIST: QueryTree on the root
        srv2 = FakeXServer(self.dir, num=9)
        try:
            srv2.children = [0x400007, 0x400008, 0x400009]
            conn2 = X11Conn(":9")
            self.addCleanup(conn2.close)
            self.assertEqual(conn2.client_list(), srv2.children)
        finally:
            srv2.stop()

    # -- geometry -----------------------------------------------------------

    def test_get_geometry_translates_origin(self):
        srv = self.serve()
        win = 0x400001
        srv.geometry[win] = (13, 17, 640, 480)   # parent-relative junk
        srv.translate[win] = (960, 0)            # true root-relative origin
        conn = self.connect()
        self.assertEqual(conn.get_geometry(win), (960, 0, 640, 480))

    # -- errors -------------------------------------------------------------

    def test_x_error_maps_to_exception(self):
        srv = self.serve()
        srv.intern("WM_CLASS")
        srv.error_windows.add(0xDEAD)
        conn = self.connect()
        with self.assertRaises(X11Error) as cm:
            conn.get_wm_class(0xDEAD)
        self.assertEqual(cm.exception.name, "BadWindow")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(cm.exception.bad_value, 0xDEAD)
        with self.assertRaises(X11Error):
            conn.get_geometry(0xDEAD)
        # the connection survives an error
        srv.set_prop(0x400001, "WM_CLASS", "STRING", 8, b"a\0B\0")
        self.assertEqual(conn.get_wm_class(0x400001), ("a", "B"))

    def test_use_after_close(self):
        self.serve()
        conn = self.connect()
        conn.close()
        conn.close()  # idempotent
        with self.assertRaises(XUnavailable):
            conn.atom("_NET_ANYTHING_NEW")

    # -- writes -------------------------------------------------------------

    def test_set_name_modes(self):
        srv = self.serve()
        win = 0x400001
        conn = self.connect()

        def changes():
            out = [e for e in srv.log if e[0] == "ChangeProperty"]
            del srv.log[:]
            return {e[2]: (e[3], e[4], e[5]) for e in out}

        conn.set_name(win, "wwx café", icon=False, long_=True)  # -N
        ch = changes()
        self.assertEqual(set(ch), {"WM_NAME", "_NET_WM_NAME"})
        self.assertEqual(ch["WM_NAME"], ("STRING", 8, b"wwx caf\xe9"))
        self.assertEqual(ch["_NET_WM_NAME"],
                         ("UTF8_STRING", 8, "wwx café".encode("utf-8")))
        conn.set_name(win, "ic", icon=True, long_=False)  # -I
        self.assertEqual(set(changes()),
                         {"WM_ICON_NAME", "_NET_WM_ICON_NAME"})
        conn.set_name(win, "both", icon=True, long_=True)  # -T
        self.assertEqual(set(changes()),
                         {"WM_NAME", "_NET_WM_NAME",
                          "WM_ICON_NAME", "_NET_WM_ICON_NAME"})

    def test_set_name_in_a_utf8_environment_deletes_the_legacy_property(self):
        """wwmctl-7: wmctrl's window_set_title has no locale copy of the
        title under envir_utf8 (a UTF-8 locale, or -u), so it deletes
        WM_NAME rather than leaving a lossy STRING behind."""
        srv = self.serve()
        win = 0x400001
        conn = self.connect()
        conn.set_name(win, "caf\u00e9 \u2713", icon=True, long_=True,
                      utf8=True)
        changed = {e[2]: (e[3], e[4], e[5]) for e in srv.log
                   if e[0] == "ChangeProperty"}
        deleted = [e[2] for e in srv.log if e[0] == "DeleteProperty"]
        self.assertEqual(set(changed), {"_NET_WM_NAME", "_NET_WM_ICON_NAME"})
        self.assertEqual(changed["_NET_WM_NAME"],
                         ("UTF8_STRING", 8, "caf\u00e9 \u2713".encode("utf-8")))
        self.assertEqual(sorted(deleted), ["WM_ICON_NAME", "WM_NAME"])

    def test_lookup_color(self):
        """wmctrl -M resolves XPM colour names with XParseColor, i.e. the
        server's own rgb.txt: LookupColor."""
        srv = self.serve()
        srv.colors["navy blue"] = (0x0000, 0x0000, 0x8080)
        conn = self.connect()
        self.assertEqual(conn.lookup_color("navy blue"),
                         (0x0000, 0x0000, 0x8080))
        self.assertIn(("LookupColor", 0, "navy blue"), srv.log)
        with self.assertRaises(X11Error):
            conn.lookup_color("nosuchcolour")

    def test_font_properties(self):
        """wxprop-9: XWayland really does serve the core fonts, so -font is
        implementable; OpenFont + QueryFont is all it needs."""
        srv = self.serve()
        srv.fonts["fixed"] = [("FOUNDRY", 0x59), ("PIXEL_SIZE", 13)]
        conn = self.connect()
        props = conn.font_properties("fixed")
        self.assertEqual(props, [(srv.intern("FOUNDRY"), 0x59),
                                 (srv.intern("PIXEL_SIZE"), 13)])
        opened = [e for e in srv.log if e[0] == "OpenFont"]
        self.assertEqual([e[2] for e in opened], ["fixed"])
        fid = opened[0][1]
        self.assertEqual(fid & ~0x3FFFFF, 0x400000)   # our id range
        self.assertIn(("CloseFont", fid), srv.log)    # released again
        with self.assertRaises(X11Error):             # BadName
            conn.font_properties("zzznosuch")

    def test_send_root_message_framing(self):
        srv = self.serve()
        conn = self.connect()
        win = 0x400001
        conn.send_root_message(win, "_NET_ACTIVE_WINDOW", [2, -1])
        sends = [e for e in srv.log if e[0] == "SendEvent"]
        self.assertEqual(len(sends), 1)
        _tag, dest, mask, event = sends[0]
        self.assertEqual(dest, FakeXServer.ROOTS[0])
        self.assertEqual(mask, 0x180000)  # SubstrNotify|SubstrRedirect
        etype, efmt, _seq, ewin, eatom = struct.unpack_from("<BBHII", event)
        self.assertEqual((etype, efmt, ewin), (33, 32, win))
        self.assertEqual(srv._names[eatom], "_NET_ACTIVE_WINDOW")
        self.assertEqual(struct.unpack_from("<5I", event, 12),
                         (2, 0xFFFFFFFF, 0, 0, 0))


# ---------------------------------------------------------------------------
# Live tests: real XWayland under headless sway.
# ---------------------------------------------------------------------------

XTERM_TITLE = "x11mini-xterm"


def _need(*tools):
    return all(shutil.which(t) for t in tools)


@unittest.skipUnless(
    _need("sway", "foot", "xterm", "xprop", "xwininfo"),
    "sway/foot/xterm/xprop/xwininfo not on PATH (run in nix develop)")
class X11LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway(
            "x11mini-live-",
            # so _NET_ACTIVE_WINDOW client messages really move focus
            extra_conf="focus_on_window_activation focus\n",
            extra_env=lambda rt: {
                "XAUTHORITY": os.path.join(rt, "no-such-authority")})
        cls.rtdir, cls.sock = cls.rig.rtdir, cls.rig.sock
        cls.display, cls.sway = cls.rig.display, cls.rig.proc
        cls.env = dict(cls.rig.env,
                       WAYLAND_DISPLAY=cls.rig.wayland_display())
        cls.kids = []
        cls.kids.append(subprocess.Popen(
            ["xterm", "-T", XTERM_TITLE, "-e", "sh", "-c", "sleep 600"],
            env=cls.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
        if not cls.wait_for(lambda: cls.xterm_node() is not None):
            cls.stop_all()
            raise unittest.SkipTest("xterm never appeared (XWayland broken?)")
        cls.kids.append(subprocess.Popen(
            ["foot", "--app-id", "footx", "--title", "x11mini-foot",
             "sh", "-c", "sleep 600"],
            env=cls.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
        if not cls.wait_for(lambda: cls.find_node(
                lambda n: n.get("app_id") == "footx") is not None):
            cls.stop_all()
            raise unittest.SkipTest("foot never appeared")
        node = cls.xterm_node()
        cls.xid = node["window"]
        cls.xpid = node["pid"]

    @classmethod
    def stop_all(cls):
        for p in getattr(cls, "kids", []):
            try:
                p.terminate()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        cls.rig.stop()

    @classmethod
    def tearDownClass(cls):
        cls.stop_all()

    # -- helpers ------------------------------------------------------------

    @classmethod
    def wait_for(cls, fn, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if fn():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    @classmethod
    def tree(cls):
        out = subprocess.run(["swaymsg", "-s", cls.sock, "-t", "get_tree"],
                             env=cls.env, capture_output=True, timeout=10,
                             check=True).stdout
        return json.loads(out)

    @classmethod
    def find_node(cls, pred):
        def walk(n):
            if pred(n):
                return n
            for c in n.get("nodes", []) + n.get("floating_nodes", []):
                if (r := walk(c)) is not None:
                    return r
            return None
        return walk(cls.tree())

    @classmethod
    def xterm_node(cls):
        return cls.find_node(lambda n: n.get("shell") == "xwayland"
                             and n.get("window"))

    @classmethod
    def swaymsg(cls, cmd):
        subprocess.run(["swaymsg", "-s", cls.sock, cmd], env=cls.env,
                       capture_output=True, timeout=10, check=True)

    def conn(self) -> X11Conn:
        with mock.patch.dict(os.environ, {"XAUTHORITY":
                                          self.env["XAUTHORITY"]}):
            c = X11Conn(self.display)
        self.addCleanup(c.close)
        return c

    def xprop(self, *args):
        return subprocess.run(["xprop", *args], env=self.env,
                              capture_output=True, text=True, timeout=10,
                              check=True).stdout

    def xprop_strings(self, target, prop):
        out = self.xprop("-id", "0x%x" % target, prop)
        return re.findall(r'"((?:[^"\\]|\\.)*)"', out)

    # -- tests --------------------------------------------------------------

    def test_01_client_list_matches_xprop_and_tree(self):
        c = self.conn()
        ids = c.client_list()
        self.assertIn(self.xid, ids)
        root_out = self.xprop("-root", "_NET_CLIENT_LIST")
        oracle = [int(h, 16) for h in re.findall(r"0x[0-9a-f]+", root_out)]
        self.assertEqual(ids, oracle)

    def test_02_wm_class_byte_parity(self):
        c = self.conn()
        inst, klass = c.get_wm_class(self.xid)
        self.assertEqual((inst, klass), ("xterm", "XTerm"))
        self.assertEqual(self.xprop_strings(self.xid, "WM_CLASS"),
                         [inst, klass])
        wp = self.xterm_node()["window_properties"]
        self.assertEqual((wp["instance"], wp["class"]), (inst, klass))

    def test_03_pid_and_client_machine(self):
        c = self.conn()
        self.assertEqual(c.get_pid(self.xid), self.xpid)
        out = self.xprop("-id", "0x%x" % self.xid, "_NET_WM_PID")
        self.assertEqual(int(out.split("=")[1]), c.get_pid(self.xid))
        machine = c.get_client_machine(self.xid)
        self.assertTrue(machine)
        self.assertEqual(self.xprop_strings(self.xid, "WM_CLIENT_MACHINE"),
                         [machine])

    def test_04_title(self):
        c = self.conn()
        self.assertEqual(c.get_prop_string(self.xid, "WM_NAME"), XTERM_TITLE)
        self.assertEqual(self.xprop_strings(self.xid, "WM_NAME"),
                         [XTERM_TITLE])
        # xterm is an old Xt app: it sets no _NET_WM_NAME, so the UTF8 read
        # degrades to "" (wmctrl falls back to WM_NAME the same way)
        self.assertIn(c.get_prop_string(self.xid, "_NET_WM_NAME"),
                      ("", XTERM_TITLE))
        self.assertEqual(self.xterm_node()["name"], XTERM_TITLE)

    def test_05_geometry_matches_xwininfo_and_tree(self):
        # resize BEFORE move: sway resizes floating windows around their
        # center, which would shift a position set beforehand
        self.swaymsg('[title="%s"] floating enable, resize set 500 350, '
                     'move position 200 100' % XTERM_TITLE)
        try:
            self.assertTrue(self.wait_for(
                lambda: self.xterm_node()["rect"]["x"] == 200
                and self.xterm_node()["rect"]["width"] == 500))
            rect = self.xterm_node()["rect"]
            c = self.conn()
            geo = c.get_geometry(self.xid)
            self.assertEqual(geo, (rect["x"], rect["y"],
                                   rect["width"], rect["height"]))
            out = subprocess.run(["xwininfo", "-id", "0x%x" % self.xid],
                                 env=self.env, capture_output=True,
                                 text=True, timeout=10, check=True).stdout
            stats = {k: int(v) for k, v in re.findall(
                r"(Absolute upper-left X|Absolute upper-left Y|Width|Height)"
                r":\s+(-?\d+)", out)}
            self.assertEqual(geo, (stats["Absolute upper-left X"],
                                   stats["Absolute upper-left Y"],
                                   stats["Width"], stats["Height"]))
        finally:
            self.swaymsg('[title="%s"] floating disable' % XTERM_TITLE)

    def test_06_set_name(self):
        c = self.conn()
        try:
            c.set_name(self.xid, "x11mini renamed", icon=False, long_=True)
            self.assertEqual(
                self.xprop_strings(self.xid, "_NET_WM_NAME"),
                ["x11mini renamed"])
            self.assertEqual(
                self.xprop_strings(self.xid, "WM_NAME"),
                ["x11mini renamed"])
            # ...and the compositor tree tracks the X-side rename
            self.assertTrue(self.wait_for(
                lambda: self.xterm_node()["name"] == "x11mini renamed"))
            c.set_name(self.xid, "iconic", icon=True, long_=False)
            self.assertEqual(
                self.xprop_strings(self.xid, "_NET_WM_ICON_NAME"),
                ["iconic"])
            self.assertEqual(
                self.xprop_strings(self.xid, "WM_ICON_NAME"), ["iconic"])
            # icon-only must not touch the window name
            self.assertEqual(
                self.xprop_strings(self.xid, "WM_NAME"),
                ["x11mini renamed"])
        finally:
            c.set_name(self.xid, XTERM_TITLE, icon=True, long_=True)
            self.wait_for(lambda: self.xterm_node()["name"] == XTERM_TITLE)

    def test_07_net_active_window_focuses(self):
        self.swaymsg("[app_id=footx] focus")
        self.assertTrue(self.wait_for(
            lambda: not self.xterm_node()["focused"]))
        c = self.conn()
        # what wmctrl -a sends (all-zero data); sway is configured with
        # focus_on_window_activation focus, so the request must move focus
        c.send_root_message(self.xid, "_NET_ACTIVE_WINDOW", [0, 0, 0, 0, 0])
        self.assertTrue(self.wait_for(
            lambda: self.xterm_node()["focused"]),
            "sway did not focus the xterm on _NET_ACTIVE_WINDOW")

    def test_08_net_wm_state_fullscreen(self):
        c = self.conn()
        fs = c.atom("_NET_WM_STATE_FULLSCREEN")
        c.send_root_message(self.xid, "_NET_WM_STATE", [1, fs, 0, 0, 0])
        self.assertTrue(self.wait_for(
            lambda: self.xterm_node()["fullscreen_mode"] == 1),
            "fullscreen add was not honored")
        c.send_root_message(self.xid, "_NET_WM_STATE", [0, fs, 0, 0, 0])
        self.assertTrue(self.wait_for(
            lambda: self.xterm_node()["fullscreen_mode"] == 0),
            "fullscreen remove was not honored")

    def test_09_missing_props_and_errors(self):
        c = self.conn()
        self.assertEqual(c.get_prop_ints(self.xid, "_NET_X11MINI_NOPE"), [])
        self.assertEqual(c.get_prop_string(self.xid, "_NET_X11MINI_NOPE"),
                         "")
        bogus = 0x7654321
        with self.assertRaises(X11Error) as cm:
            c.get_wm_class(bogus)  # GetProperty on a dead id: BadWindow
        self.assertEqual(cm.exception.name, "BadWindow")
        with self.assertRaises(X11Error):
            c.get_geometry(bogus)  # BadDrawable on real servers
        # connection still fine afterwards
        self.assertEqual(c.get_wm_class(self.xid), ("xterm", "XTerm"))

    def test_10_display_variants_and_wm_check(self):
        num = self.display.lstrip(":").split(".")[0]
        with mock.patch.dict(os.environ,
                             {"XAUTHORITY": self.env["XAUTHORITY"]}):
            for dpy in (":%s" % num, ":%s.0" % num, "unix:%s" % num):
                c = X11Conn(dpy)
                try:
                    self.assertGreater(c.root(), 0, dpy)
                finally:
                    c.close()
        # wlroots' wm-check window: root property points at a window whose
        # _NET_WM_NAME is the WM name (what wmctrl -m prints)
        c = self.conn()
        sup = c.get_prop_ints(c.root(), "_NET_SUPPORTING_WM_CHECK")
        self.assertEqual(len(sup), 1)
        self.assertTrue(c.get_prop_string(sup[0], "_NET_WM_NAME"))

    def test_11_xunavailable_on_dead_display(self):
        with self.assertRaises(XUnavailable):
            X11Conn(":63")

    @unittest.skipUnless(shutil.which("xeyes"), "xeyes not on PATH")
    def test_12_xeyes_degraded_getters(self):
        # xeyes sets WM_CLASS but no _NET_WM_PID (prep notes §5): the pid
        # getter must degrade to 0, and _NET_CLIENT_LIST must carry both
        p = subprocess.Popen(["xeyes"], env=self.env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(self.wait_for(lambda: self.find_node(
                lambda n: n.get("shell") == "xwayland"
                and n.get("window") not in (None, self.xid)) is not None))
            eyes = self.find_node(
                lambda n: n.get("shell") == "xwayland"
                and n.get("window") not in (None, self.xid))["window"]
            c = self.conn()
            self.assertEqual(c.get_wm_class(eyes), ("xeyes", "XEyes"))
            self.assertEqual(c.get_pid(eyes), 0)
            self.assertEqual(sorted(c.client_list()),
                             sorted([self.xid, eyes]))
        finally:
            p.terminate()
            p.wait(timeout=5)
            self.wait_for(lambda: self.find_node(
                lambda n: n.get("window") not in (None, self.xid)) is None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
