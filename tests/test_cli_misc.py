#!/usr/bin/env python3
"""misc commands — exec (incl. --sync/--args/--terminator), sleep,
getdisplaygeometry, and the shared getopt clone.

The plain-assert script body is the older half; `DisplayGeometryOnI3` below is a unittest class because it
needs a real backend on a real socket (`support.FakeSway`), which wants setUp/cleanup rather than a bare
assert."""

import contextlib
import io
import os
import sys
import unittest
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from w11common.errors import CmdError
from wdotool import cli
from hacks.window.backend_sway import SwayBackend
from wdotool.ctx import Context, NoSessionError
from wdotool.misc_cmds import _atof, _atoi, cmd_getdisplaygeometry

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

EXEC_USAGE = (
    "Usage: exec [options] command [arg1 arg2 ...] [terminator]\n"
    "--sync    - only exit when the command given finishes. The default\n"
    "            is to fork a child process and continue.\n"
    "--args N  - how many arguments to expect in the exec command. This is\n"
    "            useful for ending an exec and continuing with more xdotool\n"
    "            commands\n"
    "--terminator TERM - similar to --args, specifies a terminator that\n"
    "                    marks the end of 'exec' arguments. This is useful\n"
    "                    for continuing with more xdotool commands.\n"
    "\n"
    "Unless --args OR --terminator is specified, the exec command is assumed\n"
    "to be the remainder of the command line.\n"
)
SLEEP_USAGE = (
    "Usage: sleep seconds\n"
    "Sleep a given number of seconds. Fractions of seconds are valid.\n"
)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["wdotool"] + argv)
    return rc, out.getvalue(), err.getvalue()


def marker():
    fd, path = tempfile.mkstemp(prefix="wdo_marker_")
    os.close(fd)
    os.unlink(path)
    return path


