"""wmirror: the lifetime of the helper, driven against a stub wl-mirror.

wl-mirror is a process that has to outlive the command that started it, so
wmirror follows wxrandr/gamma.py's holder: double-fork, (pid, starttime) in
a state file, uid check before any signal, bounded SIGTERM then SIGKILL.
What is new here is that the process is not ours -- so the record carries
two (pid, starttime) pairs, and every transition below has to leave nothing
running that `wmirror --list` cannot find and `wmirror --stop` cannot end.

The stub stands in for wl-mirror: it prints the libEGL chatter the real one
prints while it is working perfectly (a launcher must never read stderr as
failure), and it can be told to fail at startup or to die later.
"""

import contextlib
import errno
import fcntl
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# and the tests directory itself, so `import support` works under every
# invocation form -- `python3 -m unittest tests/test_wmirror_lifetime.py`
# puts only the repository root on sys.path (see SuiteGuard in
# tests/test_passthrough.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from w11common import procs
from wmirror import cli
from hacks.mirror import core, supervise
from hacks.display import core as wxcore

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["W11_PASSTHROUGH"] = "never"

#: The stand-in lives in tests/support.py now: tests/test_wmirror_live.py
#: needs the same one on a real sway, and two copies of a fake that decides
#: what "wl-mirror failed" looks like would drift apart.
STUB = support.WL_MIRROR_STUB


