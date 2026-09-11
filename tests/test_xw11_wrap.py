#!/usr/bin/env python3
"""xw11/wrap.py: the wrapper that runs the ORIGINAL through the proxy.

Two halves, and they are tested two different ways.

The **decision** half is hermetic, the way `tests/test_passthrough.py` is: the
whole session is three temporary directories (`_X11_SOCK_DIR`, `_LOGIND_DIR`,
`_RUN_USER_DIR`) and an environment dict, the "original" is
`tests/fixtures/fake_real_tool.py` on a PATH of our own, and `os.execve` is
seamed so that the handover can be inspected instead of replacing this process.
Nothing here starts a proxy.

The **spawn** half cannot be faked and is not: it forks, daemonises and
re-execs a real `python3 -m xw11 __serve` against a real `FakeUpstream` on a
real socket, because what R11 asks is what ten of those do to each other, and
ten doubles would answer for the doubles.  Measured here on 2026-09-11: six
`ensure_proxy()` calls took 53, 105, 104, 105, 109 and 203 ms from the fork to a
display file a client can dial (one to four polls of the 50 ms interval, a
typical 105), and ten concurrent ones leave exactly one `__serve` process.  Every proxy
this file starts is stopped by the pid in its own display file -- never by a
`pkill -f`, which on a developer's box would take the session's own proxy with
it.
"""

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
# `import support` is a bare import: it resolves only with the tests directory
# on sys.path, which `python3 tests/<file>.py` gives for free.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded, and it
# reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support                                                     # noqa: E402
from w11common import passthrough                                  # noqa: E402
from xw11 import cli as cli_mod                                    # noqa: E402
from xw11 import display as display_mod                            # noqa: E402
from xw11 import wrap                                              # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
FAKE = os.path.join(FIXTURES, "fake_real_tool.py")
SHIM = os.path.join(FIXTURES, "w11_shim.py")
CHECK_DOCS = os.path.join(ROOT, "scripts", "check-docs.py")


class ExecCalled(BaseException):
    """Raised by the seamed os.execve instead of replacing this process.

    BaseException and not Exception, for `tests/test_passthrough.py`'s reason:
    an execve does not return, and the CLIs wrap their body in a "never print a
    traceback" guard that would swallow an Exception and report a one-line
    failure where a process has in fact been replaced."""

    def __init__(self, path, argv, env):
        super().__init__(path)
        self.path, self.argv, self.env = path, list(argv), dict(env)