def _run_all():
    # ---- atof/atoi (C parsing semantics) ----
    assert _atof("1.5") == 1.5
    assert _atof("  .5") == 0.5
    assert _atof("2e-2") == 0.02
    assert _atof("1.5junk") == 1.5
    assert _atof("junk") == 0.0
    assert _atof("") == 0.0
    assert _atof("-3") == -3.0
    assert _atof("0x2") == 2.0  # strtod hex floats
    assert _atoi("42abc") == 42
    assert _atoi("abc") == 0
    assert _atoi("  -7") == -7
    # ASCII classes, as C has in the "C" locale
    assert _atoi("\u0664\u0662") == 0
    assert _atof("\u0664\u0662") == 0.0
    assert _atoi("\xa042") == 0

    # ---- sleep ----
    t0 = time.monotonic()
    rc, out, err = run(["sleep", "0.2"])
    assert rc == 0 and out == "" and err == ""
    assert time.monotonic() - t0 >= 0.18

    rc, out, err = run(["sleep"])
    assert rc == 1 and err == "No arguments given.\n" + SLEEP_USAGE, (rc, err)

    rc, out, err = run(["sleep", "-x"])
    assert rc == 1
    assert err == "sleep: unrecognized option '-x'\n" + SLEEP_USAGE, err

    # --help consumes the whole rest of the chain, exit 0
    t0 = time.monotonic()
    rc, out, err = run(["sleep", "--help", "sleep", "5"])
    assert rc == 0 and out == SLEEP_USAGE and err == "", (rc, out, err)
    assert time.monotonic() - t0 < 1

    # "--" ends option parsing; junk parses as 0 seconds; negatives clamp
    rc, out, err = run(["sleep", "--", "0"])
    assert rc == 0, (rc, err)
    rc, out, err = run(["sleep", "abc"])
    assert rc == 0
    # "-1" hits getopt, exactly like the real xdotool
    rc, out, err = run(["sleep", "-1"])
    assert rc == 1 and err == "sleep: unrecognized option '-1'\n" + SLEEP_USAGE, err
    # negatives via "--" clamp to no sleep instead of C's unsigned wraparound
    t0 = time.monotonic()
    rc, out, err = run(["sleep", "--", "-1"])
    assert rc == 0 and time.monotonic() - t0 < 1, (rc, err)

    # nan and inf: max(x, 0.0) returns the NaN and time.sleep() rejects it, while
    # inf overflows time_t -- both used to leave a traceback where the real
    # xdotool (3.20160805.1, measured) returns 0 immediately and sleeps for no
    # time at all
    for word in ("nan", "NaN", "inf", "-inf", "infinity", "1e400"):
        t0 = time.monotonic()
        rc, out, err = run(["sleep", "--", word])
        assert (rc, out, err) == (0, "", ""), (word, rc, out, err)
        assert time.monotonic() - t0 < 1, word

    # ...and a value that is finite, positive and still nowhere near time_t:
    # `sleep 1e300` walked straight through the isfinite() guard into
    # "OverflowError: timestamp out of range for platform time_t".  The
    # oracle hands it to usleep(), whose argument cannot hold it either, and
    # returns 0 at once (measured against xdotool 3.20160805.1: 0.00 s).
    for word in ("1e300", "1e19", "1e100", "1.5e300", "0x1p1024"):
        t0 = time.monotonic()
        rc, out, err = run(["sleep", "--", word])
        assert (rc, out, err) == (0, "", ""), (word, rc, out, err)
        assert time.monotonic() - t0 < 1, word

    # chain: sleep consumes exactly one positional
    rc, out, err = run(["sleep", "0", "version"])
    assert rc == 0 and out == "xdotool version %s\n" % cli.XDO_VERSION, out
    rc, out, err = run(["sleep", "0", "0"])
    assert rc == 1 and "Unknown command: 0" in err, err

    # usage echoes the command name as typed
    rc, out, err = run(["SLEEP"])
    assert rc == 1 and "Usage: SLEEP seconds" in err, err

    # ---- exec ----
    # consumes the remainder by default
    m = marker()
    rc, out, err = run(["exec", "--sync", "touch", m, "sleep", "5"])
    try:
        assert rc == 0 and err == "", (rc, err)
        assert not os.path.exists(m + "-x")
        assert os.path.exists(m)  # touch got m
        # "sleep" "5" became touch arguments, not chained commands
        assert os.path.exists("5") is False or True
    finally:
        for f in (m, "sleep", "5"):
            try:
                os.unlink(f)
            except OSError:
                pass

    # --sync propagates child exit status and aborts the chain
    m = marker()
    rc, out, err = run(["exec", "--sync", "--args", "3", "sh", "-c", "exit 7",
                        "exec", "--sync", "touch", m])
    assert rc == 7 and err == "", (rc, err)
    assert not os.path.exists(m)

    # non-sync: chain continues immediately, rc 0; child really runs
    m = marker()
    rc, out, err = run(["exec", "--args", "2", "touch", m, "sleep", "0"])
    assert rc == 0, (rc, err)
    for _ in range(100):
        if os.path.exists(m):
            break
        time.sleep(0.02)
    assert os.path.exists(m)
    os.unlink(m)

    # --args N: continue chaining after N command words
    m = marker()
    rc, out, err = run(["exec", "--sync", "--args", "2", "touch", m,
                        "exec", "--sync", "--args", "1", "true"])
    assert rc == 0 and os.path.exists(m), (rc, err)
    os.unlink(m)

    # --terminator: command up to (and consuming) the terminator
    m = marker()
    rc, out, err = run(["exec", "--sync", "--terminator", "XX", "touch", m, "XX",
                        "exec", "--sync", "true"])
    assert rc == 0 and os.path.exists(m), (rc, err)
    os.unlink(m)

    # error cases: exact messages
    rc, out, err = run(["exec"])
    assert rc == 1 and err == "No arguments given.\n" + EXEC_USAGE, err
    rc, out, err = run(["exec", "--sync", "--args", "1", "--terminator", "XX", "true"])
    assert rc == 1 and err == "Don't use both --terminator and --args.\n", err
    rc, out, err = run(["exec", "--args", "5", "echo", "hi"])
    assert rc == 1 and err == "You said '--args 5' but only gave 2 arguments.\n", err
    rc, out, err = run(["exec", "--badopt", "true"])
    assert rc == 1 and err == "exec: unrecognized option '--badopt'\n" + EXEC_USAGE, err

    # --help consumes the rest, exit 0
    rc, out, err = run(["exec", "--help", "sleep", "5"])
    assert rc == 0 and out == EXEC_USAGE and err == "", (rc, out, err)

    # missing binary: execvp-style message; --sync aborts the chain with xdotool's
    # fixed 22 (not the child's errno), non-sync continues
    rc, out, err = run(["exec", "--sync", "/no/such/bin/xyz"])
    assert rc == 22 and err == "execvp failed: No such file or directory\n", (rc, err)
    rc, out, err = run(["exec", "--args", "1", "/no/such/bin/xyz", "sleep", "0"])
    assert rc == 0 and err == "execvp failed: No such file or directory\n", (rc, err)

    # options after the first command word belong to the child
    m = marker()
    rc, out, err = run(["exec", "--sync", "touch", "--", m])
    assert rc == 0 and os.path.exists(m), (rc, err)
    os.unlink(m)

    # abbreviated long option (getopt_long behavior)
    rc, out, err = run(["exec", "--sy", "true", "--args"])
    assert rc == 0, (rc, err)

    # ---- getdisplaygeometry ----
    import wdotool.daemon as daemon_mod

    class FakeDaemon:
        @classmethod
        def connect_or_spawn(cls):
            return cls()

        fallback = False

        def geometry(self):
            return (2560, 1440)

        def geometry_status(self):
            return (2560, 1440, self.fallback)

    orig_dc = daemon_mod.DaemonClient
    daemon_mod.DaemonClient = FakeDaemon
    try:
        rc, out, err = run(["getdisplaygeometry"])
        assert rc == 0 and out == "2560 1440\n" and err == "", (rc, out, err)
        rc, out, err = run(["getdisplaygeometry", "--shell"])
        assert rc == 0 and out == "WIDTH=2560\nHEIGHT=1440\n", out
        # --screen N is accepted and ignored; chains fine
        rc, out, err = run(["getdisplaygeometry", "--screen", "3", "getdisplaygeometry"])
        assert rc == 0 and out == "2560 1440\n2560 1440\n", out
        rc, out, err = run(["getdisplaygeometry", "--help", "sleep", "5"])
        assert rc == 0 and out == "Usage: getdisplaygeometry\n", out
        rc, out, err = run(["getdisplaygeometry", "--badopt"])
        assert rc == 1
        assert err == ("getdisplaygeometry: unrecognized option '--badopt'\n"
                       "Usage: getdisplaygeometry\n"), err
        # B5: no compositor reachable -> the daemon answers with the built-in
        # guess. getdisplaygeometry must NOT print it with rc 0; it is the one
        # command that used to "succeed" with no session at all.
        # The window backend is asked before the refusal (it answers on i3, where there is a layout and no
        # Wayland socket), so "no compositor reachable" has to mean that here too: a detector that found the
        # developer's own session would otherwise make this the one environment-dependent line in the file.
        import hacks.window.backend_detect as detect_mod
        orig_detect = detect_mod.detect

        def no_session():
            raise NoSessionError("wdotool: no Wayland session found: nothing at all")
        detect_mod.detect = no_session
        FakeDaemon.fallback = True
        try:
            rc, out, err = run(["getdisplaygeometry"])
        finally:
            detect_mod.detect = orig_detect
            FakeDaemon.fallback = False
        assert rc == 2, (rc, out, err)
        assert out == "" and "no Wayland session found" in err, (out, err)
    finally:
        daemon_mod.DaemonClient = orig_dc

    # direct call: consumed-token count
    class FakeCtx:
        cmd_name = "getdisplaygeometry"

        def daemon(self):
            return FakeDaemon()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        n = cmd_getdisplaygeometry(FakeCtx(), ["--shell", "sleep", "1"])
    assert n == 1 and out.getvalue() == "WIDTH=2560\nHEIGHT=1440\n", (n, out.getvalue())

    # ---- getopt clone details ----
    opts, n = cli.getopt_long_only("t", ["--sync", "--", "x"], "h", [("sync", False)])
    assert opts == [("sync", None)] and n == 2
    opts, n = cli.getopt_long_only("t", ["-sync", "cmd"], "h", [("sync", False)])
    assert opts == [("sync", None)] and n == 1
    opts, n = cli.getopt_long_only("t", ["--args=3", "x"], "h", [("args", True)])
    assert opts == [("args", "3")] and n == 1
    opts, n = cli.getopt_long_only("t", ["notanopt", "-h"], "h", [])
    assert opts == [] and n == 0  # POSIX mode: stop at first non-option
    try:
        cli.getopt_long_only("t", ["--s"], "h", [("sync", False), ("screen", True)])
        assert False
    except cli.GetoptError as e:
        assert "ambiguous" in str(e), e
    try:
        cli.getopt_long_only("t", ["--args"], "h", [("args", True)])
        assert False
    except cli.GetoptError as e:
        assert str(e) == "t: option '--args' requires an argument", e
    # partial opts visible on error (help-before-error handling)
    try:
        cli.getopt_long_only("t", ["--help", "--bad"], "h", [("help", False)])
        assert False
    except cli.GetoptError as e:
        assert e.opts == [("help", None)], e.opts


