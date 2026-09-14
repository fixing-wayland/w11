#!/usr/bin/env python3
"""U34: the six tools against a real headless labwc.

labwc is the wlroots compositor without an IPC socket that most people actually meet: Ubuntu Budgie 10.10
runs it, Xfce 4.20's Wayland session runs it, LXQt's Wayland session runs it. Everything the wlr floor gets
wrong shows up here first, and everything batch 8 fixed is meant to show up here fixed:

* `wwmctl -l` listed an XWayland xterm under `0x000f4240` with a synthesized `WM_CLASS "xterm","xterm"`,
  pid 0 and the output rectangle, where `xprop` knew it as `0x40000c` with `"xterm","XTerm"`
  [M recon2/labwc.md §4];
* `wwmctl -d` answered `get_desktop is not supported by the wlr backend` while labwc's own panel showed the
  three configured workspaces [M labwc.md §6a];
* `wwmctl -m` answered `wlr` off the backend token instead of the `wlroots wm` its own xwm publishes.

The display half, the capture half and the typing half already worked and are here as the control: if one of
them breaks, it broke in this batch. Every command below was run by hand on this guest first and the numbers
in the assertions are that run's [M labwc.md §3].

Skipped where labwc is not installed. Starts its own labwc on a private, SHORT XDG_RUNTIME_DIR (labwc dies
with `File name too long` on the suite's usual /tmp/<long prefix>, which is the Unix socket path limit).
"""

import os
import re
import shutil
import subprocess
import sys
import time
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py. This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import HeadlessLabwc
from wdotool.cli import XDO_VERSION

XTERM_TITLE = "lbxterm"
FOOT_TITLE = "lbfoot"

#: the three labwc is configured with here [M recon2/labwc.md §6a]
DESKTOPS = ["one", "two", "three"]


def oracle_xdotool():
    """The pinned xdotool 4.20260303.1, or whatever `xdotool` PATH has, or None.

    The same search `scripts/parity-oracle.sh` and `tests/test_xw11_parity.py` do, in the same order, so
    that a box with the flake's oracle built compares against the version this clone is written to and not
    against Ubuntu 26.04's 3.20160805.1, which is a different program with the same name. PATH is the
    fallback rather than a skip: `getwindowgeometry --shell` has printed the same four keys since
    2.20110530.1, and an oracle that disagrees about the numbers is a real failure either way."""
    got = os.environ.get("W11_ORACLE_PATH")
    names = [os.environ.get("W11_ORACLE_PATH_FILE"), os.path.join(ROOT, "scripts", "nixpath")]
    for name in [] if got else names:
        if name and os.path.isfile(name):
            with open(name, encoding="utf-8") as f:
                got = f.read().strip()
            break
    if not got:
        import glob
        got = ":".join(sorted(glob.glob("/nix/store/*-xdotool-%s/bin" % XDO_VERSION)))
    hit = shutil.which("xdotool", path=got + ":" + os.environ.get("PATH", "")) if got else None
    return hit or shutil.which("xdotool")


def oracle_wmctrl():
    """The pinned wmctrl 1.07, or whatever `wmctrl` PATH has, or None.

    `oracle_xdotool`'s search, one store path over: the flake pins `wmctrl-1.07` and
    `scripts/parity-oracle.sh` and `tests/test_xw11_parity.py` both glob for exactly that directory.
    Ubuntu 26.04 ships 1.07+git20240228, which prints the same `-lG` bytes as 1.07 for the columns this
    file reads -- both were measured printing `796 430 484 316` for the same xterm on 2026-09-12 -- so PATH
    is a usable fallback and not a skip."""
    got = os.environ.get("W11_ORACLE_PATH")
    names = [os.environ.get("W11_ORACLE_PATH_FILE"), os.path.join(ROOT, "scripts", "nixpath")]
    for name in [] if got else names:
        if name and os.path.isfile(name):
            with open(name, encoding="utf-8") as f:
                got = f.read().strip()
            break
    if not got:
        import glob
        got = ":".join(sorted(glob.glob("/nix/store/*-wmctrl-1.07/bin")))
    hit = shutil.which("wmctrl", path=got + ":" + os.environ.get("PATH", "")) if got else None
    return hit or shutil.which("wmctrl")