class _Sink:
    """A stderr that remembers, without importing io into the fixture."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self.parts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WrapCase(unittest.TestCase):
    """A whole session in a temporary directory, plus a fake original."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xw11_wrap_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uid = os.getuid()
        self.x11 = self.mkdir("x11")
        self.logind = self.mkdir("logind")
        self.runuser = self.mkdir("run-user")
        self.bin = self.mkdir("bin")
        for name, value in (("_X11_SOCK_DIR", self.x11),
                            ("_LOGIND_DIR", self.logind),
                            ("_RUN_USER_DIR", self.runuser)):
            p = mock.patch.object(passthrough, name, value)
            p.start()
            self.addCleanup(p.stop)
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)
        self.calls = []

    # -- the fixture ------------------------------------------------------

    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def touch(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def original(self, *names):
        """The distribution's tools on a PATH of our own."""
        for name in names or ("xdotool", "wmctrl", "xprop", "xrandr"):
            link = os.path.join(self.bin, name)
            if not os.path.exists(link):
                os.symlink(FAKE, link)
        return self.bin

    def wayland(self, **extra):
        """An in-session Wayland environment: the socket exists and is ours."""
        rt = self.mkdir("run-user", str(self.uid))
        self.touch(os.path.join(rt, "wayland-0"))
        e = {"PATH": self.bin, "HOME": self.tmp, "WAYLAND_DISPLAY": "wayland-0",
             "XDG_RUNTIME_DIR": rt}
        e.update({k: v for k, v in extra.items() if v is not None})
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
        return e

    def x11_session(self, **extra):
        """A plain X11 session: `XDG_SESSION_TYPE=x11` and a socket for it."""
        self.touch(os.path.join(self.x11, "X0"))
        e = {"PATH": self.bin, "HOME": self.tmp, "XDG_SESSION_TYPE": "x11",
             "DISPLAY": ":0"}
        e.update(extra)
        return e

    # -- the two seams ----------------------------------------------------

    def seam_proxy(self, display=":42", auth=None):
        """`ensure_proxy` without a proxy: it records that it was asked."""
        def fake(env=None, timeout=None, extra_argv=()):
            self.calls.append("ensure_proxy")
            return display
        for name, fn in (("ensure_proxy", fake),
                         ("proxy_xauthority", lambda: auth)):
            p = mock.patch.object(wrap, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def seam_exec(self):
        def fake(path, argv, env):
            raise ExecCalled(path, argv, env)
        p = mock.patch("os.execve", fake)
        p.start()
        self.addCleanup(p.stop)

    def run_hook(self, tool, args, **kw):
        """maybe_exec_through_proxy with the exec seamed: returns
        (return value, ExecCalled or None)."""
        self.seam_exec()
        try:
            return wrap.maybe_exec_through_proxy(tool, args, **kw), None
        except ExecCalled as e:
            return None, e


class TheRulesThatRunTheClone(WrapCase):
    """Rules 1 to 3 and 5: every one of them answers before a process starts."""

    def test_the_suite_escape_hatch_stops_the_proxy_too(self):
        """`W11_PASSTHROUGH=never` means our own code and no handover to
        anything -- which is why no test file in this suite needed a new line
        when the proxy landed, and why `SuiteGuard` needed no second belt."""
        self.original()
        self.seam_proxy()
        got, _ = self.run_hook("xdotool", ["search", "--class", "foot"],
                               env=self.wayland(W11_PASSTHROUGH="never"))
        self.assertIsNone(got)
        self.assertEqual(self.calls, [], "the proxy was started anyway")

    def test_w11_proxy_never_runs_the_clone(self):
        """The other variable: our own code on Wayland, the original still on
        X11.  The two answers are independent, which is why there are two."""
        self.original()
        self.seam_proxy()
        got, _ = self.run_hook("xdotool", ["search", "foot"],
                               env=self.wayland(W11_PROXY="never"))
        self.assertIsNone(got)
        self.assertEqual(self.calls, [])

    def test_the_per_tool_variable_beats_the_global_one(self):
        """`WDOTOOL_PROXY=never W11_PROXY=auto`: xdotool runs the clone and
        wmctrl still goes through the proxy, in one environment."""
        self.original()
        self.seam_proxy()
        env = self.wayland(W11_PROXY="auto", WDOTOOL_PROXY="never")
        got, _ = self.run_hook("xdotool", ["search", "foot"], env=env)
        self.assertIsNone(got)
        self.assertEqual(self.calls, [])
        _got, execed = self.run_hook("wmctrl", ["-l"], env=env)
        self.assertIsNotNone(execed, "wmctrl was not handed over")
        self.assertEqual(os.path.basename(execed.argv[0]), "wmctrl")

    def test_no_session_at_all_runs_the_clone(self):
        """Neither Wayland nor X11 -- `ssh box wmctrl -l`, a cron job.  The
        clone's own refusal names what it looked for and how to force a
        backend; a proxy with no compositor behind it would answer for an empty
        desktop instead."""
        self.original()
        self.seam_proxy()
        got, _ = self.run_hook("wmctrl", ["-l"],
                               env={"PATH": self.bin, "HOME": self.tmp})
        self.assertIsNone(got)
        self.assertEqual(self.calls, [])

    def test_a_library_caller_is_never_replaced(self):
        """`entry=False` is every in-process `main([...])` in this suite."""
        self.original()
        self.seam_proxy()
        got, _ = self.run_hook("xdotool", ["search", "foot"],
                               entry=False, env=self.wayland())
        self.assertIsNone(got)
        self.assertEqual(self.calls, [])

    def test_no_original_on_path_runs_the_clone_silently(self):
        """The normal wlroots box: no xdotool installed, so there is nothing to
        run through the proxy and the clone is the answer -- with no line of
        explanation, because there is nothing wrong."""
        self.seam_proxy()
        got, _ = self.run_hook("xdotool", ["search", "foot"], env=self.wayland())
        self.assertIsNone(got)
        self.assertEqual(self.calls, [], "a proxy was started for nothing")


class CloneOnlyTokens(WrapCase):
    """Rule 4: an option the original has never had is a request for our code.

    AGENTS.md's rule for the things that are better than X -- they live behind a
    flag the original never had -- read from the other side: a command that
    spells one of those flags is asking for the clone by name."""

    def setUp(self):
        super().setUp()
        self.original()
        self.seam_proxy()

    def clone(self, tool, args, env=None):
        got, execed = self.run_hook(tool, args, env=env or self.wayland())
        self.assertIsNone(got, "%s %s was handed over" % (tool, args))
        self.assertIsNone(execed)
        self.assertEqual(self.calls, [], "a proxy was started for the clone")

    def test_wxrandrs_own_options_stay_ours(self):
        for args in (["--persistent", "--output", "DP-1", "--auto"],
                     ["--backend", "sway"],
                     ["--print-backend"],
                     ["--backends"],
                     ["--gnome-overlap-status"],
                     ["--unsafe-gnome-overlap", "--output", "DP-1", "--pos", "960x0"]):
            with self.subTest(args=args):
                self.clone("xrandr", args)

    def test_wdotools_own_options_and_its_own_command_stay_ours(self):
        for args in (["--layout", "xkb", "type", "a"],
                     ["--vkbd", "always", "key", "a"],
                     ["keys", "explain", "a"]):
            with self.subTest(args=args):
                self.clone("xdotool", args)

    def test_an_output_named_like_one_of_our_options_is_not_one(self):
        """`xrandr --output --persistent --off` names a monitor `--persistent`,
        which the X11 handover already gets right (test_passthrough_exec.py) --
        by asking wxrandr's own argv walker rather than scanning for
        substrings.  The proxy asks the same walker."""
        got, execed = self.run_hook("xrandr", ["--output", "--persistent", "--off"],
                                    env=self.wayland())
        self.assertIsNone(got)
        self.assertIsNotNone(execed, "the original never ran")
        self.assertEqual(execed.argv[1:], ["--output", "--persistent", "--off"])

    def test_always_sends_even_our_own_options_to_the_original(self):
        """`W11_PROXY=always` says "the original's bytes or an error".  The
        original then rejects `--persistent` in its own words, which is what
        the user asked to see."""
        got, execed = self.run_hook("xrandr", ["--persistent", "--output", "DP-1"],
                                    env=self.wayland(W11_PROXY="always"))
        self.assertIsNone(got)
        self.assertIsNotNone(execed)
        self.assertEqual(execed.argv[1:], ["--persistent", "--output", "DP-1"])

    def test_the_table_is_check_docs_own_silent_list(self):
        """`scripts/check-docs.py:SILENT["wxrandr"]` is the single source for
        "an option of ours that is not in xrandr's usage text", and this is what
        keeps `CLONE_ONLY` equal to it.  The two `--q1x` tokens are the
        exception and the reason the comparison is written out rather than
        asserted whole: `--q1` and `--q12` are xrandr's OWN compatibility
        tokens, accepted and ignored, missing from its usage text because it
        never documented them -- they belong to the original and must reach
        it."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("check_docs_mod", CHECK_DOCS)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        silent = set(mod.SILENT["wxrandr"])
        self.assertEqual(set(wrap.CLONE_ONLY["xrandr"]), silent - {"--q1", "--q12"})
        self.assertTrue({"--q1", "--q12"} <= silent, "check-docs no longer lists the q1x pair")


class TheHandover(WrapCase):
    """What the original is exec'd with."""

    def test_the_child_gets_the_proxys_display_and_its_cookie(self):
        """The two values the whole wrapper exists to set, and the trap under
        them: `repair_x_env` fills DISPLAY only when the current one does not
        work (passthrough.py:784), and on a Wayland session Xwayland's `:0`
        works -- so a wrapper that let the repair choose would hand the original
        Xwayland's display and change nothing at all.  Both displays exist here
        for exactly that reason."""
        self.original()
        auth = self.touch(os.path.join(self.tmp, "Xauthority"), "cookie")
        self.touch(os.path.join(self.x11, "X0"))      # Xwayland's
        self.touch(os.path.join(self.x11, "X42"))     # the proxy's
        self.seam_proxy(display=":42", auth=auth)
        env = self.wayland(DISPLAY=":0", XAUTHORITY="/nonexistent/cookie")
        got, execed = self.run_hook("xdotool", ["search", "--class", "foot"], env=env)
        self.assertIsNone(got)
        self.assertIsNotNone(execed, "the original never ran")
        self.assertEqual(execed.env["DISPLAY"], ":42")
        self.assertEqual(execed.env["XAUTHORITY"], auth)
        self.assertEqual(self.calls, ["ensure_proxy"])

    def test_the_argv_is_the_originals_own_name_and_the_arguments_untouched(self):
        self.original()
        self.touch(os.path.join(self.x11, "X42"))
        self.seam_proxy()
        with mock.patch.object(sys, "argv", ["xdotool", "search", "foot"]):
            _got, execed = self.run_hook("xdotool", ["search", "--class", "foot"],
                                         env=self.wayland())
        self.assertEqual(execed.argv, ["xdotool", "search", "--class", "foot"])
        self.assertEqual(os.path.realpath(execed.path), os.path.realpath(FAKE))

    def test_the_handover_guard_is_set_once(self):
        """`_W11_PASSTHROUGH` is the recursion stop and it is a LIST: a wrapper
        that ran `child_env()` itself and then let `exec_real()` run it again
        would name the same original twice and halve the depth the backstop
        allows (GUARD_DEPTH is 8)."""
        self.original()
        self.touch(os.path.join(self.x11, "X42"))
        self.seam_proxy()
        _got, execed = self.run_hook("xdotool", ["key", "a"], env=self.wayland())
        seen = execed.env[passthrough.GUARD_VAR].split(os.pathsep)
        self.assertEqual(seen, [os.path.realpath(FAKE)])

    def test_everything_else_in_the_environment_survives(self):
        """The child is the user's own environment plus two values: a wrapper
        that handed the original `daemon.clean_env`'s allow-list would take
        away the LANG its output is formatted in."""
        self.original()
        self.touch(os.path.join(self.x11, "X42"))
        self.seam_proxy()
        env = self.wayland(LANG="fr_FR.UTF-8", MY_OWN="kept")
        _got, execed = self.run_hook("xdotool", ["key", "a"], env=env)
        self.assertEqual(execed.env["LANG"], "fr_FR.UTF-8")
        self.assertEqual(execed.env["MY_OWN"], "kept")

    def test_no_cookie_anywhere_leaves_xauthority_out(self):
        """sway starts Xwayland with no `-auth` (recon/env.md 2): there is no
        cookie to append and none to hand on, and the empty auth is forwarded
        and accepted."""
        self.original()
        self.touch(os.path.join(self.x11, "X42"))
        self.seam_proxy(auth=None)
        _got, execed = self.run_hook("xdotool", ["key", "a"], env=self.wayland())
        self.assertNotIn("XAUTHORITY", execed.env)


class AlwaysHasNoFallback(WrapCase):
    """`W11_PROXY=always`: the original's bytes, or an error."""

    def test_no_original_is_127_and_says_why_in_the_non_x11_wording(self):
        """`_missing_message(x11=False)`: the reason to install the original is
        the REQUEST and not the session, and "this is an X11 session" would be
        untrue on a Wayland box."""
        self.seam_proxy()
        err = _Sink()
        with mock.patch.object(sys, "stderr", err):
            got, _ = self.run_hook("xdotool", ["search", "foot"],
                                   env=self.wayland(W11_PROXY="always"))
        self.assertEqual(got, 127)
        text = err.getvalue()
        self.assertIn("no real xdotool was found on PATH", text)
        self.assertIn("a handover to the real tool was asked for", text)
        self.assertNotIn("this is an X11 session", text)

    def test_a_proxy_that_will_not_start_is_127(self):
        """The other half of "or an error": the original is installed and the
        proxy did not come up, so there is no display to run it against."""
        self.original()
        p = mock.patch.object(wrap, "ensure_proxy",
                              lambda env=None, timeout=None, extra_argv=(): None)
        p.start()
        self.addCleanup(p.stop)
        err = _Sink()
        with mock.patch.object(sys, "stderr", err):
            got, _ = self.run_hook("xdotool", ["search", "foot"],
                                   env=self.wayland(W11_PROXY="always"))
        self.assertEqual(got, 127)
        self.assertIn("no xw11 proxy could be started", err.getvalue())

    def test_an_unusable_real_override_is_127_in_auto_too(self):
        """A Wayland behaviour this batch added, said out loud because it is a
        change: `WDOTOOL_REAL_XDOTOOL` pointing at something that cannot be
        exec'd (a typo, a directory) was read only on X11 before the wrapper
        existed, and is now read on Wayland as well -- with the same message and
        the same 127 the X11 path has always given.  `real_tool()` raises rather
        than falling back to PATH on purpose (passthrough.py:700: a typo'd
        override that quietly ran something else would be worse), and a wrapper
        that swallowed it would be that same silence one level up.  No
        `W11_PROXY` here: `auto` is the default, which is the point."""
        self.original()
        self.seam_proxy()
        err = _Sink()
        with mock.patch.object(sys, "stderr", err):
            got, execed = self.run_hook(
                "xdotool", ["search", "foot"],
                env=self.wayland(WDOTOOL_REAL_XDOTOOL=self.tmp))    # a directory
        self.assertEqual(got, 127)
        self.assertIsNone(execed)
        self.assertIn("WDOTOOL_REAL_XDOTOOL", err.getvalue())
        self.assertIn("is not an executable file", err.getvalue())
        self.assertEqual(self.calls, [], "a proxy was started for a broken override")

    def test_without_always_the_same_two_run_the_clone(self):
        """The negative twin: the 127s above are the VARIABLE's doing, not the
        wrapper's ordinary behaviour."""
        self.original()
        p = mock.patch.object(wrap, "ensure_proxy",
                              lambda env=None, timeout=None, extra_argv=(): None)
        p.start()
        self.addCleanup(p.stop)
        got, _ = self.run_hook("xdotool", ["search", "foot"], env=self.wayland())
        self.assertIsNone(got)


class HelpNeedsNoProxy(WrapCase):
    """A usage string opens no display, so it starts no proxy.

    The original still runs -- on an X11 session `maybe_exec_real` hands
    `--help` over too (passthrough.py:932 makes the ONE exception for a help
    request with no original installed), and installed as `xdotool` even the
    version string has to be theirs -- but nothing here is worth a process that
    outlives the command by fifteen minutes and pins an Xwayland with it (105 ms
    and one proxy, measured; wrap.POLL_SECONDS)."""

    def setUp(self):
        super().setUp()
        self.original()
        self.seam_proxy()

    def test_the_help_and_version_requests_of_all_four_run_the_original(self):
        for tool, args in (("xdotool", ["--help"]), ("xdotool", ["--version"]),
                           ("xdotool", ["-hv"]), ("wmctrl", ["--help"]),
                           ("xprop", ["-help"]),
                           ("xrandr", ["--help"]), ("xrandr", ["--version"])):
            with self.subTest(tool=tool, args=args):
                _got, execed = self.run_hook(tool, args, env=self.wayland(DISPLAY=":0"))
                self.assertIsNotNone(execed, "%s %s did not reach the original" % (tool, args))
                self.assertEqual(execed.argv[1:], args)
                self.assertEqual(self.calls, [], "a proxy was started for %s %s" % (tool, args))
                # the environment as it was: no display of ours in it, because
                # there is no proxy to point at.
                self.assertEqual(execed.env["DISPLAY"], ":0")

    def test_a_command_that_does_open_a_display_still_starts_one(self):
        """The negative twin: the rule is about help requests and not about
        `xdotool`."""
        _got, execed = self.run_hook("xdotool", ["search", "--class", "foot"],
                                     env=self.wayland())
        self.assertIsNotNone(execed)
        self.assertEqual(self.calls, ["ensure_proxy"])

    def test_a_bare_xrandr_or_xprop_is_a_query_and_gets_a_proxy(self):
        """`passthrough._is_help_request` answers True for an empty argv too --
        right where it is used, wrong here.  Measured 2026-09-11 against the
        pinned pair and the distribution's x11-utils, with DISPLAY unset: bare
        `xdotool` prints its command list and bare `wmctrl` its usage, both
        without a display; bare `xrandr` says "Can't open display" and bare
        `xprop` "unable to open display", because the first prints the screen
        configuration and the second waits for a click.  The last two are the
        proxy's whole subject, so a wrapper that took them for help requests
        would answer them out of Xwayland."""
        for tool in ("xrandr", "xprop"):
            with self.subTest(tool=tool):
                self.calls = []
                _got, execed = self.run_hook(tool, [], env=self.wayland())
                self.assertIsNotNone(execed, "%s never ran" % tool)
                self.assertEqual(execed.env["DISPLAY"], ":42")
                self.assertEqual(self.calls, ["ensure_proxy"])
        for tool in ("xdotool", "wmctrl"):
            with self.subTest(tool=tool):
                self.calls = []
                _got, execed = self.run_hook(tool, [], env=self.wayland())
                self.assertIsNotNone(execed, "%s never ran" % tool)
                self.assertEqual(self.calls, [], "a proxy for a usage string")

    def test_help_with_no_original_is_our_help_even_under_always(self):
        """`W11_PROXY=always` is "the original's bytes or an error" -- except
        for a help request, which must never answer "not found".  The same
        exception `maybe_exec_real` makes on X11."""
        shutil.rmtree(self.bin)
        os.makedirs(self.bin)
        err = _Sink()
        with mock.patch.object(sys, "stderr", err):
            got, execed = self.run_hook("xdotool", ["--help"],
                                        env=self.wayland(W11_PROXY="always"))
        self.assertIsNone(got, "exit %s instead of our own help" % got)
        self.assertIsNone(execed)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.calls, [])


class IsUsKnowsXw11(unittest.TestCase):
    """`passthrough.is_us()` on the proxy's own script.

    The wrapper asks `real_tool()` for the original it is about to run, and
    `real_tool()` walks PATH.  An installed `xw11` that the walk did not
    recognise as ours would be a candidate `xdotool` the day somebody copies
    it under that name -- and, worse, `_exec_plan` refuses to re-exec anything
    on PATH that is not us, so a proxy the guard did not know would never be
    spawned by its own name.

    The name itself and the head sniff on a shim copied under an original's
    name are `tests/test_passthrough.py::TheProxyIsOneOfUs`'s, where the rest of
    `is_us()` is measured; what is here is what the SPAWN does with the
    answer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xw11_isus_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_copy_of_it_under_an_originals_name_is_still_us(self):
        """Guard 2 on its own: the file resolves to a name in OUR_NAMES."""
        real = os.path.join(self.tmp, "xw11")
        with open(real, "w") as f:
            f.write("#!/bin/sh\n:\n")
        os.chmod(real, 0o755)
        alias = os.path.join(self.tmp, "xdotool")
        os.symlink(real, alias)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertTrue(passthrough.is_us(alias))
            self.assertIsNone(passthrough.real_tool("xdotool", {"PATH": self.tmp}))

    def test_the_exec_plan_prefers_the_script_on_path_and_falls_back_to_dash_m(self):
        """What OUR_NAMES buys the spawn: `_exec_plan` re-execs the `xw11` it
        finds on PATH only when `is_us()` says so, and `python3 -m xw11
        __serve` is always behind it -- which is the candidate that runs from a
        source checkout, from a zipapp and in every test in this file, because
        none of them has an installed console script on PATH."""
        script = os.path.join(self.tmp, "xw11")
        with open(script, "w") as f:
            f.write("#!/usr/bin/python3\nfrom xw11.cli import main\nmain()\n")
        os.chmod(script, 0o755)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            plan = wrap._exec_plan({"PATH": self.tmp})
        self.assertEqual(plan[0][0], script)
        self.assertEqual(plan[0][1], [script, "__serve"])
        self.assertEqual(plan[-1][1][1:], ["-m", "xw11", "__serve"])
        # ...and a by-hand `xw11 --passthrough` is carried by BOTH candidates:
        # the installed script is the one a user's box takes and the `-m` route
        # is the one every test here takes, so a flag dropped from either is a
        # flag dropped on somebody (xw11/cli.py:spawn_argv).
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            flagged = wrap._exec_plan({"PATH": self.tmp}, ["--passthrough"])
        self.assertEqual(flagged[0][1], [script, "__serve", "--passthrough"])
        self.assertEqual(flagged[-1][1][1:],
                         ["-m", "xw11", "__serve", "--passthrough"])
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            bare = wrap._exec_plan({"PATH": os.path.join(self.tmp, "empty")})
        self.assertEqual(len(bare), 1, bare)
        self.assertEqual(bare[0][1][1:], ["-m", "xw11", "__serve"])

    def test_a_file_of_that_name_is_ours_by_the_name_alone(self):
        """Guard 2, said out loud because it is the guard that decides here: a
        file that RESOLVES to one of OUR_NAMES is ours whatever is in it.  That
        is what makes an installed `xw11` unmistakable, and it is also why
        `_exec_plan` can only ever be as safe as the PATH it is handed -- the
        same contract every other name in that tuple has had since the
        handover was written."""
        stranger = os.path.join(self.tmp, "xw11")
        with open(stranger, "w") as f:
            f.write("#!/bin/sh\n# somebody else's xw11\nexec /bin/true\n")
        os.chmod(stranger, 0o755)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertTrue(passthrough.is_us(stranger))
            # ...and a file that is NOT called one of our names, with nothing of
            # ours in it, is not ours: the sniff has not become "everything".
            other = os.path.join(self.tmp, "xdotool")
            with open(other, "w") as f:
                f.write("#!/bin/sh\nexec /usr/bin/xdotool-real \"$@\"\n")
            os.chmod(other, 0o755)
            self.assertFalse(passthrough.is_us(other))


# -- the X11 side, in a process of its own ------------------------------------

_CHILD = r"""
import json, os, sys
sys.path.insert(0, %(root)r)
os.environ["W11_PASSTHROUGH"] = "auto"
from w11common import passthrough
for k, v in json.loads(os.environ["SEAMS"]).items():
    setattr(passthrough, k, v)
passthrough.reset_cache()


def boom(path, argv, env):
    raise SystemExit("EXECVE %%s" %% path)


os.execve = boom
from %(mod)s import cli
# sys.argv and not an explicit argv list: `entry = argv is None` in every one of
# the four main()s, and entry=False is rule 1 -- which would answer this
# question with the wrong reason.
sys.argv = %(argv)r
try:
    print("rc: %%s" %% cli.main())
except SystemExit as e:
    print("exit: %%s" %% e)
print(json.dumps(sorted(k for k in sys.modules if k.startswith("xw11"))))
"""


class X11NeverImportsTheProxy(unittest.TestCase):
    """Design section 8.1 rule 3, proved the only way it can be: in a process
    that did not import `xw11` for its own purposes.

    On an X11 session `maybe_exec_real()` replaces the process before the
    wrapper's line is reached, so the proxy is not started, not spawned and not
    even imported -- which is what keeps an X11 desktop paying nothing at all
    for a Wayland feature, and what keeps `tests/test_passthrough.py`'s cases
    green with no new belt."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xw11_child_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.x11 = os.path.join(self.tmp, "x11")
        self.runuser = os.path.join(self.tmp, "run-user")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.x11, self.runuser, self.bin,
                  os.path.join(self.runuser, str(os.getuid()))):
            os.makedirs(d, exist_ok=True)
        os.symlink(FAKE, os.path.join(self.bin, "xdotool"))

    def run_child(self, mod, argv, env):
        src = os.path.join(self.tmp, "child.py")
        with open(src, "w") as f:
            f.write(_CHILD % {"root": ROOT, "mod": mod, "argv": argv})
        e = dict(os.environ)
        e.pop("W11_PASSTHROUGH", None)
        e.update(env)
        e["SEAMS"] = json.dumps({"_X11_SOCK_DIR": self.x11,
                                 "_LOGIND_DIR": os.path.join(self.tmp, "logind"),
                                 "_RUN_USER_DIR": self.runuser})
        e["PATH"] = self.bin
        e["HOME"] = self.tmp
        got = subprocess.run([sys.executable, src], capture_output=True,
                             text=True, timeout=120, env=e)
        self.assertEqual(got.returncode, 0, got.stderr)
        lines = got.stdout.strip().splitlines()
        return lines, json.loads(lines[-1])

    def test_an_x11_session_hands_over_before_the_wrapper_and_imports_nothing(self):
        open(os.path.join(self.x11, "X0"), "w").close()
        lines, modules = self.run_child(
            "wdotool", ["xdotool", "search", "--class", "foot"],
            {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
        self.assertTrue(any("EXECVE" in ln for ln in lines), lines)
        self.assertEqual(modules, [], "the proxy was imported on an X11 session")

    def test_the_same_command_on_wayland_does_import_it(self):
        """The negative twin, without which the test above would pass against a
        wrapper that had never been wired into `main()` at all."""
        open(os.path.join(self.runuser, str(os.getuid()), "wayland-0"), "w").close()
        lines, modules = self.run_child(
            "wdotool", ["xdotool", "search", "--class", "foot"],
            {"WAYLAND_DISPLAY": "wayland-0",
             "XDG_RUNTIME_DIR": os.path.join(self.runuser, str(os.getuid())),
             "W11_PROXY": "never"})
        self.assertIn("xw11.wrap", modules, lines)


# -- the spawn, against a real upstream ---------------------------------------

class SpawnCase(unittest.TestCase):
    """A real `FakeUpstream` on a real socket, a real daemonised proxy.

    The upstream takes a free number out of `display.allocate()` itself, so two
    runs of this file never collide and a real X server on this box is never
    touched.  `x11_mini._SOCK_DIR` is NOT seamed here: a forked child cannot be
    monkeypatched, and a spawn that only works against a seam would be a spawn
    nobody had run."""

    def setUp(self):
        taken = display_mod.allocate()
        self.upstream_num = taken.num
        taken.release()
        self.upstream = support.FakeUpstream(display_mod.sock_dir(),
                                             num=self.upstream_num)
        self.addCleanup(self.upstream.stop)
        # FakeXServer.stop() closes the listener and leaves the path: a socket
        # file with nothing behind it, which is exactly the corpse
        # /tmp/.X11-unix/X8 and X9 on this box already are (recon/env.md 4).
        # The allocator copes with those; a test that makes one still cleans up.
        self.addCleanup(self._unlink, self.upstream.path)
        self.rt = tempfile.mkdtemp(prefix="xw11-wrap-rt-")
        os.chmod(self.rt, 0o700)
        self.addCleanup(shutil.rmtree, self.rt, ignore_errors=True)
        self.log = os.path.join(self.rt, "log")
        # Registered BEFORE anything spawns, so a test that dies half way
        # through its own setup still takes its processes with it. The proxy
        # itself dials the input daemon only for XTEST and no client here sends
        # any, but a daemon left in this runtime directory would outlive the
        # temporary directory it lives in.
        self.addCleanup(support.stop_daemons_under, self.rt)
        self.addCleanup(self.stop_proxy)
        self._env = support.env(XDG_RUNTIME_DIR=self.rt,
                                DISPLAY=":%d" % self.upstream_num,
                                XW11_DISPLAY=None, XW11_LOG=self.log,
                                XW11_DEBUG=None)
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)

    # -- house-keeping ----------------------------------------------------

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except OSError:
            pass

    def stop_proxy(self):
        """Every proxy this class starts, stopped by the pid in ITS OWN display
        file -- never by a pattern over the process table, which on a developer's
        box would take the session's own proxy with it."""
        got = display_mod.read_display_file(
            os.path.join(self.rt, "xw11", "display"))
        if not got:
            return
        _name, pid, _up = got
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)
        raise AssertionError("the proxy (pid %d) would not stop" % pid)

    def settled(self, want, timeout=10.0):
        """The `__serve` processes of ours, once their number has stopped
        moving.  The nine losers of a ten-way race exit on their own as soon as
        the winner's display file is there, and "exactly one is left" is a claim
        about the end of that and not about the microsecond the last thread
        returned in: without this the count read 3 here, all three on their way
        out (measured 2026-09-11)."""
        deadline = time.monotonic() + timeout
        while True:
            got = self.proxies()
            if len(got) == want or time.monotonic() >= deadline:
                return got
            time.sleep(0.05)

    def proxies(self):
        """Every live `__serve` process of ours, by its own display file and by
        the process table -- the second is what makes "exactly one" mean
        anything."""
        out = []
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % p, "rb") as f:
                    argv = f.read().split(b"\0")
                with open("/proc/%s/environ" % p, "rb") as f:
                    env = f.read().split(b"\0")
            except OSError:
                continue
            if b"__serve" not in argv:
                continue
            if ("XDG_RUNTIME_DIR=%s" % self.rt).encode() in env:
                out.append(int(p))
        return out


