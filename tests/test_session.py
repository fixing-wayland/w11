#!/usr/bin/env python3
"""w11common/session.py over a temporary /run/user.

Every tool starts by answering "which graphical session am I aimed at?", and
session.py answers it by walking /run/user. That walk was only ever exercised
sideways -- two cases inside test_backend_gnome, the X half in
test_session_discovery, and the rest through whatever happened to be under
the real /run/user on the machine running the suite, which is exactly the
thing a test must not depend on.

So the tree here is built by hand: `session.RUN_USER_DIR` is pointed at a
temporary directory whose subdirectory *names* are the uids, which is where
session.py reads them from, and each case puts sockets in it and asks what
comes back. The four questions are deliberately separate -- the Wayland
socket, the sway IPC socket, the user bus and the compositor's bus are four
different answers on `ssh root@box`, and treating them as one is the bug
that keeps coming back.
"""

import ast
import os
import shutil
import stat
import subprocess
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

from w11common import session
from w11common.errors import CmdError
import support
from support import env

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class Tree(unittest.TestCase):
    """A temporary /run/user, and an environment that says nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11_session_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_user = os.path.join(self.tmp, "run-user")
        os.mkdir(self.run_user)
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = self.run_user
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)
        # a developer's own session must not answer for any of this
        ctx = env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY=None, SWAYSOCK=None,
                  I3SOCK=None, DBUS_SESSION_BUS_ADDRESS=None, SUDO_UID=None,
                  PKEXEC_UID=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    # -- building the tree
    def rtdir(self, uid) -> str:
        d = os.path.join(self.run_user, str(uid))
        os.makedirs(d, exist_ok=True)
        return d

    def sock(self, uid, name) -> str:
        p = os.path.join(self.rtdir(uid), name)
        open(p, "w").close()
        return p

    def dirs(self):
        return [d for _u, d in session.runtime_dir_candidates()]

    def uids(self):
        return [u for u, _d in session.runtime_dir_candidates()]


class Candidates(Tree):
    def test_a_wayland_socket_moves_a_directory_to_the_front(self):
        self.rtdir(0)
        self.rtdir(1000)
        self.sock(1001, "wayland-0")
        self.assertEqual(self.uids(), [1001, 1000, 0])

    def test_a_lock_file_is_not_a_wayland_socket(self):
        self.sock(1000, "wayland-1")
        self.sock(1001, "wayland-1.lock")
        self.assertEqual(self.uids()[0], 1000)

    def test_real_users_before_system_uids_and_then_by_number(self):
        for uid in (0, 1000, 1001, 42):
            self.rtdir(uid)
        self.assertEqual(self.uids(), [1000, 1001, 0, 42])

    def test_the_sudo_invoker_comes_first_of_all(self):
        for uid in (0, 1000, 1001):
            self.rtdir(uid)
        with env(SUDO_UID="1001"):
            self.assertEqual(self.uids(), [1001, 1000, 0])
        # PKEXEC_UID is read the same way, and a junk value is ignored
        with env(PKEXEC_UID="1001"):
            self.assertEqual(self.uids(), [1001, 1000, 0])
        with env(SUDO_UID="not-a-number"):
            self.assertEqual(self.uids(), [1000, 1001, 0])

    def test_xdg_runtime_dir_leads_its_own_group(self):
        mine = self.rtdir(1000)
        self.sock(1001, "wayland-0")
        with env(XDG_RUNTIME_DIR=mine):
            # ...but the group with a Wayland socket still comes first: an
            # XDG_RUNTIME_DIR with no compositor in it is what `sudo` and
            # `ssh root@` both hand us
            self.assertEqual(self.dirs()[0], os.path.join(self.run_user, "1001"))
        self.sock(1000, "wayland-1")
        with env(XDG_RUNTIME_DIR=mine):
            self.assertEqual(self.dirs()[0], mine)

    def test_names_that_are_not_uids_are_skipped(self):
        os.makedirs(os.path.join(self.run_user, "systemd"))
        self.rtdir(1000)
        self.assertEqual(self.uids(), [1000])

    def test_no_run_user_at_all_is_not_an_error(self):
        shutil.rmtree(self.run_user)
        self.assertEqual(session.runtime_dir_candidates(), [])
        self.assertIsNone(session.find_wayland_socket())
        self.assertIsNone(session.session_uid())


class WaylandSocket(Tree):
    def test_the_scan_answers_with_the_directory_and_the_owner(self):
        p = self.sock(1001, "wayland-1")
        self.assertEqual(session.find_wayland_socket(),
                         (1001, os.path.dirname(p), p))

    def test_the_lowest_name_in_a_directory_wins(self):
        self.sock(1000, "wayland-3")
        first = self.sock(1000, "wayland-0")
        self.assertEqual(session.find_wayland_socket()[2], first)

    def test_session_uid_is_the_socket_owner_not_the_first_candidate(self):
        self.rtdir(1000)            # a real user with no compositor
        self.sock(1001, "wayland-0")
        self.assertEqual(session.session_uid(), 1001)

    def test_session_uid_falls_back_to_the_candidate_order(self):
        self.rtdir(0)
        self.rtdir(1000)
        self.assertEqual(session.session_uid(), 1000)


class SwaySocket(Tree):
    """The sway/i3 IPC socket, over the names a live compositor really writes.

    i3 was unfindable without `$I3SOCK`, and i3 puts `$I3SOCK` only into the environment of processes it starts
    itself -- not into its own /proc/<pid>/environ, so nothing a root shell or cron runs inherits it. The old
    scan looked for `i3-ipc.*.sock`, which no i3 and no sway has ever written [M recon2/i3.md §1: with
    XDG_RUNTIME_DIR=/tmp/rti3 and `i3 --get-socketpath` answering /tmp/rti3/i3/ipc-socket.65857,
    find_sway_socket() returned None]."""

    def setUp(self):
        super().setUp()
        # the /tmp fallback is a real glob over a world-writable directory; point
        # it at a private one so a stray i3 of this user cannot answer instead
        self.tmpdir = os.path.join(self.tmp, "tmp")
        os.mkdir(self.tmpdir)
        old = session.TMP_DIR
        session.TMP_DIR = self.tmpdir
        self.addCleanup(setattr, session, "TMP_DIR", old)

    def i3_runtime_socket(self, uid, pid=4242) -> str:
        """`<runtime dir>/i3/ipc-socket.<pid>` -- a SUBDIRECTORY, which is the level the old scan never
        entered."""
        d = os.path.join(self.rtdir(uid), "i3")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ipc-socket.%d" % pid)
        open(p, "w").close()
        return p

    def i3_tmp_socket(self, user, suffix="abc", pid=4242) -> str:
        """`/tmp/i3-<user>.XXXXXX/ipc-socket.<pid>` -- what i3 writes with no XDG_RUNTIME_DIR at all."""
        d = os.path.join(self.tmpdir, "i3-%s.%s" % (user, suffix))
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ipc-socket.%d" % pid)
        open(p, "w").close()
        return p

    def test_the_environment_wins_when_the_socket_is_really_there(self):
        named = os.path.join(self.tmp, "named.sock")
        open(named, "w").close()
        self.sock(1000, "sway-ipc.1000.99.sock")
        with env(SWAYSOCK=named):
            self.assertEqual(session.find_sway_socket(), named)
        with env(I3SOCK=named):
            self.assertEqual(session.find_sway_socket(), named)

    def test_a_stale_name_falls_through_to_the_scan(self):
        p = self.sock(1000, "sway-ipc.1000.99.sock")
        with env(SWAYSOCK=os.path.join(self.tmp, "gone.sock")):
            self.assertEqual(session.find_sway_socket(), p)

    def test_a_stale_i3sock_falls_through_to_the_scan(self):
        """`$I3SOCK` survives an i3 restart in whatever shell exported it; the socket it names does not."""
        p = self.i3_runtime_socket(1000)
        with env(I3SOCK=os.path.join(self.tmp, "gone.sock")):
            self.assertEqual(session.find_sway_socket(), p)

    def test_i3s_runtime_subdirectory_is_found(self):
        p = self.i3_runtime_socket(1000)
        self.assertEqual(session.find_sway_socket(), p)

    def test_i3s_tmp_directory_is_found_and_owner_checked(self):
        """/tmp is world-writable: anyone may create `/tmp/i3-<somebody else>.aaaaaa/ipc-socket.1` and answer
        for a compositor that is not there, and every request -- the text of `type` included -- would be
        delivered to them."""
        import pwd as _pwd
        me = _pwd.getpwuid(os.getuid())
        self.rtdir(me.pw_uid)
        p = self.i3_tmp_socket(me.pw_name)
        self.assertEqual(session.find_sway_socket(), p)

        owners = {os.path.realpath(p): me.pw_uid + 12345}
        with mock.patch.object(session, "_owner",
                               lambda path: owners.get(os.path.realpath(path),
                                                       os.stat(path).st_uid)):
            self.assertIsNone(session.find_sway_socket())

    def test_the_runtime_directory_beats_tmp(self):
        """i3 writes one or the other, never both; a box that has been run both ways keeps the stale one, and
        the runtime directory is the live session's."""
        import pwd as _pwd
        me = _pwd.getpwuid(os.getuid())
        self.i3_tmp_socket(me.pw_name)
        p = self.i3_runtime_socket(me.pw_uid)
        self.assertEqual(session.find_sway_socket(), p)

    def test_the_dead_i3_ipc_pattern_is_gone(self):
        """`i3-ipc.<uid>.<pid>.sock` was the pattern this scanned for and is a name nothing writes. Keeping it
        is not harmless: it is the only thing a file of that name in a runtime directory can be, and answering
        with one would send i3-ipc frames to whatever made it."""
        self.sock(1000, "i3-ipc.1000.7.sock")
        self.assertIsNone(session.find_sway_socket())

    def test_sway_still_wins_over_an_i3_subdirectory(self):
        p = self.sock(1000, "sway-ipc.1000.99.sock")
        self.i3_runtime_socket(1000)
        self.assertEqual(session.find_sway_socket(), p)

    def test_nothing_anywhere(self):
        self.rtdir(1000)
        self.assertIsNone(session.find_sway_socket())


