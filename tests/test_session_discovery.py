#!/usr/bin/env python3
"""Session discovery under sudo / `ssh root@box`: which X socket and which
Wayland socket the two finders answer with.

`w11common/session.py` and `w11common/passthrough.py` answer different questions
(who the compositor is, versus what to hand the original tool) and each has
its own copy of the search. This file pins the two rules they must agree on,
both over temporary trees so the answers do not depend on the box the suite
runs on:

* the *session user's* X socket beats a root-owned one (SDDM leaves its
  greeter's Xorg on the lower number, so "lowest wins" hands out a DISPLAY
  the session's cookie cannot open);
* `$WAYLAND_DISPLAY` names the socket by itself -- it does not need
  `$XDG_RUNTIME_DIR` beside it, which under `sudo` is root's own dir or
  unset, and which `wxrandr -d wayland-1` never sets at all.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# and the tests directory, so `import support` resolves under every
# invocation form -- `python3 -m unittest tests/<file>.py` puts only the
# repository root on sys.path (SuiteGuard in tests/test_passthrough.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common import passthrough
from support import env
from w11common import session
from w11common.errors import CmdError
from wxrandr import cli as wxrandr_cli

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["W11_PASSTHROUGH"] = "never"


class Tree(unittest.TestCase):
    """A temporary /run/user and /tmp/.X11-unix, wired into both modules."""

    #: the session user, and a uid that is nobody in this tree
    UID = 1000
    OTHER = 125

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11_sessdisc_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the finders read the environment first: start from a session that
        # says nothing, so a developer's own DISPLAY cannot answer for them
        clear = env(DISPLAY=None, XAUTHORITY=None, WAYLAND_DISPLAY=None,
                    XDG_RUNTIME_DIR=None, SUDO_UID=None, PKEXEC_UID=None,
                    DBUS_SESSION_BUS_ADDRESS=None)
        clear.__enter__()
        self.addCleanup(clear.__exit__, None, None, None)
        self.runuser = self.mkdir("run", "user")
        self.x11 = self.mkdir("x11")
        self.owners = {}
        for mod, name in ((session, "RUN_USER_DIR"),
                          (passthrough, "_RUN_USER_DIR")):
            self.patch(mod, name, self.runuser)
        for mod, name in ((session, "X11_SOCKET_DIR"),
                          (passthrough, "_X11_SOCK_DIR")):
            self.patch(mod, name, self.x11)
        # logind and the session leaders are the *other* sources both finders
        # consult; silence them so the socket rule is what is under test
        self.patch(passthrough, "_LOGIND_DIR", self.mkdir("logind"))
        self.patch(session, "_shell_environ", lambda uid: {})
        for mod in (session, passthrough):
            self.patch(mod, "_owner",
                       lambda p, _m=mod: self.owners.get(os.path.realpath(p)))
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def touch(self, path, uid):
        with open(path, "w"):
            pass
        self.owners[os.path.realpath(path)] = uid
        return path

    def xsock(self, num, uid):
        """An X server socket owned by `uid` (only existence + owner count)."""
        return self.touch(os.path.join(self.x11, "X%d" % num), uid)

    def rtdir(self, uid):
        d = self.mkdir("run", "user", str(uid))
        self.owners[os.path.realpath(d)] = uid
        return d

    def wsock(self, uid, name="wayland-0"):
        return self.touch(os.path.join(self.rtdir(uid), name), uid)


class HyprSocket(Tree):
    """`find_hypr_socket()`: the instance directory a live Hyprland is behind.

    Hyprland writes `$XDG_RUNTIME_DIR/hypr/<instance signature>/.socket.sock` (requests) and `.socket2.sock`
    (events), and the live instance directory also holds `hyprland.lock`. Stale instance directories from
    earlier logins accumulate beside it -- five of them after five restarts -- so "the one with the lock" is
    the rule and not "the first one listed" [M recon2/hyprland.md §1, socket at
    /run/user/1000/hypr/dd220efe..._1788885217_674420693/.socket.sock]."""

    SIGS = ("aaaa_1_1", "bbbb_2_2", "cccc_3_3", "dddd_4_4", "eeee_5_5")

    def instance(self, uid, sig, lock=False, sock=True) -> str:
        d = self.mkdir("run", "user", str(uid), "hypr", sig)
        self.owners[os.path.realpath(os.path.dirname(os.path.dirname(d)))] = uid
        p = os.path.join(d, ".socket.sock")
        if sock:
            self.touch(p, uid)
        self.touch(os.path.join(d, ".socket2.sock"), uid)
        if lock:
            self.touch(os.path.join(d, "hyprland.lock"), uid)
        return p

    def test_the_instance_holding_the_lock_wins_out_of_five(self):
        self.rtdir(self.UID)
        want = None
        for sig in self.SIGS:
            p = self.instance(self.UID, sig, lock=(sig == "cccc_3_3"))
            if sig == "cccc_3_3":
                want = p
        self.assertEqual(session.find_hypr_socket(), want)

    def test_the_signature_in_the_environment_is_honoured(self):
        self.rtdir(self.UID)
        for sig in self.SIGS:
            self.instance(self.UID, sig, lock=(sig == "cccc_3_3"))
        want = os.path.join(self.runuser, str(self.UID), "hypr", "eeee_5_5", ".socket.sock")
        with env(HYPRLAND_INSTANCE_SIGNATURE="eeee_5_5"):
            self.assertEqual(session.find_hypr_socket(), want)
        # ...and a signature naming an instance that is gone falls through to
        # the lock rather than answering with a path that is not there
        with env(HYPRLAND_INSTANCE_SIGNATURE="ffff_6_6"):
            self.assertEqual(os.path.basename(os.path.dirname(session.find_hypr_socket())),
                             "cccc_3_3")

    def test_a_directory_with_no_lock_anywhere_still_answers(self):
        """`hyprland.lock` is how the live instance is told from the stale ones, not how a Hyprland is
        recognised: a session whose lock has been cleaned up still has one socket that works."""
        self.rtdir(self.UID)
        want = self.instance(self.UID, "aaaa_1_1")
        self.assertEqual(session.find_hypr_socket(), want)

    def test_a_socket_somebody_else_owns_is_refused(self):
        """An instance directory another user planted in this session's runtime dir: the socket would be
        theirs, and every `dispatch` we sent -- and every window title that came back -- would be as well."""
        self.rtdir(self.UID)
        want = self.instance(self.UID, "aaaa_1_1", lock=True)
        planted = self.instance(self.UID, "zzzz_9_9")
        self.owners[os.path.realpath(planted)] = self.OTHER
        self.assertEqual(session.find_hypr_socket(), want)
        self.owners[os.path.realpath(want)] = self.OTHER
        self.assertIsNone(session.find_hypr_socket())

    def test_the_session_users_instance_comes_before_another_users(self):
        """Both directories are candidates -- `ssh root@box` sees every /run/user -- and the ordering that
        puts the graphical session's first is what decides, exactly as it does for the Wayland socket."""
        self.wsock(self.UID)
        self.instance(self.OTHER, "zzzz_9_9", lock=True)
        want = self.instance(self.UID, "aaaa_1_1", lock=True)
        self.assertEqual(session.find_hypr_socket(), want)

    def test_no_hypr_directory_at_all(self):
        self.rtdir(self.UID)
        self.assertIsNone(session.find_hypr_socket())


