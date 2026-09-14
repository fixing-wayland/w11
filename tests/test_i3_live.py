#!/usr/bin/env python3
"""U37: the whole i3 story against a real i3 4.25.1 on a real X server.

Two halves, and they are the two things a user can do on an i3 box:

* **the default path** -- all four tools hand over to the originals, byte for byte.  That is the product
  claim for every X11 session and the only one worth making on i3, where the X server owns the layout, the
  input and the property store [M recon2/i3.md §2a: IDENTICAL for `search --name`, `-lGpx`, `-root` and
  `--listmonitors`, same rc].
* **the forced path** -- `W11_PASSTHROUGH=never` and `wxrandr --backend sway` reach our own code
  over i3's IPC socket, and this is where the four measured lies were [M recon2/i3.md §2b, §2c].  The unit
  half is tests/test_windows_i3.py over the recordings; this is the same claims against the compositor that
  produced them.

The rig is `support.HeadlessI3` (Xvfb + i3, no bar, no wizard): about three seconds, no root, no KVM
[M recon2/i3.md §5].  Skipped where Xvfb, i3 or xterm is not installed, which is every box that has not run
the CI image's package list."""

import os
import re
import shutil
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support

#: the four (ours, the original) command pairs the report compared, verbatim
PARITY = (
    (["-m", "wdotool", "search", "--name", "w11smoke"], ["xdotool", "search", "--name", "w11smoke"]),
    (["-m", "wwmctl", "-l", "-G", "-p", "-x"], ["wmctrl", "-l", "-G", "-p", "-x"]),
    (["-m", "wxprop", "-root", "_NET_CLIENT_LIST"], ["xprop", "-root", "_NET_CLIENT_LIST"]),
    (["-m", "wxrandr", "--listmonitors"], ["xrandr", "--listmonitors"]),
)