#: The oracle this file measured against, and the version it was: see `oracle_xdotool`.
ORACLE_XDOTOOL = oracle_xdotool()
#: The wmctrl oracle for the `-lG` column, which is a different program from the xdotool one and
#: disagrees with it about that column on purpose: see `TheGeometryColumn`.
ORACLE_WMCTRL = oracle_wmctrl()


def tool(rig, *args, timeout=60):
    """One of our tools, in-tree, against `rig`. -> (rc, stdout, stderr)."""
    p = subprocess.run([sys.executable, "-m", *args], env=rig.env, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def x_client_list(rig) -> list:
    """The X ids in `_NET_CLIENT_LIST`, read with the real xprop."""
    p = subprocess.run(["xprop", "-root", "_NET_CLIENT_LIST"], env=rig.env,
                       capture_output=True, text=True, timeout=30)
    return [int(v, 16) for v in re.findall(r"0x[0-9a-fA-F]+", p.stdout)]


def wait_for(pred, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(shutil.which("labwc"), "labwc not on PATH")
@unittest.skipUnless(shutil.which("xterm"), "xterm not on PATH")
@unittest.skipUnless(shutil.which("foot"), "foot not on PATH")
class LabwcWindowsTest(unittest.TestCase):
    """One xterm (XWayland) and one foot (native) side by side, the pair labwc.md was measured on."""

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessLabwc(need_display=True)
        cls.procs = []
        cls.spawn(["xterm", "-T", XTERM_TITLE, "-e", "sh", "-c", "sleep 600"])
        if not wait_for(lambda: XTERM_TITLE in tool(cls.rig, "wwmctl", "-l")[1]):
            cls.tearDownClass()
            raise unittest.SkipTest("the xterm never appeared (XWayland broken?)")
        cls.spawn(["foot", "--title", FOOT_TITLE, "sh", "-c", "sleep 600"])
        if not wait_for(lambda: FOOT_TITLE in tool(cls.rig, "wwmctl", "-l")[1]):
            cls.tearDownClass()
            raise unittest.SkipTest("the foot window never appeared")

    @classmethod
    def spawn(cls, argv):
        cls.procs.append(subprocess.Popen(argv, env=cls.rig.env,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))

    @classmethod
    def tearDownClass(cls):
        for p in getattr(cls, "procs", []):
            p.kill()
            p.wait(timeout=10)
        cls.rig.stop()

    def rows(self, *args):
        rc, out, err = tool(self.rig, "wwmctl", *args)
        self.assertEqual((rc, err), (0, ""), out)
        return [line.split(None, 1) for line in out.splitlines()]

    def test_the_xterm_is_listed_under_its_real_x_id(self):
        """The measured defect, live: `0x000f4240` where `xprop -root _NET_CLIENT_LIST` says `0x40000c`.
        The id is the thing a script carries from one tool to the next, so a made-up one is not a cosmetic
        problem."""
        xids = x_client_list(self.rig)
        self.assertEqual(len(xids), 1, "one XWayland client: the xterm")
        listed = {r[1].rsplit(None, 1)[-1]: int(r[0], 16) for r in self.rows("-l")}
        self.assertEqual(listed[XTERM_TITLE], xids[0])
        # The native toplevel keeps the floor's arrival-order id (1000000 + n; which n depends on which of
        # the two windows reached the manager first, and that is not this test's claim). What matters is
        # that it is not an X id and never could be mistaken for one.
        self.assertGreaterEqual(listed[FOOT_TITLE], 1000000)
        self.assertNotIn(listed[FOOT_TITLE], xids)

    def test_the_wm_class_pair_is_the_x_servers_and_not_a_synthesized_one(self):
        """`xterm.xterm` was the app_id twice over; the X server says `"xterm","XTerm"` and always did."""
        rc, out, _err = tool(self.rig, "wwmctl", "-lx")
        self.assertEqual(rc, 0)
        line, = [ln for ln in out.splitlines() if ln.endswith(XTERM_TITLE)]
        self.assertIn("xterm.XTerm", line)
        self.assertNotIn("xterm.xterm", line)
        foot, = [ln for ln in out.splitlines() if ln.endswith(FOOT_TITLE)]
        self.assertIn("foot.foot", foot, "a native toplevel is still its app id, twice")

    def test_the_x_client_gets_a_pid_and_a_real_rectangle(self):
        """Both come off the X plane: the floor reports pid 0 and the whole output for every window, which
        is what `wwmctl -lGp` printed on labwc before this."""
        line, = [ln for ln in tool(self.rig, "wwmctl", "-lGp")[1].splitlines()
                 if ln.endswith(XTERM_TITLE)]
        _id, _desk, pid, x, y, w, h = line.split()[:7]
        self.assertGreater(int(pid), 0)
        self.assertTrue(os.path.exists("/proc/%s" % pid), "a pid that is really running")
        self.assertLess(int(w), 1280, "the xterm's own width, not the output's")
        self.assertLess(int(h), 720)
        self.assertGreater(int(x) + int(y), 0, "and it is placed, not at the origin by default")

    def test_the_native_window_still_reports_what_the_protocol_carries(self):
        """No pid and no geometry exist for a native toplevel on either foreign-toplevel protocol, so the
        floor stays the floor -- a listing where half the columns are quietly invented would be worse."""
        line, = [ln for ln in tool(self.rig, "wwmctl", "-lGp")[1].splitlines()
                 if ln.endswith(FOOT_TITLE)]
        _id, _desk, pid, x, y, w, h = line.split()[:7]
        self.assertEqual((pid, x, y, w, h), ("0", "0", "0", "1280", "720"))

    @unittest.skipUnless(ORACLE_XDOTOOL, "the original xdotool is not on PATH")
    def test_getwindowgeometry_on_the_xwayland_window_is_what_the_original_xdotool_reads(self):
        """Route 5 through the xdotool clone, which is a different path from the `wwmctl -lGp` row above:
        `cmd_getwindowgeometry` reads `find()`/`list()` and never `views()`, so a rectangle folded into
        views() alone left this printing the floor. Measured on the resolute-labwc golden 2026-09-12:
        `wdotool getwindowgeometry 1000000` said `0,0 1920x1080` for an xterm `xdotool getwindowgeometry
        0x40000c` put at `718,395 484x316`.

        The oracle is the real xdotool on the real X id, over its own connection to the same server -- a
        different program reading a different socket, so agreement is agreement and not this backend
        agreeing with itself. It is the pinned 4.20260303.1 where the flake's oracle is built (the run this
        was written from, and what CI uses); on a box that has only the distribution's it is that one, which
        prints the same four `--shell` keys."""
        ours = self.shell_geometry("wdotool", self.our_id(XTERM_TITLE))
        theirs = self.x_geometry(x_client_list(self.rig)[0])
        self.assertEqual(ours, theirs)
        self.assertNotEqual(ours, self.output_rect(),
                            "the whole head is the floor's answer, not a window's rectangle")

    def test_getwindowgeometry_on_the_native_window_is_still_the_floor(self):
        """The half route 5 does not reach: no X server has heard of the foot window, and
        zwlr_foreign_toplevel_management_v1 carries no rectangle, so the output box is the whole truth
        here. NOT YET, and the lowest rung is 1 -- a foreign-toplevel protocol that carries a rectangle,
        which costs wlroots writing and shipping one; rung 6, a patched compositor with one geometry event
        per window, is the fallback. Pinned so that the day either lands, this line is what says so."""
        self.assertEqual(self.shell_geometry("wdotool", self.our_id(FOOT_TITLE)), self.output_rect())

    def our_id(self, title) -> str:
        """OUR id for the window with this title -- `search` reads `list()`, so it is the floor's
        arrival-order id and not the X id `wwmctl -l` prints for the same window."""
        rc, out, err = tool(self.rig, "wdotool", "search", "--name", title)
        self.assertEqual((rc, err), (0, ""), out)
        ids = out.split()
        self.assertEqual(len(ids), 1, out)
        return ids[0]

    def shell_geometry(self, clone, wid) -> tuple:
        """(x, y, w, h) out of `<clone> getwindowgeometry --shell <id>`, which is the parseable form
        xdotool has had since 2.20110530.1 and the one a script reads."""
        rc, out, err = tool(self.rig, clone, "getwindowgeometry", "--shell", str(wid))
        self.assertEqual((rc, err), (0, ""), out)
        kv = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
        return tuple(int(kv[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))

    def x_geometry(self, xid) -> tuple:
        """The same four numbers from the ORIGINAL xdotool (`ORACLE_XDOTOOL`), on the real X id."""
        p = subprocess.run([ORACLE_XDOTOOL, "getwindowgeometry", "--shell", "0x%x" % xid],
                           env=self.rig.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        kv = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if "=" in ln)
        return tuple(int(kv[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))

    def output_rect(self) -> tuple:
        """0,0 plus the head's mode: what every row of this listing read before route 5."""
        rc, out, err = tool(self.rig, "wdotool", "getdisplaygeometry")
        self.assertEqual((rc, err), (0, ""), out)
        w, h = out.split()
        return (0, 0, int(w), int(h))

    def test_wxprop_id_is_byte_identical_to_xprop(self):
        """The X plane was never the broken half: this is the control that says so."""
        if not shutil.which("xprop"):
            self.skipTest("xprop not on PATH")
        xid = "0x%x" % x_client_list(self.rig)[0]
        ours = tool(self.rig, "wxprop", "-id", xid)
        theirs = subprocess.run(["xprop", "-id", xid], env=self.rig.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(ours[0], theirs.returncode)
        self.assertEqual(ours[1], theirs.stdout)

    def test_wwmctl_m_says_what_the_check_window_says(self):
        """With an X plane up this is read straight off `_NET_SUPPORTING_WM_CHECK`; the point of
        `WlrBackend.wm_name` is that a session without one now answers the same string."""
        rc, out, _err = tool(self.rig, "wwmctl", "-m")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "Name: wlroots wm")

    def test_the_backend_that_was_chosen_is_the_wlr_floor(self):
        """No IPC socket, no bus name: detection has to land on `wlr` for any of the above to be about
        labwc at all [M labwc.md §2]."""
        rc, out, _err = tool(self.rig, "wxrandr", "--print-backend", "--verbose")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "wlr")

    def test_getwindowpid_on_the_xwayland_window_is_the_pid_the_x_server_knows(self):
        """Route 5's pid half, live. The toplevel protocol carries no pid, so this answered `window
        1000000 has no pid associated with it` while `wwmctl -lGpx` -- the same process, the same X plane,
        a different reader -- printed `0x0040000c -1 2045 718 395 484 316` on the resolute-labwc golden,
        2026-09-12 [M goal2/requests-batch-12.md 4]. The oracle is the X server's own `_NET_WM_PID`, read
        with the real `xprop`, and it is checked against /proc as well: a number that agrees with a stale
        property and with nothing running would prove nothing."""
        if not shutil.which("xprop"):
            self.skipTest("xprop not on PATH")
        xid = "0x%x" % x_client_list(self.rig)[0]
        p = subprocess.run(["xprop", "-id", xid, "_NET_WM_PID"], env=self.rig.env,
                           capture_output=True, text=True, timeout=30)
        theirs = re.search(r"= *(\d+)", p.stdout)
        self.assertIsNotNone(theirs, p.stdout)
        rc, out, err = tool(self.rig, "wdotool", "getwindowpid", self.our_id(XTERM_TITLE))
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(out.strip(), theirs.group(1))
        self.assertTrue(os.path.exists("/proc/%s" % out.strip()), "a pid that is really running")

    def test_getwindowpid_on_the_native_window_still_refuses_in_the_originals_words(self):
        """The half route 5 does not reach: no X server has heard of the foot window, and
        zwlr_foreign_toplevel_management_v1 carries no pid. The sentence is xdotool's own -- what the
        original prints for an X window with no `_NET_WM_PID` -- so parity and honesty are one string.
        NOT YET, rung 1: a foreign-toplevel protocol that carries the pid, at the cost of wlroots writing
        and shipping it."""
        wid = self.our_id(FOOT_TITLE)
        rc, _out, err = tool(self.rig, "wdotool", "getwindowpid", wid)
        self.assertEqual(rc, 1)
        self.assertEqual(err, "window %s has no pid associated with it.\n" % wid)


@unittest.skipUnless(shutil.which("labwc"), "labwc not on PATH")
@unittest.skipUnless(shutil.which("xterm"), "xterm not on PATH")
class TheGeometryColumn(unittest.TestCase):
    """`wwmctl -lG` against the original `wmctrl -lG`, on a FLOATING window placed away from 0,0.

    This is the one shape the parity oracle never saw, and it is why the bug survived: wmctrl 1.07 feeds
    the window's own x,y into `XTranslateCoordinates` instead of 0,0, so the origin is counted twice
    wherever the X parent is the root -- and a window tiled at 0,0 doubles to 0,0. Measured 2026-09-12 on
    a headless labwc 0.9.3 on this guest, one xterm placed at 398,215 484x316 with the original's own
    `wmctrl -e`, four readings of that one window:

        pinned wmctrl 1.07 -lGpx   0x0040000c 0 3144704 796  430  484  316  xterm.XTerm
        Ubuntu wmctrl 1.07-7       0x0040000c 0 3144704 796  430  484  316  xterm.XTerm
        pinned xdotool 4.2026      Position: 398,215 (screen: 0) / Geometry: 484x316
        xwininfo                   Absolute upper-left X: 398   Y: 215

    Under a REPARENTING X11 window manager the doubling shrinks to the frame border and is invisible,
    which is the other half of the same measurement (Xvfb :77-:79 the same day: openbox printed 400 255
    for a window at 399,235 with relative origin 1,20; xfwm4 408 273 for 403,244 relative 5,29; marco
    420 279 for 404,242 relative 16,37 -- `absolute + parent-relative` in all three).

    AGENTS.md: the originals' bugs are reproduced, and the better thing goes behind a flag the original
    never had. Its own labwc, because placing a window is a session-wide change."""

    XTERM_TITLE = "lbfloat"
    #: where the window is put, and what the original therefore prints
    PLACE = (398, 215, 484, 316)

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessLabwc(need_display=True)
        cls.procs = [subprocess.Popen(
            ["xterm", "-T", cls.XTERM_TITLE, "-e", "sh", "-c", "sleep 600"],
            env=cls.rig.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
        if not wait_for(lambda: cls.XTERM_TITLE in tool(cls.rig, "wwmctl", "-l")[1]):
            cls.tearDownClass()
            raise unittest.SkipTest("the xterm never appeared (XWayland broken?)")

    @classmethod
    def tearDownClass(cls):
        for p in getattr(cls, "procs", []):
            p.kill()
            p.wait(timeout=10)
        cls.rig.stop()

    def setUp(self):
        if not ORACLE_WMCTRL:
            self.skipTest("the original wmctrl is not on PATH")
        x, y, w, h = self.PLACE
        # placed with OUR tool, read with theirs: the placement is not the claim, the reading is
        rc, out, err = tool(self.rig, "wwmctl", "-i", "-r", self.xid(), "-e",
                            "0,%d,%d,%d,%d" % (x, y, w, h))
        self.assertEqual(rc, 0, out + err)
        if not wait_for(lambda: self.theirs()[:2] == [str(x * 2), str(y * 2)], 5.0):
            self.skipTest("labwc did not place the window at %d,%d (it reads %s)"
                          % (x, y, self.theirs()))

    def xid(self) -> str:
        return "0x%x" % x_client_list(self.rig)[0]

    def theirs(self) -> list:
        """The four `-lG` numbers the ORIGINAL prints for the xterm, over its own X connection."""
        p = subprocess.run([ORACLE_WMCTRL, "-lG"], env=self.rig.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        row = [ln for ln in p.stdout.splitlines() if ln.endswith(self.XTERM_TITLE)]
        self.assertEqual(len(row), 1, p.stdout)
        return row[0].split()[2:6]

    def ours(self, *flags) -> list:
        rc, out, err = tool(self.rig, "wwmctl", *flags, "-lG")
        self.assertEqual((rc, err), (0, ""), out)
        row = [ln for ln in out.splitlines() if ln.endswith(self.XTERM_TITLE)]
        self.assertEqual(len(row), 1, out)
        return row[0].split()[2:6]

    def test_the_G_column_is_the_originals_doubled_one_byte_for_byte(self):
        """The claim, against the original itself over its own connection: the two programs print the
        same four numbers for the same window, and they are the doubled ones."""
        x, y, w, h = self.PLACE
        self.assertEqual(self.theirs(), [str(x * 2), str(y * 2), str(w), str(h)],
                         "the original's own arithmetic, which is the oracle")
        self.assertEqual(self.ours(), self.theirs())

    def test_true_geometry_prints_what_xwininfo_and_xdotool_agree_on(self):
        """The flag the original never had. Its answer is checked against two OTHER programs reading the
        same X server -- xwininfo's `Absolute upper-left` and xdotool's `Position:` -- so it is not this
        clone agreeing with itself."""
        if not shutil.which("xwininfo"):
            self.skipTest("xwininfo not on PATH")
        p = subprocess.run(["xwininfo", "-id", self.xid()], env=self.rig.env,
                           capture_output=True, text=True, timeout=30)
        abs_x = int(re.search(r"Absolute upper-left X:\s+(-?\d+)", p.stdout).group(1))
        abs_y = int(re.search(r"Absolute upper-left Y:\s+(-?\d+)", p.stdout).group(1))
        x, y, w, h = self.PLACE
        self.assertEqual([abs_x, abs_y], [x, y], "xwininfo reads the window where we put it")
        self.assertEqual(self.ours("--true-geometry"), [str(x), str(y), str(w), str(h)])
        self.assertNotEqual(self.ours("--true-geometry"), self.theirs(),
                            "a window at the origin would hide the whole difference")

    @unittest.skipUnless(ORACLE_XDOTOOL, "the original xdotool is not on PATH")
    def test_the_xdotool_clone_is_not_this_column_and_did_not_move(self):
        """xdotool translates from 0,0 and never doubled, so `getwindowgeometry` was right as it stood
        and must stay byte-identical to the original's [M goal2/requests-batch-12.md 2]."""
        p = subprocess.run([ORACLE_XDOTOOL, "getwindowgeometry", "--shell", self.xid()],
                           env=self.rig.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        kv = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if "=" in ln)
        theirs = tuple(int(kv[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))
        self.assertEqual(theirs, self.PLACE)
        rc, out, err = tool(self.rig, "wdotool", "search", "--name", self.XTERM_TITLE)
        self.assertEqual((rc, err), (0, ""), out)
        rc, out, err = tool(self.rig, "wdotool", "getwindowgeometry", "--shell", out.split()[0])
        self.assertEqual((rc, err), (0, ""), out)
        kv = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
        self.assertEqual(tuple(int(kv[k]) for k in ("X", "Y", "WIDTH", "HEIGHT")), theirs)


@unittest.skipUnless(shutil.which("labwc"), "labwc not on PATH")
class LabwcDesktopsTest(unittest.TestCase):
    """Its own labwc, because switching desktops is a session-wide change."""

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessLabwc()

    @classmethod
    def tearDownClass(cls):
        cls.rig.stop()

    def desktops(self):
        rc, out, err = tool(self.rig, "wwmctl", "-d")
        self.assertEqual((rc, err), (0, ""), out)
        return [(ln.split()[1] == "*", ln.split()[-1]) for ln in out.splitlines()]

    def test_the_three_configured_desktops_are_listed(self):
        """`wwmctl -d` exited 1 with `get_desktop is not supported by the wlr backend` here, on a session
        whose own rc.xml names three [M recon2/labwc.md §3, §6a]."""
        self.assertEqual([name for _cur, name in self.desktops()], DESKTOPS)
        self.assertEqual([cur for cur, _n in self.desktops()], [True, False, False])

    def test_switching_moves_the_active_bit(self):
        """`activate` on the handle plus `commit` on the manager -- ext-workspace is double-buffered and
        the activate alone changes nothing."""
        self.addCleanup(lambda: tool(self.rig, "wwmctl", "-s", "0"))
        rc, _out, err = tool(self.rig, "wwmctl", "-s", "1")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([cur for cur, _n in self.desktops()], [False, True, False])
        self.assertEqual(tool(self.rig, "wdotool", "get_desktop")[1].strip(), "1")

    def test_a_desktop_that_is_not_there_is_refused(self):
        rc, _out, err = tool(self.rig, "wwmctl", "-s", "9")
        self.assertNotEqual(rc, 0)
        self.assertIn("workspace", err)


@unittest.skipUnless(shutil.which("labwc"), "labwc not on PATH")
@unittest.skipUnless(shutil.which("foot"), "foot not on PATH")
class LabwcTypingTest(unittest.TestCase):
    """The privilege-free typing path, end to end, on a box with no /dev/uinput node at all."""

    TEXT = "hello labwc"

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessLabwc()
        cls.path = os.path.join(cls.rig.rtdir, "typed.txt")
        cls.foot = subprocess.Popen(
            ["foot", "--title", "typedtest", "sh", "-c", "cat > %s" % cls.path],
            env=cls.rig.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for(lambda: "typedtest" in tool(cls.rig, "wwmctl", "-l")[1]):
            cls.tearDownClass()
            raise unittest.SkipTest("the foot window never appeared")

    @classmethod
    def tearDownClass(cls):
        cls.foot.kill()
        cls.foot.wait(timeout=10)
        cls.rig.stop()

    def typed(self) -> str:
        try:
            with open(self.path) as fh:
                return fh.read()
        except OSError:
            return ""

    def focus(self) -> str:
        """The foot window's id, once labwc has actually given it keyboard focus.

        `wwmctl -l` listing the title only says the surface exists. A key injected before the compositor has
        made it the keyboard focus goes nowhere: typing straight after the title appeared lost the first byte
        in 2 runs of 3 on this guest while another batch's live tests were running, and in 0 of 5 idle
        (measured 2026-09-08). The recon activated first [M recon2/labwc.md §3], and so does this."""
        rc, out, err = tool(self.rig, "wdotool", "search", "--name", "typedtest")
        self.assertEqual(rc, 0, err)
        ids = out.split()
        self.assertEqual(len(ids), 1, "one typedtest window, got %r" % out)
        rc, _out, err = tool(self.rig, "wdotool", "windowactivate", "--sync", ids[0])
        self.assertEqual(rc, 0, err)
        self.assertTrue(
            wait_for(lambda: tool(self.rig, "wdotool", "getactivewindow")[1].strip() == ids[0]),
            "labwc never made the window active")
        return ids[0]

    def warm_up(self) -> str:
        """One keystroke before the measured one; returns what the window has read by then.

        The FIRST virtual-keyboard client after a window takes focus can be dropped, whole or in part: on
        this guest a fresh `wdotool type abcde` + `key Return` landed nothing at all in 1 run of 3, and every
        run after it in the same session landed (9 of 9), and inside this file the same race cost
        `hello labwc` its `h` in 2 runs of 6 (measured 2026-09-08). It is the compositor's keymap swap and
        not the text: labwc hands the focused client the keymap we upload the first time a virtual keyboard
        appears, and a key racing that change goes nowhere, while the next client swaps nothing because the
        keymap is already ours. That race is wdotool/vkbd.py's to fix and it is filed as an open problem;
        what this test is for is the bytes, and it measures them once the swap has happened."""
        def stroke():
            rc, _out, err = tool(self.rig, "wdotool", "key", "Return")
            self.warm_err += err
            return rc == 0 and self.typed().endswith("\n")

        self.warm_err = ""
        self.assertTrue(wait_for(stroke), "no keystroke ever reached the window")
        return self.typed()

    def test_typing_lands_byte_exact_through_the_virtual_keyboard(self):
        """`WDOTOOL_FAKE_UINPUT` is not set and there is no device to open, so this is
        `zwp_virtual_keyboard_v1` and nothing else -- the path that needs no root and no udev rule
        [M recon2/labwc.md §3 wdotool]."""
        self.assertNotIn("WDOTOOL_FAKE_UINPUT", self.rig.env)
        self.focus()
        before = self.warm_up()
        rc, _out, err = tool(self.rig, "wdotool", "type", self.TEXT)
        self.assertEqual(rc, 0, err)
        self.assertEqual(tool(self.rig, "wdotool", "key", "Return")[0], 0)
        self.assertTrue(wait_for(lambda: self.typed() == before + self.TEXT + "\n"),
                        "the focused window read back %r, got %r"
                        % (before + self.TEXT + "\n", self.typed()))
        if os.path.exists("/dev/uinput"):
            return
        # The note is the daemon's and is said once per daemon [wdotool/daemon.py VKBD_CHOSE_WARNING], so it
        # belongs to whichever of these invocations started it -- the warm-up's, now that there is one.
        said = self.warm_err + err
        self.assertIn("zwp_virtual_keyboard_v1", said,
                      "and it said which path it took, once")
        self.assertEqual(said.count("zwp_virtual_keyboard_v1"), 1, said)


@unittest.skipUnless(shutil.which("labwc"), "labwc not on PATH")
@unittest.skipUnless(shutil.which("wlr-randr"), "wlr-randr not on PATH")
class LabwcDisplayTest(unittest.TestCase):
    """Two headless heads, and the applies read back through wlr-randr -- the control half of U34.

    labwc lays the initial outputs out in reverse enumeration order, HEADLESS-1 at x=1280, exactly as sway
    does on the rig's virtio-vga [M recon2/labwc.md §3], so the first thing every case here does is put
    them where it wants them."""

    #: `--newmode`'s arguments, an 800x600@60 modeline [M labwc.md §3 custom modeline]
    MODELINE = ("800x600_60", "38.22", "800", "832", "912", "1024",
                "600", "603", "607", "624", "-hsync", "+vsync")

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessLabwc(extra_env={"WLR_HEADLESS_OUTPUTS": "2"})

    @classmethod
    def tearDownClass(cls):
        cls.rig.stop()

    def wxrandr(self, *args):
        rc, out, err = tool(self.rig, "wxrandr", *args)
        self.assertEqual(rc, 0, err or out)
        return out

    def wlr_randr(self) -> str:
        p = subprocess.run(["wlr-randr"], env=self.rig.env, capture_output=True,
                           text=True, timeout=30)
        return p.stdout

    def positions(self) -> dict:
        out, name = {}, None
        for line in self.wlr_randr().splitlines():
            if line and not line[0].isspace():
                name = line.split()[0]
            elif line.strip().startswith("Position:"):
                out[name] = line.split(":", 1)[1].strip()
        return out

    def test_the_headless_rig_really_has_two_heads(self):
        self.assertEqual(sorted(self.positions()), ["HEADLESS-1", "HEADLESS-2"])

    def test_every_apply_labwc_was_measured_with_reads_back(self):
        """The five rows of labwc.md §3's table, in its order, because each one starts from the layout the
        one before it left. One test, one claim: the wlr display backend does on labwc what it does on
        sway."""
        with self.subTest("pos + primary + relative chain, one atomic call"):
            self.wxrandr("--output", "HEADLESS-1", "--pos", "0x0", "--primary",
                         "--output", "HEADLESS-2", "--right-of", "HEADLESS-1")
            self.assertEqual(self.positions(), {"HEADLESS-1": "0,0", "HEADLESS-2": "1280,0"})

        with self.subTest("rotate + fractional scale"):
            self.wxrandr("--output", "HEADLESS-2", "--rotate", "left", "--scale", "1.5x1.5")
            self.assertIn("HEADLESS-2 connected 480x853+1280+0", self.wxrandr("--query"))

        with self.subTest("mirror"):
            self.wxrandr("--output", "HEADLESS-2", "--rotate", "normal", "--scale", "1",
                         "--same-as", "HEADLESS-1")
            self.assertEqual(self.positions(), {"HEADLESS-1": "0,0", "HEADLESS-2": "0,0"})
            self.assertIn("current 1280 x 720", self.wxrandr("--query"))

        with self.subTest("off, then on again"):
            self.wxrandr("--output", "HEADLESS-2", "--off")
            self.assertNotIn("HEADLESS-2", self.positions())
            self.wxrandr("--output", "HEADLESS-2", "--auto", "--right-of", "HEADLESS-1")
            self.assertEqual(self.positions()["HEADLESS-2"], "1280,0")

        with self.subTest("custom modeline"):
            self.wxrandr("--newmode", *self.MODELINE)
            self.wxrandr("--addmode", "HEADLESS-1", "800x600_60")
            self.wxrandr("--output", "HEADLESS-1", "--mode", "800x600_60")
            self.assertIn("HEADLESS-1 connected primary 800x600+0+0", self.wxrandr("--query"))

    def test_wmirror_check_names_both_capture_protocols(self):
        """labwc has `zwlr_screencopy_manager_v1` v3 *and* `ext_image_copy_capture_manager_v1` v1
        [M labwc.md §3 wmirror], which is the pair the refusal line now names -- so the tool that GNOME and
        KDE cannot have works here exactly as on sway."""
        rc, out, err = tool(self.rig, "wmirror", "--check")
        self.assertEqual(rc, 0, err)
        capture, = [ln for ln in out.splitlines() if ln.startswith("capture:")]
        self.assertIn("zwlr_screencopy_manager_v1", capture)
        self.assertIn("ext_image_copy_capture_manager_v1", capture)


if __name__ == "__main__":
    unittest.main()
