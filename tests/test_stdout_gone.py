#!/usr/bin/env python3
"""Bug 3: what the seven tools do when their standard output is not there.

Every one of them printed something no original ever prints:

- `wdotool help >/dev/full` and `warandr --version >/dev/full` exited **120**
  with an "Exception ignored in: <_io.TextIOWrapper name='<stdout>'>" block,
  from the interpreter's own flush of the standard files, after main() had
  already returned 0 -- output lost, status success;
- `wwmctl --help >/dev/full` tracebacked (its guard covered BrokenPipeError
  and KeyboardInterrupt, not the ENOSPC of a write that cannot land);
- `wxprop -grammar >/dev/full` swallowed the error whole and exited 0;
- `wxrandr --help`, `wmirror --help` and `warandr --version` never reached
  their own guard at all, because argparse (and wxrandr's own `-help`) leave
  main() through SystemExit;
- `wdotool help >&-` (fd 1 closed before the interpreter starts, so
  `sys.stdout` is None) tracebacked with an AttributeError;
- Ctrl-C during `wdotool sleep 5` printed a KeyboardInterrupt traceback;
- `tool >/dev/full 2>&1` (0.3, live on 26.04): the diagnostic the guard
  itself prints could not land either, so the OSError left main() as a
  traceback and the interpreter's exit-time flush of that failed stderr
  buffer turned five of the six into exit 120 -- apport filed crash reports
  for two of them on a default desktop.  `stdio.warn()` writes the last
  word every tool says, and closes stderr when it cannot.

What they do now is in w11common/stdio.py: repair a missing stdout at the top
of main(), and flush -- and, on failure, CLOSE -- at the bottom of it, one
line to stderr, silence for a reader that left, and never a traceback.
"""

import io
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from w11common import stdio

# The suite never hands a tool over to the real X11 one (tests/conftest.py);
# this line covers `python3 tests/<file>.py` and reaches every child below.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: module, argv that prints to stdout without needing a session, and the name
#: that tool's diagnostics carry (wxrandr answers as `xrandr`: byte parity).
TOOLS = [
    ("wdotool", ["help"], "wdotool"),
    ("wwmctl", ["--help"], "wwmctl"),
    ("wxprop", ["-grammar"], "wxprop"),
    ("wxrandr", ["--help"], "xrandr"),
    ("warandr", ["--version"], "warandr"),
    ("wmirror", ["--help"], "wmirror"),
    # the seventh: not a clone, and the one whose --help is the only thing it
    # prints without touching a session at all
    ("xw11", ["--help"], "xw11"),
]


def child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["W11_PASSTHROUGH"] = "never"
    # No session of any kind: the sockets and the bus address as well as the
    # two display variables, so the `wdotool getactivewindow` row in FAILING
    # exits 2 (no Wayland session found) here whatever the host happens to be
    # running -- this box starts a headless sway in the live files.
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK",
                "DBUS_SESSION_BUS_ADDRESS", "WDOTOOL_BACKEND"):
        env.pop(var, None)
    return env


class NoTracebackEver(unittest.TestCase):
    def check(self, tool, err, rc):
        self.assertNotIn("Traceback", err, "%s: %s" % (tool, err))
        self.assertNotIn("Exception ignored", err, "%s: %s" % (tool, err))
        self.assertNotEqual(rc, 120, "%s exited 120 (the exit-time flush)"
                            % tool)


class FullStdout(NoTracebackEver):
    """`tool >/dev/full`: nothing printed reached the reader, so the status
    has to say so -- once, on stderr, in the tool's own name."""

    def setUp(self):
        if not os.path.exists("/dev/full"):
            self.skipTest("no /dev/full")

    def run_tool(self, mod, argv):
        with open("/dev/full", "w") as out:
            p = subprocess.run([sys.executable, "-m", mod] + argv,
                               stdout=out, stderr=subprocess.PIPE,
                               env=child_env(), text=True, timeout=60)
        return p.returncode, p.stderr

    def test_every_tool_fails_with_one_line(self):
        for mod, argv, prog in TOOLS:
            with self.subTest(tool=mod):
                rc, err = self.run_tool(mod, argv)
                self.check(mod, err, rc)
                self.assertEqual(rc, 1, "%s -> %d\n%s" % (mod, rc, err))
                lines = err.strip().split("\n")
                self.assertEqual(len(lines), 1, err)
                self.assertTrue(lines[0].startswith(prog + ": "), err)
                self.assertIn("No space left on device", err)


