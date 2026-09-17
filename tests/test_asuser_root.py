#!/usr/bin/env python3
"""The root cell of `w11common/asuser`: the real drop, a real account, and the symlink it closes.

tests/test_asuser.py proves everything around the drop with `_drop` stubbed, because CI's unit jobs run as
`ci` and a process that is not root cannot setuid anywhere.  What that cannot prove is the part the module
exists for -- that the child really IS the other account -- and the hazard it closes, which is only a hazard
when the writer is root: a plain root write into somebody else's `~/.config` follows whatever symlink is
waiting there, and lands wherever it points, owned by root.

So this file makes a throwaway account with `useradd -m`, a root:root target directory the account cannot
write, and runs `SwayBackend.apply(..., persistent=True)` against a `support.FakeSway` with the socket's
owner patched to that account -- the whole file half, not a piece of it.  Both directions are measured here:
the dropped child gets `EACCES` and writes nothing, and the same write made directly as root (the positive
control, with `HOME` pointed at the same home) creates the file the symlink pointed at, root-owned, with our
own header in it.  That control is the hazard, so it is made and immediately removed.

  sudo python3 tests/test_asuser_root.py

from the repo root.  Without sudo every case skips and nothing errors.  The account (`w11seat-<pid>`) and
the target directory (`/etc/w11target-<pid>`) are removed in `addClassCleanup`, which runs even when a case
fails; both carry this process's pid so a run that is killed outright leaves something identifiable rather
than a name a later run would collide with.
"""
import io
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py.  This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from w11common import asuser  # noqa: E402
from hacks.display import core  # noqa: E402
# the module, not the class: a TestCase imported by name would be collected and run a second
# time out of this file, and what is wanted from it is one payload constant
import test_wxrandr_sway_persistent as sway_persistent  # noqa: E402

#: The three programs the account is made and unmade with.  `runuser` is the third because the symlink has
#: to be planted BY the account -- root planting it would prove nothing about what the account can do -- and
#: on this box all three live in /usr/sbin, which a sudo PATH carries.
_TOOLS = ("useradd", "userdel", "runuser")

_WHY_SKIPPED = ("the root cell: run it with sudo from the repo root (sudo python3 tests/test_asuser_root.py);"
                " CI's unit jobs run as ci and skip it")


def _run(*argv, **kw):
    """One command, checked, with its output in the failure if it did not work."""
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=120, **kw)