class WayfireSocket(Tree):
    """`find_wayfire_socket()`: `$WAYFIRE_SOCKET`, `$_WAYFIRE_SOCKET`, the runtime-dir scan, then /tmp.

    The order is Wayfire's own (`strings libipc.so`: `_WAYFIRE_SOCKET`, `wayfire-`, `/tmp/wayfire-`), and the
    path it chose is exported to its children as `$WAYFIRE_SOCKET`. `plugins/ipc/ipc.cpp` writes two names:
    the runtime-dir one has no pid at all (`wayfire-<display>-.socket`, measured as
    `/tmp/wfrt1/wayfire-wayland-1-.socket` with XDG_RUNTIME_DIR=/tmp/wfrt1) and the /tmp one has one
    (`wayfire-<display>-<pid>.socket`), so the match is on the prefix and the suffix and never on the shape
    between them [M recon2/wayfire.md §1.2]."""

    #: the name a live Wayfire 0.10.0 wrote
    NAME = "wayfire-wayland-1-.socket"

    def setUp(self):
        super().setUp()
        self.tmpdir = self.mkdir("tmp")
        self.patch(session, "TMP_DIR", self.tmpdir)

    def test_both_variables_win_when_the_path_is_really_there(self):
        self.rtdir(self.UID)
        named = self.touch(os.path.join(self.tmp, "named.socket"), self.UID)
        for var in ("WAYFIRE_SOCKET", "_WAYFIRE_SOCKET"):
            with env(**{var: named}):
                self.assertEqual(session.find_wayfire_socket(), named)

    def test_a_stale_variable_falls_through_to_the_scan(self):
        want = self.touch(os.path.join(self.rtdir(self.UID), self.NAME), self.UID)
        with env(WAYFIRE_SOCKET=os.path.join(self.tmp, "gone.socket")):
            self.assertEqual(session.find_wayfire_socket(), want)

    def test_the_runtime_directory_scan_finds_the_recorded_name(self):
        want = self.touch(os.path.join(self.rtdir(self.UID), self.NAME), self.UID)
        self.assertEqual(session.find_wayfire_socket(), want)

    def test_the_tmp_fallback_is_owner_checked(self):
        """/tmp is world-writable and this is the branch Wayfire takes when there is no runtime directory at
        all, so a socket of that name belonging to somebody else must not be answered with: every IPC request
        -- `configure-view`, and the text of a `stipc/run` -- would go to whoever made it."""
        self.rtdir(self.UID)
        mine = self.touch(os.path.join(self.tmpdir, self.NAME), self.UID)
        self.assertEqual(session.find_wayfire_socket(), mine)
        self.owners[os.path.realpath(mine)] = self.OTHER
        self.assertIsNone(session.find_wayfire_socket())

    def test_the_runtime_directory_beats_tmp(self):
        self.touch(os.path.join(self.tmpdir, self.NAME), self.UID)
        want = self.touch(os.path.join(self.rtdir(self.UID), self.NAME), self.UID)
        self.assertEqual(session.find_wayfire_socket(), want)

    def test_nothing_anywhere(self):
        self.rtdir(self.UID)
        self.assertIsNone(session.find_wayfire_socket())


