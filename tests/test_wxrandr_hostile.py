#!/usr/bin/env python3
"""wxrandr against a hostile compositor and a hostile environment.

Everything here is a regression for a defect the hostile pass found, and
every one of them is driven at the wire: tests/fixtures/fake_wlr.py is a real
zwlr_output_manager_v1 server that can be told to go quiet at three different
moments, so the CLI is exercised exactly as a wedged wlroots session would.

  * a compositor that answers `succeeded` and then stops must not hang the
    CLI (the 10 s guard the backend arms has to survive dispatch())
  * a --scale that truncates an output's logical size below the advertised
    16x16 minimum is refused, and nothing is sent
  * --dpi 0 / nan / a negative one falls back to 96dpi instead of aborting
  * a hand-edited state file of the wrong shape never crashes a query
  * a broken stdout exits 1, not the interpreter's 120
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)



HELP_LINE = "Try 'xrandr --help' for more information.\n"


class FakeWlr(unittest.TestCase):
    """One fake wlroots compositor per test, in `mode`."""

    MODE = "normal"

    def setUp(self):
        # under /tmp when there is one: a nix sandbox's TMPDIR is long enough to push a
        # socket path past AF_UNIX's 107 bytes
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-hostile-",
                                    dir="/tmp" if os.access("/tmp", os.W_OK) else None)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sock = os.path.join(self.tmp, "wayland-fake")
        self.applies = os.path.join(self.tmp, "applies.log")
        self.server = subprocess.Popen(
            [sys.executable, os.path.join(FIXTURES, "fake_wlr.py"),
             self.sock, self.MODE, self.applies],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop_server)
        self.assertIn("listening", self.server.stdout.readline())

    def stop_server(self):
        if self.server.poll() is None:
            os.kill(self.server.pid, signal.SIGKILL)
            self.server.wait(10)

    def env(self):
        env = dict(os.environ)
        env.update({"WAYLAND_DISPLAY": self.sock, "PYTHONPATH": ROOT,
                    "XDG_RUNTIME_DIR": self.tmp,
                    "W11_PASSTHROUGH": "never"})
        env.pop("DISPLAY", None)
        return env

    def wxrandr(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, "-m", "wxrandr", "--backend", "wlr"] + list(args),
            env=self.env(), capture_output=True, text=True, timeout=timeout)

    def apply_count(self):
        try:
            with open(self.applies) as f:
                return len(f.read().split())
        except OSError:
            return 0


class ApplyGoesQuiet(FakeWlr):
    """The compositor takes the configuration, says `succeeded`, and then
    never speaks again.  dispatch() used to leave the socket blocking on the
    way out -- clearing the 10 s deadline the backend had armed -- so the
    post-apply roundtrip blocked in recvmsg with nothing to wake it and the
    CLI hung for as long as the session lasted."""

    MODE = "silent-apply"

    def test_apply_returns_and_does_not_hang(self):
        start = time.monotonic()
        p = self.wxrandr("--output", "HEAD-1", "--pos", "100x0", timeout=90)
        self.assertLess(time.monotonic() - start, 40,
                        "the CLI did not come back inside the 10s guard")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.stderr, "xrandr: the compositor applied the output "
                         "configuration and then stopped responding\n")
        self.assertEqual(self.apply_count(), 1)


class ApplyIsIgnored(FakeWlr):
    """The same, one step earlier: no answer to the apply at all.  This one
    always ended in the backend's own fatal; it is here so the two paths are
    covered by the same shape of test."""

    MODE = "mute-apply"

    def test_timeout_is_a_one_line_fatal(self):
        start = time.monotonic()
        p = self.wxrandr("--output", "HEAD-1", "--pos", "100x0", timeout=90)
        self.assertLess(time.monotonic() - start, 40)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: timed out waiting for the "
                         "compositor to apply the output configuration\n")


class HyprlandUnderTheWlrBackend(FakeWlr):
    """U08: `--backend wlr` on a session that has a Hyprland IPC socket, and the same run without one.

    Hyprland advertises `zwlr_output_manager_v1` version 4 and takes exactly ONE apply per session through it:
    the second times out at 10 s with nothing changed and no `[COutputConfiguration] Applying configuration`
    line in its own log, and with a second output present even the first one hangs -- identically for
    `wlr-randr`, which has no timeout and hangs for ever [M recon2/hyprland.md §4, and the same on Hyprland
    0.56.2 in recon2/arch.md]. The generic timeout sentence is true there and useless: the answer is a
    different backend, and this is the clause that says which.

    Detection sends a Hyprland session to `hypr`, so the only way to reach this line on Hyprland is to have
    asked for `--backend wlr` -- which is allowed, and which the smoke's control runs do.

    The fake compositor is `mute-apply`, which takes the apply and never answers: the same wire behaviour,
    without a Hyprland."""

    MODE = "mute-apply"

    def hypr_socket(self, sig="dd220efe_1788885217_674420693"):
        """`$XDG_RUNTIME_DIR/hypr/<signature>/.socket.sock`, where `session.find_hypr_socket()` looks.

        The runtime dir is the test's own (`self.tmp`, which also holds the fake `wayland-fake` socket, so it
        sorts first among the candidates), and the socket is a real listening one: the finder stats the path
        and checks its owner, and a plain file would be found just the same -- but a socket is what a live
        Hyprland leaves, and the clause must not depend on anything more than the path existing."""
        d = os.path.join(self.tmp, "hypr", sig)
        os.makedirs(d)
        path = os.path.join(d, ".socket.sock")
        if len(path.encode()) > 100:
            self.skipTest("AF_UNIX path too long under this TMPDIR: %s" % path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(path)
        srv.listen(1)
        # the compositor's own lock file, which is what picks the live instance out of the stale ones
        open(os.path.join(d, "hyprland.lock"), "w").close()
        return path

    def test_the_timeout_carries_the_hyprland_clause(self):
        self.hypr_socket()
        start = time.monotonic()
        p = self.wxrandr("--output", "HEAD-1", "--pos", "100x0", timeout=90)
        self.assertLess(time.monotonic() - start, 40)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr,
                         "xrandr: timed out waiting for the compositor to apply the output configuration"
                         " (Hyprland answers only the first output-configuration apply of a session;"
                         " use --backend hypr)\n")

    def test_without_the_instance_directory_it_is_todays_line(self):
        """The clause is a Hyprland fact and must not appear on any other wedged wlroots compositor: the same
        run with no `hypr/` under the runtime dir is byte-identical to what ApplyIsIgnored pins."""
        p = self.wxrandr("--output", "HEAD-1", "--pos", "100x0", timeout=90)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: timed out waiting for the "
                         "compositor to apply the output configuration\n")


class MinimumSize(FakeWlr):
    """The Screen line advertises `minimum 16 x 16`.  A --scale big enough to
    truncate int(px / scale) below it produced an output of 0x0 that the
    compositor accepted -- no space on the desktop, and nothing to click to
    get it back."""

    def test_scale_that_truncates_to_zero_is_refused(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale", "99999")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: output HEAD-1 cannot be smaller "
                         "than 16x16 (desired size 0x0)\n")
        self.assertEqual(self.apply_count(), 0, "a refused command sent an "
                         "output configuration anyway")

    def test_scale_below_the_minimum_is_refused(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale", "200")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: output HEAD-1 cannot be smaller "
                         "than 16x16 (desired size 9x5)\n")
        self.assertEqual(self.apply_count(), 0)

    def test_scale_from_one_pixel_is_refused(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale-from", "1x1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot be smaller than 16x16", p.stderr)
        self.assertEqual(self.apply_count(), 0)

    def test_scale_from_zero_is_refused_the_way_xrandr_refuses_it(self):
        """A zero dimension is refused at parse time, with xrandr's own line.

        This one is byte parity, not a size guard: xrandr.c tests `w <= 0 || h <= 0` and then says
        "nonnegative" anyway, and that mismatch is the oracle's, so it is reproduced.  Re-measured
        2026-09-16 with xrandr 1.5.3 against Xvfb -- `0x0`, `0x100`, `100x0` and `-1x5` all print this
        pair and exit 1.  Before wxrandr/cli.py's guard moved from `<` to `<=` the three zero forms were
        accepted, `build_targets` in hacks/display/core.py dropped them at its `fw > 0 and fh > 0` guard (a
        zero dimension has no scale to compute), and the run exited 0 with an empty stderr having sent one
        output configuration anyway."""
        for v in ("0x0", "0x100", "100x0"):
            with self.subTest(size=v):
                p = self.wxrandr("--output", "HEAD-1", "--scale-from", v)
                self.assertEqual(p.returncode, 1)
                self.assertEqual(p.stderr, "xrandr: --scale-from dimensions must be "
                                           "nonnegative\n" + HELP_LINE)
                self.assertEqual(self.apply_count(), 0, "a refused command sent an "
                                 "output configuration anyway")

    def test_dryrun_refuses_it_too(self):
        p = self.wxrandr("--dryrun", "--output", "HEAD-1", "--scale", "99999")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot be smaller than 16x16", p.stderr)

    def test_an_ordinary_scale_still_applies(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale", "2")
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertEqual(self.apply_count(), 1)


class DryrunPrimary(FakeWlr):
    """`--dryrun` documents itself as mutating nothing, and did mutate the
    one thing wxrandr persists: the primary output.  cli set state.primary
    from the stanzas *before* the dryrun branch, which then saved the state
    file, so a dryrun's `--primary` stuck and the next --query named it."""

    def state_bytes(self):
        """What wxrandr persists, or b"" while it has had nothing to say."""
        try:
            with open(os.path.join(self.tmp, "wxrandr-state.json"), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return b""

    def test_dryrun_primary_leaves_the_state_file_alone(self):
        # a real run first, so the store exists and the comparison is about
        # what a dryrun writes into it rather than about creating it
        self.assertEqual(self.wxrandr("--output", "HEAD-1",
                                      "--auto").returncode, 0)
        before = self.state_bytes()
        self.assertTrue(before)
        p = self.wxrandr("--dryrun", "--output", "HEAD-1", "--primary")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.state_bytes(), before)
        self.assertNotIn(" primary ", self.wxrandr("--query").stdout)

    def test_dryrun_noprimary_leaves_it_alone_too(self):
        self.assertEqual(self.wxrandr("--output", "HEAD-1",
                                      "--primary").returncode, 0)
        self.assertIn(" primary ", self.wxrandr("--query").stdout)
        before = self.state_bytes()
        p = self.wxrandr("--dryrun", "--noprimary")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.state_bytes(), before)
        self.assertIn(" primary ", self.wxrandr("--query").stdout)

    def test_a_real_run_still_sets_it(self):
        self.assertEqual(self.wxrandr("--output", "HEAD-1",
                                      "--primary").returncode, 0)
        self.assertIn("HEAD-1 connected primary ",
                      self.wxrandr("--query").stdout)