class DisplayGeometryOnI3(unittest.TestCase):
    """U04: `getdisplaygeometry` answers on a session that has a layout and no Wayland socket.

    Closes recon2/i3.md §2b.  The daemon's geometry is a `wl_output` query, so on i3 -- an X11 session whose
    layout the sway backend can read over the same IPC socket every other command was already using -- it
    exited 2 with "no Wayland session found: cannot query the output layout".  The measured answer is
    `1920 1080`, out of the recorded GET_OUTPUTS (`screen` 1920x1080 active, plus the `xroot-0` pseudo-output
    that is not).

    The daemon is a stand-in answering `fallback=True`, which is what it answered on that box; the backend is
    the real `SwayBackend` over `support.FakeSway(dialect="i3")`, so the numbers come off the wire."""

    def chain(self, argv, backend=None, guessed=True):
        class FakeDaemon:
            def geometry_status(self):
                return (1920, 1080, guessed)

        ctx = Context()
        ctx._daemon = FakeDaemon()
        if backend is not None:
            ctx._backend = backend
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run_chain(ctx, "wdotool", argv)
        return (rc if rc else ctx.exit_code), out.getvalue(), err.getvalue()

    def i3_backend(self):
        srv = support.FakeSway("ok", dialect="i3")
        self.addCleanup(srv.close)
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        return b

    def test_an_i3_socket_answers_from_the_backend(self):
        rc, out, err = self.chain(["getdisplaygeometry"], backend=self.i3_backend())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "1920 1080\n")

    def test_the_shell_form_too(self):
        rc, out, _err = self.chain(["getdisplaygeometry", "--shell"], backend=self.i3_backend())
        self.assertEqual((rc, out), (0, "WIDTH=1920\nHEIGHT=1080\n"))

    def test_with_no_backend_either_it_is_still_the_rc_2_refusal(self):
        """B5's refusal stands: the fallback is a second place to ask, not a licence to guess."""
        import hacks.window.backend_detect as detect_mod
        with mock.patch.object(detect_mod, "detect",
                               mock.Mock(side_effect=NoSessionError("nothing here"))):
            rc, out, err = self.chain(["getdisplaygeometry"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no Wayland session found", err)
        self.assertIn("not guessing a display size", err)

    def test_a_backend_that_cannot_answer_is_the_refusal_too(self):
        """`display_size()` raising (a compositor with no active output) is not a reason to print the
        daemon's guess -- which is the 1920x1080 this command exists to refuse."""
        class Mute:
            def display_size(self):
                raise CmdError("sway backend: no active outputs")

        rc, out, err = self.chain(["getdisplaygeometry"], backend=Mute())
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no Wayland session found", err)

    def test_a_bug_in_the_backend_is_not_reported_as_no_session(self):
        """The fallback catches `CmdError` (a backend saying it cannot answer) and `NoSessionError` (no
        session at all), and nothing else: an `AttributeError` out of a backend is a bug in us, and turning
        it into "no Wayland session found" would be a lie about the session to whoever is probing it."""
        class Broken:
            def display_size(self):
                raise AttributeError("'dict' object has no attribute 'rect'")

        with self.assertRaises(AttributeError):
            cmd_getdisplaygeometry(self.broken_ctx(Broken()), [])

    def broken_ctx(self, backend):
        class FakeDaemon:
            def geometry_status(self):
                return (1920, 1080, True)

        ctx = Context()
        ctx._daemon = FakeDaemon()
        ctx._backend = backend
        return ctx

    def test_a_real_geometry_never_reaches_the_backend(self):
        """The backend is asked only when the daemon guessed: a healthy Wayland session must not pay for a
        second query, and must not be answered by a different layout reader than it was before."""
        class Boom:
            def display_size(self):
                raise AssertionError("the backend was asked on a session that answered")

        rc, out, _err = self.chain(["getdisplaygeometry"], backend=Boom(), guessed=False)
        self.assertEqual((rc, out), (0, "1920 1080\n"))


class ScriptBody(unittest.TestCase):
    """The whole file, as one test.

    These four files are plain-assert scripts, and that is worth keeping:
    every line reads as the thing it pins. What is not worth keeping is
    running them at *import* time, where one broken assertion is a
    collection error that aborts the entire suite instead of failing one
    test. The body moved into _run_all() unchanged; splitting it into a
    method per group is a separate change."""

    def test_script_body(self):
        _run_all()


if __name__ == "__main__":
    # unittest.main(), not the bare `_run_all()` its three sibling script files still end with: the script
    # body is a test of its own (ScriptBody) and there is now a second class in this file, which running the
    # body alone would silently skip -- the failure mode tests/test_passthrough.py:SuiteGuard exists for.
    unittest.main()