#: a command per tool that reports a diagnostic and exits non-zero without
#: needing a session -- the last thing a tool ever writes, which is the write
#: `stdio.warn()` exists for. Two statuses per row: what the tool exits with
#: when fd 2 was CLOSED before the interpreter (the write goes nowhere and
#: nobody was listening: the tool's own status stands) and what it exits with
#: when fd 2 is /dev/full (the diagnostic was written and could not land, so
#: the run failed for a reason of its own: 1, per w11common/stdio.py).
FAILING = [
    ("wdotool", ["nosuchcommand"], 1, 1),
    # rc 2, not 1: "there is no session to talk to" is its own answer
    # (wdotool/backend_detect.py's NoSessionError). The row is here because
    # every other one exits 1 and an interpreter that dies of
    # `AttributeError: 'NoneType' object has no attribute 'write'` -- what a
    # tool writing to a closed stderr used to do -- exits 1 too, so 1 alone
    # cannot tell "handled it" from "crashed on the way". On /dev/full it is
    # 1 like the rest: there the diagnostic itself failed to land.
    ("wdotool", ["getactivewindow"], 2, 1),
    ("wwmctl", ["-Z"], 1, 1),
    ("wxprop", ["-nosuchopt"], 1, 1),
    ("wxrandr", ["--output"], 1, 1),
    ("warandr", ["--save", "/nonexistent-dir/x.sh"], 1, 1),
    ("wmirror", ["--stop", "nosuch"], 1, 1),
]


class FullStderr(NoTracebackEver):
    """The other half of bug 3: stderr cannot take the diagnostic either.

    `tool >/dev/full 2>&1` is the cron job whose disk filled up, and it used to
    end in a traceback and exit 120 -- the status the module docstring of
    w11common/stdio.py says no original produces.  Nothing can be printed here,
    so the whole of the contract is the exit status."""

    def setUp(self):
        if not os.path.exists("/dev/full"):
            self.skipTest("no /dev/full")

    def run_tool(self, mod, argv, out, err):
        p = subprocess.run([sys.executable, "-m", mod] + argv,
                           stdout=out, stderr=err,
                           env=child_env(), text=True, timeout=60)
        return p.returncode

    def test_both_streams_full(self):
        """`tool >/dev/full 2>&1`: output lost, so exit 1, never 120."""
        for mod, argv, _prog in TOOLS:
            with self.subTest(tool=mod):
                with open("/dev/full", "w") as full:
                    rc = self.run_tool(mod, argv, full, full)
                self.assertEqual(rc, 1, "%s -> %d" % (mod, rc))

    def test_a_diagnostic_that_cannot_land(self):
        """stdout is fine, stderr is not: the run failed, and it says so
        with the 1 that a lost diagnostic earns -- never the interpreter's
        120, and never a traceback."""
        for mod, argv, _closed, want in FAILING:
            with self.subTest(tool=mod):
                with open("/dev/full", "w") as full:
                    rc = self.run_tool(mod, argv, subprocess.DEVNULL, full)
                self.assertEqual(rc, want, "%s -> %d" % (mod, rc))


class ClosedStdout(NoTracebackEver):
    """`tool >&-`: fd 1 was closed before the interpreter started, so
    `sys.stdout` is None and the first print() is an AttributeError.  The C
    originals write into a closed descriptor, fail quietly and get on with
    the job; so do we."""

    def run_tool(self, mod, argv):
        p = subprocess.run([sys.executable, "-m", mod] + argv,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, env=child_env(),
                           text=True, timeout=60,
                           preexec_fn=lambda: os.close(1))
        return p.returncode, p.stderr

    def test_every_tool_survives_a_closed_fd_1(self):
        for mod, argv, _prog in TOOLS:
            with self.subTest(tool=mod):
                rc, err = self.run_tool(mod, argv)
                self.check(mod, err, rc)
                self.assertEqual((rc, err), (0, ""), mod)


