"""wmirror against a real headless sway: detection, a start, and the two
output changes that must end a running mirror.

Everything else about wmirror is proved against doubles -- a fake registry,
`FakeWatch` replaying zwlr_output_manager events, a stub wl-mirror. This file
is the one place where the supervisor's watch reads REAL
zwlr_output_management events off a real compositor, which is the half the
doubles cannot vouch for: the fake decides both what an event looks like and
when it arrives, so a supervisor that never subscribed, or that read a serial
it never got, would pass every one of those tests.

sway is the whole audience for this tool (wlroots only, by design), and
`swaymsg create_output` gives a headless one a second head with no hardware
at all -- so the two-output layout the mirror needs is real, and
`swaymsg output HEADLESS-2 disable` is the same request a user makes.

The helper is still the stub from tests/support.py: wl-mirror is not a
dependency of this suite, and what is under test here is what the supervisor
does about the layout, not what wl-mirror paints. Measured on sway 1.11 on
this host -- `wmirror --check` reads
`zwlr_screencopy_manager_v1 v3, ext_image_copy_capture_manager_v1 v1` off
that registry.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns. It goes BEFORE
# the first tool import, which is the suite's rule: passthrough is decided
# in cli.main here, but a module that read the variable at import time would
# read it too late.
os.environ["W11_PASSTHROUGH"] = "never"

import support                                                    # noqa: E402
from hacks.mirror import supervise  # noqa: E402

#: The two heads this file works with. HEADLESS-1 is the one sway boots with
#: (support.HeadlessSway.CONF names its mode); HEADLESS-2 comes from
#: `swaymsg create_output`, and is given a DIFFERENT logical size on purpose:
#: two outputs of one size at one position already mirror on wlroots, and
#: wmirror refuses that case by name rather than starting a helper for it.
SRC, DST = "HEADLESS-1", "HEADLESS-2"
SRC_MODE, DST_MODE = "1280x720", "1280x1024"


def state_records(rtdir):
    """Every mirror record in `rtdir`'s wmirror-state.json, target -> record.

    The file is wxrandr's `State`: one top-level key per compositor socket,
    each holding this tool's `mirrors` container. A test that read `mirrors`
    off the top level found nothing and quietly asserted about an empty
    dictionary, which is why the shape is unpacked in one place here."""
    try:
        with open(os.path.join(rtdir, "wmirror-state.json")) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for session in doc.values() if isinstance(doc, dict) else ():
        if not isinstance(session, dict):
            continue
        for target, rec in (session.get("mirrors") or {}).items():
            if isinstance(rec, dict):
                out[target] = rec
    return out


def gone(pid, seconds):
    """Wait up to `seconds` for that pid to disappear. True when it did."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(shutil.which("sway"), "no sway on PATH")
