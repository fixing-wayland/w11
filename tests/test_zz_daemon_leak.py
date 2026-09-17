"""Suite-wide guard: no test may leave an input daemon running.

This is the leak itself, not a hypothetical one. The suite spawned daemons
and did not stop them, which is how the test rig came to have 161 of them
alive at once -- ~3GB of resident memory, each listening on a socket in a
per-test runtime directory that had since been deleted, so they were
unreachable as well as immortal; the oldest had been running eighteen
hours. Stopping belongs to the tests that spawn (support.stop_daemons_under
registered before the spawn, so it runs however the test ends); this makes
sure the next test to spawn one cannot quietly forget.

`zz`, because both runners that collect a directory sort the files: this
one goes last. The daemons alive at *import* are the baseline, and import
is before any test has run under both of them -- `unittest discover` loads
every module while building the suite, and pytest collects before it runs.
A daemon that was already there is somebody's real one, or another suite
running beside this one, and is not this suite's business.
"""

import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import (compositor_pids, daemon_pids, daemon_runtime_dir,
                     stop_compositor, stop_daemon)

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

#: the name `hacks/display/gamma.py` gives the anonymous file it ships the ramp in
#: (`os.memfd_create("wxrandr-gamma")`), which is what /proc/<pid>/fd calls it
GAMMA_MEMFD = "memfd:wxrandr-gamma"


def gamma_holder_pids():
    """pids of this euid holding a `wxrandr-gamma` memfd.

    A gamma holder is a detached process like the input daemon, but it carries
    nothing that names it: it is a fork of whatever ran `--brightness`, so its
    cmdline is that command's and `daemon_pids`'s trick does not transfer.  The
    memfd is the one thing in /proc that says `wxrandr` at all.

    It is a narrow net, and deliberately named as one: `holder_main` closes the
    fd in the `finally` of the same `submit()` that sends it, so a settled
    holder has none (measured on a live one here: fds 0/1/2 on /dev/null and two
    sockets, nothing else).  What it catches is a holder stuck in or before that
    send -- a compositor that stopped reading -- which is exactly the state in
    which one would sit for ever.  The holder that has finished starting is
    covered instead by the tests that prove it ends itself
    (tests/test_wxrandr_gamma.py test_04, test_11)."""
    me = os.geteuid()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat("/proc/%d" % pid).st_uid != me:
                continue
            fds = os.listdir("/proc/%d/fd" % pid)
        except OSError:
            continue           # it exited, or is not ours to look inside
        for fd in fds:
            try:
                target = os.readlink("/proc/%d/fd/%s" % (pid, fd))
            except OSError:
                continue
            if GAMMA_MEMFD in target:
                out.append(pid)
                break
    return sorted(out)


_BEFORE = set(daemon_pids())
_SWAY_BEFORE = {pid for pid, _ in compositor_pids()}
_GAMMA_BEFORE = set(gamma_holder_pids())

# A daemon signalled a moment ago is allowed to finish dying: a test's own
# cleanup waits for its daemons, but a daemon that is on its way out for a
# reason of its own (an idle timer, a socket that has just gone) is racing
# this file, and it is the leftovers that matter, not the microseconds.
GRACE = 5.0


