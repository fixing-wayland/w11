#!/usr/bin/env python3
"""integration tests against a real headless sway — sway backend
(search/activate/move/resize/scratchpad/desktops/selectwindow/multi-output)
plus a smoke pass of the wlr foreign-toplevel backend against the same
compositor.

Skipped when sway/foot are not on PATH (run inside `nix develop`). The
compositor is `support.HeadlessSway`, the same rig the other four live files
boot, on a private XDG_RUNTIME_DIR so concurrent runs cannot collide. This
file used to boot one by hand and then wait with bare `time.sleep(0.2/0.3)`
calls, which made it the live file most likely to flake under load; every
wait is now the `wait(pred, 15)` poll the others use."""

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
# `import support` / `import wl_fake` resolve only with the tests directory
# itself on sys.path: running this file by path puts it there for free,
# `python3 -m unittest tests/<file>.py` does not (tests/test_passthrough.py
# fails over a file that imports one of them without this line).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (run in nix develop)")
@unittest.skipUnless(shutil.which("foot"), "foot not on PATH (run in nix develop)")
class SwayWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = support.HeadlessSway(
            prefix="wdotool-winc-", need_display=False,
            extra_conf='exec foot --app-id foota --title "Foot A"\n'
                       'exec foot --app-id footb --title "Foot B"\n')
        cls.rtdir, cls.sock, cls.sway = cls.rig.rtdir, cls.rig.sock, cls.rig.proc
        cls.wl = cls.rig.wayland_display()
        if not cls.wait(lambda: len(cls.wdo("search", "--class",
                                            "foot[ab]")[1].split()) == 2):
            cls.rig.stop()
            raise unittest.SkipTest("foot windows never appeared")

    @classmethod
    def tearDownClass(cls):
        cls.rig.stop()

    @classmethod
    def wait(cls, pred, timeout=15):
        """Poll `pred` until it is true, the way the other live files do. A
        fixed sleep is a bet on how loaded the box is: the rig runs these
        under `nix develop` on a laptop and on a QEMU guest with four CPUs,
        and the same 0.3 s was both twice too long and not long enough."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if pred():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    backend = "sway"

    @classmethod
    def _wdo_env(cls, backend=None):
        return dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            WDOTOOL_BACKEND=backend or cls.backend,
            SWAYSOCK=cls.sock,
            WAYLAND_DISPLAY=cls.wl,
        )

    @classmethod
    def wdo(cls, *args, backend=None):
        p = subprocess.run(
            [sys.executable, "-m", "wdotool", *args],
            env=cls._wdo_env(backend), capture_output=True, text=True,
            cwd=ROOT, timeout=30,
        )
        return p.returncode, p.stdout, p.stderr

    def swaymsg(self, cmd):
        subprocess.run(
            ["swaymsg", "-s", self.sock, cmd],
            env=dict(os.environ, XDG_RUNTIME_DIR=self.rtdir),
            capture_output=True, timeout=10, check=True,
        )

    # -- sway backend -------------------------------------------------------

    def test_01_search_and_queries(self):
        rc, out, err = self.wdo("search", "--class", "foota")
        self.assertEqual(rc, 0, err)
        wid = int(out)
        rc, out, _e = self.wdo("getwindowclassname", str(wid))
        self.assertEqual(out, "foota\n")
        rc, out, _e = self.wdo("search", "--class", "foota", "getwindowpid")
        self.assertEqual(rc, 0)
        self.assertGreater(int(out), 0)
        rc, out, _e = self.wdo("search", "--class", "foota", "getwindowname")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith("\n") and out.strip())

    def test_02_search_forms(self):
        rc, out, _e = self.wdo("search", "--shell", "--prefix", "W",
                               "--class", "foot[ab]")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("WWINDOWS=(") and out.endswith(")\n"))
        rc, out, _e = self.wdo("search", "--limit", "1", "--class", "foot[ab]")
        self.assertEqual(len(out.split()), 1)
        rc, out, _e = self.wdo("search", "--class", "nosuchapp")
        self.assertEqual((rc, out), (1, ""))

    def test_03_activate_focus(self):
        for cls_ in ("foota", "footb"):
            rc, _o, err = self.wdo("search", "--class", cls_,
                                   "windowactivate", "--sync")
            self.assertEqual(rc, 0, err)
            rc, out, _e = self.wdo("getactivewindow", "getwindowclassname")
            self.assertEqual(out, cls_ + "\n")
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowfocus", "--sync")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wdo("getwindowfocus", "getwindowclassname")
        self.assertEqual(out, "foota\n")

    def test_04_move_resize_floating(self):
        self.swaymsg("[app_id=foota] floating enable")
        try:
            rc, _o, err = self.wdo("search", "--class", "foota",
                                   "windowmove", "--sync", "100", "50")
            self.assertEqual(rc, 0, err)
            rc, out, _e = self.wdo("search", "--class", "foota",
                                   "getwindowgeometry", "--shell")
            geo = dict(line.split("=") for line in out.split())
            self.assertEqual((geo["X"], geo["Y"]), ("100", "50"))

            # x/y literal passthrough
            rc, _o, _e = self.wdo("search", "--class", "foota",
                                  "windowmove", "x", "200")
            rc, out, _e = self.wdo("search", "--class", "foota",
                                   "getwindowgeometry", "--shell")
            geo = dict(line.split("=") for line in out.split())
            self.assertEqual((geo["X"], geo["Y"]), ("100", "200"))

            rc, _o, err = self.wdo("search", "--class", "foota",
                                   "windowsize", "--sync", "400", "300")
            self.assertEqual(rc, 0, err)
            rc, out, _e = self.wdo("search", "--class", "foota",
                                   "getwindowgeometry", "--shell")
            geo = dict(line.split("=") for line in out.split())
            # foot may shave up to one terminal cell to snap to its grid
            self.assertLessEqual(abs(int(geo["WIDTH"]) - 400), 16, out)
            self.assertLessEqual(abs(int(geo["HEIGHT"]) - 300), 16, out)
        finally:
            self.swaymsg("[app_id=foota] floating disable")

    def test_05_move_tiled_warns_but_succeeds(self):
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowmove", "10", "10")
        self.assertEqual(rc, 0)
        self.assertIn("xdo_move_window reported an error", err)

    def test_06_scratchpad_map_unmap(self):
        rc, _o, err = self.wdo("search", "--class", "foota", "windowunmap")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wdo("search", "--onlyvisible", "--class", "foota")
        self.assertEqual((rc, out), (1, ""))
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowmap", "--sync")
        self.assertEqual(rc, 0, err)
        rc, _o, _e = self.wdo("search", "--onlyvisible", "--class", "foota")
        self.assertEqual(rc, 0)
        # windowminimize behaves like unmap on sway
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowminimize", "--sync")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "foota", "windowmap")
        self.assertEqual(rc, 0, err)
        self.swaymsg("[app_id=foota] floating disable")

    def test_07_windowstate(self):
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowstate", "--add", "FULLSCREEN")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowstate", "--toggle", "FULLSCREEN")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowstate", "--add", "MAXIMIZED_VERT")
        self.assertEqual(rc, 1)
        self.assertIn("xdo_window_property reported an error", err)

    def test_08_desktops(self):
        rc, out, _e = self.wdo("get_desktop")
        self.assertEqual((rc, out), (0, "0\n"))
        rc, out, _e = self.wdo("set_desktop", "1", "get_desktop")
        self.assertEqual(out, "1\n")
        rc, out, _e = self.wdo("set_desktop", "--relative", "--", "-1",
                               "get_desktop")
        self.assertEqual(out, "0\n")
        rc, out, err = self.wdo("search", "--class", "footb",
                                "set_desktop_for_window", "1",
                                "get_desktop_for_window")
        self.assertEqual(out, "1\n", err)
        rc, _o, _e = self.wdo("search", "--class", "footb",
                              "set_desktop_for_window", "0")
        rc, out, _e = self.wdo("get_num_desktops")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(int(out), 1)
        rc, out, _e = self.wdo("get_desktop_viewport", "--shell")
        self.assertEqual(out, "X=0\nY=0\n")

    def test_09_warn_and_succeed(self):
        rc, out, err = self.wdo("search", "--class", "foot[ab]",
                                "windowreparent", "%1", "%2")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("windowreparent", err)
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "set_window", "--name", "zzz")
        self.assertEqual(rc, 0)
        self.assertIn("set_window", err)
        rc, _o, err = self.wdo("search", "--class", "foota", "windowraise")
        self.assertEqual(rc, 0)  # tiled: warn + succeed
        rc, _o, err = self.wdo("search", "--class", "foota", "windowlower")
        self.assertEqual(rc, 0)

    def test_10_selectwindow(self):
        t = threading.Timer(
            1.0, lambda: self.swaymsg("[app_id=footb] focus"))
        t.start()
        try:
            rc, out, err = self.wdo("selectwindow", "getwindowclassname")
            self.assertEqual(rc, 0, err)
            self.assertEqual(out, "footb\n")
        finally:
            t.cancel()

    def test_11_close_quit_kill(self):
        self.swaymsg("exec foot --app-id victim1")
        self.swaymsg("exec foot --app-id victim2")
        self.assertTrue(
            self.wait(lambda: len(self.wdo("search", "--class",
                                           "victim[12]")[1].split()) == 2),
            "victim windows never appeared")
        rc, _o, err = self.wdo("search", "--class", "victim1", "windowclose")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "victim2", "windowkill")
        self.assertEqual(rc, 0, err)
        self.assertTrue(
            self.wait(lambda: self.wdo("search", "--class",
                                       "victim[12]")[0] == 1),
            "victims did not go away")

    # -- wlr backend smoke --------------------------------------------------

    def test_12_wlr_smoke(self):
        rc, out, err = self.wdo("search", "--class", "foot[ab]",
                                backend="wlr")
        self.assertEqual(rc, 0, err)
        wids = [int(x) for x in out.split()]
        self.assertEqual(len(wids), 2)
        for wid in wids:
            self.assertGreaterEqual(wid, 1000000)
        rc, out, err = self.wdo("search", "--class", "foota",
                                "getwindowclassname", backend="wlr")
        self.assertEqual((rc, out), (0, "foota\n"))
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowactivate", backend="wlr")
        self.assertEqual(rc, 0, err)
        self.assertTrue(
            self.wait(lambda: self.wdo("getactivewindow",
                                       "getwindowclassname",
                                       backend="wlr")[1] == "footb\n"),
            "the wlr activate never took effect")
        rc, out, _e = self.wdo("search", "--class", "foota",
                               "getwindowgeometry", backend="wlr")
        self.assertIn("Geometry: 1280x720", out)  # geometry unknown: output size
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowstate", "--add", "FULLSCREEN",
                               backend="wlr")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowstate", "--remove", "FULLSCREEN",
                               backend="wlr")
        self.assertEqual(rc, 0, err)
        # The desktop commands follow the registry: sway 1.11 as Arch builds it
        # publishes ext_workspace_manager_v1 and the floor answers over it, Ubuntu's
        # 1.11 does not and the floor says so (CI run 34364127816 measured both).
        reg = subprocess.run(
            [sys.executable, "-c", "from hacks.window import backend_detect as b; "
             "print(' '.join(sorted(b.session_registry() or {})))"],
            env=self._wdo_env(None), capture_output=True, text=True, cwd=ROOT, timeout=30)
        has_ws = "ext_workspace_manager_v1" in reg.stdout.split()
        rc, out, err = self.wdo("get_desktop", backend="wlr")
        if has_ws:
            self.assertEqual(rc, 0, err)
            self.assertRegex(out, r"^\d+\n$")
        else:
            self.assertEqual(rc, 1)
            self.assertIn("not supported by the wlr backend", err)
            self.assertIn("ext_workspace_manager_v1", err)
        rc, _o, err = self.wdo("search", "--class", "foota", "getwindowpid",
                               backend="wlr")
        self.assertEqual(rc, 1)


    # -- two outputs --------------------------------------------------------

    def test_13_two_outputs_with_a_negative_origin(self):
        """T53: a second head to the LEFT of the first, i.e. at a negative x.

        The layout is a bounding box, not a sum of widths, and an origin left
        of zero is the case that tells the two apart: HEADLESS-1 is
        1280x720 at x=0 and HEADLESS-2 is 800x600 at x=-800, so the box runs
        from -800 to 1280 -- 2080 wide -- and the naive sum would say 2080 as
        well while a naive max would say 1280. The heights differ too (sway
        puts the pair at y=100 here), and the box is the taller span, 720.

        `wwmctl -d` then has to report each workspace's own work area in
        layout coordinates, negatives included -- xrandr and wmctrl both
        publish those, and a script that reads them positions windows by
        them. The output is unplugged again at the end so that the tests
        that ran before this one are the only ones that ever saw one head."""
        self.swaymsg("create_output")
        try:
            self.assertTrue(
                self.wait(lambda: "HEADLESS-2" in self.outputs()),
                "sway did not create a second output")
            self.swaymsg("output HEADLESS-2 mode 800x600 pos -800 100")
            self.assertTrue(
                self.wait(lambda: self.wdo("getdisplaygeometry")[1]
                          == "2080 720\n"),
                "getdisplaygeometry: %r" % self.wdo("getdisplaygeometry")[1])
            rows = self.wwmctl("-d").splitlines()
            self.assertEqual(len(rows), 2, rows)
            for row in rows:
                self.assertIn("DG: 2080x720", row)
            self.assertTrue(
                any("WA: -800,100 800x600" in r for r in rows), rows)
        finally:
            self.swaymsg("output HEADLESS-2 unplug")
        self.assertTrue(
            self.wait(lambda: self.wdo("getdisplaygeometry")[1]
                      == "1280 720\n"),
            "the second output was not unplugged")

    def outputs(self):
        p = subprocess.run(
            ["swaymsg", "-s", self.sock, "-t", "get_outputs", "--raw"],
            env=dict(os.environ, XDG_RUNTIME_DIR=self.rtdir),
            capture_output=True, text=True, timeout=10)
        return p.stdout

    def wwmctl(self, *args):
        """The other tool against the same compositor: -d is where a second
        output shows up as a second workspace with its own work area."""
        p = subprocess.run(
            [sys.executable, "-m", "wwmctl", *args],
            env=self._wdo_env(), capture_output=True, text=True, cwd=ROOT,
            timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout


if __name__ == "__main__":
    unittest.main(verbosity=2)