class Buses(Tree):
    def test_a_bus_beside_a_compositor_beats_an_environment_bus_without_one(self):
        """`ssh root@box` gets DBUS_SESSION_BUS_ADDRESS=/run/user/0/bus from
        pam_systemd -- a real bus, with no compositor on it."""
        roots = self.sock(0, "bus")
        self.sock(1000, "wayland-0")
        session_bus = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + roots):
            self.assertEqual(session.find_user_bus(),
                             (1000, "unix:path=" + session_bus))

    def test_the_environment_bus_wins_when_it_is_in_the_session(self):
        self.sock(1000, "wayland-0")
        mine = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + mine):
            self.assertEqual(session.find_user_bus()[1], "unix:path=" + mine)

    def test_with_no_compositor_anywhere_the_environment_still_wins(self):
        roots = self.sock(0, "bus")
        self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + roots):
            self.assertEqual(session.find_user_bus()[1], "unix:path=" + roots)

    def test_an_environment_bus_that_does_not_exist_is_ignored(self):
        p = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + p + "-gone"):
            self.assertEqual(session.find_user_bus(), (1000, "unix:path=" + p))

    def test_the_session_bus_is_anchored_on_the_wayland_socket(self):
        self.sock(0, "bus")
        self.sock(1000, "wayland-0")
        compositor_bus = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path="
                 + os.path.join(self.run_user, "0", "bus")):
            self.assertEqual(session.find_session_bus(),
                             (1000, "unix:path=" + compositor_bus))

    def test_the_session_bus_degrades_to_the_user_bus_with_no_compositor(self):
        p = self.sock(1000, "bus")
        self.assertEqual(session.find_session_bus(), (1000, "unix:path=" + p))

    def test_no_bus_anywhere(self):
        self.sock(1000, "wayland-0")
        self.assertIsNone(session.find_user_bus())


