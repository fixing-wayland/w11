#!/usr/bin/env python3
"""`w11common/asuser.run_as_uid`: one function run in a child that IS another account.

What it is for is `wxrandr --persistent` on a session that is not the caller's -- root over ssh or under
`sudo` driving the seated user's sway or Hyprland.  The IPC half crosses the uid boundary by itself and
lands; `$HOME` does not, so the file half used to go into `/root/.config/sway/w11-outputs.conf`, a file sway
has never opened.  Writing it into THEIR home as root is not the fix either: root follows whatever symlink
is waiting in their config directory.  So the write happens as them, and this file pins the machinery that
gets it there.

None of it needs privileges.  `asuser._drop` is patched to a no-op and `asuser.pwd.getpwuid` to a
passwd-shaped entry for a temp directory, and everything else is real: the fork really happens, the child
really writes the pipe, the environment rewrite is the one the code does, the file lands on the disk and the
child's stderr really comes back up.  That is tests/test_dbus_mini.py's ForkHandoff pattern (`:1413`, the
euid-0 retry with `_drop_privileges` stubbed), for the same reason -- a runner that cannot setuid can still
prove every line around the setuid.

The root cell -- the real drop, the real passwd database and the planted symlink -- is
tests/test_asuser_root.py, which skips unless it is run with sudo.
"""
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
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

from w11common import asuser  # noqa: E402

#: The uid every case below asks for.  Nothing is ever really dropped to it -- `_drop` is stubbed -- so it
#: only has to be an integer no test compares against its own.
SEAT_UID = 424242

#: `_drop` as the module defines it, kept before `Base` replaces it with a no-op: `TheDropOrder` below is
#: the one case about the real function and has to reach past its own fixture to get at it.
REAL_DROP = asuser._drop


class Base(unittest.TestCase):
    """A passwd database of one account, a home of its own, and no drop.

    `asuser.pwd.getpwuid` is patched on the `pwd` module object rather than on a name inside `asuser`, so
    `hacks/display/monitors_xml.home_of` and `core._seated_config_path` -- which ask the same database from
    their own import of it -- see the same entry.  That is what keeps the path a failure note prints and the
    path the child writes the same path in the tests that assert on both."""

    def setUp(self):
        self.seat = tempfile.mkdtemp(prefix="w11-asuser-seat-")
        self.addCleanup(shutil.rmtree, self.seat, ignore_errors=True)
        self.ent = pwd.struct_passwd(("seat", "x", SEAT_UID, os.getgid(), "", self.seat, "/bin/sh"))
        real = pwd.getpwuid
        self.getpwuid = mock.patch.object(
            pwd, "getpwuid", side_effect=lambda uid: self.ent if uid == SEAT_UID else real(uid))
        self.getpwuid.start()
        self.addCleanup(self.getpwuid.stop)
        dropp = mock.patch.object(asuser, "_drop", lambda uid, ent: None)
        dropp.start()
        self.addCleanup(dropp.stop)

    def run_as(self, fn, uid=SEAT_UID, **kw):
        """`run_as_uid` with the child's relayed stderr captured, as every real caller's test captures it."""
        err = io.StringIO()
        with redirect_stderr(err):
            ok, why = asuser.run_as_uid(uid, fn, **kw)
        return ok, why, err.getvalue()