class DpiFallback(FakeWlr):
    """`--dpi 0` (and nan, and a negative one) reached a plain division in
    the verbose/dryrun screen line.  Real xrandr prints a line; we aborted."""

    def test_zero_nan_and_negative_dpi(self):
        for spelling in ("0", "nan", "-1", "inf"):
            with self.subTest(dpi=spelling):
                p = self.wxrandr("--dryrun", "--dpi", spelling,
                                 "--output", "HEAD-1", "--auto")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertNotIn("nan", p.stdout)
                self.assertNotIn("inf", p.stdout)
                self.assertTrue(p.stdout.startswith("screen 0: "), p.stdout)

    def test_a_real_dpi_is_still_honoured(self):
        p = self.wxrandr("--dryrun", "--dpi", "192", "--output", "HEAD-1",
                         "--auto")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.split("\n")[0],
                         "screen 0: 1920x1080 254x142 mm 192.00dpi")


class NonFiniteArguments(FakeWlr):
    """float() also takes `nan`, `inf` and anything that overflows to one.
    Left alone they surfaced as raw interpreter text much later -- or, for
    --newmode, went into the state file as a mode line nothing can render."""

    CASES = [
        (["--output", "HEAD-1", "--scale", "nan"],
         "xrandr: failed to parse 'nan' as a scaling factor\n"),
        (["--output", "HEAD-1", "--scale", "inf"],
         "xrandr: failed to parse 'inf' as a scaling factor\n"),
        (["--output", "HEAD-1", "--scale", "1e400"],
         "xrandr: failed to parse '1e400' as a scaling factor\n"),
        (["--output", "HEAD-1", "--rate", "nan"],
         "xrandr: failed to parse 'nan' as a number\n"),
        (["-s", "inf"], "xrandr: failed to parse 'inf' as a number\n"),
        (["--newmode", "m", "nan", "1", "2", "3", "4", "5", "6", "7", "8"],
         "xrandr: failed to parse 'nan' as a number\n"),
        (["--output", "HEAD-1", "--brightness", "nan"],
         "xrandr: --brightness: invalid argument 'nan'\n"),
        (["--output", "HEAD-1", "--gamma", "inf:1:1"],
         "xrandr: --gamma: invalid argument 'inf:1:1'\n"),
    ]

    def test_each_is_one_xrandr_line(self):
        for argv, want in self.CASES:
            with self.subTest(argv=argv):
                p = self.wxrandr(*argv)
                self.assertEqual(p.returncode, 1)
                self.assertEqual(p.stdout, "")
                # the whole of it: the message, then at most xrandr's own
                # "Try 'xrandr --help'" line.  No interpreter text.
                self.assertIn(p.stderr, (want, want + HELP_LINE), p.stderr)
        self.assertEqual(self.apply_count(), 0)