class ClosedStderr(NoTracebackEver):
    """`tool 2>&-`: fd 2 closed before the interpreter, so the diagnostic has
    nowhere to go and `sys.stderr` is None.

    The mirror of ClosedStdout, and the case a login shell produces every time
    someone writes `wmctrl -l 2>&- >out`. The contract is the same as with a
    full stderr: whatever the tool would have exited with, it still exits with,
    and never the interpreter's 120."""

    def run_tool(self, mod, argv, close=(2,)):
        p = subprocess.run([sys.executable, "-m", mod] + argv,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, env=child_env(),
                           text=True, timeout=60,
                           preexec_fn=lambda: [os.close(fd) for fd in close])
        return p.returncode, p.stdout

    def test_a_failing_tool_keeps_its_own_status(self):
        for mod, argv, want, _full in FAILING:
            with self.subTest(tool=mod):
                rc, _out = self.run_tool(mod, argv)
                self.assertEqual(rc, want, "%s -> %d" % (mod, rc))

    def test_a_tool_that_prints_still_prints(self):
        """The other way a crash on the way to stderr would hide: fd 2 is
        gone, fd 1 is a pipe, and every TOOLS command prints to it.
        A tool that died on `sys.stderr is None` before reaching its own
        output would still exit 0 here with nothing on stdout, so the bytes
        are the assertion -- byte-for-byte what the same command prints on a
        healthy terminal (`warandr --version` is the shortest at 14)."""
        for mod, argv, _prog in TOOLS:
            with self.subTest(tool=mod):
                healthy = subprocess.run(
                    [sys.executable, "-m", mod] + argv,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=child_env(), text=True, timeout=60)
                self.assertEqual(healthy.returncode, 0, mod)
                self.assertTrue(healthy.stdout, mod)
                rc, out = self.run_tool(mod, argv)
                self.assertEqual(rc, 0, "%s -> %d" % (mod, rc))
                self.assertEqual(out, healthy.stdout, mod)

    def test_both_descriptors_closed_still_exits_0(self):
        """`tool >&- 2>&-`: nothing can be said and nothing needed saying, so
        the commands in TOOLS -- each of which succeeds on a healthy
        terminal -- succeed here too."""
        for mod, argv, _prog in TOOLS:
            with self.subTest(tool=mod):
                rc, out = self.run_tool(mod, argv, close=(1, 2))
                self.assertEqual((rc, out), (0, ""), mod)


# The child every DEBUG case below runs. `Boom` stands in for a backend that
# has gone wrong in a way nobody wrote a message for: an AttributeError, the
# shape of every real bug of this kind. sys.argv[0] is set to what `python -m
# <tool>` leaves behind, because that is what the tools' prog-name helpers
# translate back into "wdotool"/"wwmctl"/"wxprop".
_BOOM = """
import os, sys
sys.path.insert(0, %r)
os.environ["W11_PASSTHROUGH"] = "never"
from wdotool.backend import WindowBackend


class Boom(WindowBackend):
    name = "boom"

    def list(self):
        raise AttributeError("boom")

    def views(self):
        raise AttributeError("boom")

    def _nodes(self):
        raise AttributeError("boom")


def boom(*a, **k):
    raise AttributeError("boom")


def xboom(*a, **k):
    # BadWindow (code 3) from X_GetProperty (major 20), the error a real
    # xprop meets when the id it was handed has just been destroyed.
    from wdotool.x11_mini import X11Error
    raise X11Error(3, 20, 0, 0x1)


sys.argv = ["__main__.py"]
%s
sys.exit(%s)
"""

#: module, the patch that plants Boom, the call, and the name the one-line
#: report carries.
DEBUG_CASES = [
    ("wdotool",
     "from wdotool import backend_detect, cli\n"
     "backend_detect.detect = lambda: Boom()",
     'cli.main(["__main__.py", "getactivewindow"])', "wdotool"),
    ("wwmctl",
     "from wwmctl import core, cli\n"
     "core._detect_backend = lambda: Boom()\n"
     "core._x11_connect = lambda: None",
     'cli.main(["-l"])', "wwmctl"),
    # wxprop is the odd one out: core.Session.backend() and core.Session.nodes()
    # catch everything a backend throws and answer with an empty list, so a
    # broken compositor view never reaches main() there. The X connection is the
    # seam that does -- core._x11_connect() guards its own body, not a
    # replacement of it.
    ("wxprop",
     "from wxprop import core, cli\n"
     "core._detect_backend = lambda: None\n"
     "core._x11_connect = boom",
     'cli.main(["-root", "_NET_CLIENT_LIST"])', "wxprop"),
]


