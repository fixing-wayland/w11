#!/usr/bin/env python3
"""live tests against a real headless sway with XWayland.

xterm (X plane, via XWayland) and foot (native Wayland) run side by side.
The X-plane contract is BYTE parity: for the same window state, wxprop's
stdout/stderr/exit code must equal real xprop 1.2.8's, and every parity
test literally runs both binaries and diffs. The native plane is checked
against the synthesized-property contract in docs/WXPROP.md. -set/-remove are
cross-verified (wxprop writes, real xprop reads back, and vice versa),
and -spy transcripts are compared byte for byte.

Skipped cleanly when sway/foot/xterm/xprop are not on PATH (run inside
`nix develop`). Starts its own sway on a private XDG_RUNTIME_DIR, so it
never collides with concurrent sessions."""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import HeadlessSway

XTERM_TITLE = "WXL-Xterm"
FOOT_TITLE = "WXL-Foot"


def _normalize_serials(text: str) -> str:
    return re.sub(r"(Serial number of failed request:|Current serial number"
                  r" in output stream:)\s+\d+", r"\1 N", text)


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (nix develop)")
@unittest.skipUnless(shutil.which("foot"), "foot not on PATH (nix develop)")
@unittest.skipUnless(shutil.which("xterm"), "xterm not on PATH")
@unittest.skipUnless(shutil.which("xprop"), "oracle xprop not on PATH")
class WxpropLiveTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway("wxprop-live-")
        cls.rtdir, cls.sock = cls.rig.rtdir, cls.rig.sock
        cls.display, cls.sway = cls.rig.display, cls.rig.proc
        cls.swaymsg_cls("exec xterm -T %s -e sh -c 'sleep 600'"
                        % XTERM_TITLE)
        if not cls.wait(lambda: cls.view(name=XTERM_TITLE) is not None):
            cls.rig.stop()
            raise unittest.SkipTest("xterm never appeared (XWayland?)")
        cls.swaymsg_cls("exec foot --app-id footw --title %s "
                        "sh -c 'sleep 600'" % FOOT_TITLE)
        if not cls.wait(lambda: cls.view(app_id="footw") is not None):
            cls.rig.stop()
            raise unittest.SkipTest("foot window never appeared")
        cls.xterm_id = cls.view(name=XTERM_TITLE)["window"]
        cls.xterm_hex = "0x%x" % cls.xterm_id
        cls.foot_node = cls.view(app_id="footw")["id"]
        # pin the window state: focus foot so the xterm's _NET_WM_STATE
        # stays (MAXIMIZED_VERT, MAXIMIZED_HORZ) for every parity diff
        cls.swaymsg_cls("[app_id=footw] focus")
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        cls.rig.stop()

    # -- helpers ------------------------------------------------------------

    @classmethod
    def _env(cls, nox=False):
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            WDOTOOL_BACKEND="sway",
            SWAYSOCK=cls.sock,
            WAYLAND_DISPLAY="wayland-1",
            DISPLAY=cls.display,
            WXPROP_ARGV0="xprop",  # byte parity for program_name in errors
            LC_ALL="C",
        )
        env.pop("COLORTERM", None)
        env.pop("XPROPFORMATS", None)
        if nox:
            env["WXPROP_NO_X"] = "1"
        else:
            env.pop("WXPROP_NO_X", None)
        return env

    @classmethod
    def wx(cls, *args, nox=False, timeout=30):
        p = subprocess.run(
            [sys.executable, "-m", "wxprop", *args],
            env=cls._env(nox=nox), capture_output=True, text=True,
            cwd=ROOT, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr

    @classmethod
    def xp(cls, *args, timeout=30):
        p = subprocess.run(
            ["xprop", *args], env=cls._env(), capture_output=True,
            text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr

    def parity(self, *args, retries=3):
        """Run oracle and clone; assert identical (rc, stdout, stderr).
        Retried a couple of times in case compositor-owned state (focus,
        _NET_WM_STATE) shifted between the two invocations."""
        last = None
        for _ in range(retries):
            o = self.xp(*args)
            w = self.wx(*args)
            if o == w:
                return o
            last = (o, w)
            time.sleep(0.3)
        self.assertEqual(last[1], last[0], "args=%r" % (args,))

    @classmethod
    def swaymsg_cls(cls, cmd):
        subprocess.run(
            ["swaymsg", "-s", cls.sock, cmd],
            env=dict(os.environ, XDG_RUNTIME_DIR=cls.rtdir),
            capture_output=True, timeout=10, check=True,
        )

    def swaymsg(self, cmd):
        self.swaymsg_cls(cmd)

    @classmethod
    def view(cls, **match):
        p = subprocess.run(
            ["swaymsg", "-s", cls.sock, "-t", "get_tree"],
            env=dict(os.environ, XDG_RUNTIME_DIR=cls.rtdir),
            capture_output=True, timeout=10, check=True,
        )
        tree = json.loads(p.stdout)
        out = []

        def walk(node):
            if node.get("pid") and (node.get("app_id") is not None
                                    or node.get("window_properties")):
                out.append(node)
            for ch in (node.get("nodes") or []) + \
                    (node.get("floating_nodes") or []):
                walk(ch)

        walk(tree)
        for v in out:
            if all(v.get(k) == want for k, want in match.items()):
                return v
        return None

    @classmethod
    def wait(cls, pred, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def x11conn(self):
        from hacks.window import x11_mini
        old_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = self.display
        try:
            conn = x11_mini.X11Conn(self.display)
        finally:
            if old_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = old_display
        self.addCleanup(conn.close)
        return conn

    # -- X plane: byte parity ------------------------------------------------

    def test_01_default_dump_parity(self):
        rc, out, _ = self.parity("-id", self.xterm_hex)
        self.assertEqual(rc, 0)
        self.assertIn('WM_CLASS(STRING) = "xterm", "XTerm"\n', out)
        self.assertIn("WM_HINTS(WM_HINTS):\n", out)
        self.assertIn("WM_NORMAL_HINTS(WM_SIZE_HINTS):\n", out)

    def test_02_option_battery_parity(self):
        batteries = (
            ("-notype", "-id", self.xterm_hex),
            ("-id", self.xterm_hex, "-len", "8"),
            ("-id", self.xterm_hex, "-len", "0"),
            ("-id", self.xterm_hex, "-notype", "-len", "3", "WM_CLASS"),
            ("-id", self.xterm_hex, "-len", "12", "_NET_WM_STATE"),
            ("-id", self.xterm_hex, "WM_CLASS"),
            ("-id", self.xterm_hex, "WM_CLASS", "WM_NAME", "BOGUSPROP"),
            ("-id", self.xterm_hex, "-f", "WM_CLASS", "32x", "WM_CLASS"),
            ("-id", self.xterm_hex, "8s", " instance=$0 class=$1\\n",
             "WM_CLASS"),
            ("-id", self.xterm_hex, "-f", "WM_NAME", "0s", "$1\\n",
             "WM_NAME"),
            ("-id", str(self.xterm_id), "WM_CLASS"),  # decimal id
            ("-frame", "-id", self.xterm_hex, "WM_CLASS"),
        )
        for args in batteries:
            with self.subTest(args=args):
                self.parity(*args)

    def test_03_root_parity(self):
        rc, out, _ = self.parity("-root")
        self.assertEqual(rc, 0)
        self.assertIn("_NET_SUPPORTING_WM_CHECK(WINDOW): window id # ", out)
        self.parity("-root", "_NET_SUPPORTING_WM_CHECK")

    def test_04_name_selection_parity(self):
        self.parity("-name", XTERM_TITLE, "WM_CLASS")
        rc, _out, err = self.parity("-name", "zz-no-such-window-zz")
        self.assertEqual(rc, 1)
        self.assertEqual(
            err, "xprop: error: No window with name zz-no-such-window-zz "
                 "exists!\n")

    def test_05_set_remove_cross_verified(self):
        xid = self.xterm_hex
        # wxprop sets, the real xprop reads back
        rc, _o, err = self.wx("-id", xid, "-f", "MY_TEST", "8s",
                              "-set", "MY_TEST", "hello")
        self.assertEqual((rc, err), (0, ""))
        rc, out, _e = self.xp("-id", xid, "MY_TEST")
        self.assertEqual(out, 'MY_TEST(STRING) = "hello"\n')
        self.parity("-id", xid, "MY_TEST")
        # wxprop removes, both agree it is gone
        rc, _o, err = self.wx("-id", xid, "-remove", "MY_TEST")
        self.assertEqual((rc, err), (0, ""))
        self.parity("-id", xid, "MY_TEST")  # ":  not found."
        # the real xprop sets, wxprop reads back
        rc, _o, _e = self.xp("-id", xid, "-f", "ORC_SET", "8s",
                             "-set", "ORC_SET", "oracle")
        self.assertEqual(rc, 0)
        rc, out, _e = self.wx("-id", xid, "ORC_SET")
        self.assertEqual(out, 'ORC_SET(STRING) = "oracle"\n')
        self.xp("-id", xid, "-remove", "ORC_SET")
        # conversions: u, a, c round-trips + parity of the verify
        for fmt, name, value in (("8u", "MY_UTF8", "héllo wörld"),
                                 ("32a", "MY_ATOMP", "SOMEATOM"),
                                 ("32c", "MY_NUM", "42,7"),
                                 ("32i", "MY_INT", "42"),
                                 ("32b", "MY_BOOL", "True")):
            with self.subTest(name=name):
                rc, _o, err = self.wx("-id", xid, "-f", name, fmt,
                                      "-set", name, value)
                self.assertEqual((rc, err), (0, ""), name)
                self.parity("-id", xid, name)
                self.wx("-id", xid, "-remove", name)

    def test_06_set_remove_error_parity(self):
        xid = self.xterm_hex
        self.parity("-id", xid, "-set", "ANOTHER_PROP", "xyz")  # exit 1
        self.parity("-id", xid, "-remove", "NOSUCHPROP_QQ")     # 2 spaces
        # the -set token-swallow bug (xprop.c:2052), verified live
        self.parity("-id", xid, "-set", "SWALLOW_P", "v", "WM_CLASS")
        self.parity("-id", xid, "-remove", "NOX", "WM_CLASS")   # usage

    def test_07_icon_ascii_art_parity(self):
        conn = self.x11conn()
        vals = [8, 8]
        for _y in range(8):
            for x in range(8):
                if x == 0:
                    px = 0x00000000
                elif x == 1:
                    px = 0x40FF0000
                elif x == 2:
                    px = 0x80808080
                else:
                    level = (x * 255) // 7
                    px = 0xFF000000 | (level << 16) | (level << 8) | level
                vals.append(px)
        conn.change_property(self.xterm_id, "_NET_WM_ICON", "CARDINAL", 32,
                             struct.pack("<%dI" % len(vals), *vals))
        try:
            rc, out, _e = self.parity("-id", self.xterm_hex, "_NET_WM_ICON")
            self.assertEqual(rc, 0)
            self.assertIn("\tIcon (8 x 8):\n", out)
            self.assertIn("\t  ++lltt]]??--  \n", out)
            # too-wide icon: the "(not shown)" path
            big = [90, 2] + [0xFF204060] * 180
            conn.change_property(
                self.xterm_id, "_NET_WM_ICON", "CARDINAL", 32,
                struct.pack("<%dI" % len(big), *big))
            rc, out, _e = self.parity("-id", self.xterm_hex, "_NET_WM_ICON")
            self.assertIn("\t(not shown)\n", out)
        finally:
            conn.delete_property(self.xterm_id, "_NET_WM_ICON")

    def test_08_text_encoding_parity(self):
        conn = self.x11conn()
        conn.change_property(self.xterm_id, "T_NUL", "STRING", 8,
                             b"foo\0bar")
        conn.change_property(self.xterm_id, "T_BADU", "UTF8_STRING", 8,
                             b"a\xc3")
        conn.change_property(self.xterm_id, "T_CTRL", "STRING", 8,
                             b'a"b\nc\xe9')
        try:
            self.parity("-id", self.xterm_hex, "-f", "T_NUL", "8t",
                        "T_NUL", "T_BADU", "T_CTRL")
        finally:
            for p in ("T_NUL", "T_BADU", "T_CTRL"):
                conn.delete_property(self.xterm_id, p)

    def test_09_bad_window_error_block(self):
        o = self.xp("-id", "0x999999")
        w = self.wx("-id", "0x999999")
        self.assertEqual(o[0], 1)
        self.assertEqual(w[0], 1)
        self.assertEqual(_normalize_serials(w[2]),
                         _normalize_serials(o[2]))
        self.assertTrue(o[2].startswith(
            "X Error of failed request:  BadWindow "
            "(invalid Window parameter)\n"))

    def test_10_spy_parity(self):
        env = self._env()
        op = subprocess.Popen(["xprop", "-spy", "-id", self.xterm_hex],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        wp = subprocess.Popen(
            [sys.executable, "-m", "wxprop", "-spy", "-id", self.xterm_hex],
            env=env, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            time.sleep(1.5)
            conn = self.x11conn()
            conn.set_name(self.xterm_id, "spy-rename-one", icon=False,
                          long_=True)
            time.sleep(0.6)
            conn.set_name(self.xterm_id, "spy-rename-two", icon=False,
                          long_=True)
            time.sleep(1.5)
        finally:
            for p in (op, wp):
                p.terminate()
            oout, oerr = op.communicate(timeout=10)
            wout, werr = wp.communicate(timeout=10)
            conn.set_name(self.xterm_id, XTERM_TITLE, icon=False,
                          long_=True)
            time.sleep(0.3)
        self.assertEqual(wout, oout)
        self.assertEqual(werr, oerr)
        self.assertIn(b'WM_NAME(STRING) = "spy-rename-two"\n', wout)
        self.assertIn(b'_NET_WM_NAME(UTF8_STRING) = "spy-rename-two"\n',
                      wout)

    def test_11_spy_exits_zero_on_destroy(self):
        self.swaymsg("exec xterm -T WXL-Victim -e sh -c 'sleep 600'")
        self.assertTrue(self.wait(
            lambda: self.view(name="WXL-Victim") is not None))
        vid = "0x%x" % self.view(name="WXL-Victim")["window"]
        env = self._env()
        op = subprocess.Popen(["xprop", "-spy", "-id", vid], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        wp = subprocess.Popen(
            [sys.executable, "-m", "wxprop", "-spy", "-id", vid],
            env=env, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        time.sleep(1.5)
        self.swaymsg("[title=WXL-Victim] kill")
        orc = op.wait(timeout=15)
        wrc = wp.wait(timeout=15)
        oout, _ = op.communicate()
        wout, _ = wp.communicate()
        self.assertEqual((wrc, orc), (0, 0))
        self.assertEqual(wout, oout)

    def test_12_x_id_interop_with_wwmctl(self):
        # the id wwmctl prints is directly usable by wxprop, and the output
        # satisfies xprop-parsing scripts
        rc, out, _e = self.wx("-id", self.xterm_hex, "WM_CLASS")
        self.assertEqual(rc, 0)
        m = re.match(r'WM_CLASS\(STRING\) = "(.*)", "(.*)"\n', out)
        self.assertEqual((m.group(1), m.group(2)), ("xterm", "XTerm"))

    # -- native plane ---------------------------------------------------------

    def test_20_native_dump(self):
        rc, out, err = self.wx("-id", str(self.foot_node))
        self.assertEqual((rc, err), (0, ""))
        lines = out.splitlines()
        self.assertIn('WM_CLASS(STRING) = "footw", "footw"', lines)
        self.assertIn('WM_NAME(STRING) = "%s"' % FOOT_TITLE, lines)
        self.assertIn('_NET_WM_NAME(UTF8_STRING) = "%s"' % FOOT_TITLE,
                      lines)
        self.assertIn("_NET_WM_WINDOW_TYPE(ATOM) = "
                      "_NET_WM_WINDOW_TYPE_NORMAL", lines)
        self.assertIn("_NET_WM_DESKTOP(CARDINAL) = 0", lines)
        pid = self.view(app_id="footw")["pid"]
        self.assertIn("_NET_WM_PID(CARDINAL) = %d" % pid, lines)

    def test_21_native_script_parsing(self):
        # `wxprop -id <node> WM_CLASS` must satisfy xprop-parsing scripts
        rc, out, _e = self.wx("-id", str(self.foot_node), "WM_CLASS")
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'WM_CLASS(STRING) = "footw", "footw"\n')
        rc, out, _e = self.wx("-notype", "-len", "3", "-id",
                              str(self.foot_node), "WM_CLASS")
        self.assertEqual(out, 'WM_CLASS = "foo"\n')
        rc, out, _e = self.wx("-id", str(self.foot_node), "8s",
                              " i=$0\\n", "WM_CLASS")
        self.assertEqual(out, 'WM_CLASS(STRING) i="footw"\n')

    def test_22_native_set_remove_errors(self):
        rc, out, err = self.wx("-id", str(self.foot_node), "-set", "A", "b")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(
            err, "xprop: error: -set cannot work on a native Wayland "
                 "window (it has no X property store)\n")
        rc, _o, err = self.wx("-id", str(self.foot_node), "-remove", "A")
        self.assertEqual(rc, 1)
        self.assertIn("-remove cannot work on a native Wayland window",
                      err)

    def test_23_native_fullscreen_state(self):
        self.swaymsg("[app_id=footw] fullscreen enable")
        try:
            self.assertTrue(self.wait(
                lambda: self.view(app_id="footw")["fullscreen_mode"] == 1))
            rc, out, _e = self.wx("-id", str(self.foot_node),
                                  "_NET_WM_STATE")
            self.assertEqual(
                out,
                "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n")
        finally:
            self.swaymsg("[app_id=footw] fullscreen disable")

    def test_24_native_spy(self):
        wp = subprocess.Popen(
            [sys.executable, "-m", "wxprop", "-spy", "-id",
             str(self.foot_node), "_NET_WM_STATE"],
            env=self._env(), cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            time.sleep(1.5)
            self.swaymsg("[app_id=footw] fullscreen enable")
            time.sleep(0.6)
            self.swaymsg("[app_id=footw] fullscreen disable")
            time.sleep(1.0)
        finally:
            wp.terminate()
            out, err = wp.communicate(timeout=10)
        self.assertEqual(err, b"")
        self.assertEqual(
            out,
            b"_NET_WM_STATE(ATOM) = \n"
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n"
            b"_NET_WM_STATE(ATOM) = \n")

    def test_25_native_spy_exits_on_close(self):
        self.swaymsg("exec foot --app-id victimw --title WXL-FVictim "
                     "sh -c 'sleep 600'")
        self.assertTrue(self.wait(
            lambda: self.view(app_id="victimw") is not None))
        node = self.view(app_id="victimw")["id"]
        wp = subprocess.Popen(
            [sys.executable, "-m", "wxprop", "-spy", "-id", str(node)],
            env=self._env(), cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        time.sleep(1.5)
        self.swaymsg("[app_id=victimw] kill")
        rc = wp.wait(timeout=15)
        wp.communicate()
        self.assertEqual(rc, 0)

    def test_26_native_root_without_x(self):
        rc, out, err = self.wx("-root", nox=True)
        self.assertEqual((rc, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("_NET_SUPPORTED(ATOM) = "))
        self.assertTrue(any(ln.startswith(
            "_NET_CLIENT_LIST(WINDOW): window id # ") for ln in lines))
        self.assertIn("_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 1", lines)

    def test_27_xwayland_node_id_resolves_to_x_plane(self):
        # addressing the xterm by its NODE id reads the real X properties
        node = self.view(name=XTERM_TITLE)["id"]
        rc, out, _e = self.wx("-id", str(node), "WM_CLASS")
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_28_click_select_next_focus(self):
        self.swaymsg("[app_id=footw] focus")
        time.sleep(0.3)
        t = threading.Timer(
            1.2, lambda: self.swaymsg("[class=XTerm] focus"))
        t.start()
        try:
            rc, out, err = self.wx("WM_CLASS", timeout=20)
        finally:
            t.cancel()
            self.swaymsg_cls("[app_id=footw] focus")
        self.assertEqual(rc, 0, err)
        self.assertIn("xprop: focus the target window to select it\n", err)
        self.assertEqual(out, 'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_29_name_matches_native_when_x_cannot(self):
        rc, out, _e = self.wx("-name", FOOT_TITLE, "WM_CLASS")
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'WM_CLASS(STRING) = "footw", "footw"\n')


if __name__ == "__main__":
    unittest.main(verbosity=2)