class RuntimeDir(Tree):
    """runtime_dir(): where a socket, a lock and a state file may live."""

    def setUp(self):
        super().setUp()
        self.fallback = os.path.join(self.tmp, "wdotool-%d")
        old = session.FALLBACK_RUNTIME_DIR
        session.FALLBACK_RUNTIME_DIR = self.fallback
        self.addCleanup(setattr, session, "FALLBACK_RUNTIME_DIR", old)

    def mine(self):
        return self.fallback % os.getuid()

    def test_xdg_runtime_dir_is_taken_as_it_is(self):
        d = self.rtdir(1000)
        with env(XDG_RUNTIME_DIR=d):
            self.assertEqual(session.runtime_dir(), d)

    def test_a_name_that_is_not_a_directory_falls_back(self):
        p = os.path.join(self.tmp, "not-a-dir")
        open(p, "w").close()
        with env(XDG_RUNTIME_DIR=p):
            self.assertEqual(session.runtime_dir(), self.mine())

    def test_the_fallback_is_private_and_ours(self):
        d = session.runtime_dir()
        self.assertEqual(d, self.mine())
        st = os.lstat(d)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_mode & 0o077, 0)

    def test_an_existing_private_directory_is_reused(self):
        os.mkdir(self.mine(), 0o700)
        self.assertEqual(session.runtime_dir(), self.mine())

    def test_a_directory_anyone_could_enter_is_refused(self):
        os.mkdir(self.mine(), 0o755)
        with self.assertRaises(CmdError) as cm:
            session.runtime_dir()
        self.assertIn("not a private directory", str(cm.exception))

    def test_a_symlink_planted_in_its_place_is_refused(self):
        os.symlink(self.tmp, self.mine())
        with self.assertRaises(CmdError) as cm:
            session.runtime_dir()
        self.assertIn("not a private directory", str(cm.exception))

    def test_a_directory_that_cannot_be_made_says_so(self):
        session.FALLBACK_RUNTIME_DIR = os.path.join(self.tmp, "gone", "x-%d")
        with self.assertRaises(CmdError) as cm:
            session.runtime_dir()
        self.assertIn("cannot create", str(cm.exception))


