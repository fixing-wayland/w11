#!/usr/bin/env python3
"""U36: river 0.4, where window control does not exist -- and the tools have to say so.

river creates a `wlr.ForeignToplevelHandleV1` for every window and only ever pushes state at it. Read in
v0.4.8's `river/river/Window.zig`: `handle.setTitle`/`setAppId` (438-440, 1226-1245),
`setActivated` (795), `handle.destroy()` (1201), and a grep for `request_close|request_activate|
request_maximize|request_minimize|request_fullscreen` over river/*.zig finds nothing at all. Measured against
that reading with river 0.4.8 + tinyrwm and one foot window: `windowclose` twice five seconds apart left it
running and listed, `windowactivate` left ACTIVATED on the other window, `windowminimize` and
`windowstate --add FULLSCREEN` changed nothing -- and all four exited 0 [M recon2/river.md §2a].

Every mutating command silently succeeding is worse than one that fails, so this file is the live half of
U09: the same commands, against the real compositor that behaves that way, and each one has to warn.

The other two halves are the control. river's display side is plain `zwlr_output_management` and the wlr
backend is exactly right there, and typing lands through `zwp_virtual_keyboard_v1` with no device
[M river.md §3]. If either breaks, the read-only check broke it.

Skipped where river or tinyrwm is missing, which is every CI runner: neither is packaged anywhere we build,
so this runs where somebody built them -- the rig's arch-river golden is the measured place.
"""

import os
import shutil
import subprocess
import sys
import time
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py. This line is what covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import HeadlessRiver

TITLE = "rvfoot"

#: what `WlrBackend` says when a request was accepted and nothing happened
NOT_APPLIED = "did not apply it"

#: `close` gets its own line, because its silence has a cause the others do not -- a client that is asking
#: to save. It names river and never blames the compositor alone [hacks/window/backend_wlr.py CLOSE_REASON].
NOT_CLOSED = "the window did not close within"

HAVE_RIVER = bool(shutil.which("river") and shutil.which(HeadlessRiver.WM))