class XSocketOwnership(Tree):
    """SDDM's greeter Xorg is root's and sits on :0; the KDE session's
    Xwayland is the user's and sits on :1."""

    def sddm(self):
        self.xsock(0, 0)                 # the greeter's, root-owned
        self.xsock(1, self.UID)          # the session's Xwayland

    def test_passthrough_prefers_the_session_users_socket(self):
        self.sddm()
        # the bug: ":0", a display the session's cookie cannot open
        self.assertEqual(passthrough.find_x_display({}, self.UID), ":1")

    def test_session_prefers_the_session_users_socket(self):
        self.sddm()
        self.assertEqual(session.find_x_display(self.UID), ":1")

    def test_both_finders_agree(self):
        self.sddm()
        self.assertEqual(passthrough.find_x_display({}, self.UID),
                         session.find_x_display(self.UID))

    def test_sudo_warandr_gets_the_session_display(self):
        """The reported trigger: `sudo warandr` on KDE+SDDM. The child
        environment repair is what the real xrandr is run with."""
        self.sddm()
        self.rtdir(self.UID)
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            e = passthrough.repair_x_env({"SUDO_UID": str(self.UID)})
        self.assertEqual(e["DISPLAY"], ":1")

    def test_a_plain_x11_session_still_gets_roots_xorg(self):
        """A real Xorg *is* root's: with no socket of the user's own, the
        root-owned one is still the answer, not None."""
        self.xsock(0, 0)
        self.assertEqual(passthrough.find_x_display({}, self.UID), ":0")
        self.assertEqual(session.find_x_display(self.UID), ":0")

    def test_another_users_socket_is_never_the_answer(self):
        """Neither ours nor root's: a second seat's server, or a planted
        one. `mine or root` must not quietly widen to "anybody's"."""
        self.xsock(3, self.OTHER)
        self.assertIsNone(passthrough.find_x_display({}, self.UID))
        self.assertIsNone(session.find_x_display(self.UID))

    def test_unknown_uid_keeps_the_lowest_socket(self):
        """With no target uid there is nothing to prefer, and the old
        answer stands (test_passthrough pins this one too)."""
        self.sddm()
        self.assertEqual(passthrough.find_x_display({}, None), ":0")