class NoDaemonIsLeftBehind(unittest.TestCase):
    def test_the_suite_stopped_every_daemon_it_started(self):
        deadline = time.monotonic() + GRACE
        while True:
            left = sorted(set(daemon_pids()) - _BEFORE)
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if not left:
            return

        # Say which, before killing them: the pid on its own tells nobody
        # which test is at fault and the runtime directory usually does.
        # And kill them, so that a suite which fails this does not hand the
        # next run a machine with even more of them on it.
        detail = ", ".join(
            "pid %d (%s)" % (pid, daemon_runtime_dir(pid) or "unknown dir")
            for pid in left)
        stuck = [pid for pid in left if not stop_daemon(pid)]
        self.fail("%d wdotool daemon(s) still running at the end of the "
                  "suite: %s%s -- whatever spawned them must stop them "
                  "(tests/support.py: stop_daemons_under)"
                  % (len(left), detail,
                     "; could not stop %s" % stuck if stuck else ""))

    def test_the_suite_stopped_every_compositor_it_started(self):
        """The same rule for the headless compositors the live tests boot.

        Six were found alive on the test machine, the oldest forty-six hours
        old, left by scripts run beside the suite rather than by the suite
        itself.  The suite is clean today and this keeps it that way."""
        deadline = time.monotonic() + GRACE
        while True:
            now = {pid: conf for pid, conf in compositor_pids()}
            left = sorted(set(now) - _SWAY_BEFORE)
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if not left:
            return
        detail = ", ".join("pid %d (%s)" % (pid, now.get(pid) or "no config")
                           for pid in left)
        stuck = [pid for pid in left if not stop_compositor(pid)]
        self.fail("%d headless compositor(s) still running at the end of the "
                  "suite: %s%s -- whatever booted them must stop them "
                  "(tests/support.py: HeadlessSway)"
                  % (len(left), detail,
                     "; could not stop %s" % stuck if stuck else ""))

    def test_no_gamma_holder_is_left_wedged_before_it_sends_the_ramp(self):
        """The same rule for the gamma holders, as far as /proc lets it reach.

        The holder outlives the command that set the brightness on purpose --
        the zwlr gamma control dies with its client connection, so something has
        to keep one -- which makes it the third kind of process this suite can
        leave behind.  But it carries no name of its own: it is a fork of
        whatever ran `--brightness`, so its cmdline is that command's, and the
        `wxrandr-gamma` memfd is the only thing in /proc that says `wxrandr` at
        all.  `holder_main` closes that fd in the `finally` of the same
        `submit()` that sends it (measured on a live one here: fds 0/1/2 on
        /dev/null and two sockets, nothing else), so a holder that finished
        starting is invisible to this and the name says so.

        What is left is the holder wedged in or before that send -- a compositor
        that stopped reading -- which is the one that would sit there for ever.
        The settled holder is covered instead by the tests that prove it ends
        itself (tests/test_wxrandr_gamma.py test_04, test_11).  Making the
        settled one visible needs a marker the scanner can read without an fd
        (a `WXRANDR_GAMMA_HOLDER=<output>` entry in the child's environ, or a
        prctl name): that is a change to hacks/display/gamma.py, which no fix in this
        batch's list covers, so it is proposed rather than made."""
        deadline = time.monotonic() + GRACE
        while True:
            left = sorted(set(gamma_holder_pids()) - _GAMMA_BEFORE)
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if not left:
            return
        detail = ", ".join("pid %d" % pid for pid in left)
        stuck = [pid for pid in left if not stop_daemon(pid)]
        self.fail("%d wxrandr gamma holder(s) still running at the end of the "
                  "suite: %s%s -- whatever set a brightness must drop it "
                  "(wxrandr.gamma.stop_holder)"
                  % (len(left), detail,
                     "; could not stop %s" % stuck if stuck else ""))


class TheScannerFindsWhatItLooksFor(unittest.TestCase):
    """The guard above is only worth its line if it can see one.

    Every one of these three is a real process of this uid: one holding a memfd
    with the holder's name, one holding a memfd with another name, and one
    holding neither.

    These are self-checks of `gamma_holder_pids`, a helper in this file -- not
    coverage of wxrandr's gamma path, and not a matrix cell.  A scanner that
    silently matched nothing would make the guard above pass for ever, which is
    the failure they exist to catch."""

    def holder_like(self, name):
        p = subprocess.Popen(
            [sys.executable, "-c",
             "import os, sys, time\n"
             "fd = os.memfd_create(%r)\n"
             "sys.stdout.write('up\\n'); sys.stdout.flush()\n"
             "time.sleep(30)" % name],
            stdout=subprocess.PIPE)
        self.addCleanup(self.stop, p)
        self.assertEqual(p.stdout.readline(), b"up\n")
        return p

    def stop(self, p):
        if p.poll() is None:
            p.kill()
            p.wait(5)
        p.stdout.close()

    def test_a_process_holding_the_memfd_is_found(self):
        p = self.holder_like("wxrandr-gamma")
        self.assertIn(p.pid, gamma_holder_pids())

    def test_another_anonymous_file_is_not_a_holder(self):
        p = self.holder_like("something-else")
        self.assertNotIn(p.pid, gamma_holder_pids())

    def test_and_it_is_gone_the_moment_the_process_is(self):
        p = self.holder_like("wxrandr-gamma")
        self.assertIn(p.pid, gamma_holder_pids())
        self.stop(p)
        deadline = time.monotonic() + 5.0
        while p.pid in gamma_holder_pids() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotIn(p.pid, gamma_holder_pids())


if __name__ == "__main__":
    unittest.main()