@unittest.skipUnless(shutil.which("Xvfb"), "Xvfb is not installed")
@unittest.skipUnless(shutil.which("i3"), "i3 is not installed")
@unittest.skipUnless(shutil.which("xterm"), "xterm is not installed")
class I3Live(unittest.TestCase):
    """One i3 and one xterm for the whole file: nothing here closes a window, and booting an X server per
    test would multiply three seconds by a dozen."""

    @classmethod
    def setUpClass(cls):
        cls.rig = support.HeadlessI3()
        cls.addClassCleanup(cls.rig.stop)
        # the daemon getdisplaygeometry spawns lands in the rig's runtime dir, and is stopped before the
        # directory that holds its socket goes away (cleanups are last-in-first-out)
        cls.addClassCleanup(support.stop_daemons_under, cls.rig.rtdir)
        cls.xterm = subprocess.Popen(
            ["xterm", "-T", "w11smoke", "-e", "sleep", "600"], env=cls.rig.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls._stop_xterm)
        cls.xid = cls._wait_for_window()

    @classmethod
    def _stop_xterm(cls):
        cls.xterm.terminate()
        try:
            cls.xterm.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.xterm.kill()
            cls.xterm.wait(timeout=5)

    @classmethod
    def _wait_for_window(cls):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            out = subprocess.run(["wmctrl", "-l"], env=cls.rig.env,
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                if line.endswith("w11smoke"):
                    return int(line.split()[0], 16)
            if cls.xterm.poll() is not None:
                raise unittest.SkipTest("xterm exited at startup")
            time.sleep(0.2)
        raise unittest.SkipTest("the xterm never appeared in wmctrl -l")

    # -- running things ------------------------------------------------------

    def ours(self, *argv, handover=False):
        """One of our tools as a real process. `handover=True` drops the suite's escape hatch, which is the
        only way to measure the thing this file's first half is about."""
        env = dict(self.rig.env)
        if handover:
            env.pop("W11_PASSTHROUGH", None)
        else:
            env["W11_PASSTHROUGH"] = "never"
        return subprocess.run([sys.executable] + list(argv), env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=120)

    def original(self, *argv):
        env = dict(self.rig.env)
        env.pop("W11_PASSTHROUGH", None)
        return subprocess.run(list(argv), env=env, capture_output=True, text=True, timeout=120)

    def i3_run(self, command):
        """One RUN_COMMAND over i3's own socket, so a test can arrange the session without i3-msg."""
        from hacks.display.core import SwayIPC
        ipc = SwayIPC(sockpath=self.rig.sock)
        try:
            reply = ipc.msg(0, command)
        finally:
            ipc.close()
        self.assertTrue(reply[0].get("success"), reply)

    def xwininfo(self, xid):
        out = subprocess.run(["xwininfo", "-id", str(xid)], env=self.rig.env,
                             capture_output=True, text=True, timeout=60).stdout
        got = {}
        for key, field in (("Absolute upper-left X", "x"), ("Absolute upper-left Y", "y"),
                           ("Width", "w"), ("Height", "h")):
            m = re.search(r"^\s*%s:\s+(-?\d+)$" % re.escape(key), out, re.M)
            self.assertIsNotNone(m, out)
            got[field] = int(m.group(1))
        return got

    # -- the default path ----------------------------------------------------

    def test_all_four_tools_hand_over_byte_for_byte(self):
        """The claim the README's X11 column makes, on the desktop it had never been measured on."""
        for ours, theirs in PARITY:
            with self.subTest(tool=theirs[0]):
                a, b = self.ours(*ours, handover=True), self.original(*theirs)
                self.assertEqual(a.returncode, b.returncode, a.stderr)
                self.assertEqual(a.stdout, b.stdout)

    def test_the_handover_is_what_answered(self):
        """The control for the four above: our own code, on this same session, does not answer the same
        thing.  `wmctrl -d` is where it shows -- i3 publishes no `_NET_DESKTOP_GEOMETRY` and no
        `_NET_WORKAREA`, so the original prints `DG: N/A ... WA: N/A` and we print the workspace rect out of
        GET_WORKSPACES [M recon2/i3.md §2b].  Without this, a clone that happened to agree with the
        originals would pass the four comparisons above whether it handed over or not."""
        forced = self.ours("-m", "wwmctl", "-d").stdout
        theirs = self.original("wmctrl", "-d").stdout
        handed = self.ours("-m", "wwmctl", "-d", handover=True).stdout
        self.assertIn("DG: N/A", theirs)
        self.assertIn("DG: 1920x1080", forced)
        self.assertEqual(handed, theirs)

    # -- the forced path -----------------------------------------------------

    def test_the_ids_are_the_x_servers_own(self):
        """Every id our tools hand out is a window the X server knows, and no con id is among them: on the
        live 4.25.1 `search` printed `97479943571072` where xdotool printed `8388621`."""
        out = self.ours("-m", "wdotool", "search", "--class", "XTerm")
        self.assertEqual(out.returncode, 0, out.stderr)
        ids = {int(line) for line in out.stdout.split()}
        self.assertIn(self.xid, ids)
        for wid in ids:
            self.assertLess(wid, 2 ** 32)
        # `-tree`, not `-children`: i3 reparents, so the xterm hangs under one of i3's frames rather than
        # off the root, and the claim is "an id this X server has", not "a direct child of the root".
        self.assertLessEqual(ids, self.root_tree_ids())

    def root_tree_ids(self):
        """Every window id in this X server's tree, root included."""
        tree = subprocess.run(["xwininfo", "-root", "-tree", "-all"], env=self.rig.env,
                              capture_output=True, text=True, timeout=60).stdout
        found = {int(m, 16) for m in re.findall(r"(0x[0-9a-f]+)", tree)}
        self.assertIn(self.xid, found, tree)         # the scan itself finds what we know is there
        return found

    def test_a_con_id_is_not_among_them(self):
        """The control for the test above, and the measured bug itself: `search` printed i3's node id
        `97479943571072` where the real xdotool printed `8388621`, and `xwininfo -root -tree` has never
        heard of it.  Without this, an id set the tree happened to contain would pass either way."""
        from hacks.display.core import SwayIPC
        ipc = SwayIPC(sockpath=self.rig.sock)
        self.addCleanup(ipc.close)
        cons = []

        def walk(node):
            if node.get("window") == self.xid:
                cons.append(node["id"])
            for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
                walk(child)

        walk(ipc.msg(4, ""))                        # GET_TREE
        self.assertEqual(len(cons), 1, cons)
        self.assertGreater(cons[0], 2 ** 32)        # a pointer, which is the whole problem
        self.assertNotIn(cons[0], self.root_tree_ids())

    def test_a_floated_window_moves(self):
        """§2c end to end: i3 wraps the floated view in a `floating_con`, and every `windowmove` used to be
        refused as tiled although `i3-msg '[con_id=N] move absolute position'` did it."""
        tiled = self.ours("-m", "wdotool", "windowmove", str(self.xid), "100", "100")
        self.assertIn("cannot move a tiled window", tiled.stderr)
        self.i3_run("[id=%d] floating enable" % self.xid)
        self.addCleanup(self.i3_run, "[id=%d] floating disable" % self.xid)
        # i3 places the *container* and the client sits inside its decoration: with the rig's default
        # floating border the xterm landed at 102,120 for a requested 100,120.  `border none` takes the
        # decoration out of the picture so the assertion can be the exact number, which is the only version
        # of it that would catch an off-by-one in the y adjustment.
        self.i3_run("[id=%d] border none" % self.xid)
        self.addCleanup(self.i3_run, "[id=%d] border normal" % self.xid)
        moved = self.ours("-m", "wdotool", "windowmove", str(self.xid), "100", "120")
        self.assertEqual((moved.returncode, moved.stderr), (0, ""))
        got = self.xwininfo(self.xid)
        self.assertEqual((got["x"], got["y"]), (100, 120), got)

    def test_getwindowpid_answers(self):
        """i3's tree has no `pid` at all; the X plane's `_NET_WM_PID` does, and it is the xterm we started."""
        out = self.ours("-m", "wdotool", "getwindowpid", str(self.xid))
        self.assertEqual((out.returncode, out.stderr), (0, ""))
        self.assertEqual(int(out.stdout), self.xterm.pid)

    def test_onlyvisible_matches_the_window_on_the_visible_workspace(self):
        out = self.ours("-m", "wdotool", "search", "--onlyvisible", "--class", "XTerm")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(str(self.xid), out.stdout.split())

    def test_getdisplaygeometry_answers(self):
        """It exited 2 with "no Wayland session found: cannot query the output layout" on a session whose
        layout the same process was already connected to."""
        out = self.ours("-m", "wdotool", "getdisplaygeometry")
        self.assertEqual((out.returncode, out.stdout), (0, "1920 1080\n"))

    def test_wxrandr_labels_i3_and_refuses_an_apply(self):
        out = self.ours("-m", "wxrandr", "--backend", "sway", "--print-backend", "--verbose")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("compositor: i3 4.", out.stdout)
        self.assertNotIn("compositor: sway", out.stdout)

        query = self.ours("-m", "wxrandr", "--backend", "sway", "--query")
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertIn("screen connected 1920x1080+0+0", query.stdout)
        self.assertNotIn("xroot-0", query.stdout)       # i3's pseudo-output is not a head

        apply_ = self.ours("-m", "wxrandr", "--backend", "sway", "--output", "screen", "--auto")
        self.assertEqual((apply_.returncode, apply_.stdout), (1, ""))
        self.assertEqual(
            apply_.stderr,
            "xrandr: this is i3, which has no output command; the X server owns the layout here"
            " -- use xrandr (or drop --backend sway)\n")

    def test_the_apply_i3_would_have_refused_says_so_in_its_own_words(self):
        """Why the refusal is up front rather than a relayed error: what i3 says to an `output` command is a
        parse error listing every command it does have."""
        from hacks.display.core import SwayIPC
        ipc = SwayIPC(sockpath=self.rig.sock)
        self.addCleanup(ipc.close)
        reply = ipc.msg(0, "output screen position 0 0")
        self.assertFalse(reply[0]["success"])
        self.assertTrue(reply[0]["parse_error"])
        self.assertIn("Expected one of these tokens", reply[0]["error"])


if __name__ == "__main__":
    unittest.main()
