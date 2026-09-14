#!/usr/bin/env python3
"""U38: Openbox on a real X server -- the desktop Lubuntu ships, measured.

Lubuntu 26.04 is LXQt 2.3 on Openbox under SDDM, and nothing in this tree had ever mentioned any of the
three [M recon2/openbox.md].  What that report found, in a real SDDM+LXQt VM, was that in the session every
tool hands over and works, and out of it -- `ssh root@box` with an empty environment -- all four failed with
`Authorization required, but no authorization protocol specified`, because SDDM writes the cookie to
`/tmp/xauth_<random>` and `_SESSION_LEADERS` named no process of that desktop to read it from.

Three claims here.  Two are against `support.HeadlessOpenbox` (Xvfb + openbox, no LXQt around it):

* the handover parity of the i3 file, on the other X11 window manager;
* `_shell_environ()` recognising a real `openbox` process as a session leader and reading its `$DISPLAY`,
  which is the mechanism the cookie route is built on.  The cookie itself needs a display manager and is the
  rig's business (`vm/flavors/resolute-lxqt.yaml`); what can be proved on this box is the scan.

The third is `SessionCookieUid` at the end, which needs neither: §2d of the same report is about *which uid*
that scan is run for, and it is measured entirely in which argument reaches `session.find_xauthority()`."""

import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

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
from w11common import session
from hacks.window import x11_mini

#: the same four pairs tests/test_i3_live.py compares, which are the report's own four
PARITY = (
    (["-m", "wdotool", "search", "--name", "w11smoke"], ["xdotool", "search", "--name", "w11smoke"]),
    (["-m", "wwmctl", "-l", "-G", "-p", "-x"], ["wmctrl", "-l", "-G", "-p", "-x"]),
    (["-m", "wxprop", "-root", "_NET_CLIENT_LIST"], ["xprop", "-root", "_NET_CLIENT_LIST"]),
    (["-m", "wxrandr", "--listmonitors"], ["xrandr", "--listmonitors"]),
)