class TwoStarters(SpawnCase):
    """R11: two wrappers starting two proxies at once.

    Ten threads, ten `ensure_proxy()` calls, one proxy.  The lock is an abstract
    socket bind (`cli.session_lock`) and not a file: a starter that is killed
    between the bind and the display file leaves nothing to recognise, which is
    the failure mode every lock FILE in /tmp has."""

    def test_ten_concurrent_starters_leave_one_serve_and_one_display(self):
        results = []
        errors = []
        barrier = threading.Barrier(10)

        def start():
            try:
                barrier.wait(timeout=30)
                results.append(wrap.ensure_proxy())
            except Exception as e:                  # pragma: no cover - a real failure
                errors.append(e)

        threads = [threading.Thread(target=start) for _ in range(10)]
        began = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        took = time.monotonic() - began
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10, results)
        self.assertEqual(len(set(results)), 1,
                         "the ten starters disagree about the display: %s" % set(results))
        self.assertIsNotNone(results[0], "no proxy came up in %.1fs" % took)
        live = self.settled(1)
        self.assertEqual(len(live), 1, "%d __serve processes: %s" % (len(live), live))
        got = display_mod.read_display_file(os.path.join(self.rt, "xw11", "display"))
        self.assertEqual(got[0], results[0])
        self.assertEqual(got[1], live[0], "the display file names another process")
        self.assertEqual(got[2], ":%d" % self.upstream_num)

    def test_stop_takes_the_one_that_was_left(self):
        """...and `--stop` then leaves none, which is what makes the assertion
        above a fact about processes rather than about a file."""
        self.assertIsNotNone(wrap.ensure_proxy())
        self.assertEqual(len(self.settled(1)), 1)
        self.assertEqual(cli_mod.stop(None), 0)
        self.assertEqual(self.settled(0), [])
        self.assertFalse(os.path.exists(os.path.join(self.rt, "xw11", "display")))