class TheChild(Base):
    """It is a child, it is in their home, and it wrote what it was asked to write."""

    def test_the_work_runs_in_a_forked_child_with_the_seated_environment(self):
        """The three facts the whole module exists for: another process, `HOME` from the passwd entry, and
        no `XDG_CONFIG_HOME` -- so that `core.sway_config_dir()`, which reads both, recomputes to the seated
        user's `~/.config/sway` rather than the caller's."""
        probe = os.path.join(self.seat, "probe.json")

        def fn():
            os.makedirs(os.path.join(os.environ["HOME"], ".config"), exist_ok=True)
            with open(probe, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "home": os.environ.get("HOME"),
                           "xdg": os.environ.get("XDG_CONFIG_HOME")}, fh)

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/caller/config", "HOME": "/caller"}):
            ok, why, _err = self.run_as(fn)
            self.assertEqual((ok, why), (True, None))
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], "/caller/config",
                             "the rewrite happens in the child; the parent's environment is untouched")
        with open(probe, encoding="utf-8") as fh:
            got = json.load(fh)
        self.assertNotEqual(got["pid"], os.getpid(), "the work ran in this process, not in a child")
        self.assertEqual(got["home"], self.seat)
        self.assertIsNone(got["xdg"])

    def test_a_file_the_child_wrote_under_their_config_is_really_there(self):
        """The whole point: the bytes outlive the child.  Nothing else about it does."""
        def fn():
            d = os.path.join(os.environ["HOME"], ".config", "sway")
            os.makedirs(d)
            with open(os.path.join(d, "w11-outputs.conf"), "w", encoding="utf-8") as fh:
                fh.write("output Virtual-2 mode 1280x1024@60.000Hz position 1920 0\n")

        ok, why, _err = self.run_as(fn)
        self.assertEqual((ok, why), (True, None))
        with open(os.path.join(self.seat, ".config", "sway", "w11-outputs.conf"), encoding="utf-8") as fh:
            self.assertIn("output Virtual-2", fh.read())


class TheStderrRelay(Base):
    """`core.warn()` in the child is written to the child's copy of the parent's stderr, and dies with it.

    Under `contextlib.redirect_stderr`, which is how every unit test in this tree reads a note, that copy is
    a StringIO in a process that `os._exit`s: the parent's object never sees a byte.  So the child captures
    its own stderr and the JSON carries it, and the parent writes it out before its own line."""

    def test_the_childs_lines_come_back_once_and_before_the_parents(self):
        def fn():
            sys.stderr.write("child says\n")

        err = io.StringIO()
        with redirect_stderr(err):
            ok, why = asuser.run_as_uid(SEAT_UID, fn)
            sys.stderr.write("parent says\n")
        self.assertEqual((ok, why), (True, None))
        text = err.getvalue()
        self.assertEqual(text.count("child says"), 1, "relayed twice, or written twice: %r" % text)
        self.assertLess(text.index("child says"), text.index("parent says"),
                        "the child's notes belong before the line the parent adds about them")

    def test_a_child_that_failed_still_relays_what_it_had_printed(self):
        """The half-done write's notes are the user's best account of what happened before the refusal."""
        def fn():
            sys.stderr.write("xrandr: added an `include` line\n")
            raise PermissionError(13, "Permission denied", "/home/seat/.config/sway/w11-outputs.conf.new")

        ok, why, err = self.run_as(fn)
        self.assertFalse(ok)
        self.assertIn("added an `include` line", err)
        self.assertIn("Permission denied", why)