@unittest.skipUnless(shutil.which("Xvfb"), "Xvfb is not installed")
@unittest.skipUnless(shutil.which("openbox"), "openbox is not installed")
@unittest.skipUnless(shutil.which("xterm"), "xterm is not installed")
class OpenboxLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = support.HeadlessOpenbox()
        cls.addClassCleanup(cls.rig.stop)
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

    def handover_env(self):
        """The rig's environment with the suite's escape hatch dropped: handing over is what is measured."""
        env = dict(self.rig.env)
        env.pop("W11_PASSTHROUGH", None)
        return env

    def ours(self, *argv):
        return subprocess.run([sys.executable] + list(argv), env=self.handover_env(),
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def original(self, *argv):
        return subprocess.run(list(argv), env=self.handover_env(),
                              capture_output=True, text=True, timeout=120)

    def test_all_four_tools_hand_over_byte_for_byte(self):
        for ours, theirs in PARITY:
            with self.subTest(tool=theirs[0]):
                a, b = self.ours(*ours), self.original(*theirs)
                self.assertEqual(a.returncode, b.returncode, a.stderr)
                self.assertEqual(a.stdout, b.stdout)

    def test_wwmctl_m_names_openbox_with_a_blank_class(self):
        """Openbox's `_NET_SUPPORTING_WM_CHECK` window carries `_NET_WM_NAME` and no `WM_CLASS`, so the class
        line is empty rather than `N/A` -- one of the shapes `wmctrl -m` can print, and the one this desktop
        produces [M recon2/openbox.md §2]."""
        ours = self.ours("-m", "wwmctl", "-m")
        theirs = self.original("wmctrl", "-m")
        self.assertEqual(ours.returncode, 0, ours.stderr)
        self.assertEqual(ours.stdout, theirs.stdout)
        self.assertTrue(ours.stdout.startswith("Name: Openbox\nClass: \n"), repr(ours.stdout))

    def test_an_openbox_process_is_a_session_leader(self):
        """The regression this closes: with no openbox, lxqt-session, lxsession or labwc in
        `_SESSION_LEADERS`, `_shell_environ()` had nothing to read on a Lubuntu box and every tool run from
        outside the session lost the X cookie [M recon2/openbox.md §2].

        The scan is narrowed to the rig's own openbox: this box may well be running one of its own (and a
        CI container may run none), and the claim is about a process whose `$DISPLAY` is known."""
        env = self.narrowed_shell_environ()
        self.assertEqual(env.get("DISPLAY"), self.rig.display)

    def test_without_the_name_the_same_process_is_invisible(self):
        """The other half, so the test above cannot pass on some other leader: the same live openbox, with
        its rank removed, answers nothing at all."""
        ranks = {k: v for k, v in session._LEADER_RANK.items() if not k.startswith("openbox")}
        with mock.patch.object(session, "_LEADER_RANK", ranks):
            self.assertEqual(self.narrowed_shell_environ(), {})

    def narrowed_shell_environ(self):
        real_listdir = session.os.listdir

        def only_the_rig(path):
            if path == "/proc":
                return [str(self.rig.wm.pid)]
            return real_listdir(path)

        with mock.patch.object(session.os, "listdir", only_the_rig):
            return session._shell_environ(os.getuid())


class SessionCookieUid(unittest.TestCase):
    """The other half of the same report, hermetic: `x11_mini._session_xauthority()` never searches root's.

    §2d of recon2/openbox.md measured this from `ssh root@` on the live SDDM+LXQt VM: `session_uid()`
    answers 0 there, because /run/user/0 is a real runtime directory and the first candidate, and the cookie
    search then goes to root's own environment and /root/.Xauthority -- neither of which has anything to do
    with the graphical session, whose cookie SDDM wrote to /tmp/xauth_<random> under uid 1000.
    `passthrough.target_uid()` has skipped uid 0 for the same reason for as long as it has existed.

    No X server and no openbox here: the claim is which uid is handed to `session.find_xauthority()`, so
    that is the only thing mocked and the only thing read."""

    def calls(self, session_uid, candidates):
        """`find_xauthority`'s argument for one (session_uid, runtime-dir candidates) pair."""
        seen = []
        with mock.patch.object(session, "session_uid", lambda: session_uid), \
                mock.patch.object(session, "runtime_dir_candidates", lambda: candidates), \
                mock.patch.object(session, "find_xauthority",
                                  lambda uid=None: seen.append(uid) or "/tmp/cookie"):
            self.assertEqual(x11_mini._session_xauthority(), "/tmp/cookie")
        self.assertEqual(len(seen), 1, seen)
        return seen[0]

    def test_root_is_skipped_for_the_real_users_runtime_dir(self):
        """`ssh root@box`: /run/user/0 is first and 1000 is the session."""
        self.assertEqual(self.calls(0, [(0, "/run/user/0"), (1000, "/run/user/1000")]), 1000)

    def test_a_box_with_only_root_falls_back_to_what_it_always_did(self):
        """Nothing is invented when there is no other candidate: `find_xauthority(None)` is the old call,
        which re-asks `session_uid()` itself."""
        self.assertIsNone(self.calls(0, [(0, "/run/user/0")]))

    def test_an_ordinary_uid_is_passed_straight_through(self):
        """The control: the skip is about 0 and nothing else -- a normal login must not be re-resolved."""
        self.assertEqual(self.calls(1000, [(1000, "/run/user/1000")]), 1000)

    def test_a_raising_scan_is_no_cookie_rather_than_a_traceback(self):
        """This runs inside connection setup on every tool; a broken /run/user must not kill the process."""
        with mock.patch.object(session, "session_uid", side_effect=OSError("no /run/user")):
            self.assertIsNone(x11_mini._session_xauthority())


if __name__ == "__main__":
    unittest.main()