class SpawnEnvironment(SpawnCase):
    """What the spawned proxy carries, read out of /proc.

    A proxy outlives the command that started it by up to fifteen minutes
    (design section 2.6), so it must not pin that command's session state --
    the same rule `daemon.clean_env` was written for (B10), with the compositor
    and X-server variables added back because a proxy needs both."""

    def test_the_environment_is_the_allow_list_and_nothing_else(self):
        """The list is written out here, not read back off `spawn_env()`: a
        `want` derived from the function under test would pass for an allow-list
        that had quietly grown `XDG_SESSION_ID` or lost
        `DBUS_SESSION_BUS_ADDRESS`, and design section 8.2 names the members one
        by one.  `daemon._KEEP_ENV` is cited and not copied because it is that
        module's decision (B10) and this one only adds to it."""
        from wdotool import daemon
        with support.env(DESKTOP_STARTUP_ID="from-the-launcher",
                         W11_NOT_KEPT="x", WDOTOOL_BACKEND="sway",
                         WXRANDR_BACKEND="sway", XW11_DEBUG=None,
                         DBUS_SESSION_BUS_ADDRESS="unix:path=/nowhere/bus",
                         WAYLAND_DISPLAY="wayland-7", XDG_SESSION_ID="42",
                         XAUTHORITY=os.path.join(self.rt, "cookie")):
            self.assertIsNotNone(wrap.ensure_proxy())
            # design section 8.2's list, spelled out: the daemon's own, plus
            # the X server, the compositor and the bus, plus the three prefixes.
            names = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HOME", "PATH",
                     "USER", "LOGNAME", "LANG", "LC_ALL", "SUDO_UID",
                     "PKEXEC_UID", "SWAYSOCK", "I3SOCK",
                     "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                     "HYPRLAND_INSTANCE_SIGNATURE", "WAYFIRE_SOCKET")
            self.assertEqual(sorted(daemon._KEEP_ENV),
                             sorted(n for n in names if n in daemon._KEEP_ENV),
                             "daemon._KEEP_ENV changed under this list")
            want = {n for n in names if n in os.environ}
            want |= {k for k in os.environ
                     if k.startswith(("WDOTOOL_", "WXRANDR_", "XW11_"))}
        pid = self.proxies()[0]
        with open("/proc/%d/environ" % pid, "rb") as f:
            got = dict(kv.decode().split("=", 1)
                       for kv in f.read().split(b"\0") if b"=" in kv)
        # PYTHONPATH is the re-exec plan's own doing: `python -m xw11` needs the
        # package's parent on the path, and for a zipapp that is the .pyz.
        self.assertEqual(set(got) - {"PYTHONPATH"}, want)
        self.assertNotIn("DESKTOP_STARTUP_ID", got)
        self.assertNotIn("W11_NOT_KEPT", got)
        # XDG_SESSION_ID is the near miss: the runtime directory IS kept and
        # this one is not, because a proxy that outlives the command must not
        # carry that command's logind session with it.
        self.assertNotIn("XDG_SESSION_ID", got)
        self.assertEqual(got["WDOTOOL_BACKEND"], "sway")
        self.assertEqual(got["WXRANDR_BACKEND"], "sway",
                         "a backend forced for wxrandr is forced for the proxy too")
        self.assertEqual(got["DISPLAY"], ":%d" % self.upstream_num)
        self.assertEqual(got["XDG_RUNTIME_DIR"], self.rt)
        # the three that are this file's addition to the daemon's list and are
        # each here for a named reason (design section 8.2): the X cookie, the
        # bus the GNOME and KDE backends speak on, the compositor socket.
        self.assertEqual(got["XAUTHORITY"], os.path.join(self.rt, "cookie"))
        self.assertEqual(got["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/nowhere/bus")
        self.assertEqual(got["WAYLAND_DISPLAY"], "wayland-7")

    def test_the_argv_ends_in_the_word_ps_can_be_grepped_for(self):
        """`__daemon`'s precedent: a word no shell user types."""
        self.assertIsNotNone(wrap.ensure_proxy())
        pid = self.proxies()[0]
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [w.decode() for w in f.read().split(b"\0") if w]
        self.assertEqual(argv[-1], wrap.SERVE_ARGV)
        self.assertIn("xw11", argv[-2])

    def test_it_is_out_of_the_launchers_process_group_and_cwd(self):
        """The double fork, from the outside: a proxy in the launcher's session
        dies with the terminal that started it, and one holding the launcher's
        cwd keeps a removed directory busy."""
        self.assertIsNotNone(wrap.ensure_proxy())
        pid = self.proxies()[0]
        self.assertNotEqual(os.getsid(pid), os.getsid(os.getpid()))
        self.assertEqual(os.readlink("/proc/%d/cwd" % pid), "/")
        self.assertNotEqual(os.getpgid(pid), os.getpgid(os.getpid()))


class PrintDisplayAndStop(SpawnCase):
    """`xw11 --print-display` and `xw11 --stop`, the two by-hand commands."""

    def parse(self, argv):
        return cli_mod.build_parser().parse_args(argv)

    def test_it_starts_one_and_the_second_call_prints_the_same(self):
        """`DISPLAY=$(xw11 --print-display) python3 my-xlib-script.py` is the
        long tail's whole interface, so the second script of the session has to
        get the first one's display and not a second proxy."""
        out = _Sink()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli_mod.print_display(self.parse(["--print-display"])), 0)
            first = out.getvalue().strip()
            self.assertEqual(cli_mod.print_display(self.parse(["--print-display"])), 0)
        both = out.getvalue().split()
        self.assertEqual(both, [first, first])
        self.assertEqual(len(self.settled(1)), 1)

    def test_stop_removes_the_display_file_the_lock_and_the_socket(self):
        got = wrap.ensure_proxy()
        self.assertIsNotNone(got)
        num = int(got[1:])
        self.assertTrue(os.path.exists(display_mod.sock_path(num)))
        self.assertTrue(os.path.exists(display_mod.lock_path(num)))
        self.assertEqual(cli_mod.stop(None), 0)
        self.assertEqual(self.settled(0), [])
        self.assertFalse(os.path.exists(os.path.join(self.rt, "xw11", "display")))
        self.assertFalse(os.path.exists(display_mod.sock_path(num)))
        self.assertFalse(os.path.exists(display_mod.lock_path(num)))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        with self.assertRaises(OSError):
            s.connect("\0" + display_mod.sock_path(num))
        s.close()

    def test_bare_xw11_daemonises_and_prints_where_it_went(self):
        """The default with no option at all.  `--foreground` is what the rigs,
        the parity oracle and every live test pass, so the bare form -- the one
        a user types -- is the one nothing else exercises: it has to come back,
        which means the loop is NOT running in this process, and it has to say
        the display, which means the proxy is up before it says anything."""
        out = _Sink()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli_mod._run([]), 0)
        got = out.getvalue().strip()
        self.assertRegex(got, r"^:\d+$")
        self.assertEqual(len(self.settled(1)), 1)
        self.assertEqual(display_mod.read_display(), got)

    def test_the_flags_of_a_by_hand_command_reach_the_daemonised_child(self):
        """`xw11 --passthrough` is a by-hand command (design section 8.2) and it
        has to start a FORWARDING proxy.  The spawn re-execs `xw11 __serve`, so
        a flag that `spawn_argv` does not carry is a flag argparse accepted and
        threw away -- and a user who asked for pure forwarding would get the
        synthesizing default with nothing said.  Read off the child's own
        /proc/<pid>/cmdline, which is the only place the answer is."""
        out = _Sink()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli_mod._run(["--passthrough"]), 0)
        self.assertRegex(out.getvalue().strip(), r"^:\d+$")
        pid = self.settled(1)[0]
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [w.decode() for w in f.read().split(b"\0") if w]
        self.assertEqual(argv[-2:], ["__serve", "--passthrough"])

    def test_the_upstream_a_by_hand_command_names_reaches_it_too(self):
        """The same for `--upstream :M`, which without this goes to $DISPLAY --
        a proxy in front of a server the user did not name."""
        out = _Sink()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli_mod._run(["--print-display", "--upstream",
                                           ":%d" % self.upstream_num]), 0)
        pid = self.settled(1)[0]
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [w.decode() for w in f.read().split(b"\0") if w]
        self.assertEqual(argv[-3:], ["__serve", "--upstream",
                                     ":%d" % self.upstream_num])
        got = display_mod.read_display_file(
            os.path.join(self.rt, "xw11", "display"))
        self.assertEqual(got[2], ":%d" % self.upstream_num)

    def test_a_second_proxy_is_refused_rather_than_answered_with_the_first(self):
        """The other half of carrying the flags: a session has one proxy (the
        lock, R11), so when one is already up there is nothing to start -- and
        answering `--passthrough` with the running synthesizing proxy would be
        the same silent drop one level along.  `--foreground` is the way to have
        a second one, which is what scripts/parity-oracle.sh does."""
        first = wrap.ensure_proxy()
        self.assertIsNotNone(first)
        err = _Sink()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(cli_mod._run(["--passthrough"]), 1)
            self.assertEqual(cli_mod._run(["--print-display", "--passthrough"]), 1)
        text = err.getvalue()
        self.assertIn("a proxy is already running on %s" % first, text)
        self.assertIn("--foreground", text)
        self.assertEqual(len(self.settled(1)), 1, "a second proxy was started")
        self.assertEqual(display_mod.read_display(), first)

    def test_print_display_with_no_flags_is_still_a_lookup(self):
        """The negative twin of the refusal above: the bare `--print-display`
        exists to ANSWER with the running proxy -- that is the long tail's whole
        interface -- and only a flag that describes a different proxy turns it
        into a start."""
        first = wrap.ensure_proxy()
        out = _Sink()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(cli_mod._run(["--print-display"]), 0)
        self.assertEqual(out.getvalue().strip(), first)
        self.assertEqual(len(self.settled(1)), 1)

    def test_stop_with_no_proxy_says_so_and_exits_1(self):
        out = _Sink()
        with mock.patch.object(sys, "stderr", out):
            self.assertEqual(cli_mod.stop(None), 1)
        self.assertIn("no proxy is running", out.getvalue())


