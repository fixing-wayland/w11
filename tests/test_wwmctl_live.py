#!/usr/bin/env python3
"""wwmctl live tests against a real headless sway with XWayland.

foot (native Wayland) and xterm (legacy X via XWayland) run side by side;
wwmctl must list BOTH planes with correct classes/ids/geometry, the real
wmctrl binary is the byte-parity oracle for the X rows, and actions
(-a/-c/-e/-b/-t/-R/-s) are verified through swaymsg. Every listing assertion
runs in both modes: X-enriched (when wdotool/x11_mini.py is implemented and
the X server is reachable) and compositor-only (WWMCTL_NO_X=1).

Skipped when sway/foot/xterm are not on PATH (run inside `nix develop`).
Starts its own sway on a private XDG_RUNTIME_DIR."""

import os
import shutil
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

XTERM_TITLE = "XW-Xterm"
FOOT_TITLE = "WL-Foot"


def _x11_mini_implemented() -> bool:
    try:
        from wdotool import x11_mini
        return hasattr(x11_mini, "X11Conn")
    except ImportError:
        return False


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (nix develop)")
@unittest.skipUnless(shutil.which("foot"), "foot not on PATH (nix develop)")
@unittest.skipUnless(shutil.which("xterm"), "xterm not on PATH (nix develop)")
class WwmctlLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway("wwmctl-live-")
        cls.rtdir, cls.sock = cls.rig.rtdir, cls.rig.sock
        cls.display, cls.sway = cls.rig.display, cls.rig.proc
        # xterm FIRST so it tiles at x=0 (keeps the oracle geometry
        # comparison out of wmctrl's coordinate-doubling quirk under
        # non-reparenting X window managers, see test_04), foot second.
        cls.swaymsg_cls("exec xterm -T %s -e sh -c 'sleep 600'" % XTERM_TITLE)
        if not cls.wait(lambda: XTERM_TITLE in cls.wwm("-l")[1]):
            cls.rig.stop()
            raise unittest.SkipTest("xterm window never appeared "
                                    "(XWayland broken?)")
        cls.swaymsg_cls("exec foot --app-id footw --title %s "
                        "sh -c 'sleep 600'" % FOOT_TITLE)
        if not cls.wait(lambda: FOOT_TITLE in cls.wwm("-l")[1]):
            cls.rig.stop()
            raise unittest.SkipTest("foot window never appeared")

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
        )
        if nox:
            env["WWMCTL_NO_X"] = "1"
        else:
            env.pop("WWMCTL_NO_X", None)
        return env

    @classmethod
    def wwm(cls, *args, nox=False):
        p = subprocess.run(
            [sys.executable, "-m", "wwmctl", *args],
            env=cls._env(nox=nox), capture_output=True, text=True,
            cwd=ROOT, timeout=30,
        )
        return p.returncode, p.stdout, p.stderr

    @classmethod
    def oracle(cls, *args):
        p = subprocess.run(
            ["wmctrl", *args], env=cls._env(), capture_output=True,
            text=True, timeout=30,
        )
        return p.returncode, p.stdout, p.stderr

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
    def tree(cls):
        p = subprocess.run(
            ["swaymsg", "-s", cls.sock, "-t", "get_tree"],
            env=dict(os.environ, XDG_RUNTIME_DIR=cls.rtdir),
            capture_output=True, timeout=10, check=True,
        )
        import json
        return json.loads(p.stdout)

    @classmethod
    def views(cls):
        out = []

        def walk(node):
            if node.get("pid") and (node.get("app_id") is not None
                                    or node.get("window_properties")):
                out.append(node)
            for ch in (node.get("nodes") or []) + \
                    (node.get("floating_nodes") or []):
                walk(ch)

        walk(cls.tree())
        return out

    @classmethod
    def view(cls, **match):
        for v in cls.views():
            if all(v.get(k) == want for k, want in match.items()):
                return v
        return None

    @classmethod
    def focused_name(cls):
        for v in cls.views():
            if v.get("focused"):
                return v.get("name")
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

    def rows(self, *args, nox=False):
        rc, out, err = self.wwm(*args, nox=nox)
        self.assertEqual(rc, 0, err)
        return out.splitlines()

    def xterm_row(self, listing_args, nox=False):
        rows = [r for r in self.rows(*listing_args, nox=nox)
                if XTERM_TITLE in r]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def foot_row(self, listing_args, nox=False):
        rows = [r for r in self.rows(*listing_args, nox=nox)
                if FOOT_TITLE in r]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    # -- listings: both planes, both modes ----------------------------------

    def test_01_lists_both_planes(self):
        for nox in (False, True):
            rows = self.rows("-lGpx", nox=nox)
            self.assertEqual(len(rows), 2, rows)
            xrow = self.xterm_row(["-lGpx"], nox=nox)
            frow = self.foot_row(["-lGpx"], nox=nox)
            self.assertIn("xterm.XTerm", xrow)
            self.assertIn("footw.footw", frow)
            # XWayland row carries the REAL X11 id; native row the node id
            self.assertGreaterEqual(int(xrow.split()[0], 16), 0x400000)
            self.assertLess(int(frow.split()[0], 16), 0x400000)
            # pids are real
            for row in (xrow, frow):
                pid = int(row.split()[2])
                self.assertTrue(os.path.exists("/proc/%d" % pid), row)
            # geometry: two tiles side by side on the 1280x720 output, read through `--true-geometry`.
            # Plain `-lG` is the ORIGINAL's arithmetic and doubles the origin: sway's xwm does not
            # reparent (`xwininfo -tree` on a headless sway 1.11 on this guest, 2026-09-12, answered
            # `Parent window id: 0x234 (the root window)`) and a native row is only ever reachable by
            # the original through xw11, whose shadows are root children too.  The claim here is about
            # where sway put the two windows, so the flag that answers that is the one to read.
            xg = self.xterm_row(["--true-geometry", "-lGpx"], nox=nox).split()[3:7]
            fg = self.foot_row(["--true-geometry", "-lGpx"], nox=nox).split()[3:7]
            self.assertEqual(xg, ["0", "0", "640", "720"], xrow)
            self.assertEqual(fg, ["640", "0", "640", "720"], frow)
            # ...and the plain column really is the doubled one for the window that is not at the origin.
            self.assertEqual(frow.split()[3:7], ["1280", "0", "640", "720"], frow)

    @unittest.skipUnless(shutil.which("wmctrl"), "oracle wmctrl not on PATH")
    def test_02_oracle_agrees_on_x_rows(self):
        # The real wmctrl sees only the X plane (the xterm); its rows for
        # that window must match ours byte for byte — in X-enriched mode AND
        # compositor-only mode. (The window sits at 0,0 so the oracle's
        # XTranslateCoordinates position-doubling quirk under wlroots'
        # non-reparenting xwm cannot skew the -lG comparison.)
        for flags in ("-l", "-lp", "-lG", "-lx", "-lGpx"):
            rc, out, err = self.oracle(flags)
            self.assertEqual(rc, 0, err)
            oracle_rows = [r for r in out.splitlines() if XTERM_TITLE in r]
            self.assertEqual(len(oracle_rows), 1, out)
            for nox in (False, True):
                ours = self.xterm_row([flags], nox=nox)
                self.assertEqual(ours, oracle_rows[0],
                                 "flags=%s nox=%s" % (flags, nox))

    def test_03_x_id_interop(self):
        # the printed X id is usable by other X tools
        xid = self.xterm_row(["-l"]).split()[0]
        if shutil.which("xprop"):
            p = subprocess.run(["xprop", "-id", xid, "WM_CLASS"],
                               env=self._env(), capture_output=True,
                               text=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('"xterm", "XTerm"', p.stdout)

    def test_04_desktops_format(self):
        rc, out, err = self.wwm("-d")
        self.assertEqual(rc, 0, err)
        rows = out.splitlines()
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("0  * DG: 1280x720  VP: 0,0  "
                                           "WA: "), rows[0])
        self.assertTrue(rows[0].endswith("  1"), rows[0])

    # -- actions through the compositor -------------------------------------

    def test_10_activate_by_title(self):
        self.swaymsg("[class=XTerm] focus")
        rc, _o, err = self.wwm("-a", FOOT_TITLE.lower())  # case-insensitive
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.focused_name() == FOOT_TITLE))

    def test_11_activate_by_class_and_id(self):
        rc, _o, err = self.wwm("-x", "-a", "xterm.XTerm")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.focused_name() == XTERM_TITLE))
        frow = self.foot_row(["-l"])
        rc, _o, err = self.wwm("-i", "-a", frow.split()[0])
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.focused_name() == FOOT_TITLE))
        # decimal X id also resolves (X-plane checked first)
        xid = int(self.xterm_row(["-l"]).split()[0], 16)
        rc, _o, err = self.wwm("-i", "-a", str(xid))
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.focused_name() == XTERM_TITLE))

    def test_12_active_magic(self):
        self.swaymsg("[app_id=footw] focus")
        time.sleep(0.3)
        rc, _o, err = self.wwm("-v", "-a", ":ACTIVE:")
        self.assertEqual(rc, 0, err)
        frow = self.foot_row(["-l"])
        self.assertIn("Using window: %s" % frow.split()[0], err)

    def test_13_select_magic(self):
        t = threading.Timer(
            1.0, lambda: self.swaymsg("[class=XTerm] focus"))
        t.start()
        try:
            rc, _o, err = self.wwm("-v", "-a", ":SELECT:")
            self.assertEqual(rc, 0, err)
            xrow = self.xterm_row(["-l"])
            self.assertIn("Using window: %s" % xrow.split()[0], err)
        finally:
            t.cancel()

    def test_20_move_to_desktop_and_back(self):
        rc, _o, err = self.wwm("-r", XTERM_TITLE, "-t", "1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.xterm_row(["-l"]).split()[1] == "1"))
        # -s switches; -d marks the current desktop
        rc, _o, err = self.wwm("-s", "1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(lambda: any(
            r.startswith("1  *") for r in self.rows("-d"))))
        rc, _o, err = self.wwm("-s", "0")
        self.assertEqual(rc, 0, err)
        # -R: move to current desktop and activate
        rc, _o, err = self.wwm("-R", XTERM_TITLE)
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.xterm_row(["-l"]).split()[1] == "0"))
        self.assertTrue(self.wait(
            lambda: self.focused_name() == XTERM_TITLE))

    def test_21_move_resize_floating(self):
        self.swaymsg("[class=XTerm] floating enable")
        try:
            rc, _o, err = self.wwm("-r", XTERM_TITLE, "-e", "0,50,60,420,320")
            self.assertEqual(rc, 0, err)

            def placed():
                v = self.view(name=XTERM_TITLE)
                r = v["rect"]
                return (r["x"], r["y"]) == (50, 60) and \
                    abs(r["width"] - 420) <= 25 and \
                    abs(r["height"] - 320) <= 25
            self.assertTrue(self.wait(placed),
                            self.view(name=XTERM_TITLE)["rect"])
            # -1 leaves fields unchanged (move only)
            rc, _o, err = self.wwm("-r", XTERM_TITLE, "-e", "0,200,90,-1,-1")
            self.assertEqual(rc, 0, err)
            self.assertTrue(self.wait(lambda: (
                self.view(name=XTERM_TITLE)["rect"]["x"],
                self.view(name=XTERM_TITLE)["rect"]["y"]) == (200, 90)))
            # our -lG reports the compositor truth in both modes -- under `--true-geometry`, the flag
            # wmctrl never had.  Plain `-lG` is the original's doubled origin on this xwm (test_02's
            # parenthesis is the same quirk), which is what the second assertion pins.
            for nox in (False, True):
                g = self.xterm_row(["--true-geometry", "-lG"], nox=nox).split()[2:6]
                self.assertEqual(g[:2], ["200", "90"], g)
                doubled = self.xterm_row(["-lG"], nox=nox).split()[2:6]
                self.assertEqual(doubled[:2], ["400", "180"], doubled)
        finally:
            self.swaymsg("[class=XTerm] floating disable")

    def test_22_move_tiled_warns_but_succeeds(self):
        rc, _o, err = self.wwm("-r", XTERM_TITLE, "-e", "0,5,5,-1,-1")
        self.assertEqual(rc, 0)
        self.assertIn("; ignoring", err)

    def test_23_fullscreen_state(self):
        rc, _o, err = self.wwm("-r", FOOT_TITLE, "-b", "add,fullscreen")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.view(app_id="footw")["fullscreen_mode"] == 1))
        rc, _o, err = self.wwm("-r", FOOT_TITLE, "-b", "toggle,fullscreen")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: self.view(app_id="footw")["fullscreen_mode"] == 0))

    def test_24_maximize_warns_but_succeeds(self):
        rc, _o, err = self.wwm("-r", FOOT_TITLE, "-b",
                               "add,maximized_vert,maximized_horz")
        self.assertEqual(rc, 0)
        self.assertEqual(err.count("; ignoring"), 2, err)

    def test_25_set_title(self):
        # X window: works via X ChangeProperty once x11_mini is implemented
        # (warn+succeed while it is the stub or X is unreachable); native
        # Wayland window: always warn+succeed.
        rc, _o, err = self.wwm("-r", XTERM_TITLE, "-N", "Renamed-by-wwmctl")
        self.assertEqual(rc, 0, err)
        if _x11_mini_implemented() and "ignoring" not in err:
            self.assertTrue(self.wait(
                lambda: self.view(name="Renamed-by-wwmctl") is not None))
            rc, _o, err = self.wwm("-r", "Renamed-by-wwmctl", "-N",
                                   XTERM_TITLE)
            self.assertEqual(rc, 0, err)
            self.assertTrue(self.wait(
                lambda: self.view(name=XTERM_TITLE) is not None))
        else:
            self.assertIn("ignoring", err)
        rc, _o, err = self.wwm("-r", FOOT_TITLE, "-N", "nope")
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)
        self.assertIsNotNone(self.view(name=FOOT_TITLE))

    def test_26_wm_info(self):
        rc, out, err = self.wwm("-m")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("Name: "), out)
        if _x11_mini_implemented() and shutil.which("wmctrl"):
            orc, oout, _oe = self.oracle("-m")
            if orc == 0:
                self.assertEqual(out, oout)

    def test_27_warn_and_succeed_ops(self):
        for args in (("-k", "on"), ("-o", "0,0"), ("-n", "4"),
                     ("-g", "1280,720")):
            rc, _o, err = self.wwm(*args)
            self.assertEqual(rc, 0, args)
            self.assertIn("ignoring", err)

    def test_30_close_window(self):
        self.swaymsg("exec xterm -T XW-Victim -e sh -c 'sleep 600'")
        self.assertTrue(self.wait(
            lambda: any("XW-Victim" in r for r in self.rows("-l"))))
        rc, _o, err = self.wwm("-c", "XW-Victim")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.wait(
            lambda: not any("XW-Victim" in r for r in self.rows("-l"))))

    def test_31_no_match_and_bad_id(self):
        rc, out, err = self.wwm("-a", "zzz-no-such-window")
        self.assertEqual((rc, out, err), (1, "", ""))
        rc, _o, err = self.wwm("-i", "-a", "0x7ff12345")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