def sigkill(pid):
    """Cleanup that does not care whether the process is already gone."""
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def gone(pid, tries=100):
    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.02)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wmirror-life-")
        self.addCleanup(shutil.rmtree, self.tmp,
                        ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.stub = support.write_wl_mirror_stub(self.bin, core.HELPER)
        self.log = os.path.join(self.tmp, "stub.log")
        env = {"XDG_RUNTIME_DIR": self.tmp, "WMIRROR_STUB_LOG": self.log,
               "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        # a start blocks for this long watching the helper stay up; the
        # forked supervisor inherits the shortened value
        self.window = mock.patch.object(supervise, "STARTUP_SECONDS", 0.2)
        self.window.start()
        self.addCleanup(self.window.stop)
        self.started = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        """Nothing this file started outlives it.

        Both sources of pids, not just the records the test kept a name for:
        a test that drove `cli.main` and then failed before calling `live()`
        left a supervisor and a wl-mirror stub behind, and the state file
        under self.tmp is the only place those pids are written down. The
        file is read before the temporary directory goes (cleanups run
        last-in-first-out and this one is registered after the rmtree)."""
        pids = []
        for rec in self.started:
            pids += [rec.get("pid"), rec.get("helper_pid")]
        # the file is wxrandr's State: one top-level key per compositor
        # socket, each holding this tool's `mirrors` container.
        try:
            with open(os.path.join(self.tmp, "wmirror-state.json")) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            doc = {}
        for session in doc.values() if isinstance(doc, dict) else ():
            if not isinstance(session, dict):
                continue
            for rec in (session.get("mirrors") or {}).values():
                if isinstance(rec, dict):
                    pids += [rec.get("pid"), rec.get("helper_pid")]
        for pid in pids:
            # never this process: Serialising._record_of_something_running
            # writes os.getpid() into the file as the supervisor, and a
            # cleanup that took the file at its word SIGKILLed the test run
            # itself (measured: exit 137 halfway through the class).
            if procs.as_pid(pid) is None or pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def start(self, recs, source="A", target="B", region=None, **kw):
        argv = core.build_argv(source, target, region=region,
                               helper=self.stub)
        err = supervise.start(recs, source, target, argv, region=region,
                              **kw)
        if target in recs:
            self.started.append(recs[target])
        return err


class Starting(Base):
    def test_the_record_carries_both_processes_and_both_run(self):
        recs = {}
        self.assertIsNone(self.start(recs))
        rec = recs["B"]
        self.assertNotEqual(rec["pid"], rec["helper_pid"])
        self.assertNotEqual(rec["start"], "?")
        self.assertEqual(supervise.liveness(rec), (True, True))
        self.assertEqual(rec["source"], "A")
        with open(self.log) as f:
            self.assertEqual(f.read().split(" wayland=")[0],
                             "--fullscreen-output B --scaling fit A")

    def test_the_supervisor_is_detached(self):
        """setsid + double fork: it is not our child and not in our process
        group, so the shell that started the mirror can go away."""
        recs = {}
        self.start(recs)
        pid = recs["B"]["pid"]
        self.assertNotEqual(os.getpgid(pid), os.getpgid(0))
        self.assertNotEqual(os.getsid(pid), os.getsid(0))  # own session
        with open("/proc/%d/stat" % pid, "rb") as f:
            after = f.read().rsplit(b")", 1)[1].split()
        self.assertNotEqual(int(after[1]), os.getpid())   # reparented

    def test_an_interrupt_mid_start_leaves_a_stoppable_record(self):
        """Ctrl-C while the start is still reading the status pipe.

        The supervisor names itself before it can fail, and that line is
        acted on the moment it arrives rather than when the start finishes
        -- so the record naming it is already there when the interrupt
        lands. A start that buffered its lines would leave a mirror running
        with nothing able to find it."""
        recs = {}
        real_select = procs.select.select

        class Shim:                # only the second call is interrupted
            @staticmethod
            def select(*a, **kw):
                if recs.get("B", {}).get("pid"):
                    raise KeyboardInterrupt
                return real_select(*a, **kw)

        # the verdict is a second away, so there is a second select call
        with mock.patch.object(supervise, "STARTUP_SECONDS", 1.0), \
                mock.patch.object(procs, "select", Shim):
            with self.assertRaises(KeyboardInterrupt):
                self.start(recs)
        rec = recs["B"]
        self.assertTrue(procs.alive(rec["pid"], rec["start"],
                                    supervise.SUPERVISOR_COMM))
        self.assertTrue(supervise.stop_record(rec))
        self.assertEqual(supervise.liveness(rec), (False, False))

    def test_a_supervisor_that_cannot_make_its_stderr_file_names_the_cause(self):
        """Fix 43: the grandchild's exceptions come back up the status pipe.

        `_Stderr()` is the first thing the supervisor does after naming
        itself, and on a read-only /tmp its `tempfile.mkstemp` raises EROFS.
        The grandchild has no stderr (spawn_detached points it at /dev/null)
        and nobody waits for it, so before the fix that exception was
        invisible: the start sat out the whole STARTUP_SECONDS + 4 s budget,
        said `wl-mirror did not report that it started`, and KEPT a record
        naming a supervisor that had already died -- a record `--list` then
        showed and `--stop` claimed to stop."""
        recs = {}
        began = time.monotonic()
        with mock.patch.object(supervise.tempfile, "mkstemp",
                               side_effect=OSError(errno.EROFS, "Read-only file system")):
            err = self.start(recs)
        self.assertIsNotNone(err)
        self.assertIn("Read-only file system", " ".join(err))
        self.assertNotIn("did not report", " ".join(err))
        self.assertNotIn("B", recs)
        self.assertLess(time.monotonic() - began, supervise.STARTUP_SECONDS + 4.0)

    def test_a_sigterm_the_instant_the_helper_exists_still_ends_it(self):
        """Fix 44 (finding F5.7): the SIGTERM handler goes on before Popen.

        `wmirror --stop` SIGTERMs the supervisor and only then reaches past
        it for the helper, and a start blocks for STARTUP_SECONDS -- so the
        signal really can land in the window between fork/exec and the line
        that installed the handler. With the default disposition the
        supervisor died as WIFSIGNALED 15, its `finally` never ran, and
        wl-mirror stayed fullscreen on the target with nobody watching it.
        A signal arriving inside the Popen call itself is the second half:
        it unwinds before `proc` is bound, so the handle is lost unless
        SIGTERM is blocked across the spawn.

        The supervisor is run in a real forked process here rather than in a
        thread, because "how did it exit" is the assertion: WIFEXITED with
        status 0, not WIFSIGNALED 15."""
        argv = core.build_argv("A", "B", helper=self.stub)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:                       # the supervisor, for real
            os.close(r)
            code = 9
            try:
                real_popen = subprocess.Popen

                def popen(*a, **kw):
                    proc = real_popen(*a, **kw)
                    os.write(w, b"%d\n" % proc.pid)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return proc          # ...if we ever get here

                with mock.patch.object(supervise.subprocess, "Popen", popen):
                    supervise.supervisor_main(argv, "A", "B")
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
            except BaseException:
                code = 8
            finally:
                os._exit(code)
        os.close(w)
        line = b""
        while not line.endswith(b"\n"):
            chunk = os.read(r, 32)
            if not chunk:
                break
            line += chunk
        os.close(r)
        self.assertTrue(line.strip().isdigit(), "the helper was never spawned")
        helper = int(line)
        self.addCleanup(self._kill, helper)
        status = os.waitpid(pid, 0)[1]
        self.assertTrue(os.WIFEXITED(status),
                        "the supervisor was killed by signal %d instead of handling it"
                        % (os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertTrue(gone(helper), "wl-mirror outlived the supervisor that owned it")

    def test_the_helper_does_not_inherit_the_supervisors_blocked_sigterm(self):
        """Fix 44's second half must not follow the helper through exec.

        The supervisor blocks SIGTERM across its own Popen (above), and
        CPython restores the PARENT's pre-fork mask in the child before exec
        -- so without wmirror.supervise._unblock_term the helper came up with
        `SigBlk: 0000000000004000`, bit 14, measured straight out of
        /proc/<helper>/status on this host (Python 3.14.4); the same is true
        of a plain `Popen(["sleep", "30"])` under a blocked SIGTERM. wl-mirror
        with SIGTERM blocked ignores `_stop_child`'s terminate(), so every
        stop fell through to the SIGKILL after STOP_SECONDS: the helper
        outlived a SIGTERM to its supervisor by 0.502 s, against 2-4 ms with
        the mask cleared. Both halves are asserted, because the timing alone
        passes on a fast enough SIGKILL fallback."""
        recs = {}
        self.assertIsNone(self.start(recs))
        rec = recs["B"]
        with open("/proc/%d/status" % rec["helper_pid"]) as f:
            blocked = [x for x in f.read().splitlines() if x.startswith("SigBlk:")]
        self.assertEqual(len(blocked), 1, blocked)
        mask = int(blocked[0].split()[1], 16)
        self.assertFalse(mask & (1 << (signal.SIGTERM - 1)),
                         "the helper inherited a blocked SIGTERM: %s" % blocked[0])
        began = time.monotonic()
        os.kill(rec["pid"], signal.SIGTERM)
        self.assertTrue(gone(rec["helper_pid"]))
        took = time.monotonic() - began
        self.assertLess(took, supervise.STOP_SECONDS / 2.0,
                        "the helper only died to the SIGKILL fallback (%.3f s)" % took)

    @staticmethod
    def _kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def test_stderr_chatter_is_not_failure(self):
        """The real wl-mirror prints libEGL warnings while working; the stub
        prints one too. A start that read stderr as failure would refuse
        every real mirror."""
        recs = {}
        self.assertIsNone(self.start(recs))
        self.assertEqual(supervise.liveness(recs["B"])[1], True)

    def test_a_helper_that_fails_at_startup_is_reported_by_its_own_words(self):
        recs = {}
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_FAIL": "1"}):
            err = self.start(recs)
        self.assertTrue(err)
        self.assertIn("output NOPE not found", err[0])
        self.assertEqual(recs, {})               # no record left behind

    def test_a_helper_that_cannot_be_executed_at_all(self):
        recs = {}
        argv = core.build_argv("A", "B", helper=os.path.join(self.tmp, "no"))
        err = supervise.start(recs, "A", "B", argv)
        self.assertTrue(err)
        self.assertIn("cannot run", err[0])
        self.assertEqual(recs, {})


class Stopping(Base):
    def test_stop_ends_both_processes(self):
        recs = {}
        self.start(recs)
        rec = recs["B"]
        self.assertTrue(supervise.stop_record(rec))
        self.assertTrue(gone(rec["helper_pid"]))
        self.assertTrue(gone(rec["pid"]))
        self.assertEqual(supervise.liveness(rec), (False, False))

    def test_stopping_something_already_gone_is_not_an_error(self):
        recs = {}
        self.start(recs)
        rec = recs["B"]
        supervise.stop_record(rec)
        self.assertFalse(supervise.stop_record(rec))

    def test_killing_the_supervisor_leaves_a_findable_mirror(self):
        """Our own process being killed must not strand wl-mirror painting
        somebody's screen with no way to reach it: the record carries the
        helper's own (pid, starttime) for exactly this."""
        recs = {}
        self.start(recs)
        rec = recs["B"]
        os.kill(rec["pid"], signal.SIGKILL)
        self.assertTrue(gone(rec["pid"]))
        self.assertEqual(supervise.liveness(rec), (False, True))
        supervise.reap(recs)
        self.assertIn("B", recs)
        self.assertTrue(recs["B"]["orphan"])
        self.assertIn("(supervisor gone)", core.fmt_record("B", recs["B"]))
        self.assertTrue(supervise.stop_record(rec))
        self.assertTrue(gone(rec["helper_pid"]))

    def test_a_helper_that_dies_on_its_own_takes_the_supervisor_with_it(self):
        recs = {}
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": "0.6"}):
            self.assertIsNone(self.start(recs))
        rec = recs["B"]
        self.assertTrue(gone(rec["helper_pid"]))
        self.assertTrue(gone(rec["pid"]))
        supervise.reap(recs)
        self.assertEqual(recs, {})               # reaped, silently

    def test_a_stubborn_helper_gets_sigkill(self):
        proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 3"],
                                stderr=subprocess.PIPE)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.wait)
        with mock.patch.object(supervise, "STOP_SECONDS", 0.3):
            supervise._stop_child(proc)
        self.assertIsNotNone(proc.poll())


class Reaping(Base):
    def test_a_recycled_pid_is_never_mistaken_for_ours(self):
        """The (pid, starttime) guard: a pid whose process started at a
        different moment is a different process, and never signalled."""
        recs = {}
        self.start(recs)
        rec = recs["B"]
        stale = {"helper_pid": rec["helper_pid"],
                 "helper_start": str(int(rec["helper_start"]) + 5)}
        self.assertEqual(supervise.liveness(stale), (False, False))
        self.assertFalse(supervise.stop_record(stale))
        self.assertFalse(gone(rec["helper_pid"], tries=5))

    def test_garbage_records_are_dropped(self):
        recs = {"B": "not a dict", "C": {}, "D": {"pid": 0}}
        supervise.reap(recs)
        self.assertEqual(recs, {})

    def test_a_zombie_helper_is_not_a_running_mirror(self):
        """A process that has exited but has not been waited for keeps its
        /proc directory, its owner and its start time, so every other test
        here says it is alive. `--list` would print a mirror that had
        stopped painting, and `--stop` would report stopping it."""
        pid = os.fork()
        if pid == 0:                                   # the "helper"
            os._exit(0)
        self.addCleanup(self._reap, pid)
        for _ in range(200):
            if procs.zombie(pid):
                break
            time.sleep(0.01)
        start = supervise.proc_starttime(pid)
        self.assertTrue(procs.zombie(pid), "no zombie to test with")
        self.assertIsNotNone(start)                    # /proc still has it
        self.assertEqual(os.stat("/proc/%d" % pid).st_uid, os.geteuid())
        self.assertFalse(supervise.alive(pid, start, core.HELPER))

    def test_a_record_whose_helper_is_a_zombie_is_reaped(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        self.addCleanup(self._reap, pid)
        for _ in range(200):
            if procs.zombie(pid):
                break
            time.sleep(0.01)
        recs = {"B": {"source": "A", "helper_pid": pid,
                      "helper_start": supervise.proc_starttime(pid)}}
        self.assertEqual(supervise.liveness(recs["B"]), (False, False))
        self.assertTrue(supervise.reap(recs))
        self.assertEqual(recs, {})

    # -- pids that are not pids (fix 42) --------------------------------------
    #
    # The state file is plain JSON in the runtime directory, documented as
    # readable and hand-editable. Every value in it therefore arrives as
    # whatever JSON allows, and `"/proc/%d" % "4242"` is a TypeError: before
    # the guard in procs.as_pid, ONE mistyped pid made `--list`, `--stop` and
    # `--stop-all` all exit 1 with `wmirror: %d format: a real number is
    # required, not str` -- including the commands that would have reaped the
    # bad line, so the file could not be repaired by the tool that wrote it.

    GARBAGE = ("4242", [1], 1.5, -1, 0, None, True)

    def test_a_pid_that_is_not_a_pid_is_not_a_process(self):
        """procs.as_pid: only a positive int names a process.

        `True` is in the list on purpose -- it is an `int` in Python, and
        without the bool check a record of `{"pid": true}` would have been
        read as pid 1, which is init."""
        for value in self.GARBAGE:
            self.assertIsNone(procs.as_pid(value), repr(value))
            self.assertFalse(procs.alive(value, "?", supervise.SUPERVISOR_COMM), repr(value))
            self.assertFalse(procs.owned_by_us(value), repr(value))
            self.assertFalse(procs.kill_bounded(value), repr(value))
            self.assertTrue(procs.wait_gone(value, "?"), repr(value))
        self.assertEqual(procs.as_pid(4242), 4242)

    def test_a_hand_edited_pid_is_reaped_rather_than_a_traceback(self):
        """reap() drops the record and says it changed something, so the
        next save writes the bad line out of the file for good."""
        recs = {"B": {"source": "A", "pid": "4242", "helper_pid": [1]},
                "C": {"source": "A", "pid": 1.5, "helper_pid": -1},
                "D": {"source": "A", "pid": None, "helper_pid": None}}
        self.assertTrue(supervise.reap(recs))
        self.assertEqual(recs, {})

    def test_a_bystander_python_is_not_a_supervisor(self):
        """A `'?'` record (written when /proc could not be read at spawn
        time) falls back to matching a name. comm is `python3` for every
        detached child in this tree, so matching comm against "python" made
        that record claim any python process this user happened to be
        running -- and `--stop-all` would have SIGTERMed it. The command line
        is what carries the word `wmirror`."""
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.assertEqual(procs.comm(bystander.pid)[:6], "python")
        rec = {"source": "A", "pid": bystander.pid, "start": "?"}
        self.assertEqual(supervise.liveness(rec), (False, False))
        self.assertFalse(supervise.stop_record(rec))
        self.assertIsNone(bystander.poll(), "the bystander was signalled")

    def test_a_real_supervisor_with_no_starttime_is_still_stoppable(self):
        """The other half of the same rule: a record whose starttime could
        not be read must still name its own supervisor, or the mirror is one
        nobody can end.

        The supervisor is EXECed here, not forked out of the test runner.
        The word the '?' fallback matches has to come from the supervisor's
        own command line, and a supervisor forked from the runner borrows the
        runner's argv instead -- which happens to carry the word only when
        this file is run by path: the test passed as
        `python3 tests/test_wmirror_lifetime.py` and failed as
        `python3 -m unittest discover -s tests` (alive() -> False), which is
        how the whole suite runs. What runs below is the shape the package
        ships, `python3 <something called wmirror>`: comm is `python3`, and
        only the command line says which python this is -- asserted both ways
        here, because comm alone was the bug (see SUPERVISOR_COMM). It
        reports up the status pipe on its stdout, so both pids come from the
        protocol rather than from guessing at /proc, and sh backgrounds it so
        it is orphaned like the real one and there is nothing left to reap."""
        script = os.path.join(self.bin, "wmirror")
        with open(script, "w") as f:
            f.write("import sys\n"
                    "sys.path.insert(0, %r)\n"
                    "from hacks.mirror import supervise\n"
                    "supervise.STARTUP_SECONDS = 0.2\n"
                    "raise SystemExit(supervise.supervisor_main(sys.argv[1:], 'A', 'B',\n"
                    "                                           status_fd=1))\n" % ROOT)
        argv = core.build_argv("A", "B", helper=self.stub)
        sh = subprocess.Popen(["/bin/sh", "-c", 'exec "$0" "$@" &',
                               sys.executable, script] + argv,
                              stdout=subprocess.PIPE)
        self.addCleanup(sh.wait)
        lines = {}
        for _ in range(3):                    # pid, helper, ok
            line = sh.stdout.readline().decode().split()
            if not line:
                break
            lines[line[0]] = line[1:]
        sh.stdout.close()
        self.assertIn("ok", lines, lines)
        pid, helper = int(lines["pid"][0]), int(lines["helper"][0])
        self.addCleanup(sigkill, pid)
        self.addCleanup(sigkill, helper)
        self.assertNotEqual(pid, os.getpid(), "the supervisor is a process of its own")
        self.assertIn("wmirror", procs.cmdline(pid))
        self.assertEqual(procs.comm(pid)[:6], "python")   # only the argv carries the word
        # only the supervisor's start time is lost here -- that is the record
        # the fallback is for; the helper keeps its own, as it does in a real
        # record whose /proc read failed for the supervisor alone.
        rec = {"source": "A", "pid": pid, "start": "?",
               "helper_pid": helper, "helper_start": lines["helper"][1]}
        self.assertTrue(procs.alive(pid, "?", supervise.SUPERVISOR_COMM))
        self.assertEqual(supervise.liveness(rec), (True, True))
        self.assertTrue(supervise.stop_record(rec))
        self.assertTrue(gone(helper))
        self.assertTrue(gone(pid))

    @staticmethod
    def _reap(pid):
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    @unittest.skipIf(os.geteuid() == 0, "runs as root: everything is ours")
    def test_a_process_that_is_not_ours_is_never_signalled(self):
        """The uid guard from gamma.stop_holder: a state file is a cache,
        and a pid in it that belongs to somebody else is not our helper,
        whatever the record claims."""
        self.assertFalse(supervise.alive(1, "?", core.HELPER))
        self.assertFalse(supervise.stop_record(
            {"helper_pid": 1, "helper_start": "?"}))


class Diagnosis(unittest.TestCase):
    """What a start says when the helper is gone before the window is out.

    wl-mirror prints `error:` lines while it works -- measured on the rig, a
    mirror that ran for minutes and matched its source pixel for pixel
    printed `error: mirror-screencopy::on_dmabuf_allocated(): failed to
    allocate dmabuf` and fell back to shm. Reporting a helper's death in
    the words of an error it survived is a confident wrong answer."""

    CHATTER = ["libEGL warning: DRI2: failed to authenticate",
               "error: mirror-screencopy::on_dmabuf_allocated(): "
               "failed to allocate dmabuf",
               "warning: falling back to shm capture"]

    def test_its_own_fatal_line_is_the_verdict(self):
        self.assertEqual(
            supervise._diagnosis(
                self.CHATTER
                + ["error: options::find_output(): output NOPE not found"],
                1),
            "error: options::find_output(): output NOPE not found")

    def test_the_chatter_it_survives_is_never_the_verdict(self):
        said = supervise._diagnosis(self.CHATTER, 1)
        self.assertNotIn("dmabuf", said)
        self.assertIn("exited with status 1", said)

    def test_a_signal_is_named_rather_than_a_negative_number(self):
        said = supervise._diagnosis(self.CHATTER, -11)
        self.assertIn("SIGSEGV", said)
        self.assertNotIn("dmabuf", said)

    def test_a_helper_that_said_nothing_at_all(self):
        self.assertIn("exited with status 3", supervise._diagnosis([], 3))


class HelperStderr(unittest.TestCase):
    """`_Stderr`: the helper's stderr on an unlinked temp file, trimmed.

    A pipe would tie wl-mirror's life to ours twice over (it stalls when
    nobody drains it, and after our death its first write is SIGPIPE), so the
    supervisor gives the child its own O_APPEND descriptor on an unlinked
    file and truncates the file when it grows. `wl-mirror -v` writes about
    20 kB/s, so a mirror left up overnight is the case: the truncation
    happens WHILE the helper is writing, and it must never cost the helper a
    write or the diagnosis its last line."""

    def test_trimming_under_a_writer_never_breaks_it_and_keeps_the_last_line(self):
        stderr = supervise._Stderr()
        self.addCleanup(stderr.close)
        pad = "x" * 78
        # four bursts of ~86 kB (CAP is 64 kB), 50 ms apart, so the trimming
        # below certainly happens while the writer still holds its O_APPEND
        # descriptor open -- one long burst raced the loop and the file was
        # sometimes complete before a single trim ran.
        script = ("i=0; while [ $i -lt 4 ]; do j=0; "
                  "while [ $j -lt 900 ]; do echo \"burst $i line $j %s\"; j=$((j+1)); done; "
                  "sleep 0.05; i=$((i+1)); done; echo THE-LAST-LINE" % pad)
        proc = subprocess.Popen(["sh", "-c", script], stdout=stderr.wfd,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        stderr.close_write()          # only the writer holds the write end
        sizes = []
        while proc.poll() is None:
            sizes.append(os.fstat(stderr.fd).st_size)
            stderr.trim()
            time.sleep(0.002)
        self.assertEqual(proc.returncode, 0, "the writer died of the trimming")
        self.assertTrue(any(b < a for a, b in zip(sizes, sizes[1:])),
                        "the file never shrank, so nothing was trimmed: %r" % (sizes,))
        tail = stderr.tail()
        self.assertEqual(tail[-1], "THE-LAST-LINE")
        self.assertLessEqual(len("\n".join(tail)), stderr.CAP + len(pad) + 32)


class Watching(Base):
    """The supervisor's own reasons to end a mirror. wl-mirror exits by
    itself when its SOURCE disappears; everything else here is ours."""

    class FakeWatch:
        def __init__(self, heads):
            self.a, self.b = socket.socketpair()
            self.serial = 1
            self.heads = list(heads)
            self.raise_on_dispatch = None
            self.conn = mock.Mock()
            self.conn.sock = self.b
            self.conn.dispatch.side_effect = self._dispatch

        def _dispatch(self, timeout=None):
            self.b.recv(4096)
            if self.raise_on_dispatch:
                raise self.raise_on_dispatch
            self.serial += 1
            return True

        def live_heads(self):
            return self.heads

        def close(self):
            self.a.close()
            self.b.close()

        def change(self, heads):
            self.heads = list(heads)
            self.a.sendall(b"x")

    @staticmethod
    def head(name, x=0, w=1920, enabled=True):
        """One zwlr head as WlrOutputs.live_heads() hands it over: every
        field the wire sets, because the snapshot reads every field."""
        return {"id": 1, "name": name, "description": name,
                "enabled": enabled, "x": x, "y": 0, "transform": 0,
                "scale": 1.0, "current": 1, "gone": False,
                "mm_w": 0, "mm_h": 0, "make": "Unknown", "model": "Unknown",
                "serial": "Unknown",
                "modes": [{"id": 1, "w": w, "h": 1080, "refresh": 60000,
                           "preferred": True}]}

    def supervise_in_thread(self, watch, life="30", **kw):
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": life}):
            proc = subprocess.Popen([self.stub, "x"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        done = threading.Event()
        self.addCleanup(watch.close)
        self.addCleanup(proc.stderr.close)
        t = threading.Thread(
            target=lambda: (supervise._supervise(
                proc, None, "A", "B", "sock", **kw), done.set()),
            daemon=True)
        # cleanup is LIFO: end the helper first, which ends the loop, then
        # join the thread
        self.addCleanup(t.join, 5)
        self.addCleanup(self._end, proc)
        with mock.patch.object(supervise, "_open_watch", return_value=watch):
            t.start()
            time.sleep(0.1)          # the patch only has to cover the open
        return proc, done, t

    @staticmethod
    def _end(proc):
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()

    def test_the_target_disappearing_ends_the_mirror(self):
        """wl-mirror survives this one on its own -- and sway then moves the
        mirror window onto another output, which can be its own source."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        self.assertFalse(done.wait(0.3))
        watch.change([self.head("A")])
        self.assertTrue(done.wait(3), "the supervisor kept going")

    def test_the_target_being_switched_off_ends_it(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A"),
                      self.head("B", x=1920, enabled=False)])
        self.assertTrue(done.wait(3))

    def test_a_layout_change_onto_the_source_ends_it(self):
        """`wxrandr --output B --same-as A` under a running mirror: the two
        now share pixels, so the helper would capture its own window."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A"), self.head("B", x=0)])
        self.assertTrue(done.wait(3))

    def test_a_harmless_change_does_not(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A"), self.head("B", x=2000, w=1280)])
        self.assertFalse(done.wait(1))

    def test_the_compositor_going_away_ends_it(self):
        """A compositor restart: wl-mirror dies of a broken pipe and so do
        we, leaving no orphan and no record."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        watch.raise_on_dispatch = RuntimeError("wayland connection closed")
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A")])
        self.assertTrue(done.wait(3))

    def test_the_helper_dying_ends_it(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch, life="0.4")
        self.assertTrue(done.wait(3))

    def test_the_source_moving_ends_a_region_mirror(self):
        """wl-mirror resolves a layout rectangle against the source once,
        when it starts, and then clamps in silence. A start refuses a
        region that does not fit its source; the layout must not be able to
        arrange that afterwards behind the mirror's back."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(
            watch, region=(100, 100, 500, 300), src_rect=(0, 0, 1920, 1080))
        watch.change([self.head("A", x=3840), self.head("B", x=1920)])
        self.assertTrue(done.wait(3))

    def test_the_same_move_leaves_a_whole_output_mirror_alone(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A", x=3840), self.head("B", x=1920)])
        self.assertFalse(done.wait(1.5))


class HelperEnvironment(Base):
    """wmirror runs from a hotkey, from `sudo` and from `ssh root@box` with
    an empty environment, like every other tool here -- so the helper is
    told which compositor to talk to instead of reading a WAYLAND_DISPLAY
    that may not be set at all."""

    def test_the_socket_is_handed_to_the_helper(self):
        env = supervise.helper_env("/run/user/1000/wayland-1")
        self.assertEqual(env["WAYLAND_DISPLAY"], "/run/user/1000/wayland-1")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_without_one_the_environment_is_left_alone(self):
        self.assertIsNone(supervise.helper_env(None))

    def test_the_helper_really_gets_it(self):
        recs = {}
        argv = [self.stub, "--show-env"]
        supervise.start(recs, "A", "B", argv,
                        wayland_socket="/run/user/4242/wayland-9")
        if "B" in recs:
            self.started.append(recs["B"])
        with open(self.log) as f:
            self.assertIn("wayland=/run/user/4242/wayland-9", f.read())


class SpawnDetached(unittest.TestCase):
    """procs.spawn_detached() on its own, under an interrupt.

    docs/WMIRROR.md promises an interrupted start stays stoppable, and that rests
    on two things this pins directly rather than through a supervisor: every
    line reaches `on_line` as it arrives (so the record naming the child is
    already written when the Ctrl-C lands), and the read end of the status
    pipe is closed on the way out however the call ends -- otherwise a
    long-running warandr or wmirror leaks one fd per interrupted start, and
    the child's later writes block on a pipe nobody will ever read instead
    of getting EPIPE."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wmirror-spawn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def fds():
        return set(os.listdir("/proc/self/fd"))

    def test_every_line_arrives_before_the_next_one_is_written(self):
        def child(fd):
            os.write(fd, b"pid 4242\n")
            time.sleep(0.3)
            os.write(fd, b"ready\n")

        seen = []

        def on_line(line):
            seen.append((line, time.monotonic()))
            return line.startswith("pid ")     # claimed: not the verdict

        start = time.monotonic()
        status = procs.spawn_detached(child, 5.0, on_line)
        self.assertEqual(status, "ready")
        self.assertEqual([line for line, _t in seen], ["pid 4242", "ready"])
        # the first line was acted on while the start was still running
        self.assertLess(seen[0][1] - start, 0.25)

    def test_an_interrupt_mid_start_still_closes_the_pipe(self):
        def child(fd):
            os.write(fd, b"pid 4242\n")
            time.sleep(0.3)
            os.write(fd, b"ready\n")
            time.sleep(2)

        seen = []

        def on_line(line):
            seen.append(line)
            if len(seen) == 2:
                raise KeyboardInterrupt
            return True

        before = self.fds()
        with self.assertRaises(KeyboardInterrupt):
            procs.spawn_detached(child, 5.0, on_line)
        # the child named itself first, and that line was delivered
        self.assertEqual(seen, ["pid 4242", "ready"])
        self.assertEqual(self.fds(), before)

    def test_an_interrupt_before_any_line_closes_the_pipe_too(self):
        def child(fd):
            time.sleep(2)

        def boom(_line):
            raise AssertionError("no line should have arrived")

        real_select = procs.select.select

        class Shim:
            @staticmethod
            def select(*a, **kw):
                raise KeyboardInterrupt

        before = self.fds()
        with mock.patch.object(procs, "select", Shim):
            with self.assertRaises(KeyboardInterrupt):
                procs.spawn_detached(child, 5.0, boom)
        self.assertEqual(self.fds(), before)
        self.assertIs(procs.select.select, real_select)

    def test_a_child_that_raises_says_so_instead_of_dying_silently(self):
        """Fix 43: `except Exception -> emit("failed supervisor: ...")`.

        The grandchild's stdin/stdout/stderr are /dev/null by then and it is
        nobody's child, so an exception in `child_main` used to leave no
        trace at all: the start read the lines that had already arrived, then
        sat out its whole budget and reported "no verdict". The status pipe
        is the only way out, and `failed ` is the prefix the callers'
        `on_line` already reads as one."""
        def child(fd):
            os.write(fd, b"pid 4242 990011\n")
            raise OSError(errno.EROFS, "Read-only file system")

        seen = []

        def on_line(line):
            seen.append(line)
            return line.startswith("pid ")      # claimed: not the verdict

        before = self.fds()
        began = time.monotonic()
        status = procs.spawn_detached(child, 5.0, on_line)
        self.assertIsNotNone(status, "the child died without saying anything")
        self.assertTrue(status.startswith("failed "), status)
        self.assertIn("Read-only file system", status)
        self.assertEqual(seen[0], "pid 4242 990011")
        self.assertLess(time.monotonic() - began, 4.0)
        self.assertEqual(self.fds(), before)

    def test_a_stop_racing_a_start_is_a_plain_exit_not_a_failed_supervisor(self):
        """Fix 43 narrowed: `except Exception`, so SystemExit still means what it meant.

        `wmirror --stop` SIGTERMs the supervisor, and `supervise._on_term`
        answers by raising SystemExit(0); the gamma holder's handler does the
        same (wxrandr/gamma.py). Caught as a failure -- which
        `except BaseException` did -- a stop that landed during a start came
        back as the verdict `failed supervisor: SystemExit(0)`, so the start
        printed a crash for something that had simply been asked to stop.
        SystemExit and KeyboardInterrupt belong to the `os._exit(0)` at the
        bottom of spawn_detached's child, where they were before fix 43: the
        pipe closes, and the caller reports no verdict."""
        def child(fd):
            os.write(fd, b"pid 4242 990011\n")
            raise SystemExit(0)

        seen = []

        def on_line(line):
            seen.append(line)
            return line.startswith("pid ")

        began = time.monotonic()
        status = procs.spawn_detached(child, 5.0, on_line)
        self.assertIsNone(status, status)
        self.assertEqual(seen, ["pid 4242 990011"])
        self.assertLess(time.monotonic() - began, 4.0)

    def test_a_verdict_already_sent_is_never_sent_twice(self):
        """Fix 43, the other half: emit() never writes to a status fd it has closed.

        A supervisor goes on living after `emit("ok", close=True)`, and the
        number that pipe had is free the moment it closes. Measured on a real
        headless sway: the status pipe was (3, 4), and the supervisor's fd
        table a moment later was {3: the wmirror-helper temp file, 4: the
        compositor socket}. So a second emit from the catch clause -- a watch
        loop throwing once the mirror is up -- wrote `failed supervisor: ...`
        into the Wayland connection and then closed it. The caller has its
        answer already; procs._CLOSED_STATUS_FDS is what makes the late one a
        no-op."""
        path = os.path.join(self.tmp, "reused")
        marker = os.path.join(self.tmp, "marker")
        open(path, "w").close()

        def child(fd):
            procs.emit(fd, "ok", close=True)
            # The read end went at the top of spawn_detached's child, so both
            # pipe numbers are free and an open takes the lower one first.
            # Fill up to the status fd and the next open lands exactly where
            # it was -- which is what the supervisor's own Wayland connection
            # did on sway.
            fillers = []
            while True:
                got = os.open(os.devnull, os.O_RDONLY)
                if got >= fd:
                    os.close(got)
                    break
                fillers.append(got)
            reused = os.open(path, os.O_WRONLY | os.O_APPEND)
            with open(marker, "w") as f:
                f.write("%d %d" % (fd, reused))
            raise RuntimeError("thrown after the verdict")

        status = procs.spawn_detached(child, 5.0, lambda line: False)
        self.assertEqual(status, "ok")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not os.path.exists(marker):
            time.sleep(0.01)
        self.assertTrue(os.path.exists(marker), "the child never reopened the fd")
        with open(marker) as f:
            status_fd, reused = (int(x) for x in f.read().split())
        self.assertEqual(reused, status_fd,
                         "the open did not take the closed pipe's number; the test proves nothing")
        # the bad write landed within microseconds of the marker without the
        # guard; half a second is a wide margin.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            self.assertEqual(os.path.getsize(path), 0,
                             "a status line was written into the fd's new owner")
            time.sleep(0.02)

    def test_a_child_that_says_nothing_gives_up_at_the_deadline(self):
        before = self.fds()
        start = time.monotonic()
        status = procs.spawn_detached(lambda fd: time.sleep(2), 0.5,
                                      lambda line: True)
        self.assertIsNone(status)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(self.fds(), before)

    def test_a_child_that_closes_the_pipe_ends_the_start_at_once(self):
        start = time.monotonic()
        status = procs.spawn_detached(lambda fd: None, 30.0,
                                      lambda line: True)
        self.assertIsNone(status)
        self.assertLess(time.monotonic() - start, 5.0)


class MirrorCli:
    """cli.main() with the compositor faked out and real processes
    underneath. Not a TestCase: mixed into the ones below."""

    def outputs(self):
        return [wxcore.OutputState("A", True, 0, 0, 1920, 1080),
                wxcore.OutputState("B", True, 1920, 0, 1280, 1024)]

    def invoke(self, argv):
        conn = mock.Mock()
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "open_conn", return_value=conn), \
                mock.patch.object(core, "require_capture",
                                  return_value=[(core.SCREENCOPY, 3)]), \
                mock.patch.object(core, "read_outputs",
                                  return_value=self.outputs()), \
                contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(argv)
        return rc, o.getvalue(), e.getvalue()

    def live(self):
        state = core.load_state()
        recs = core.records(state)
        for rec in recs.values():
            self.started.append(rec)
        return recs

class Commands(MirrorCli, Base):
    """The transitions through the command line."""

    def test_start_list_stop(self):
        rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 0, e)
        self.assertIn("B <- A", o)
        self.assertIn("wl-mirror pid", o)
        recs = self.live()
        self.assertEqual(supervise.liveness(recs["B"]), (True, True))

        rc, o, e = self.invoke(["--list"])
        self.assertEqual(rc, 0)
        self.assertIn("B <- A", o)

        rc, o, e = self.invoke(["--stop", "B"])
        self.assertEqual(rc, 0, e)
        self.assertIn("stopped", o)
        self.assertTrue(gone(recs["B"]["helper_pid"]))
        rc, o, e = self.invoke(["--list"])
        self.assertEqual(o, "")

    def test_a_second_mirror_on_one_target_is_refused_then_replaced(self):
        """wl-mirror would happily run two on one output, the older one
        invisible behind the newer. The user could see one and stop the
        other."""
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        first = dict(self.live()["B"])
        rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 1)
        self.assertIn("already mirroring", e)
        self.assertEqual(supervise.liveness(first), (True, True))

        rc, o, e = self.invoke(["A", "--to", "B", "--replace"])
        self.assertEqual(rc, 0, e)
        second = self.live()["B"]
        self.assertTrue(gone(first["helper_pid"]))
        self.assertNotEqual(second["helper_pid"], first["helper_pid"])
        self.assertEqual(supervise.liveness(second), (True, True))

    def test_stop_all_ends_everything_it_started(self):
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        recs = dict(self.live())
        rc, o, e = self.invoke(["--stop-all"])
        self.assertEqual(rc, 0)
        self.assertIn("stopped", o)
        self.assertTrue(gone(recs["B"]["helper_pid"]))
        self.assertEqual(self.invoke(["--list"])[1], "")

    def test_a_dead_mirror_is_reaped_by_the_next_query(self):
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": "0.6"}):
            self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        recs = dict(self.live())
        # Both, not just the helper: reap() drops a record only once the
        # supervisor has gone too, and the supervisor goes a moment after
        # the helper it was watching. Waiting for one of the two made this
        # fail under load -- the query ran while the supervisor was still
        # on its way out, and found the record still there.
        self.assertTrue(gone(recs["B"]["helper_pid"]))
        self.assertTrue(gone(recs["B"]["pid"]))
        self.assertEqual(self.invoke(["--list"])[1], "")
        # ...and the target is free again
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        self.live()

    def test_a_hand_edited_pid_does_not_take_every_command_down(self):
        """One bad line in the state file used to break all three queries.

        The file is plain JSON in the runtime directory and documented as
        readable; a pid of `"4242"` (quoted by hand, or by a script that
        wrote the file with a string) reached `"/proc/%d" % pid` and every
        command -- including the ones that would have reaped the bad record
        -- exited 1 with `wmirror: %d format: a real number is required, not
        str`. See w11common.procs.as_pid."""
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        good = dict(self.live()["B"])
        state = core.load_state()
        recs = core.records(state)
        recs["C"] = dict(good, target="C", pid="4242", helper_pid=[1])
        recs["D"] = dict(good, target="D", pid=1.5, helper_pid=-1)
        state.save()

        rc, o, e = self.invoke(["--list"])
        self.assertEqual(rc, 0, e)
        self.assertIn("B <- A", o)
        self.assertNotIn("C <-", o)
        self.assertNotIn("D <-", o)
        self.assertEqual(supervise.liveness(good), (True, True))

        rc, o, e = self.invoke(["--stop", "B"])
        self.assertEqual(rc, 0, e)
        self.assertTrue(gone(good["helper_pid"]))
        self.assertEqual(self.invoke(["--stop-all"])[0], 0)
        self.assertEqual(self.invoke(["--list"])[1], "")

    def test_replace_on_a_refusal_leaves_the_running_mirror_alone(self):
        """--replace decides FIRST, with the record it would replace out of
        the way, and only stops it once the new one is going ahead. A refusal
        that had already killed the old mirror would leave the user with
        neither picture and nothing in `--list`."""
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        first = dict(self.live()["B"])
        rc, o, e = self.invoke(["A", "--to", "B", "--replace",
                                "--region", "500x300+9000+0"])
        self.assertEqual(rc, 1)
        self.assertIn("is not inside A", e)
        self.assertEqual(supervise.liveness(first), (True, True))
        self.assertIn("B <- A", self.invoke(["--list"])[1])
        self.assertEqual(self.live()["B"]["helper_pid"], first["helper_pid"])

    def test_the_region_reaches_the_helper(self):
        rc, o, e = self.invoke(["A", "--to", "B", "--region", "500x300+100+100",
                             "--scaling", "cover"])
        self.assertEqual(rc, 0, e)
        self.live()
        with open(self.log) as f:
            self.assertEqual(
                f.read().split(" wayland=")[0],
                "--fullscreen-output B --scaling cover "
                "--region 100,100 500x300 A")
        self.assertIn("region 500x300+100+100", self.invoke(["--list"])[1])


class Serialising(MirrorCli, Base):
    """Two wmirrors at once.

    The state file is the only trace a mirror leaves -- wl-mirror is
    invisible to output management -- so a write that loses a record leaves
    a helper fullscreen on somebody's screen that `--list` cannot see and
    `--stop` cannot end. Measured before the lock existed: two starts on
    one target, two helpers, one record, one orphan."""

    def _record_of_something_running(self):
        """A record that survives a reap: our own pid as the supervisor,
        a real stub as the helper."""
        helper = subprocess.Popen([self.stub, "x"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(helper.wait)
        self.addCleanup(helper.kill)
        return {"source": "A", "target": "B", "scaling": "fit",
                "region": None,
                "pid": os.getpid(),
                "start": supervise.proc_starttime(os.getpid()),
                "helper_pid": helper.pid,
                "helper_start": supervise.proc_starttime(helper.pid)}

    def test_a_start_waits_for_the_one_in_flight_and_then_sees_it(self):
        """Without the lock both starts read an empty file, both spawn a
        helper, and the second write drops the first record."""
        rec = self._record_of_something_running()
        pid = os.fork()
        if pid == 0:                       # the wmirror already in flight
            try:
                with core.state_lock():
                    time.sleep(0.5)        # ...still starting its helper
                    state = core.load_state()
                    core.records(state)["B"] = rec
                    state.save()
            finally:
                os._exit(0)
        self.addCleanup(self._reap, pid)
        time.sleep(0.1)
        began = time.monotonic()
        with mock.patch.object(supervise, "start") as start:
            rc, o, e = self.invoke(["A", "--to", "B"])
        waited = time.monotonic() - began
        self.assertEqual(rc, 1)
        self.assertIn("already mirroring", e)
        start.assert_not_called()
        self.assertGreater(waited, 0.3, "did not wait for the lock")

    def test_a_killed_start_does_not_leave_the_lock_to_the_mirror(self):
        """The lock is a POSIX record lock, which belongs to the process,
        so the supervisor we fork does not carry it. With flock the lock
        lives in the open file description the supervisor inherits: kill a
        start before it can unlock and every later wmirror would wait out
        the whole timeout (both measured)."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:                       # a start that holds the lock
            os.close(r)
            try:
                with core.state_lock():
                    grand = os.fork()      # ...and forks its supervisor
                    if grand == 0:
                        os.close(w)
                        time.sleep(5)
                        os._exit(0)
                    os.write(w, str(grand).encode() + b"\n")
                    time.sleep(5)
            finally:
                os._exit(0)
        os.close(w)
        supervisor = int(os.read(r, 32))   # named once the lock is held
        os.close(r)
        self.addCleanup(self._kill, supervisor)
        os.kill(pid, signal.SIGKILL)       # killed before it can unlock
        self._reap(pid)
        began = time.monotonic()
        with core.state_lock():
            waited = time.monotonic() - began
        self.assertLess(waited, 1.0,
                        "the lock outlived the command that took it")

    def _lock_is_free(self):
        """Can another process take the start lock right now?"""
        pid = os.fork()
        if pid == 0:
            code = 2
            try:
                fd = os.open(core.lock_path(),
                             os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    code = 0
                except OSError:
                    code = 1
            except OSError:
                pass
            os._exit(code)
        return os.waitpid(pid, 0)[1] == 0

    def test_saving_the_state_does_not_drop_the_start_lock(self):
        """Closing any fd to a file drops that process's POSIX locks on it,
        and State.save opens and closes its own lock file on every write --
        so the two locks must not live on the same file."""
        self.assertNotEqual(core.lock_path(), core.state_path() + ".lock")
        with core.state_lock():
            self.assertFalse(self._lock_is_free())
            state = core.load_state()
            core.records(state)["B"] = {"source": "A"}
            state.save()                   # opens and closes ITS lock file
            self.assertFalse(self._lock_is_free(),
                             "State.save's lock file dropped ours")
        self.assertTrue(self._lock_is_free())

    def test_a_start_interrupted_mid_flight_is_still_stoppable(self):
        """Ctrl-C in the second a start blocks for. The supervisor names
        itself before it can fail, so the record exists -- it just has to
        reach the file, or the mirror is one nobody can end."""
        real = supervise.start

        def interrupted(*a, **kw):
            real(*a, **kw)
            raise KeyboardInterrupt

        with mock.patch.object(supervise, "start", interrupted):
            rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 130)
        recs = self.live()
        self.assertIn("B", recs)
        helper = recs["B"]["helper_pid"]
        self.assertEqual(self.invoke(["--stop", "B"])[0], 0)
        self.assertTrue(gone(helper))

    def test_a_start_that_cannot_be_written_down_is_stopped_again(self):
        """An unwritable state file is not a reason to leave a mirror
        painting with nothing able to find it."""
        seen = []

        def unwritable(target, rec):
            seen.append(dict(rec))
            return False

        with mock.patch.object(core, "recorded", unwritable):
            rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 1)
        self.assertIn("could not write it down", e)
        self.assertTrue(gone(seen[0]["helper_pid"]))
        self.assertTrue(gone(seen[0]["pid"]))
        self.assertEqual(self.invoke(["--list"])[1], "")

    # -- the timeout on the lock ---------------------------------------------

    @contextlib.contextmanager
    def short_lock(self, seconds):
        """`core.state_lock` with a shorter timeout, patched in both places.

        `state_lock(timeout=LOCK_SECONDS)` binds its default at import, so
        patching the module attribute alone changes nothing today; the fix
        that derives LOCK_SECONDS from the hold budget would make the module
        attribute the live one. Patching both means these tests say the same
        thing on either side of it, and neither of them is waiting out eight
        real seconds to find out."""
        with mock.patch.object(core, "LOCK_SECONDS", seconds), \
                mock.patch.object(core.state_lock.__wrapped__, "__defaults__", (seconds,)):
            yield

    def hold_the_lock(self, seconds=5):
        """A second process holding the start lock, from the moment this
        returns. LOCK_NB from the outside would tell us nothing about when it
        was taken, so the holder says so up a pipe instead of us sleeping."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                with core.state_lock():
                    os.write(w, b"held\n")
                    time.sleep(seconds)
            finally:
                os._exit(0)
        os.close(w)
        held = os.read(r, 8)
        os.close(r)
        self.addCleanup(self._reap, pid)
        self.addCleanup(self._kill, pid)
        self.assertEqual(held, b"held\n", "the holder never took the lock")
        return pid

    def test_a_query_still_answers_while_a_start_holds_the_lock(self):
        """The fall-open is right for a read and stays: `--list` waits for
        the writer, and then answers anyway rather than refusing. A query
        that refused because somebody else was starting a mirror would make
        the tool useless exactly when the user is trying to find out what is
        going on."""
        self.hold_the_lock()
        began = time.monotonic()
        with self.short_lock(0.4):
            rc, o, e = self.invoke(["--list"])
        waited = time.monotonic() - began
        self.assertEqual(rc, 0, e)
        self.assertGreater(waited, 0.3, "did not wait for the lock at all")
        self.assertLess(waited, 3.0, "did not fall open after the timeout")

    @unittest.expectedFailure
    def test_a_start_that_cannot_get_the_lock_refuses_instead_of_racing(self):
        """DEFERRED fix 41 (finding F5.5): `core.state_lock` falls open after
        LOCK_SECONDS for every caller, a START included -- and a start that
        goes ahead unlocked is the precise race the lock was added to
        prevent: two starts both read the file, both spawn a helper, and the
        second write drops the first record, leaving a wl-mirror fullscreen
        on the target that `--list` cannot see and `--stop` cannot end
        (measured, twice, before the lock existed).

        The fix keeps the fall-open for the queries and refuses only the
        start, with `another wmirror is still starting`. Until it lands this
        asserts the refusal that is not there yet."""
        self.hold_the_lock()
        began = time.monotonic()
        with self.short_lock(0.5), \
                mock.patch.object(supervise, "start", return_value=["stub"]) as start:
            rc, o, e = self.invoke(["A", "--to", "B"])
        waited = time.monotonic() - began
        self.assertIn("another wmirror is still starting", e)
        self.assertEqual(rc, 1)
        start.assert_not_called()
        self.assertLess(waited, 3.0)

    @staticmethod
    def _reap(pid):
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    @staticmethod
    def _kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class LockBudget(unittest.TestCase):
    """How long a start can hold the lock, against how long the lock waits.

    Deliberately not a `Base` subclass: Base shortens STARTUP_SECONDS to
    0.2 s for the tests that spawn real supervisors, and the arithmetic here
    is about the shipped value."""

    @unittest.expectedFailure
    def test_the_lock_waits_out_the_longest_start_it_serialises(self):
        """DEFERRED fix 41 (finding F5.6): LOCK_SECONDS is not derived from
        the budget it has to cover.

        A start holds the lock for the whole of read-decide-start-write.
        `procs.spawn_detached` is given STARTUP_SECONDS + 4.0 s to hear a
        verdict, and `--replace` first stops the mirror already on the target
        -- two `procs.kill_bounded` calls (the supervisor and the helper),
        each of which waits `50 * 0.02` s after the SIGTERM and again after
        the SIGKILL. That is 1.0 + 4.0 + 4.0 = 9.0 s against LOCK_SECONDS =
        8.0, so the second wmirror times out and (see the test above) goes
        ahead unlocked while the first is still inside its own start."""
        budget = supervise.STARTUP_SECONDS + 4.0 + 2 * 2 * 50 * 0.02
        self.assertGreaterEqual(core.LOCK_SECONDS, budget)


if __name__ == "__main__":
    unittest.main()