class WhichArgvReachesTheHook(unittest.TestCase):
    """Rule 4 from the OTHER side: what each `main()` has already done to the
    argv, and to the command, before the hook is called at all.

    Two of `CLONE_ONLY`'s tokens never get there, and that is right rather than
    a gap: `wdotool keys` and `wxrandr --print-backend` describe our own code
    and have nothing in the original to hand over to, so their `main()` answers
    and returns several screens above the call -- `W11_PROXY=always` included,
    because "the original's bytes" is not an answer to a question the original
    has never been asked.  The two that DO get there have to arrive whole: both
    `main()`s strip their own leading options before the X11 handover (the real
    xdotool has never had `--layout`), and an argv the hook received stripped
    would send `--layout xkb type a` to the original with the layout silently
    gone -- rule 4 broken in the only place it is ever really asked.

    The hook itself is seamed: what is measured here is the argv it is handed,
    which is the one thing `tests/test_xw11_wrap.py`'s other classes cannot see
    (they call it directly)."""

    def setUp(self):
        self.seen = []
        self.out = _Sink()
        for name in ("stdout", "stderr"):
            p = mock.patch.object(sys, name, self.out)
            p.start()
            self.addCleanup(p.stop)

        def spy(tool, args, *, entry=True, env=None):
            self.seen.append((tool, list(args)))
            return 0            # "handled": main() returns here and runs nothing

        p = mock.patch.object(wrap, "maybe_exec_through_proxy", spy)
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, mod, argv):
        with mock.patch.object(sys, "argv", argv):
            return mod.main()

    def test_wdotools_own_leading_options_reach_the_hook_unstripped(self):
        from wdotool import cli as wdotool_cli
        argv = ["--layout", "xkb", "type", "a"]
        self.assertEqual(self.run_main(wdotool_cli, ["xdotool"] + argv), 0)
        self.assertEqual(self.seen, [("xdotool", argv)])

    def test_wxrandrs_own_apply_option_reaches_it_unstripped(self):
        from wxrandr import cli as wxrandr_cli
        argv = ["--persistent", "--output", "DP-1", "--auto"]
        self.assertEqual(self.run_main(wxrandr_cli, ["xrandr"] + argv), 0)
        self.assertEqual(self.seen, [("xrandr", argv)])

    def test_the_ordinary_argv_reaches_it_unchanged(self):
        from wdotool import cli as wdotool_cli
        argv = ["search", "--class", "foot"]
        self.assertEqual(self.run_main(wdotool_cli, ["xdotool"] + argv), 0)
        self.assertEqual(self.seen, [("xdotool", argv)])

    def test_the_commands_that_answer_for_themselves_never_reach_it(self):
        from wdotool import cli as wdotool_cli
        from wxrandr import cli as wxrandr_cli
        with support.env(W11_PROXY="always"):
            self.assertEqual(self.run_main(wdotool_cli,
                                           ["xdotool", "keys", "explain", "a"]), 0)
            self.assertEqual(self.run_main(wxrandr_cli,
                                           ["xrandr", "--print-backend"]), 0)
        self.assertEqual(self.seen, [])
        self.assertIn("wdotool type 'a'", self.out.getvalue())