def tool(rig, *args, timeout=60):
    p = subprocess.run([sys.executable, "-m", *args], env=rig.env, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def wait_for(pred, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return False


class RiverWithoutAWindowManager(HeadlessRiver):
    """river with nobody on the other end of `river_window_manager_v1`.

    Not a broken rig: it is a measured state of the session. A client connects and lives, and both
    foreign-toplevel protocols report zero handles, because windows only exist once a window manager has
    mapped them [M recon2/river.md §2b]."""

    def __init__(self, *a, **kw):
        # HeadlessRiver.__init__ skips when tinyrwm is missing, which is right for the rig that runs one.
        # This one runs none, so `river` alone is the requirement and the class decorator is the only gate;
        # skipping here for a binary this rig never spawns would give a misleading reason.
        super(HeadlessRiver, self).__init__(*a, **kw)

    def argv(self, confdir):
        return [self.BINARY]


@unittest.skipUnless(HAVE_RIVER, "river and tinyrwm are not both installed")
class RiverIsReadOnlyTest(unittest.TestCase):
    """river 0.4 + tinyrwm, one foot window, and the four commands that do nothing."""

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessRiver()
        cls.foot = subprocess.Popen(
            ["foot", "--title", TITLE, "sh", "-c", "sleep 600"], env=cls.rig.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for(lambda: TITLE in tool(cls.rig, "wwmctl", "-l")[1]):
            cls.tearDownClass()
            raise unittest.SkipTest("the foot window never appeared under tinyrwm")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "foot", None) is not None:
            cls.foot.kill()
            cls.foot.wait(timeout=10)
        cls.rig.stop()

    def wid(self) -> str:
        rows = [ln for ln in tool(self.rig, "wwmctl", "-l")[1].splitlines()
                if ln.endswith(TITLE)]
        self.assertEqual(len(rows), 1, "exactly one window is listed")
        return str(int(rows[0].split()[0], 16))

    def test_windowclose_warns_and_the_window_is_still_there(self):
        """The headline measurement: rc 0, `foot` still running, still listed -- twice, five seconds
        apart. Exit 0 with no word said is the thing this batch exists to stop."""
        wid = self.wid()
        rc, _out, err = tool(self.rig, "wdotool", "windowclose", wid)
        self.assertEqual(rc, 0, "wdotool prints the reason and succeeds; it is not a failed request")
        self.assertIn(NOT_CLOSED, err)
        self.assertIn("river 0.4", err)
        self.assertIsNone(self.foot.poll(), "the client is still running")
        self.assertIn(TITLE, tool(self.rig, "wwmctl", "-l")[1])

    def test_windowminimize_warns(self):
        rc, _out, err = tool(self.rig, "wdotool", "windowminimize", self.wid())
        self.assertEqual(rc, 0)
        self.assertIn(NOT_APPLIED, err)
        self.assertIn("set_minimized", err)

    def test_windowstate_fullscreen_reports_the_reason_and_succeeds(self):
        """`set_state` has somewhere to return this, so it comes back as the contract's one-line reason
        rather than a warning -- and wdotool prints it and exits 0, the way it does for KWin's
        accepted-and-ignored operations."""
        rc, _out, err = tool(self.rig, "wdotool", "windowstate",
                             "--add", "FULLSCREEN", self.wid())
        self.assertEqual(rc, 0)
        self.assertIn(NOT_APPLIED, err)
        self.assertIn("set_fullscreen", err)

    def test_the_reason_names_river_and_the_protocol(self):
        """`windowactivate` and not `windowclose`: close is the one request whose silence a client can cause,
        so it names river without calling the handle read-only. Every other request can only be the
        compositor, and river's handle is why."""
        _rc, _out, err = tool(self.rig, "wdotool", "windowactivate", self.wid())
        self.assertIn("river 0.4", err)
        self.assertIn("read-only", err)
        self.assertNotIn("minimiz", err, "activate has no business naming minimize")

    def test_the_geometry_commands_still_refuse_outright(self):
        """A different thing from "accepted and ignored": there is no request to send at all, so this is a
        refusal and not a warning."""
        rc, _out, err = tool(self.rig, "wdotool", "windowmove", self.wid(), "10", "10")
        self.assertNotEqual(rc, 0)
        self.assertIn("carries no geometry and no stacking", err)

    def test_wxrandr_query_works_because_the_display_half_is_plain_wlr(self):
        rc, out, err = tool(self.rig, "wxrandr", "--query")
        self.assertEqual(rc, 0, err)
        self.assertIn("Screen 0:", out)
        self.assertEqual(tool(self.rig, "wxrandr", "--print-backend")[1].strip(), "wlr")

    def test_typing_lands_through_the_virtual_keyboard(self):
        path = os.path.join(self.rig.rtdir, "typed.txt")
        proc = subprocess.Popen(["foot", "--title", "typedtest", "sh", "-c",
                                 "cat > %s" % path], env=self.rig.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        self.assertTrue(wait_for(lambda: "typedtest" in tool(self.rig, "wwmctl", "-l")[1]))
        self.assertEqual(tool(self.rig, "wdotool", "type", "hello river")[0], 0)
        self.assertEqual(tool(self.rig, "wdotool", "key", "Return")[0], 0)

        def typed():
            try:
                with open(path) as fh:
                    return fh.read()
            except OSError:
                return ""

        self.assertTrue(wait_for(lambda: typed() == "hello river\n"), typed())


@unittest.skipUnless(shutil.which("river"), "river is not installed")
class RiverWithoutAWindowManagerTest(unittest.TestCase):
    """No window manager means no windows -- and an empty listing, not an error."""

    @classmethod
    def setUpClass(cls):
        cls.rig = RiverWithoutAWindowManager()
        cls.foot = subprocess.Popen(
            ["foot", "--title", TITLE, "sh", "-c", "sleep 600"], env=cls.rig.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.0)   # long enough for a window to have appeared if one were going to

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "foot", None) is not None:
            cls.foot.kill()
            cls.foot.wait(timeout=10)
        cls.rig.stop()

    def test_the_listing_is_empty_and_the_client_is_alive(self):
        rc, out, err = tool(self.rig, "wwmctl", "-l")
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertIsNone(self.foot.poll(),
                          "the client connected and lives; it is simply never mapped")


if __name__ == "__main__":
    unittest.main()