@unittest.skipUnless(shutil.which("swaymsg"), "no swaymsg on PATH")
class Live(unittest.TestCase):
    """One headless sway for the class, two heads, a fresh mirror per test."""

    @classmethod
    def setUpClass(cls):
        cls.rig = support.HeadlessSway(prefix="wmirror-live-",
                                       need_display=False)
        # kill whatever is written down BEFORE sway goes: the state file lives
        # in the rig's runtime directory, and rig.stop() removes it. Cleanups
        # run last-in-first-out, so this one is registered second.
        cls.addClassCleanup(cls.rig.stop)
        cls.addClassCleanup(cls._kill_recorded)
        cls.stubdir = os.path.join(cls.rig.rtdir, "bin")
        support.write_wl_mirror_stub(cls.stubdir)
        cls.env = dict(os.environ,
                       XDG_RUNTIME_DIR=cls.rig.rtdir,
                       PATH=cls.stubdir + os.pathsep + os.environ.get("PATH", ""),
                       W11_PASSTHROUGH="never")
        cls.env.pop("WAYLAND_DISPLAY", None)
        if cls.swaymsg("create_output").returncode != 0:
            cls.rig.stop()
            raise unittest.SkipTest("this sway has no create_output")

    @classmethod
    def _kill_recorded(cls):
        """SIGKILL both pids of every record the state file still holds.

        A supervisor is detached and reparented to init, so nothing here
        waits for it and a failed test would otherwise leave one -- plus its
        stub, which sleeps 30 s -- running after the compositor it was
        watching had gone."""
        for rec in state_records(cls.rig.rtdir).values():
            for key in ("pid", "helper_pid"):
                pid = rec.get(key)
                if isinstance(pid, int) and pid > 0 and pid != os.getpid():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

    @classmethod
    def swaymsg(cls, *args):
        return subprocess.run(["swaymsg"] + list(args), env=cls.rig.env,
                              capture_output=True, text=True, timeout=20)

    def setUp(self):
        self.wmirror("--stop-all")
        # Every test starts from the same two rectangles, side by side and
        # not touching: sway re-arranges the heads by itself when one is
        # created or disabled, so the positions are set here rather than
        # assumed from whatever the previous test left behind.
        self.layout(dst_pos=(1280, 0))
        self.addCleanup(self.wmirror, "--stop-all")

    def layout(self, dst_pos=(1280, 0), dst_enabled=True):
        self.swaymsg("output", SRC, "enable", "mode", SRC_MODE, "pos", "0", "0")
        if dst_enabled:
            self.swaymsg("output", DST, "enable", "mode", DST_MODE,
                         "pos", str(dst_pos[0]), str(dst_pos[1]))
        else:
            self.swaymsg("output", DST, "disable")
        self.assertTrue(self.wait_for_layout(dst_pos if dst_enabled else None),
                        "sway did not settle on the layout this test needs")

    def wait_for_layout(self, dst_pos, seconds=5.0):
        """Poll sway until DST is where (or gone from where) we asked."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            outs = {o["name"]: o for o in
                    json.loads(self.swaymsg("-t", "get_outputs", "--raw").stdout)}
            dst = outs.get(DST, {})
            if dst_pos is None:
                if not dst.get("active"):
                    return True
            elif dst.get("active") and (dst["rect"]["x"], dst["rect"]["y"]) == dst_pos:
                return True
            time.sleep(0.05)
        return False

    def wmirror(self, *argv, timeout=30):
        """`python3 -m wmirror ...` as a real subprocess -> (rc, out, err).

        A subprocess and not cli.main(): the stub has to be found on PATH,
        the supervisor has to detach from something, and the connection has
        to be made from a process whose XDG_RUNTIME_DIR is the rig's."""
        p = subprocess.run([sys.executable, "-m", "wmirror"] + list(argv),
                           env=self.env, cwd=ROOT, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def record(self, target=DST):
        """The record for `target` in the rig's state file, or None."""
        return state_records(self.rig.rtdir).get(target)

    # -- detection -------------------------------------------------------------

    def test_check_reads_the_capture_protocols_and_heads_off_the_real_registry(self):
        """`--check` against a real wl_registry, not a fake one.

        sway 1.11 advertises zwlr_screencopy_manager_v1 v3 (and, since 1.11,
        ext_image_copy_capture_manager_v1 v1), which is what makes wl-mirror
        possible here at all -- the fixture cannot vouch for that, because a
        fixture is written from the same reading of the protocol as the code
        under test. The outputs come from zwlr_output_manager, the same call
        `wxrandr --query` renders."""
        rc, out, err = self.wmirror("--check")
        self.assertEqual(rc, 0, err)
        self.assertIn("zwlr_screencopy_manager_v1", out)
        self.assertIn(self.rig.rtdir, out)          # the socket it found
        self.assertRegex(out, r"outputs:.*%s 1280x720\+0\+0" % SRC)
        self.assertIn("%s 1280x1024+1280+0" % DST, out)
        self.assertIn("mirrors:  none", out)

    # -- a mirror, for real ----------------------------------------------------

    def test_a_mirror_starts_and_is_listed(self):
        rc, out, err = self.wmirror(SRC, "--to", DST)
        self.assertEqual(rc, 0, err)
        self.assertIn("%s <- %s" % (DST, SRC), out)
        rec = self.record()
        self.assertIsNotNone(rec, "the start wrote no record")
        self.assertEqual(supervise.liveness(rec), (True, True))
        rc, out, err = self.wmirror("--list")
        self.assertEqual(rc, 0, err)
        self.assertIn("%s <- %s" % (DST, SRC), out)
        self.assertIn("wl-mirror pid %d" % rec["helper_pid"], out)

    def test_disabling_the_target_ends_the_mirror(self):
        """The supervisor's whole reason to exist, on real events.

        wl-mirror exits when the SOURCE disappears and survives the TARGET
        disappearing -- on sway that relocates the mirror window onto the
        source, so the helper carries on capturing a picture of itself. The
        supervisor watches zwlr_output_management and ends it instead. Here
        those events come from sway, off the wire, in answer to the same
        `swaymsg output ... disable` a user types."""
        self.assertEqual(self.wmirror(SRC, "--to", DST)[0], 0)
        rec = self.record()
        self.assertIsNotNone(rec)
        self.swaymsg("output", DST, "disable")
        # POLL_SECONDS is the longest the supervisor sleeps with nothing to
        # do; the event wakes its select() long before that. WMIRROR.md
        # promises "within a poll"; twice over is the margin this test allows
        # a loaded box.
        self.assertTrue(gone(rec["helper_pid"], 2 * supervise.POLL_SECONDS),
                        "wl-mirror outlived the output it was painting on")
        self.assertTrue(gone(rec["pid"], 2 * supervise.POLL_SECONDS))
        self.addCleanup(self.layout)
        self.assertEqual(self.wmirror("--list"), (0, "", ""))

    def test_moving_the_target_onto_the_source_ends_it_as_self_capture(self):
        """The same guard the start applies, applied for as long as it runs.

        Over a shared rectangle sway draws both outputs' windows on both
        heads, so a fullscreen mirror window on the target lands on the
        source too and wl-mirror captures its own picture. A start refuses
        that layout by name; a mirror the user then moves INTO it has to end
        the same way."""
        self.assertEqual(self.wmirror(SRC, "--to", DST)[0], 0)
        rec = self.record()
        self.assertIsNotNone(rec)
        self.swaymsg("output", DST, "pos", "0", "0")
        self.assertTrue(gone(rec["helper_pid"], 2 * supervise.POLL_SECONDS),
                        "the mirror kept running over a shared rectangle")
        self.addCleanup(self.layout)
        self.assertEqual(self.wmirror("--list"), (0, "", ""))
        # ...and a fresh start on that layout is refused for the same reason
        rc, out, err = self.wmirror(SRC, "--to", DST)
        self.assertEqual(rc, 1)
        self.assertIn("share pixels", err)


if __name__ == "__main__":
    unittest.main()