class WaylandDisplayNamesTheSocket(Tree):
    """`sudo wxrandr -d wayland-1`: WAYLAND_DISPLAY is set, XDG_RUNTIME_DIR
    is root's own (or unset), and the session runs two compositors."""

    def two_sockets(self):
        self.wsock(self.UID, "wayland-0")
        self.wsock(self.UID, "wayland-1")
        self.rtdir(0)                    # root's own runtime dir, empty

    def test_named_display_wins_under_sudo(self):
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=os.path.join(self.runuser, "0"),
                 WAYLAND_DISPLAY="wayland-1", SUDO_UID=str(self.UID),
                 PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        # the bug: the scan answered with wayland-0, whatever -d said
        self.assertEqual(os.path.basename(hit[2]), "wayland-1")
        self.assertEqual(hit[1], os.path.join(self.runuser, str(self.UID)))

    def test_named_display_wins_with_no_runtime_dir_at_all(self):
        """`ssh root@box` / cron: XDG_RUNTIME_DIR is not in the environment."""
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY="wayland-1",
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(os.path.basename(hit[2]), "wayland-1")

    def test_absolute_display_needs_no_runtime_dir(self):
        self.two_sockets()
        sock = os.path.join(self.runuser, str(self.UID), "wayland-1")
        with env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY=sock,
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(hit[2], sock)

    def test_in_session_runtime_dir_still_wins(self):
        """Unchanged for the normal case: the name is resolved against
        $XDG_RUNTIME_DIR before anything is scanned."""
        self.wsock(self.UID, "wayland-0")
        other = self.rtdir(self.OTHER)
        self.touch(os.path.join(other, "wayland-0"), self.OTHER)
        mine = os.path.join(self.runuser, str(self.UID))
        with env(XDG_RUNTIME_DIR=mine, WAYLAND_DISPLAY="wayland-0",
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(hit[2], os.path.join(mine, "wayland-0"))
        self.assertEqual(hit[1], mine)

    def test_a_stale_name_still_falls_through_to_the_scan(self):
        """A WAYLAND_DISPLAY naming a socket that exists nowhere is not an
        answer and must not become one: the scan still runs."""
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY="wayland-9",
                 SUDO_UID=None, PKEXEC_UID=None):
            hit = session.find_wayland_socket()
        self.assertEqual(os.path.basename(hit[2]), "wayland-0")

    def test_wxrandr_d_option_reaches_the_finder(self):
        """End to end over the option itself: `-d wayland-1` is what sets
        WAYLAND_DISPLAY, and the finder is what the backends then use."""
        self.two_sockets()
        with env(XDG_RUNTIME_DIR=os.path.join(self.runuser, "0"),
                 WAYLAND_DISPLAY=None, SUDO_UID=str(self.UID),
                 PKEXEC_UID=None):
            wxrandr_cli.parse(["-d", "wayland-1", "--query"])
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")
            hit = session.find_wayland_socket()
        self.assertEqual(os.path.basename(hit[2]), "wayland-1")