class HandEditedState(FakeWlr):
    """The state file is a plain JSON file the docs invite you to edit, in a
    directory shared with everything else that has your uid.  A container of
    the wrong type used to survive setdefault() and come back as a str, an
    int or a list, whose next [] or .get() raised somewhere else entirely."""

    SHAPES = [
        {"modes": "nope"}, {"gamma": "nope"}, {"addmode": 3},
        {"lastmode": []}, {"modes": None, "gamma": 7, "addmode": "x"},
    ]

    def state_path(self):
        return os.path.join(self.tmp, "wxrandr-state.json")

    def test_a_wrong_shaped_container_never_crashes(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                with open(self.state_path(), "w") as f:
                    json.dump({self.sock: shape}, f)
                p = self.wxrandr("--query")
                self.assertEqual((p.returncode, p.stderr), (0, ""))
                self.assertIn("HEAD-1 connected", p.stdout)
                p = self.wxrandr("--output", "HEAD-1", "--primary")
                self.assertEqual((p.returncode, p.stderr), (0, ""))

    def test_the_top_level_can_be_junk_too(self):
        for junk in ("[]", '"nope"', "3", "{"):
            with self.subTest(junk=junk):
                with open(self.state_path(), "w") as f:
                    f.write(junk)
                p = self.wxrandr("--query")
                self.assertEqual((p.returncode, p.stderr), (0, ""))


class BrokenStdout(FakeWlr):
    """A stdout that has gone must still exit 1.  The flush used to sit
    inside the try, so the failure became `xrandr: <errno>` and exit 1 --
    and then the interpreter flushed the same stream again on the way out,
    failed again, and turned that into exit 120."""

    def run_with_stdout(self, target):
        with open(target, "w") as out:
            return subprocess.run(
                [sys.executable, "-m", "wxrandr", "--backend", "wlr",
                 "--query"], env=self.env(), stdout=out,
                stderr=subprocess.PIPE, text=True, timeout=60)

    def test_a_full_stdout_exits_one(self):
        if not os.path.exists("/dev/full"):
            self.skipTest("no /dev/full")
        p = self.run_with_stdout("/dev/full")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(len(p.stderr.strip().split("\n")), 1, p.stderr)
        self.assertTrue(p.stderr.startswith("xrandr: "), p.stderr)

    def test_a_working_stdout_still_exits_zero(self):
        p = self.run_with_stdout(os.path.join(self.tmp, "out.txt"))
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        with open(os.path.join(self.tmp, "out.txt")) as f:
            self.assertIn("HEAD-1 connected", f.read())


class ConnectionsAreClosed(FakeWlr):
    """Session opens the compositor connection and never closed it: the
    socket went to the garbage collector, which says so at whatever moment it
    gets round to it."""

    def test_no_resource_warning(self):
        p = subprocess.run(
            [sys.executable, "-W", "always::ResourceWarning", "-m", "wxrandr",
             "--backend", "wlr", "--query"], env=self.env(),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("ResourceWarning", p.stderr)


if __name__ == "__main__":
    unittest.main()