@unittest.skipUnless(os.geteuid() == 0 and all(shutil.which(t) for t in _TOOLS), _WHY_SKIPPED)
class TheRealDrop(unittest.TestCase):
    """One throwaway account, four questions: is the child them, does the whole sway file half land in their
    home owned by them, does a symlink they planted reach anything they could not already write, and does
    the child carry any of root's descriptors."""

    @classmethod
    def setUpClass(cls):
        cls.user = "w11seat-%d" % os.getpid()
        _run("useradd", "-m", "-s", "/bin/bash", cls.user)
        cls.addClassCleanup(subprocess.run, ["userdel", "-r", cls.user],
                            capture_output=True, text=True, timeout=120)
        cls.ent = pwd.getpwnam(cls.user)
        cls.uid, cls.gid = cls.ent.pw_uid, cls.ent.pw_gid
        cls.home = cls.ent.pw_dir
        # Root's own directory, which the account has no business writing into: the thing a planted symlink
        # would reach if the write were made as root.  0755 root:root is the ordinary case, not a hardened
        # one -- /etc looks like this.
        cls.target = "/etc/w11target-%d" % os.getpid()
        os.makedirs(cls.target, mode=0o755, exist_ok=True)
        os.chmod(cls.target, 0o755)
        cls.addClassCleanup(shutil.rmtree, cls.target, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11-asuser-root-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # -- the shared apply -----------------------------------------------------

    def sway_apply(self):
        """The real `SwayBackend.apply(..., persistent=True)` against a `support.FakeSway`, with the IPC
        socket's owner answered as the throwaway account.

        The apply itself runs in this root parent -- only `persist_sway_layout` goes into the child -- so
        who owns the socket file the double made is beside the point, and `core._socket_owner` is patched
        rather than the socket chowned.  What is NOT faked is everything the child does: the fork, the
        initgroups/setgid/setuid, the environment rewrite and every open the write makes."""
        srv = support.FakeSway("ok", outputs=sway_persistent.ThroughTheBackend.OUTPUTS)
        self.addCleanup(srv.close)
        backend = core.SwayBackend(core.SwayIPC(srv.path))
        self.addCleanup(backend.close)
        state = core.State("test", path=os.path.join(self.tmp, "state.json"))
        t = core.Target(output=core.OutputState(name="Virtual-2", active=True,
                                                current=core.Mode(w=1280, h=1024, refresh_mhz=60000)),
                        stanza=None, enabled=True, mode=core.Mode(w=1280, h=1024, refresh_mhz=60000),
                        sway_tf="normal", scale=1.0, changed=True)
        err = io.StringIO()
        with mock.patch.object(core, "_socket_owner", return_value=self.uid):
            with redirect_stderr(err):
                backend.apply(state, [t], persistent=True)
        return err.getvalue()

    def as_the_user(self, script):
        """One `sh -c` run as the account itself, so what it makes is made with their permissions."""
        return _run("runuser", "-u", self.user, "--", "sh", "-c", script)

    def sway_paths(self):
        d = os.path.join(self.home, ".config", "sway")
        return d, os.path.join(d, core.SWAY_CONF_NAME), os.path.join(d, "config")

    def drop_sway_dir(self):
        """Put the account's `~/.config/sway` back to not existing, so the cases do not inherit each
        other's files (they run in name order, and the symlink one is first)."""
        shutil.rmtree(os.path.join(self.home, ".config", "sway"), ignore_errors=True)

    # -- the cases ------------------------------------------------------------

    def test_the_child_is_the_user(self):
        """Every fact of the drop at once, read from inside the child: uid, euid, the group list initgroups
        built, and the environment the write will resolve its paths out of."""
        probe = os.path.join(self.home, ".config", "probe.json")

        def fn():
            import json
            os.makedirs(os.path.dirname(probe), exist_ok=True)
            with open(probe, "w", encoding="utf-8") as fh:
                json.dump({"uid": os.getuid(), "euid": os.geteuid(), "groups": sorted(os.getgroups()),
                           "home": os.environ.get("HOME"),
                           "xdg": os.environ.get("XDG_CONFIG_HOME")}, fh)

        self.addCleanup(lambda: os.path.exists(probe) and os.unlink(probe))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/root/.config"}):
            ok, why = asuser.run_as_uid(self.uid, fn)
        self.assertEqual((ok, why), (True, None))
        import json
        with open(probe, encoding="utf-8") as fh:
            got = json.load(fh)
        self.assertEqual((got["uid"], got["euid"]), (self.uid, self.uid))
        self.assertEqual(got["groups"], [self.gid],
                         "initgroups built the account's own group list, not root's")
        self.assertEqual(got["home"], self.home)
        self.assertIsNone(got["xdg"], "the caller's XDG_CONFIG_HOME would name root's directory")
        st = os.stat(probe)
        self.assertEqual((st.st_uid, st.st_gid), (self.uid, self.gid))
        self.assertEqual(os.geteuid(), 0, "the parent is still root: only the child moved")

    def test_the_whole_sway_file_half_lands_as_the_user(self):
        """`--persistent` end to end as root against their session: the rules file, the config the child had
        to create for the `include`, and the directories on the way -- every one of them theirs."""
        self.drop_sway_dir()
        self.addCleanup(self.drop_sway_dir)
        err = self.sway_apply()
        d, rules, conf = self.sway_paths()
        self.assertTrue(os.path.exists(rules), err)
        self.assertTrue(os.path.exists(conf), err)
        with open(rules, encoding="utf-8") as fh:
            self.assertIn("output Virtual-2 mode 1280x1024@60.000Hz position 0 0", fh.read())
        with open(conf, encoding="utf-8") as fh:
            self.assertIn("include %s" % rules, fh.read())
        for path in (os.path.join(self.home, ".config"), d, rules, conf):
            st = os.stat(path)
            self.assertEqual((st.st_uid, st.st_gid), (self.uid, self.gid),
                             "%s is not theirs: a file they cannot rewrite is a layout they cannot change"
                             % path)
        self.assertIn("written as %s (uid %d)" % (self.user, self.uid), err)

    def test_a_planted_symlink_reaches_only_what_the_user_could_write(self):
        """The hazard, both ways round.

        They plant `w11-outputs.conf.new` -- the temporary name `_write_sway_rules` opens before renaming it
        into place -- as a symlink into a directory only root can write.  The dropped child follows it with
        their own permissions and is refused, so nothing is written and the note says whose refusal it was.
        Then the positive control: the same `persist_sway_layout` called directly as root, with `HOME`
        pointed at the same home and no drop, creates that root-owned file with our header as its first
        line.  That is what the drop is for, so the control's file is removed the moment it is measured."""
        self.drop_sway_dir()
        self.addCleanup(self.drop_sway_dir)
        d, rules, _conf = self.sway_paths()
        pwned = os.path.join(self.target, "pwned")
        self.as_the_user("mkdir -p ~/.config/sway && ln -sf %s ~/.config/sway/%s.new"
                         % (pwned, core.SWAY_CONF_NAME))
        self.assertTrue(os.path.islink(rules + ".new"))

        err = self.sway_apply()
        self.assertIn("could not be written as uid %d, the owner of this sway session" % self.uid, err)
        self.assertIn("(PermissionError: [Errno 13] Permission denied: '%s.new')" % rules, err)
        self.assertEqual(os.listdir(self.target), [], "the child wrote through the symlink")
        self.assertFalse(os.path.exists(rules), "and it left no rules file behind either")

        # the positive control: the same write, as root, with only $HOME moved -- the old behaviour
        fresh = [core.OutputState(name="Virtual-2", active=True, x=0, y=0,
                                  current=core.Mode(w=1280, h=1024, refresh_mhz=60000))]
        target = core.Target(output=core.OutputState(name="Virtual-2", active=True), stanza=None,
                             changed=True)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            with redirect_stderr(io.StringIO()):
                core.persist_sway_layout([target], fresh)
        st = os.stat(pwned)
        self.assertEqual((st.st_uid, st.st_gid), (0, 0), "the control is the hazard: root:root in /etc")
        with open(pwned, encoding="utf-8") as fh:
            self.assertEqual(fh.readline(), core.SWAY_CONF_HEADER.splitlines(True)[0])
        os.unlink(pwned)
        self.assertEqual(os.listdir(self.target), [])
        self.assertTrue(os.path.islink(rules), "os.replace renamed the symlink itself into place")

    def test_the_child_carries_none_of_roots_descriptors(self):
        """fork() hands the child root's whole fd table -- a compositor IPC socket, the state file's lock, a
        bus connection -- and the next thing it does is become somebody else.  What it keeps is its status
        pipe, and the directory `listdir` reads itself through."""
        out = os.path.join(self.tmp, "fds")
        os.chmod(self.tmp, 0o777)
        extra = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, extra)

        def fn():
            fds = sorted(int(n) for n in os.listdir("/proc/self/fd"))
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(" ".join(str(fd) for fd in fds))

        ok, why = asuser.run_as_uid(self.uid, fn)
        self.assertEqual((ok, why), (True, None))
        with open(out, encoding="utf-8") as fh:
            fds = [int(n) for n in fh.read().split()]
        self.assertLessEqual(len([fd for fd in fds if fd > 2]), 2,
                             "the child kept root's descriptors: %r" % fds)


if __name__ == "__main__":
    unittest.main()