class TheUpstreamItFindsItself(unittest.TestCase):
    """Which X server the daemonised proxy forwards to (design section 8.2).

    `--upstream`, else `$DISPLAY`, else `session.find_x_display()` -- and the
    third is the one that matters, because the proxy is spawned out of a wrapper
    that may have no DISPLAY at all: `sudo xdotool key a`, a cron line, `ssh
    root@box`.  That is the case `passthrough.repair_x_env` exists for, and the
    wrapper runs the repair only AFTER `ensure_proxy()` has come back -- so
    without this fallback the child died "no upstream display", the wrapper
    polled its two seconds and fell to the clone, on exactly the boxes the
    repair was written for.

    Seamed and not spawned: `find_x_display()` answers out of
    /tmp/.X11-unix, which on this box holds two corpses of its own
    (recon/env.md 4) plus whatever else is running, so a spawned child would be
    measuring the box and not the code."""

    def args(self, *argv):
        return cli_mod.build_parser().parse_args(list(argv))

    def test_the_flag_wins_over_the_environment(self):
        with support.env(DISPLAY=":1"):
            self.assertEqual(cli_mod._upstream(self.args("--upstream", ":3")), ":3")

    def test_display_wins_over_the_search(self):
        with mock.patch.object(cli_mod.session, "find_x_display",
                               lambda uid=None: ":9"), support.env(DISPLAY=":1"):
            self.assertEqual(cli_mod._upstream(self.args()), ":1")

    def test_with_no_display_it_finds_the_sessions_own(self):
        with mock.patch.object(cli_mod.session, "find_x_display",
                               lambda uid=None: ":9"), support.env(DISPLAY=None):
            self.assertEqual(cli_mod._upstream(self.args()), ":9")

    def test_with_nothing_to_find_it_says_so_and_names_both_ways_out(self):
        with mock.patch.object(cli_mod.session, "find_x_display",
                               lambda uid=None: None), support.env(DISPLAY=None):
            with self.assertRaises(ValueError) as caught:
                cli_mod._upstream(self.args())
        self.assertIn("--upstream", str(caught.exception))
        self.assertIn("$DISPLAY", str(caught.exception))


