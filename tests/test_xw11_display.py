#!/usr/bin/env python3
"""xw11/display.py: becoming a display without stealing anybody's.

Four measurements are under test here, and each one is a trap somebody already
fell into on this box:

* libxcb tries the **abstract** socket first, so a proxy that binds only the
  filesystem path is silently bypassed by whatever holds the abstract name
  [recon/wire.md 9.2; re-measured for this batch on 2026-09-10 against libxcb
  1.17.0 -- with a listener on both names of :134, `xdpyinfo` was accepted on
  the abstract one].
* a socket FILE is not evidence: /tmp/.X11-unix/X8 and X9 exist here with no
  listener at either name [recon/env.md 4].
* the gate a second X server hits is the LOCK file, 11 bytes, mode 0444, the pid
  right-justified in 10 columns plus a newline [recon/env.md 4].
* the X server re-reads its `-auth` file, and matches on (protocol name, cookie
  bytes) while ignoring the display recorded in the client's entry
  [recon/wire.md 8.3, measured twice]. That is what makes the whole
  authentication design one appended record.

The last one has a live half (R9's local half): an `Xvfb -auth` started with one
cookie, the proxy's entry appended AFTER startup, and a real `xdpyinfo` through
the proxy -- with the same test run again without the entry, so that what passes
is the append and not the box's good nature.
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                     # noqa: E402
from support import write_xauth                                    # noqa: E402
from wdotool import x11_mini                                       # noqa: E402
from xw11 import cli as cli_mod                                     # noqa: E402
from xw11 import display as display_mod                            # noqa: E402
from xw11 import server as server_mod                              # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

HAVE_XVFB = bool(subprocess.run(["sh", "-c", "command -v Xvfb"],
                                capture_output=True).returncode == 0)
HAVE_XDPYINFO = bool(subprocess.run(["sh", "-c", "command -v xdpyinfo"],
                                    capture_output=True).returncode == 0)


class DisplayCase(unittest.TestCase):
    """Every socket and lock file this class makes lives in a directory of its
    own, so a run never touches the box's real displays -- and because an
    abstract name is `"\\0" + <that directory>/XN`, two runs never collide
    either, even though abstract names are not scoped by the filesystem."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="xw11-display-")
        self.addCleanup(self._rmtree)
        self._old_sock = x11_mini._SOCK_DIR
        self._old_lock = display_mod._LOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        display_mod._LOCK_DIR = self.dir
        self.addCleanup(self._restore)
        self.taken = []

    def _restore(self):
        x11_mini._SOCK_DIR = self._old_sock
        display_mod._LOCK_DIR = self._old_lock

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def allocate(self, display=None):
        got = display_mod.allocate(display)
        self.taken.append(got)
        self.addCleanup(got.release)
        return got

    def hold(self, name):
        """Bind one name and keep it for the test."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(name)
        s.listen(4)
        self.addCleanup(s.close)
        return s


class BindBoth(DisplayCase):
    def test_a_pre_bound_abstract_name_makes_the_scan_move_on(self):
        """The trap: a stranger on the abstract name of :20 takes every libxcb
        client that asks for :20, whatever the proxy does with the path.

        And the order is observable here: the abstract name is bound FIRST, so
        a number lost on that bind leaves no filesystem socket behind. A
        filesystem-first allocator would have created X20 and then unbound it,
        or worse, left it."""
        self.hold("\0%s/X%d" % (self.dir, display_mod.FIRST_NUM))
        got = self.allocate()
        self.assertEqual(got.num, display_mod.FIRST_NUM + 1)
        self.assertFalse(os.path.exists("%s/X%d" % (self.dir, display_mod.FIRST_NUM)),
                         "the filesystem socket of a number we did not take exists")

    def test_a_pre_bound_filesystem_name_makes_the_scan_move_on(self):
        self.hold("%s/X%d" % (self.dir, display_mod.FIRST_NUM))
        got = self.allocate()
        self.assertEqual(got.num, display_mod.FIRST_NUM + 1)

    def test_both_names_of_the_number_taken_answer(self):
        """Both are bound, so neither a libxcb that prefers the abstract name
        (re-measured for this batch, scratchpad/b1/sockpref.py, libxcb 1.17.0)
        nor a client that dials the path can miss the proxy. The ORDER is the
        test above; this one is that both answer."""
        got = self.allocate()
        for target in ("\0" + got.fs_path, got.fs_path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(target)          # raises if nothing is listening
            s.close()
        self.assertTrue(os.path.exists(got.fs_path))

    def test_a_stale_filesystem_socket_is_unlinked_and_the_number_reused(self):
        """/tmp/.X11-unix/X8 and X9 on this box are exactly this: files with no
        listener. Existence-of-file is not a usable allocator."""
        path = "%s/X%d" % (self.dir, display_mod.FIRST_NUM)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(path)
        dead.close()                   # the file stays, the listener is gone
        self.assertTrue(os.path.exists(path))
        got = self.allocate()
        self.assertEqual(got.num, display_mod.FIRST_NUM)

    def test_a_regular_file_at_the_number_is_not_a_dead_servers_socket(self):
        """recon/env.md 4 measured stale SOCKETS with no listener, and those are
        cleared. A regular file at XN is not that: this process did not create
        it, connect() would fail on it exactly as it fails on a corpse, and
        unlinking a stranger's file breaks the rule this module is built on."""
        path = "%s/X%d" % (self.dir, display_mod.FIRST_NUM)
        with open(path, "w") as f:
            f.write("not a socket")
        got = self.allocate()
        self.assertEqual(got.num, display_mod.FIRST_NUM + 1)
        self.assertTrue(os.path.exists(path), "a stranger's file was unlinked")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "not a socket")

    def test_a_named_number_whose_file_is_not_a_socket_is_refused(self):
        path = "%s/X%d" % (self.dir, display_mod.FIRST_NUM)
        os.mkdir(path)
        with self.assertRaises(display_mod.DisplayError) as caught:
            display_mod.allocate(":%d" % display_mod.FIRST_NUM)
        self.assertIn("not a socket", str(caught.exception))
        self.assertTrue(os.path.isdir(path))

    def test_a_lock_file_with_a_live_pid_skips_the_number(self):
        with open(display_mod.lock_path(display_mod.FIRST_NUM), "w") as f:
            f.write("%10d\n" % os.getpid())
        got = self.allocate()
        self.assertEqual(got.num, display_mod.FIRST_NUM + 1)

    def test_a_lock_file_with_a_dead_pid_does_not(self):
        """X's own message tells the user to remove a stale lock by hand; the
        proxy does it, which is the only difference and it is deliberate."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path = display_mod.lock_path(display_mod.FIRST_NUM)
        with open(path, "w") as f:
            f.write("%10d\n" % dead.pid)
        got = self.allocate()
        self.assertEqual(got.num, display_mod.FIRST_NUM)
        self.assertEqual(display_mod._lock_holder(path), os.getpid())

    def test_a_named_number_that_is_taken_is_refused_and_never_scanned_past(self):
        """A script that exported DISPLAY=:42 must not end up talking to
        something else, so --display never falls through to the scan."""
        self.hold("\0%s/X42" % self.dir)
        with self.assertRaises(display_mod.DisplayError):
            display_mod.allocate(":42")

    def test_the_environment_names_a_number_too(self):
        with support.env(XW11_DISPLAY=":37"):
            got = self.allocate()
        self.assertEqual(got.num, 37)

    def test_two_allocations_in_a_row_take_two_numbers(self):
        first, second = self.allocate(), self.allocate()
        self.assertEqual(second.num, first.num + 1)

    def test_the_name_is_always_the_bare_colon_form(self):
        """libxcb 1.17 refuses `unix:74` outright [recon/wire.md 9.3], so the
        spelling the proxy hands out is never anything else."""
        self.assertEqual(self.allocate().name, ":%d" % display_mod.FIRST_NUM)


class LockFile(DisplayCase):
    def test_it_is_eleven_bytes_of_right_justified_pid(self):
        got = self.allocate()
        with open(got.lock_path, "rb") as f:
            raw = f.read()
        self.assertEqual(len(raw), 11)
        self.assertEqual(raw, b"%10d\n" % os.getpid())
        self.assertEqual(raw[-1:], b"\n")

    def test_its_mode_is_0444(self):
        got = self.allocate()
        self.assertEqual(os.stat(got.lock_path).st_mode & 0o777, 0o444)

    def test_release_takes_the_lock_and_the_socket_and_nothing_else(self):
        stranger = "%s/X%d" % (self.dir, display_mod.LAST_NUM)
        self.hold("\0" + stranger)
        with open(stranger, "wb"):
            pass
        got = self.allocate()
        lock, path = got.lock_path, got.fs_path
        got.release()
        self.assertFalse(os.path.exists(lock))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(stranger), "release unlinked a file it did not make")

    def test_release_is_idempotent(self):
        got = self.allocate()
        got.release()
        got.release()


class XauthAppend(DisplayCase):
    """R9's local half. recon/wire.md 8.1 hexdumped the record; this is that
    hexdump, back as bytes."""

    COOKIE = bytes(range(0x10, 0x20))

    def path(self, name="f.xauth"):
        return os.path.join(self.dir, name)

    def test_the_record_is_the_captured_57_byte_shape(self):
        """`xauth -f test.xauth add :90 MIT-MAGIC-COOKIE-1 <32 hex>` wrote
        family 256, an 11-byte hostname, the number as ASCII digits, the
        18-byte protocol name and 16 bytes of cookie: 57 bytes."""
        rec = display_mod.xauth_record(90, self.COOKIE, host="fuckwayland")
        self.assertEqual(len(rec), 57)
        self.assertEqual(rec[:2], b"\x01\x00")                       # family 256, big-endian
        self.assertEqual(rec[2:4] + rec[4:15], b"\x00\x0bfuckwayland")
        self.assertEqual(rec[15:19], b"\x00\x0290")
        self.assertEqual(rec[19:39], b"\x00\x12MIT-MAGIC-COOKIE-1")
        self.assertEqual(rec[39:41], b"\x00\x10")
        self.assertEqual(rec[41:], self.COOKIE)

    def test_x11_mini_parses_back_what_we_wrote(self):
        """The reader this project already ships is the oracle for the writer."""
        path = self.path()
        write_xauth(path, [(0xFFFF, b"", b"", b"MIT-MAGIC-COOKIE-1", b"A" * 16)])
        display_mod.xauth_append(path, 31, self.COOKIE)
        entries = x11_mini._read_xauth(path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1], (256, x11_mini.hostname().encode(), b"31",
                                      b"MIT-MAGIC-COOKIE-1", self.COOKIE))

    def test_append_then_remove_leaves_the_file_byte_identical(self):
        path = self.path()
        write_xauth(path, [
            (256, x11_mini.hostname().encode(), b"7", b"MIT-MAGIC-COOKIE-1", b"B" * 16),
            (0xFFFF, b"", b"", b"MIT-MAGIC-COOKIE-1", b"C" * 16)])
        with open(path, "rb") as f:
            before = f.read()
        display_mod.xauth_append(path, 44, self.COOKIE)
        with open(path, "rb") as f:
            self.assertNotEqual(f.read(), before)
        self.assertTrue(display_mod.xauth_remove(path, 44))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_removing_ours_leaves_somebody_elses_entry_for_the_same_number(self):
        """The one case a "drop every entry for :N" removal would corrupt."""
        path = self.path()
        write_xauth(path, [(0xFFFF, b"", b"44", b"MIT-MAGIC-COOKIE-1", b"D" * 16)])
        with open(path, "rb") as f:
            before = f.read()
        display_mod.xauth_append(path, 44, self.COOKIE)
        display_mod.xauth_remove(path, 44)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_a_fifo_at_the_path_is_refused(self):
        """An XAUTHORITY pointing at a FIFO must not block us -- the rule
        x11_mini._read_xauth already applies to reading, applied to writing."""
        path = self.path("fifo")
        os.mkfifo(path)
        with self.assertRaises(OSError):
            display_mod.xauth_append(path, 44, self.COOKIE)

    def test_a_symlink_is_not_written_through(self):
        real = self.path("real")
        with open(real, "wb"):
            pass
        link = self.path("link")
        os.symlink(real, link)
        with self.assertRaises(OSError):
            display_mod.xauth_append(link, 44, self.COOKIE)
        self.assertEqual(os.path.getsize(real), 0)

    def test_find_cookie_names_the_file_the_cookie_is_in(self):
        """The record has to go in the file the SERVER was started with, which
        is the one it re-reads -- so the finder answers with a path."""
        path = self.path()
        write_xauth(path, [(256, x11_mini.hostname().encode(), b"66",
                            b"MIT-MAGIC-COOKIE-1", self.COOKIE)])
        with support.env(XAUTHORITY=path):
            got_path, got_cookie = display_mod.find_cookie(66)
        self.assertEqual((got_path, got_cookie), (path, self.COOKIE))

    def test_no_cookie_anywhere_is_not_a_failure(self):
        """sway starts Xwayland with no `-auth` at all [recon/env.md 2]: the
        empty auth is forwarded and accepted, and nothing is written."""
        path = self.path("empty")
        with open(path, "wb"):
            pass
        with support.env(XAUTHORITY=path, HOME=self.dir):
            self.assertEqual(display_mod.find_cookie(66), (None, None))


class XauthLive(DisplayCase):
    """The measurement recon/wire.md 8.3 called the load-bearing half, now under
    test: the server re-reads its `-auth` file, so an entry appended for the
    proxy's number AFTER Xvfb started is accepted."""

    @unittest.skipUnless(HAVE_XVFB and HAVE_XDPYINFO, "needs Xvfb and xdpyinfo")
    def test_a_client_reaches_the_upstream_through_the_appended_entry(self):
        cookie = bytes(range(0x20, 0x30))
        auth = os.path.join(self.dir, "live.xauth")
        # A real display number, because a real xdpyinfo has to reach it: both
        # the Xvfb and the proxy come out of the same scan of :20..:69 and are
        # taken by binding, so a parallel run cannot collide with this one.
        self._restore()                      # back to the box's /tmp/.X11-unix
        upstream = display_mod.allocate()
        write_xauth(auth, [(256, x11_mini.hostname().encode(),
                            str(upstream.num).encode(),
                            b"MIT-MAGIC-COOKIE-1", cookie)])
        upstream.release()                   # hand the number to Xvfb itself
        xvfb = subprocess.Popen(
            ["Xvfb", ":%d" % upstream.num, "-screen", "0", "640x480x24",
             "-auth", auth],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(_reap, xvfb)
        self.assertTrue(_wait_for_display(upstream.num), "Xvfb never came up")

        proxy = display_mod.allocate()
        self.addCleanup(proxy.release)
        devnull = open(os.devnull, "w")
        self.addCleanup(devnull.close)
        server = server_mod.Server(proxy, ":%d" % upstream.num, log=devnull,
                                   idle=0.0, check=0.05, watch_wayland=False)
        self.addCleanup(server.stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        env = dict(os.environ, XAUTHORITY=auth, DISPLAY=proxy.name)
        before = subprocess.run(["xdpyinfo"], env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertNotEqual(before.returncode, 0,
                            "the client got in with no entry for the proxy's number, "
                            "so this test would pass without the append")

        with support.env(XAUTHORITY=auth):
            self.assertTrue(proxy.add_xauth(upstream.num), "no cookie found to append")
        after = subprocess.run(["xdpyinfo"], env=env, capture_output=True,
                               text=True, timeout=30)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("name of display:    %s" % proxy.name, after.stdout)

        with open(auth, "rb") as f:
            self.assertEqual(len(x11_mini._read_xauth(f.name)), 2)
        proxy.release()
        self.assertEqual(len(x11_mini._read_xauth(auth)), 1,
                         "the entry outlived the proxy that made it")


def _reap(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - a wedged Xvfb
            proc.kill()
            proc.wait(timeout=5)


def _wait_for_display(num, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect("\0%s/X%d" % (x11_mini._SOCK_DIR, num))
            return True
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.1)
    return False


class RuntimeDirCase(DisplayCase):
    """A DisplayCase with an `$XDG_RUNTIME_DIR` of its own, so that no test ever
    reads or writes the session's real display file."""

    def setUp(self):
        super().setUp()
        self.rt = tempfile.mkdtemp(prefix="xw11-rt-")
        os.chmod(self.rt, 0o700)
        self.addCleanup(self._rmrt)
        self._env = support.env(XDG_RUNTIME_DIR=self.rt)
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)

    def _rmrt(self):
        import shutil
        shutil.rmtree(self.rt, ignore_errors=True)


class DisplayFileVerified(RuntimeDirCase):
    """The file is a hint and the socket is the proof: a display file in a
    shared runtime directory can be planted, and what goes down the socket it
    names is every keystroke a wrapped `xdotool type` sends."""

    def test_a_live_proxy_of_our_uid_is_accepted(self):
        got = self.allocate()
        got.write_display_file(":7")
        self.assertEqual(display_mod.read_display(), got.name)

    def test_the_file_carries_the_number_the_pid_and_the_upstream(self):
        got = self.allocate()
        path = got.write_display_file(":7")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "%s %d :7\n" % (got.name, os.getpid()))
        self.assertEqual(display_mod.read_display_file(path),
                         (got.name, os.getpid(), ":7"))

    def test_a_file_naming_another_pid_is_refused(self):
        """The socket answers, and it answers with OUR pid -- which is not the
        pid the file claims, so the reader walks away."""
        got = self.allocate()
        path = got.write_display_file(":7")
        with open(path, "w", encoding="utf-8") as f:
            f.write("%s %d :7\n" % (got.name, os.getpid() + 100000))
        self.assertIsNone(display_mod.read_display())

    def test_a_file_naming_a_display_nothing_listens_on_is_refused(self):
        path = display_mod.display_file_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(":%d %d :7\n" % (display_mod.LAST_NUM, os.getpid()))
        self.assertIsNone(display_mod.read_display())

    def test_no_file_at_all_is_none(self):
        self.assertIsNone(display_mod.read_display())
        self.assertIsNone(display_mod.read_display_file())

    def test_a_file_that_is_not_the_shape_is_none(self):
        path = display_mod.display_file_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        for junk in ("", "\n", ":20\n", ":20 notapid :7\n"):
            with self.subTest(junk):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(junk)
                self.assertIsNone(display_mod.read_display_file(path))

    def test_the_directory_is_owner_only(self):
        got = self.allocate()
        path = got.write_display_file(":7")
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)

    def test_release_takes_the_file_away(self):
        got = self.allocate()
        path = got.write_display_file(":7")
        got.release()
        self.assertFalse(os.path.exists(path))


class PeerCredentials(DisplayCase):
    def test_the_peer_of_our_own_socket_is_us(self):
        """SO_PEERCRED is what the refusal above rests on, so it is checked
        directly rather than only through its consequence."""
        got = self.allocate()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect("\0" + got.fs_path)
        try:
            pid, uid = display_mod._peer(s)
        finally:
            s.close()
        self.assertEqual((pid, uid), (os.getpid(), os.geteuid()))
        self.assertEqual(struct.calcsize("3i"), 12)


class _StubServer:
    """`cli.serve()` builds a Server and runs its loop; what is under test in
    the class below is everything it does BEFORE that, so the loop is stopped at
    the door. Four methods and a record of what was said -- a duck, not a fake
    with behaviour, so it stays here rather than in support.py."""

    instances = []

    def __init__(self, display, upstream, **_kw):
        self.display = display
        self.upstream = upstream
        self.said = []
        _StubServer.instances.append(self)

    def say(self, text):
        self.said.append(text)

    def install_signal_handlers(self):
        pass

    def serve_forever(self):
        return 0


class ServeStartup(RuntimeDirCase):
    """The two things `cli.serve()` does around the display file and the cookie
    before the loop starts, both of which decide what a SECOND proxy in the same
    session does to the first one."""

    def setUp(self):
        super().setUp()
        _StubServer.instances = []
        self._real_server = cli_mod.Server
        cli_mod.Server = _StubServer
        self.addCleanup(self._restore_server)
        self._senv = support.env(DISPLAY=None, XAUTHORITY=None, XW11_DISPLAY=None)
        self._senv.__enter__()
        self.addCleanup(self._senv.__exit__, None, None, None)

    def _restore_server(self):
        cli_mod.Server = self._real_server

    def run_serve(self, *argv):
        args = cli_mod.build_parser().parse_args(list(argv))
        rc = cli_mod.serve(args)
        stub = _StubServer.instances[-1] if _StubServer.instances else None
        if stub is not None:
            self.addCleanup(stub.display.release)
        return rc, stub

    def test_the_first_proxy_in_a_session_writes_the_file(self):
        rc, stub = self.run_serve("--upstream", ":7", "--foreground")
        self.assertEqual(rc, 0)
        path = display_mod.display_file_path()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(),
                             "%s %d :7\n" % (stub.display.name, os.getpid()))

    def test_a_second_proxy_leaves_a_live_ones_file_alone(self):
        """Design section 2.1 is one proxy per session, and the session's file
        is how everything else finds it. A second proxy -- which is exactly what
        `W11_PARITY_PROXY=1 sh scripts/parity-oracle.sh` starts -- would
        otherwise overwrite the line and then unlink it at its own exit, and the
        live proxy would exit within _CHECK_SECONDS through its own file-gone
        check (server.py's exit_reason)."""
        first = self.allocate()
        path = first.write_display_file(":7")
        with open(path, encoding="utf-8") as f:
            before = f.read()
        self.assertEqual(display_mod.read_display(), first.name,
                         "the planted proxy does not read back as live")
        rc, stub = self.run_serve("--upstream", ":7", "--foreground")
        self.assertEqual(rc, 0)
        self.assertNotEqual(stub.display.name, first.name)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before, "a live proxy's file was overwritten")
        self.assertIsNone(stub.display.file_path,
                          "the second proxy would unlink the first's file at exit")
        self.assertTrue(any("does not write it" in t for t in stub.said), stub.said)

    def test_a_dead_proxys_file_is_taken_over(self):
        """The other side of it: a file left behind by a proxy that is gone is
        not a live one, and the next proxy owns the session again."""
        path = display_mod.display_file_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(":%d %d :7\n" % (display_mod.LAST_NUM, os.getpid()))
        rc, stub = self.run_serve("--upstream", ":7", "--foreground")
        self.assertEqual(rc, 0)
        self.assertEqual(stub.display.file_path, path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(),
                             "%s %d :7\n" % (stub.display.name, os.getpid()))

    @unittest.skipIf(os.geteuid() == 0, "root can write a 0444 file")
    def test_a_cookie_that_cannot_be_appended_to_is_logged_and_survived(self):
        """A cookie that EXISTS and cannot be written: a read-only
        ~/.Xauthority, or an $XAUTHORITY that belongs to another uid after a
        sudo. The proxy still serves -- a client whose cookie the upstream then
        refuses gets the upstream's own Failed reason, and this line is what
        explains it."""
        auth = os.path.join(self.rt, "ro.xauth")
        write_xauth(auth, [(256, x11_mini.hostname().encode(), b"7",
                            b"MIT-MAGIC-COOKIE-1", b"K" * 16)])
        os.chmod(auth, 0o444)
        before = os.path.getsize(auth)
        with support.env(XAUTHORITY=auth):
            rc, stub = self.run_serve("--upstream", ":7", "--foreground")
        self.assertEqual(rc, 0, "a read-only xauth file stopped the proxy")
        self.assertIsNone(stub.display.xauth)
        self.assertEqual(os.path.getsize(auth), before)
        self.assertTrue(any("no xauth entry written" in t for t in stub.said),
                        stub.said)

    def test_a_cookie_it_can_write_is_written(self):
        """Without this, the test above would pass against a serve() that never
        appends anything at all."""
        auth = os.path.join(self.rt, "rw.xauth")
        write_xauth(auth, [(256, x11_mini.hostname().encode(), b"7",
                            b"MIT-MAGIC-COOKIE-1", b"K" * 16)])
        before = os.path.getsize(auth)
        with support.env(XAUTHORITY=auth):
            rc, stub = self.run_serve("--upstream", ":7", "--foreground")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(stub.display.xauth)
        self.assertGreater(os.path.getsize(auth), before)
        self.assertEqual(stub.said, [])


if __name__ == "__main__":
    unittest.main()