class TheReasons(Base):
    """Every `(False, reason)` the caller can print, in the words it prints them in."""

    def test_an_exception_is_named_by_its_type(self):
        """`[Errno 13] Permission denied: '...'` on its own reads like a sentence we wrote; with the type in
        front of it the user can tell whose complaint it is."""
        def fn():
            raise ValueError("boom")

        ok, why, _err = self.run_as(fn)
        self.assertEqual((ok, why), (False, "ValueError: boom"))
        self.assertEqual(os.listdir(self.seat), [], "a child that raised wrote nothing")

    def test_a_fatal_crosses_as_its_own_sentence(self):
        """`Fatal` and `CmdError` are this tree's user-facing refusals: their text is already what the user
        is owed, and the note that quotes it carries its own newline."""
        class Fatal(Exception):
            pass

        def fn():
            raise Fatal("Hyprland accepted the mode 1280x1024 for Virtual-1 and did not apply it\n")

        ok, why, _err = self.run_as(fn)
        self.assertEqual((ok, why),
                         (False, "Hyprland accepted the mode 1280x1024 for Virtual-1 and did not apply it"))

    def test_a_failed_drop_is_the_kernels_own_words(self):
        """What a non-root caller gets: `initgroups` refuses and the EPERM is the reason, which is why there
        is no root pre-check in front of the fork."""
        with mock.patch.object(asuser, "_drop",
                               side_effect=PermissionError("[Errno 1] Operation not permitted")):
            ok, why, _err = self.run_as(lambda: None)
        self.assertEqual((ok, why), (False, "PermissionError: [Errno 1] Operation not permitted"))

    def test_no_passwd_entry_is_answered_before_the_fork(self):
        """There is nothing to drop to, so there is nothing to fork for: `os.fork` patched to raise proves
        the check comes first."""
        with mock.patch.object(asuser.os, "fork", side_effect=AssertionError("forked anyway")):
            ok, why, _err = self.run_as(lambda: None, uid=999777)
        self.assertEqual((ok, why), (False, "uid 999777 has no passwd entry"))

    def test_an_account_with_no_home_is_its_own_reason(self):
        """A system account with an empty `pw_dir`: rewriting `HOME` to "" would put the write at the mercy
        of `os.path.expanduser`, which falls back to the passwd entry it just came from."""
        self.ent = pwd.struct_passwd(("seat", "x", SEAT_UID, os.getgid(), "", "", "/bin/sh"))
        with mock.patch.object(asuser.os, "fork", side_effect=AssertionError("forked anyway")):
            ok, why, _err = self.run_as(lambda: None)
        self.assertEqual((ok, why), (False, "uid %d has no home directory in the passwd database" % SEAT_UID))

    def test_a_child_that_outstays_its_deadline_is_killed_and_reaped(self):
        """It holds a copy of every descriptor the parent had, so it does not get to hang the command; and
        it is waited for, so a command that runs several of these leaves no zombies.

        `_reap` is the seam that tells the test which pid the fork produced -- the parent has no other way
        to know it -- and `waitpid(WNOHANG)` afterwards proves the wait already happened."""
        seen = []
        real_reap = asuser._reap
        started = time.monotonic()
        with mock.patch.object(asuser, "GRACE", 0.2):
            with mock.patch.object(asuser, "_reap",
                                   side_effect=lambda pid: (seen.append(pid), real_reap(pid))):
                ok, why, _err = self.run_as(lambda: time.sleep(30), timeout=0.2)
        self.assertEqual((ok, why),
                         (False, "the writer for uid %d said nothing within 0.4 s and was killed" % SEAT_UID))
        self.assertLess(time.monotonic() - started, 2.0, "it waited for the sleep instead of the deadline")
        self.assertEqual(len(seen), 1, "the child was never reaped")
        with self.assertRaises(ChildProcessError):
            os.waitpid(seen[0], os.WNOHANG)


class TheDropOrder(Base):
    """`_drop` itself: three calls, in the one order that works, and a check that the last one stuck.

    `initgroups` needs the privilege it is giving up, so it goes first and `setuid` last; between them
    `setgid`, because a process that has already dropped its uid cannot change its gid.  The three are
    patched to record themselves -- into a file, because they are called in the CHILD, whose memory the
    parent never sees -- and `getuid`/`geteuid` are patched to agree, since nothing really moved."""

    def test_initgroups_then_setgid_then_setuid_and_the_uid_is_checked(self):
        log = os.path.join(self.seat, "drop.log")

        def note(text):
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")

        gid = os.getgid()
        with mock.patch.object(asuser, "_drop", REAL_DROP), \
             mock.patch.object(asuser.os, "initgroups",
                               lambda name, g: note("initgroups:%s:%d" % (name, g))), \
             mock.patch.object(asuser.os, "setgid", lambda g: note("setgid:%d" % g)), \
             mock.patch.object(asuser.os, "setuid", lambda u: note("setuid:%d" % u)), \
             mock.patch.object(asuser.os, "getuid", lambda: SEAT_UID), \
             mock.patch.object(asuser.os, "geteuid", lambda: SEAT_UID):
            ok, why, _err = self.run_as(lambda: None)
        self.assertEqual((ok, why), (True, None))
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(),
                             ["initgroups:seat:%d" % gid, "setgid:%d" % gid, "setuid:%d" % SEAT_UID])

    def test_a_uid_that_did_not_stick_is_a_failure_and_not_a_write(self):
        """`setuid` on a process that kept a saved-set uid is reversible, and a child that could climb back
        is not the thing this module claims to hand the write to."""
        wrote = os.path.join(self.seat, "written")
        with mock.patch.object(asuser, "_drop", REAL_DROP), \
             mock.patch.object(asuser.os, "initgroups", lambda name, g: None), \
             mock.patch.object(asuser.os, "setgid", lambda g: None), \
             mock.patch.object(asuser.os, "setuid", lambda u: None), \
             mock.patch.object(asuser.os, "getuid", lambda: 0), \
             mock.patch.object(asuser.os, "geteuid", lambda: 0):
            ok, why, _err = self.run_as(lambda: open(wrote, "w").close())
        self.assertEqual((ok, why), (False, "PermissionError: uid %d did not stick" % SEAT_UID))
        self.assertFalse(os.path.exists(wrote))