# -- what w11common is allowed to depend on ------------------------------------

class Closure(unittest.TestCase):
    """w11common is the shared floor: standard library only, and nothing from
    the rest of this tree.

    It is not a style rule. `w11common/passthrough.py` runs BEFORE any tool
    decides anything -- it is what hands `xdotool` over to the real one on an
    X11 box -- and `w11common/session.py` is what a root shell uses to find
    the session at all. An import of `wdotool` or a third-party package here
    would make those two paths fail on exactly the machine that needs them:
    a `sudo` with no user site-packages, a cron job, a .deb whose payload is
    one directory, the `python3 -I -S` case below. The .deb ships w11common/
    as a plain directory beside the tools, and the tools import it by name."""

    PACKAGE = os.path.join(ROOT, "w11common")

    def modules(self):
        return sorted(n for n in os.listdir(self.PACKAGE) if n.endswith(".py"))

    def test_nothing_outside_the_standard_library_is_imported(self):
        self.maxDiff = None
        allowed = set(sys.stdlib_module_names) | {"w11common"}
        outside = []
        for name in self.modules():
            with open(os.path.join(self.PACKAGE, name)) as f:
                tree = ast.parse(f.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] not in allowed:
                            outside.append("%s: import %s" % (name, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    # a relative import would work here but not in the .deb's
                    # flat payload, where w11common is found by name on sys.path
                    if node.level:
                        outside.append("%s: relative import (level %d)" % (name, node.level))
                    elif node.module and node.module.split(".")[0] not in allowed:
                        outside.append("%s: from %s" % (name, node.module))
        self.assertEqual(outside, [])

    def test_it_imports_with_nothing_else_on_the_box(self):
        """`python3 -I -S` is the harshest form: no site-packages, no user
        site, no PYTHONPATH, no cwd -- and a temporary directory holding a
        copy of w11common/ and nothing else. If every module here imports
        under that, no machine's Python configuration can take the passthrough
        or the session scan away."""
        tmp = tempfile.mkdtemp(prefix="w11common-closure-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(self.PACKAGE, os.path.join(tmp, "w11common"))
        names = ", ".join("w11common." + n[:-3] for n in self.modules()
                          if n != "__init__.py")
        code = "import sys; sys.path.insert(0, %r); import %s; print(w11common.session.__file__)" % (tmp, names)
        p = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                           cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertTrue(p.stdout.strip().startswith(tmp),
                        "it imported the repository's copy, not the isolated one: %r" % p.stdout)


# -- the session leader whose environment is read -----------------------------

class Xauthority(Tree):
    """Where the X11 cookie is looked for once the session leader cannot be read.

    That is the `ssh root@box` / cron case the whole leader scan exists for, and on GNOME-on-Xorg it had a
    hole: GDM keeps the cookie at `<runtime dir>/gdm/Xauthority`, one directory below everything
    find_xauthority() globbed, so with no live readable gnome-shell it answered None although the file was
    there [M recon2/gnome-xorg.md: find_xauthority(1000) -> /tmp/gx-runtime-1000/gdm/Xauthority with the
    leader up, None with it gone]."""

    def setUp(self):
        super().setUp()
        ctx = env(XAUTHORITY=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        # "with no leader alive": the process scan is the other route to the
        # cookie and is not what this is about
        p = mock.patch.object(session, "_shell_environ", lambda uid: {})
        p.start()
        self.addCleanup(p.stop)

    def cookie(self, uid, *parts) -> str:
        d = self.rtdir(uid)
        if len(parts) > 1:
            d = os.path.join(d, *parts[:-1])
            os.makedirs(d, exist_ok=True)
        p = os.path.join(d, parts[-1])
        open(p, "w").close()
        return p

    def test_gdms_cookie_one_directory_down_is_found(self):
        self.sock(1000, "wayland-0")
        want = self.cookie(1000, "gdm", "Xauthority")
        self.assertEqual(session.find_xauthority(1000), want)

    def test_it_is_ranked_with_the_others_by_mtime(self):
        """The three globs share one newest-wins rule, and they have to: a box that has run both GDM and a
        Mutter session has both files, and the one written last is this session's."""
        self.sock(1000, "wayland-0")
        gdm = self.cookie(1000, "gdm", "Xauthority")
        mutter = self.cookie(1000, ".mutter-Xwaylandauth.QKQRV3")
        os.utime(gdm, (1000, 1000))
        os.utime(mutter, (2000, 2000))
        self.assertEqual(session.find_xauthority(1000), mutter)
        os.utime(gdm, (3000, 3000))
        self.assertEqual(session.find_xauthority(1000), gdm)

    def test_another_uids_runtime_directory_is_not_read(self):
        self.sock(1000, "wayland-0")
        self.cookie(1001, "gdm", "Xauthority")
        self.assertIsNone(session.find_xauthority(1000))


class SessionLeaderNames(unittest.TestCase):
    """The nine names appended to `_SESSION_LEADERS`, and the nixpkgs comm spelling.

    From `ssh root@box` with an empty environment all four tools failed on LXQt with "Authorization required,
    but no authorization protocol specified": SDDM 0.21 writes /tmp/xauth_<random>, and no LXQt process was in
    the tuple, so `_shell_environ()` never read the one environment that names it. Adding four names turned all
    four tools from broken to working in the same session [M recon2/openbox.md §2]; the same gap was measured
    for mate-session [M recon2/mate.md] and for i3 [M recon2/i3.md §2d].

    Unlike `SessionLeaders` above this does not skip wholesale when a real leader of this uid is running: it
    skips the one name that leader would outrank, and runs the rest. On the box this was written on the guest's
    own `openbox` (rank 14) is up, which would otherwise have silenced every case here."""

    def setUp(self):
        self.uid = os.geteuid()
        self.real_rank = self.best_real_rank()

    @staticmethod
    def rank(name: str) -> int:
        return session._SESSION_LEADERS.index(name)

    def best_real_rank(self) -> int:
        """The best `_SESSION_LEADERS` rank among processes of this uid that this test did not start. A
        stand-in ranked worse than that would lose to the box's own desktop and prove nothing."""
        best = len(session._SESSION_LEADERS)
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % entry) as f:
                    comm = f.read().strip()
                rank = session._LEADER_RANK.get(comm)
                if rank is None or rank >= best:
                    continue
                if os.stat("/proc/" + entry).st_uid != self.uid:
                    continue
            except OSError:
                continue
            best = rank
        return best

    def need(self, name):
        if self.rank(name) >= self.real_rank:
            self.skipTest("a real leader ranked at or above %s is running here" % name)

    def test_every_name_is_reachable_through_the_15_byte_comm(self):
        """comm truncates at 15 bytes, and the tuple carries one 16-byte name (`cinnamon-session`), so the
        match is on `name[:15]` and not on the name: an entry compared whole would silently never fire, which
        is exactly the failure `startlxqtwayland` was kept out of the tuple for."""
        for i, name in enumerate(session._SESSION_LEADERS):
            comm, wrapped = session._leader_comms(name)
            self.assertLessEqual(len(comm), 15, name)
            self.assertLessEqual(len(wrapped), 15, name)
            self.assertEqual(session._LEADER_RANK[comm], i, name)
            self.assertEqual(session._LEADER_RANK[wrapped], i, name)
        # every spelling is distinct, so no name is shadowed by another's
        self.assertEqual(len(session._LEADER_RANK),
                         2 * len(session._SESSION_LEADERS))
        self.assertEqual(session._SESSION_LEADERS[:8],
                         ("gnome-shell", "startplasma-x11", "kwin_x11", "kwin_wayland",
                          "plasmashell", "ksmserver", "xfce4-session", "sway"))

    def test_each_appended_name_carries_the_session(self):
        added = ("i3", "mate-session", "cinnamon-session", "cinnamon",
                 "lxqt-session", "lxsession", "openbox", "labwc", "wayfire")
        for name in added:
            self.assertIn(name, session._SESSION_LEADERS, name)
            if self.rank(name) >= self.real_rank:
                continue
            cookie = tempfile.mkstemp(prefix="xauth_")[1]
            self.addCleanup(os.unlink, cookie)
            with support.leader_process(name, {"DISPLAY": ":91", "XAUTHORITY": cookie}), \
                    support.env(DISPLAY=None, XAUTHORITY=None):
                # The stand-in must be the uid's winning leader for the claim to be
                # about it.  Another process of this uid ranked at or above `name`
                # -- a headless sway/labwc a live test elsewhere in the suite left
                # running, seen once on the fedora44 lane of CI run 34667595059 --
                # wins the /proc scan instead, and then DISPLAY is not :91 and
                # find_xauthority reads that leader's cookie (or None).  That is a
                # true answer about a different session, not this test's subject,
                # so skip the instant it happens rather than assert against it.
                got = session._shell_environ(self.uid)
                if got.get("DISPLAY") != ":91":
                    self.skipTest("a leader of this uid outranked the %s stand-in "
                                  "mid-scan (%r)" % (name, got.get("DISPLAY")))
                self.assertEqual(session.find_xauthority(self.uid), cookie, name)

    def test_the_tuple_order_decides_between_two_of_them(self):
        """A session really can have both: LXQt's `openbox` runs under an Xfce-started session on a box that
        has been switched, and the two environments name different displays."""
        self.need("xfce4-session")
        with support.leader_process("lxqt-session", {"DISPLAY": ":92"}), \
                support.leader_process("xfce4-session", {"DISPLAY": ":93"}), \
                support.env(DISPLAY=None, XAUTHORITY=None):
            self.assertEqual(session._shell_environ(self.uid).get("DISPLAY"), ":93")

    def test_a_nix_wrapped_leader_is_read(self):
        """nixpkgs' gnome-shell is a wrapper binary beside a hidden real one, and comm comes from the file that
        is executed: the running shell's comm is `.gnome-shell-wr`, which was in no list, so a NixOS GNOME
        session lost $DISPLAY/$XAUTHORITY discovery for root and cron entirely [M recon2/nixos.md §6.1]."""
        self.need("gnome-shell")
        leader = support.leader_process(".gnome-shell-wrapped", {"DISPLAY": ":94"})
        self.addCleanup(leader.stop)
        self.assertEqual(leader.comm(), ".gnome-shell-wr")
        with support.env(DISPLAY=None, XAUTHORITY=None):
            self.assertEqual(session._shell_environ(self.uid).get("DISPLAY"), ":94")

    def test_a_wrapped_name_that_is_not_a_leader_is_still_ignored(self):
        """The rule is `.<name>-wrapped` for a name in the tuple, not "anything that starts with a dot": a
        wrapped program of this user that is not a session leader must not be read for its environment."""
        self.assertNotIn(".foot-wrapped"[:15], session._LEADER_RANK)
        with support.leader_process(".foot-wrapped", {"DISPLAY": ":95"}), \
                support.env(DISPLAY=None, XAUTHORITY=None):
            self.assertNotEqual(session._shell_environ(self.uid).get("DISPLAY"), ":95")


class SessionLeaders(unittest.TestCase):
    """Which of a session's processes is asked where the X plane is.

    SDDM writes its cookie to /tmp/xauth_<random>, which is in nobody's
    runtime directory and is not ~/.Xauthority, so on Plasma the environment
    of one of the session's own processes is the only place a root shell
    (`ssh root@box`, cron, a hotkey run under sudo) can learn the path at
    all. `_SESSION_LEADERS` ranks the candidates; these tests are about what
    that ranking does when the best-ranked one does not carry the variables.

    The stand-ins are real processes with the real comm names
    (support.leader_process), because that is what session.py reads."""

    def setUp(self):
        # a leader of this uid that this test did not start would outrank the
        # stand-ins and decide the answer instead of them
        mine = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % entry) as f:
                    comm = f.read().strip()
                if comm in session._SESSION_LEADERS and \
                        os.stat("/proc/" + entry).st_uid == os.geteuid():
                    mine.append(comm)
            except OSError:
                continue
        if mine:
            self.skipTest("a real session leader of this uid is running: %s" % mine)
        self.cookie = tempfile.mkstemp(prefix="xauth_")[1]
        self.addCleanup(os.unlink, self.cookie)
        self.uid = os.geteuid()

    @unittest.expectedFailure
    def test_the_leader_that_carries_the_variables_is_the_one_read(self):
        """DEFERRED fix 46 (finding F5.8): `_shell_environ(uid)` returns the
        best-ranked leader's environment whether or not it holds the keys the
        caller came for.

        On a Plasma Wayland session with Xwayland, `kwin_wayland` (rank 3) is
        started before Xwayland exists, so its environment has no DISPLAY and
        no XAUTHORITY; `plasmashell` (rank 4) is started after and has both.
        Rank 3 wins, its environment is not empty, and the answer is "" for
        each -- so find_x_display falls through to guessing at the socket
        directory and find_xauthority to ~/.Xauthority, which SDDM never
        wrote. The X plane silently disappears from a sudo or ssh-root run,
        which is the case the leader scan was added for.

        The fix takes the keys the caller wants: `_shell_environ(uid, keys)`
        returns the best-ranked environment that HAS them."""
        with support.leader_process("kwin_wayland", {"XDG_SESSION_TYPE": "wayland"}), \
                support.leader_process("plasmashell", {"DISPLAY": ":1",
                                                       "XAUTHORITY": self.cookie}), \
                support.env(DISPLAY=None, XAUTHORITY=None):
            self.assertEqual(session.find_x_display(self.uid), ":1")
            self.assertEqual(session.find_xauthority(self.uid), self.cookie)

    def test_a_better_ranked_leader_that_has_them_still_wins(self):
        """The ranking itself is not the bug and must not be lost to the fix:
        gnome-shell (rank 0) carrying the variables outranks plasmashell
        (rank 4) carrying different ones. A GNOME session with a leftover
        plasmashell is contrived; a session with two candidates that disagree
        is not, and the order in _SESSION_LEADERS is the answer."""
        other = tempfile.mkstemp(prefix="xauth_")[1]
        self.addCleanup(os.unlink, other)
        with support.leader_process("gnome-shell", {"DISPLAY": ":2",
                                                    "XAUTHORITY": other}), \
                support.leader_process("plasmashell", {"DISPLAY": ":1",
                                                       "XAUTHORITY": self.cookie}), \
                support.env(DISPLAY=None, XAUTHORITY=None):
            self.assertEqual(session.find_x_display(self.uid), ":2")
            self.assertEqual(session.find_xauthority(self.uid), other)

    def test_a_leader_of_another_uid_is_never_read(self):
        """The scan is uid-qualified: it trusts a process of the target user,
        the same trust ~/.Xauthority already gets, and nothing else."""
        with support.leader_process("gnome-shell", {"DISPLAY": ":2",
                                                    "XAUTHORITY": self.cookie}), \
                support.env(DISPLAY=None, XAUTHORITY=None):
            self.assertEqual(session._shell_environ(self.uid).get("DISPLAY"), ":2")
            self.assertEqual(session._shell_environ(self.uid + 12345), {})


if __name__ == "__main__":
    unittest.main()