class DebugTraceback(NoTracebackEver):
    """$DEBUG turns the one-line report back into a traceback (fix 31).

    "never a traceback" is right for users and useless for a bug report:
    `wwmctl: boom` names neither the file nor the frame, and the only way to
    see one was to edit the installed tool. DEBUG set to anything at all --
    including the empty string, so `is not None` rather than truthiness -- lets
    the exception out of main() and Python prints what it always would.

    The tools keep the promise when DEBUG is unset, which is the other half of
    every case here: exactly one line, `<prog>: ... boom`, and rc 1."""

    def run_tool(self, patch, call, debug):
        env = child_env()
        if debug is None:
            env.pop("DEBUG", None)
        else:
            env["DEBUG"] = debug
        # A file, not -c: the assertion below wants the offending source line in
        # the traceback, and Python 3.12 prints none for code that came in as a
        # string (3.14 does), so 24.04 and 26.04 would disagree.
        d = tempfile.mkdtemp(prefix="w11-boom-")
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "boom.py")
        with open(script, "w") as f:
            f.write(_BOOM % (ROOT, patch, call))
        p = subprocess.run([sys.executable, script],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, text=True, timeout=60)
        return p.returncode, p.stderr

    def test_without_debug_one_line_and_rc_1(self):
        for mod, patch, call, prog in DEBUG_CASES:
            with self.subTest(tool=mod):
                rc, err = self.run_tool(patch, call, None)
                self.check(mod, err, rc)
                self.assertEqual(rc, 1, mod)
                lines = [ln for ln in err.splitlines() if ln.strip()]
                self.assertEqual(len(lines), 1, err)
                self.assertIn("boom", lines[0])
                self.assertTrue(lines[0].startswith(prog + ":"), lines[0])

    def test_with_debug_the_traceback_names_the_frame(self):
        for mod, patch, call, _prog in DEBUG_CASES:
            for val in ("1", "0", ""):
                with self.subTest(tool=mod, DEBUG=val):
                    rc, err = self.run_tool(patch, call, val)
                    self.assertIn("Traceback", err, mod)
                    self.assertIn("AttributeError: boom", err, mod)
                    # the frame it came from, not only the message
                    self.assertIn('raise AttributeError("boom")', err, mod)
                    self.assertEqual(rc, 1, mod)