class TheHygiene(Base):
    """What the child does NOT carry: the parent's descriptors, and the parent's unflushed stdout."""

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "no /proc to count descriptors in")
    def test_the_child_holds_its_pipe_and_nothing_else(self):
        """fork() hands the child the whole fd table -- the compositor IPC socket, the state file's lock, a
        bus connection -- and the next thing it does is become the seated user, who is exactly who should
        not be holding root's open descriptors.  `xkbmap._read_all_as` measured the same difference: 12
        entries against the 2 the two closeranges leave."""
        out = os.path.join(self.seat, "fds")
        extra = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, extra)

        def fn():
            # counted BEFORE the report file is opened, so the only descriptors in it are the status pipe
            # and the directory `listdir` is reading itself through
            fds = sorted(int(n) for n in os.listdir("/proc/self/fd"))
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(repr(fds))

        ok, why, _err = self.run_as(fn)
        self.assertEqual((ok, why), (True, None))
        with open(out, encoding="utf-8") as fh:
            fds = eval(fh.read())                      # a list of ints this test's own child wrote
        above = [fd for fd in fds if fd > 2]
        self.assertLessEqual(len(above), 2, "the child kept the parent's descriptors: %r" % fds)

    def test_a_block_buffered_stdout_is_not_written_twice(self):
        """Measured hazard, and the reason `run_as_uid` flushes before it forks: a stdout that is a FILE (or
        a pipe) is block-buffered, the child inherits the buffer with the bytes still in it, and the child's
        own flush at `_exit` writes them a second time.  A terminal hides it -- line buffering -- so this
        case runs the whole thing in a subprocess with stdout redirected to a real file."""
        d = tempfile.mkdtemp(prefix="w11-asuser-out-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        out = os.path.join(d, "stdout")
        prog = (
            "import os, sys, pwd, tempfile\n"
            "sys.path.insert(0, %r)\n"
            "from unittest import mock\n"
            "from w11common import asuser\n"
            "home = tempfile.mkdtemp()\n"
            "ent = pwd.struct_passwd(('seat', 'x', %d, os.getgid(), '', home, '/bin/sh'))\n"
            "mock.patch.object(pwd, 'getpwuid', lambda uid: ent).start()\n"
            "mock.patch.object(asuser, '_drop', lambda uid, e: None).start()\n"
            "sys.stdout.write('parent-before\\n')\n"
            "print(asuser.run_as_uid(%d, lambda: sys.stderr.write('child\\n')), file=sys.stderr)\n"
            "sys.stdout.write('parent-after\\n')\n"
        ) % (ROOT, SEAT_UID, SEAT_UID)
        with open(out, "wb") as fh:
            p = subprocess.run([sys.executable, "-c", prog], stdout=fh, stderr=subprocess.PIPE,
                               cwd=d, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertIn("(True, None)", p.stderr.decode())
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("parent-before"), 1, "stdout was re-emitted by the child: %r" % text)
        self.assertEqual(text.count("parent-after"), 1, text)


class TheLookups(Base):
    """`home_of` and `name_of`: the passwd database, and None where there is none to ask."""

    def test_they_answer_the_passwd_entry(self):
        self.assertEqual(asuser.home_of(SEAT_UID), self.seat)
        self.assertEqual(asuser.name_of(SEAT_UID), "seat")

    def test_an_account_that_is_not_there_is_none_and_not_a_traceback(self):
        """A minimal container has no passwd database at all, and a note that says `~424242` is better than
        a KeyError out of a command that had already applied the layout."""
        for bad in (999777, None, "seat"):
            self.assertIsNone(asuser.home_of(bad), bad)
            self.assertIsNone(asuser.name_of(bad), bad)

    def test_an_empty_home_reads_as_none(self):
        self.ent = pwd.struct_passwd(("seat", "x", SEAT_UID, os.getgid(), "", "", "/bin/sh"))
        self.assertIsNone(asuser.home_of(SEAT_UID))


if __name__ == "__main__":
    unittest.main()