class TheTwoRefusals(unittest.TestCase):
    """What the proxy will not do, and what each refusal owes the reader."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xw11-refuse-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_sock = display_mod.x11_mini._SOCK_DIR
        self._old_lock = display_mod._LOCK_DIR
        display_mod.x11_mini._SOCK_DIR = self.tmp
        display_mod._LOCK_DIR = self.tmp
        self.addCleanup(self._restore)
        self.err = _Sink()
        p = mock.patch.object(sys, "stderr", self.err)
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        display_mod.x11_mini._SOCK_DIR = self._old_sock
        display_mod._LOCK_DIR = self._old_lock

    def serve(self, *argv):
        return cli_mod.serve(cli_mod.build_parser().parse_args(list(argv)))

    def test_it_refuses_to_sit_in_front_of_itself(self):
        """`XW11_DISPLAY=:7 DISPLAY=:7`: every request would be forwarded to
        this process's own loop, which is waiting to write the answer."""
        with support.env(XW11_DISPLAY=":7", DISPLAY=":7"):
            self.assertEqual(self.serve("--foreground"), 1)
        self.assertIn("is the upstream display", self.err.getvalue())

    def test_it_never_starts_an_xwayland_and_says_what_would(self):
        """AGENTS.md's shape for a thing we do not do yet: the route that would
        close it and what that route costs.  Nothing answers :7 here and
        `xwayland_running()` is seamed false, which is the box with a compositor
        that has not started its X server."""
        with mock.patch.object(cli_mod.session, "xwayland_running",
                               lambda uid=None: False), \
                support.env(XW11_DISPLAY=None, DISPLAY=None):
            self.assertEqual(self.serve("--foreground", "--upstream", ":7"), 1)
        text = self.err.getvalue()
        self.assertIn("no X server answers :7", text)
        self.assertIn("not yet", text)
        self.assertIn("AGENTS.md route 2", text)

    def test_a_live_upstream_is_not_refused(self):
        """The negative twin: the refusal is about there being no X server, not
        about `xwayland_running()` -- a listener at :7 is enough, which is what
        an Xvfb or a second X server is."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(self.tmp, "X7"))
        s.listen(1)
        self.addCleanup(s.close)
        with mock.patch.object(cli_mod.session, "xwayland_running",
                               lambda uid=None: False):
            args = cli_mod.build_parser().parse_args(["--upstream", ":7"])
            self.assertIsNone(cli_mod._refusal(args, ":7"))


if __name__ == "__main__":
    unittest.main()