class DebugAndXProtocolErrors(NoTracebackEver):
    """Where fix 31's `raise` sits in wxprop's except-block, pinned.

    wxprop/cli.py has two arms below `except Exception`: an exception
    carrying `.code`/`.major` prints Xlib's classic five-line block (byte
    parity with the real xprop, which is Xlib's own default handler), and
    everything else is the one-line report. The DEBUG check is above BOTH,
    so `DEBUG=1` turns an X protocol error into a traceback as well -- the
    hatch is "let the exception out of main()", not "let the ones we have no
    message for out". Nothing else pins that choice, so this does; the
    default path keeps the block byte for byte."""

    PATCH = ("from wxprop import core, cli\n"
             "core._detect_backend = lambda: None\n"
             "core._x11_connect = xboom")
    CALL = 'cli.main(["-root", "_NET_CLIENT_LIST"])'

    def run_tool(self, debug):
        env = child_env()
        env.pop("WXPROP_NO_X", None)         # _x11_connect must be reached
        if debug is None:
            env.pop("DEBUG", None)
        else:
            env["DEBUG"] = debug
        p = subprocess.run(
            [sys.executable, "-c", _BOOM % (ROOT, self.PATCH, self.CALL)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            text=True, timeout=60)
        return p.returncode, p.stderr

    def test_without_debug_it_is_xlibs_block(self):
        rc, err = self.run_tool(None)
        self.check("wxprop", err, rc)
        self.assertEqual(rc, 1)
        self.assertEqual(err,
                         "X Error of failed request:  BadWindow (invalid "
                         "Window parameter)\n"
                         "  Major opcode of failed request:  20 "
                         "(X_GetProperty)\n"
                         "  Resource id in failed request:  0x1\n"
                         "  Serial number of failed request:  0\n"
                         "  Current serial number in output stream:  0\n")

    def test_with_debug_even_an_x_error_is_a_traceback(self):
        for val in ("1", ""):
            with self.subTest(DEBUG=val):
                rc, err = self.run_tool(val)
                self.assertIn("Traceback", err)
                self.assertIn("X11Error", err)
                self.assertNotIn("X Error of failed request", err)
                self.assertEqual(rc, 1)


class BrokenPipe(NoTracebackEver):
    """`tool | head -1`: the reader leaves.  The originals die of SIGPIPE
    without a word; we exit 1 without a word (and without the interpreter's
    "Exception ignored" epilogue, which is what closing stdout buys)."""

    def run_tool(self, mod, argv):
        # a pipe whose read end is already gone: every write is EPIPE, with
        # none of the timing luck of a real `| head -1`
        r, w = os.pipe()
        os.close(r)
        try:
            p = subprocess.run([sys.executable, "-m", mod] + argv, stdout=w,
                               stderr=subprocess.PIPE, env=child_env(),
                               text=True, timeout=60)
        finally:
            os.close(w)
        return p.returncode, p.stderr

    def test_every_tool_exits_quietly(self):
        for mod, argv, _prog in TOOLS:
            with self.subTest(tool=mod):
                rc, err = self.run_tool(mod, argv)
                self.check(mod, err, rc)
                self.assertEqual((rc, err), (1, ""), mod)


class ControlC(NoTracebackEver):
    """Ctrl-C in the middle of a command: 130 (128 + SIGINT), silently."""

    def test_wdotool_sleep(self):
        p = subprocess.Popen([sys.executable, "-m", "wdotool", "sleep", "5"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=child_env(), text=True)
        # Wait for the child to really be in the sleep, rather than for a
        # fixed moment: the signal handler is installed late, and a signal
        # that arrives during the imports is a different test. The kernel
        # knows -- a process inside time.sleep parks in hrtimer_nanosleep.
        deadline = time.time() + 20
        while time.time() < deadline:
            if p.poll() is not None:             # died on its own: let the
                break                            # assertions below say so
            try:
                if "nanosleep" in pathlib.Path(
                        "/proc/%d/wchan" % p.pid).read_text():
                    break
            except OSError:                      # no procfs, or it just went
                time.sleep(1.0)                  # away: fall back to waiting
                break
            time.sleep(0.02)
        else:
            p.kill()
            self.fail("the child never reached the sleep")
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.check("wdotool", err, p.returncode)
        self.assertEqual((p.returncode, out, err), (130, "", ""))


class _Failing(io.StringIO):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc
        self.closed_by = 0

    def flush(self):
        raise self.exc

    def close(self):
        self.closed_by += 1
        super().close()


class StdioUnit(unittest.TestCase):
    def setUp(self):
        self.out, self.err = sys.stdout, sys.stderr
        self.addCleanup(self.restore)

    def restore(self):
        sys.stdout, sys.stderr = self.out, self.err

    def test_a_healthy_stdout_is_flushed_and_left_open(self):
        sys.stdout = io.StringIO()
        sys.stdout.write("hi")
        self.assertTrue(stdio.flush_stdout("t"))
        self.assertFalse(sys.stdout.closed)

    def test_a_broken_pipe_is_silent_and_closes(self):
        sys.stdout = _Failing(BrokenPipeError(32, "Broken pipe"))
        sys.stderr = io.StringIO()
        self.assertFalse(stdio.flush_stdout("t"))
        self.assertEqual(sys.stderr.getvalue(), "")
        self.assertTrue(sys.stdout.closed)

    def test_a_full_stdout_says_so_once_and_closes(self):
        sys.stdout = _Failing(OSError(28, "No space left on device"))
        sys.stderr = io.StringIO()
        self.assertFalse(stdio.flush_stdout("wmirror"))
        self.assertEqual(sys.stderr.getvalue(),
                         "wmirror: [Errno 28] No space left on device\n")
        self.assertTrue(sys.stdout.closed)

    def test_quiet_leaves_the_talking_to_the_caller(self):
        sys.stdout = _Failing(OSError(28, "No space left on device"))
        sys.stderr = io.StringIO()
        self.assertFalse(stdio.flush_stdout("wmirror", quiet=True))
        self.assertEqual(sys.stderr.getvalue(), "")

    def test_a_none_stdout_is_repaired(self):
        sys.stdout = sys.stderr = None
        stdio.repair_std()
        opened = [sys.stdout, sys.stderr]
        try:
            self.assertTrue(all(f is not None for f in opened))
            print("into the void")                    # must not raise
            self.assertTrue(stdio.flush_stdout("t"))
        finally:
            for f in opened:
                if f is not None:
                    f.close()

    def test_repair_leaves_a_working_stdout_alone(self):
        sys.stdout = mine = io.StringIO()
        stdio.repair_std()
        self.assertIs(sys.stdout, mine)

    def test_exit_after_flush_reraises_what_it_was_given(self):
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        exc = SystemExit(2)
        with self.assertRaises(SystemExit) as cm:
            stdio.exit_after_flush("t", exc)
        self.assertIs(cm.exception, exc)
        self.assertEqual(cm.exception.code, 2)

    def test_a_lost_stdout_turns_a_successful_exit_into_a_failure(self):
        sys.stdout = _Failing(OSError(28, "No space left on device"))
        sys.stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            stdio.exit_after_flush("t", SystemExit(0))
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