class RuntimeDirOfAnotherUser(Tree):
    """`runtime_dir(uid=...)`: the seated user's session directory, asked for by
    a process that is not that user -- root, and only root (xw11/wrap.py's
    `_seated_uid`).

    Measured before it existed (goal2/recon/gaps.md §3d): with $XDG_RUNTIME_DIR
    unset `runtime_dir()` answers `/tmp/wdotool-1000` for uid 1000 and
    `/tmp/wdotool-0` for root, and pam_systemd gives an `ssh root@box` login its
    own empty `/run/user/0` -- so root's answer never had the session's proxy,
    socket or state files in it. The keyword is the whole change: every call
    without it is the answer it always was."""

    def setUp(self):
        super().setUp()
        # never the real /tmp/wdotool-<uid>: this class asks about uids it does
        # not own, and a directory created for one of them would outlive the run
        self.fallback = os.path.join(self.tmp, "wdotool-%d")
        self.patch(session, "FALLBACK_RUNTIME_DIR", self.fallback)

    def as_root(self):
        return mock.patch.object(session.os, "getuid", lambda: 0)

    @unittest.skipIf(os.getuid() == 0, "the seated user has to be somebody root is not")
    def test_the_seated_users_dir_is_the_one_holding_the_wayland_socket(self):
        """`runtime_dir_candidates()` is the helper that already ranks them, and
        the ranking -- not the uid filter -- is what picks the graphical
        session out of two directories that both belong to the seated user.

        The pair is the one a real box has: `$XDG_RUNTIME_DIR` (which enters the
        candidates first, with its `os.stat().st_uid`) pointing at a directory
        with no compositor socket in it, against `/run/user/<uid>` holding
        `wayland-0`. `runtime_dir_candidates()` sorts `_has_wayland_socket`
        first, so the socket one wins; with that sort off the environment's
        answers, and root talks to no proxy.

        The seated uid here is the runner's, because the XDG candidate's uid
        comes from a real `os.stat()` and cannot be invented; the greeter's
        socketless `/run/user/125` is in the tree too, for the uid filter."""
        self.rtdir(self.OTHER)
        seated = os.getuid()
        seat = os.path.dirname(self.wsock(seated))
        elsewhere = self.mkdir("xdg-no-socket")
        with env(XDG_RUNTIME_DIR=elsewhere), self.as_root():
            self.assertEqual(session.runtime_dir_candidates()[0][1], seat)
            self.assertEqual(session.runtime_dir(uid=seated), seat)
            self.assertNotEqual(session.runtime_dir(uid=self.OTHER), seat)

    def test_a_session_with_no_run_user_dir_is_the_private_tmp_name(self):
        """The /tmp fallback, uid'd: `wdotool` under `su -`, cron and a bare
        container writes its socket there (the docstring above it), so that is
        where root looks for that user's files too."""
        with self.as_root():
            self.assertEqual(session.runtime_dir(uid=self.UID),
                             self.fallback % self.UID)

    def test_it_creates_nothing_for_a_user_it_is_not(self):
        """A root-owned `/tmp/wdotool-1000` is a directory uid 1000's own
        `runtime_dir()` then refuses for the rest of the box's uptime -- it
        checks the owner after creating it, because an attacker may have got
        there first. So the uid'd lookup only ever reads."""
        with self.as_root():
            session.runtime_dir(uid=self.UID)
        self.assertFalse(os.path.exists(self.fallback % self.UID))

    def test_a_tmp_dir_that_is_not_that_users_is_refused(self):
        """/tmp is world-writable: anyone may create `/tmp/wdotool-1000` before
        the user does. Reading a display file or a state file out of it would be
        reading what that anyone wrote, so it is a CmdError and not an answer --
        the same rule `runtime_dir()` applies to our own.

        `OTHER` and not `UID` because the directory is really made and really
        owned by whoever runs the suite (often uid 1000 itself, which is UID):
        the refusal under test is an ownership mismatch and has to be a real
        one."""
        planted = self.fallback % self.OTHER
        os.makedirs(planted, 0o700)          # ours, which uid 125's it is not
        with self.as_root(), self.assertRaises(CmdError) as caught:
            session.runtime_dir(uid=self.OTHER)
        self.assertIn(planted, str(caught.exception))

    def test_our_own_uid_and_no_uid_are_the_same_answer_as_ever(self):
        """Item 4 of the batch: every default path stays byte-identical. A
        caller inside the session passes no uid, and one that passes its own
        must not be sent down the foreign path either."""
        mine = os.path.join(self.tmp, "mine")
        os.makedirs(mine, 0o700)
        with env(XDG_RUNTIME_DIR=mine):
            self.assertEqual(session.runtime_dir(), mine)
            self.assertEqual(session.runtime_dir(uid=os.getuid()), mine)

if __name__ == "__main__":
    unittest.main()
