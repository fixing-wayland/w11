#!/usr/bin/env python3
"""The handover itself, against a fake install tree — real processes only.

The tree mirrors what the README tells people to build:

    <tmp>/local/{xdotool,wmctrl,xprop,xrandr}  -> tests/fixtures/w11_shim.py
                                                  (us, "installed over")
    <tmp>/bin/{xdotool,wmctrl,xprop,xrandr}    -> tests/fixtures/fake_real_tool.py
                                                  (the distribution's)
    <tmp>/pybin/python3                        -> sys.executable

with `PATH=<tmp>/local:<tmp>/bin:<tmp>/pybin` and nothing else, so no real
xdotool on the developer's box can take part. The session is described
entirely by the seam directories (`$W11_SHIM_SEAMS`) and the environment.

The crisp assertion that this is an `execve` and not a `subprocess` is that
the *fake's own pid, logged from inside it, equals the pid we started*.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from w11common import distro  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SHIM = os.path.join(FIXTURES, "w11_shim.py")
FAKE = os.path.join(FIXTURES, "fake_real_tool.py")
TOOLS = ("xdotool", "wmctrl", "xprop", "xrandr")
HAVE_BASH = shutil.which("bash")
CAT = shutil.which("cat")
HEAD = shutil.which("head")


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11_exec_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.local = self.mkdir("local")
        self.bin = self.mkdir("bin")
        self.pybin = self.mkdir("pybin")
        self.x11 = self.mkdir("x11")
        self.logind = self.mkdir("logind")
        self.runuser = self.mkdir("run-user", str(os.getuid()))
        os.symlink(sys.executable, os.path.join(self.pybin, "python3"))
        for name in TOOLS:
            os.symlink(SHIM, os.path.join(self.local, name))
            os.symlink(FAKE, os.path.join(self.bin, name))
        self.log = os.path.join(self.tmp, "log")

    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def path(self, real=True):
        dirs = [self.local] + ([self.bin] if real else []) + [self.pybin]
        return os.pathsep.join(dirs)

    def env(self, real=True, **extra):
        e = {
            "PATH": self.path(real),
            "HOME": self.tmp,
            "XDG_SESSION_TYPE": "x11",
            "DISPLAY": ":0",
            "FAKE_REAL_LOG": self.log,
            # This file is about the X11 handover, and every case in it runs a
            # real process on a session it seams into being.  Since the X11
            # proxy landed, a *Wayland* session in that shape has a second
            # handover under the first one -- and the wrapper would spawn a
            # real `xw11 __serve` out of the test, against whatever X server
            # this box has at $DISPLAY, leaving it running for fifteen minutes
            # (measured: `test_x11_on_a_wayland_session_hands_over` did exactly
            # that, and only when an Xwayland happened to be up, which is a
            # test that passes or fails by what else is running).  `never` here
            # keeps this file's subject; the proxy's own handover is
            # tests/test_xw11_wrap.py's, and the one case in this file that
            # wants it asks for `auto` by name.
            "W11_PROXY": "never",
            "W11_SHIM_SEAMS": json.dumps({
                "_X11_SOCK_DIR": self.x11, "_LOGIND_DIR": self.logind,
                "_RUN_USER_DIR": os.path.dirname(self.runuser)}),
        }
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def run_tool(self, tool, *args, **kw):
        env = kw.pop("env", None) or self.env()
        p = subprocess.Popen([os.path.join(self.local, tool)] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, **kw)
        out, err = p.communicate(timeout=60)
        return p, out.decode(), err.decode()

    def records(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]


class OurOwnOptions(Tree):
    """wxrandr has options real xrandr has never heard of, and an X11 session
    hands the argv over untouched. Measured in the 0.4 retest on
    `resolute-kde-x11` and `noble-xfce`: `--persistent` reached the original,
    which answered `unrecognized option '--persistent'` and exit 1 with the
    layout unchanged -- while WXRANDR.md says an X11 apply saves nothing,
    which reads as an apply that works."""

    def test_persistent_is_dropped_and_the_apply_still_happens(self):
        p, out, err = self.run_tool("xrandr", "--persistent",
                                    "--output", "DP-1", "--auto")
        self.assertEqual(p.returncode, 0, err)
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0]["argv"], ["--output", "DP-1", "--auto"])
        self.assertEqual(recs[0]["pid"], p.pid)      # still an execve

    def test_an_output_named_like_the_option_is_handed_over_whole(self):
        p, out, err = self.run_tool("xrandr", "--output", "--persistent", "--off")
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(self.records()[0]["argv"],
                         ["--output", "--persistent", "--off"])

    def test_the_overlap_flag_is_refused_in_the_words_every_other_session_uses(self):
        p, out, err = self.run_tool("xrandr", "--unsafe-gnome-overlap",
                                    "--output", "DP-1", "--pos", "960x0")
        self.assertEqual(p.returncode, 1, out)
        self.assertIn("--unsafe-gnome-overlap only means anything on GNOME", err)
        self.assertIn("this session is x11", err)
        self.assertEqual(self.records(), [])    # the original never ran

    def test_the_bookkeeping_options_answer_for_themselves(self):
        for opt in ("--gnome-overlap-status", "--gnome-overlap-forget"):
            os.path.exists(self.log) and os.remove(self.log)
            p, out, err = self.run_tool("xrandr", opt)
            self.assertEqual(self.records(), [], opt)
            self.assertNotIn("unrecognized", err, opt)


class Handover(Tree):
    def test_argv_survives_and_it_is_an_exec(self):
        p, out, err = self.run_tool("xdotool", "search", "--name", "x y",
                               "windowactivate", "--sync")
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("fake-real-tool xdotool", out)
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        self.assertEqual(r["argv"],
                         ["search", "--name", "x y", "windowactivate", "--sync"])
        self.assertEqual(os.path.basename(r["argv0"]), "xdotool")
        # execve, not subprocess: same process, so the same pid
        self.assertEqual(r["pid"], p.pid)

    def test_every_tool_hands_over_with_its_own_argv(self):
        cases = {"xdotool": ["key", "a"], "wmctrl": ["-l", "-G", "-p", "-x"],
                 "xprop": ["-root", "WM_CLASS"], "xrandr": ["--query"]}
        for tool, args in cases.items():
            os.path.exists(self.log) and os.remove(self.log)
            p, out, err = self.run_tool(tool, *args)
            self.assertEqual(p.returncode, 0, err)
            recs = self.records()
            self.assertEqual(len(recs), 1, (tool, recs))
            self.assertEqual(recs[0]["argv"], args, tool)
            self.assertEqual(os.path.basename(recs[0]["argv0"]), tool)
            self.assertEqual(recs[0]["pid"], p.pid, tool)

    def test_exit_code_propagates(self):
        p, out, err = self.run_tool("xdotool", "key", "a",
                               env=self.env(FAKE_REAL_RC="42"))
        self.assertEqual(p.returncode, 42)

    def test_signal_death_propagates(self):
        """`xprop` killed by SIGINT has to look killed, not exit 130 — which
        is free with execve and impossible to get exactly right by hand."""
        p, out, err = self.run_tool("xprop", "-root",
                               env=self.env(FAKE_REAL_SIGNAL="15"))
        self.assertEqual(p.returncode, -15)
        p, out, err = self.run_tool("xprop", "-root",
                               env=self.env(FAKE_REAL_SIGNAL="2"))
        self.assertEqual(p.returncode, -2)

    def shell_fake(self, tool, body):
        """Replace the fake original with a shell script. The originals are
        compiled binaries; a *Python* stand-in cannot see what it inherited
        (CPython installs SIG_IGN for SIGPIPE itself at startup) and cannot
        see argv[0] either (the kernel replaces it with the script path when
        it follows a shebang), so the two tests that care about exactly those
        use /bin/sh instead."""
        p = os.path.join(self.bin, tool)
        os.remove(p)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(p, 0o755)
        return p

    @unittest.skipUnless(HAVE_BASH and CAT and HEAD,
                         "needs bash (pipefail), cat and head")
    def test_sigpipe_is_restored(self):
        """`xprop -root | head -1`: Python *ignores* SIGPIPE, and an ignored
        disposition survives execve, so without the SIG_DFL reset the original
        would print `write error: Broken pipe` and exit 1 where it used to die
        quietly of the signal."""
        big = os.path.join(self.tmp, "big")
        with open(big, "w") as f:
            f.writelines("line %d\n" % i for i in range(200000))
        self.shell_fake("xprop", "exec '%s' '%s'\n" % (CAT, big))
        # absolute paths: the fake tree's PATH holds nothing but the tools
        cmd = "set -o pipefail; '%s' -root | '%s' -1" % (
            os.path.join(self.local, "xprop"), HEAD)
        p = subprocess.run([HAVE_BASH, "-c", cmd], env=self.env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=60)
        self.assertEqual(p.stdout, b"line 0\n")
        self.assertEqual(p.stderr, b"")
        self.assertEqual(p.returncode, 141)      # 128 + SIGPIPE

    def test_ignored_signals_are_reset(self):
        """The same thing seen directly: nothing is ignored in the child."""
        out = os.path.join(self.tmp, "sigign")
        # shell builtins only: the fake tree's PATH has no /bin in it
        self.shell_fake("xdotool",
                        "while read -r l; do case \"$l\" in SigIgn:*)"
                        " echo \"$l\" > '%s';; esac; done"
                        " < /proc/self/status\n" % out)
        p, _o, err = self.run_tool("xdotool", "key", "a")
        self.assertEqual(p.returncode, 0, err)
        with open(out) as f:
            mask = int(f.read().split()[1], 16)
        import signal
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.assertFalse(mask & (1 << (sig - 1)), "%s still ignored" % sig)

    def test_argv0_is_the_originals_own_name(self):
        """`wmctrl: invalid option -- 'x'`, not
        `/usr/bin/wmctrl: invalid option`: the original prints argv[0]."""
        p, out, err = self.run_tool(
            "xdotool", "-c", "printf %s \"$0\"",
            env=self.env(WDOTOOL_REAL_XDOTOOL="/bin/sh"))
        self.assertEqual(out, "xdotool")

    def test_stdio_is_the_real_thing(self):
        """No pipe in between: the original writes to our fds."""
        out_file = os.path.join(self.tmp, "out")
        with open(out_file, "w") as f:
            p = subprocess.Popen([os.path.join(self.local, "xdotool"), "key", "a"],
                                 stdout=f, env=self.env())
            p.wait(timeout=60)
        with open(out_file) as f:
            self.assertEqual(f.read(), "fake-real-tool xdotool\n")


class Environment(Tree):
    def test_display_and_xauthority_are_repaired(self):
        """The sudo/cron case: no DISPLAY, no XAUTHORITY in the environment.
        We know how to find the session's X plane, so the original gets it."""
        open(os.path.join(self.x11, "X99"), "w").close()
        cookie = os.path.join(self.runuser, "xauth_abc")
        open(cookie, "w").close()
        env = self.env(DISPLAY=None, XAUTHORITY=None,
                       XDG_RUNTIME_DIR=self.runuser)
        p, out, err = self.run_tool("xdotool", "key", "a", env=env)
        self.assertEqual(p.returncode, 0, err)
        r = self.records()[0]
        self.assertEqual(r["env"]["DISPLAY"], ":99")
        self.assertEqual(r["env"]["XAUTHORITY"], cookie)

    def test_a_working_display_is_left_alone(self):
        open(os.path.join(self.x11, "X0"), "w").close()
        open(os.path.join(self.x11, "X99"), "w").close()
        p, out, err = self.run_tool("xdotool", "key", "a")
        self.assertEqual(self.records()[0]["env"]["DISPLAY"], ":0")

    def test_guard_variable_is_set_for_the_child(self):
        p, out, err = self.run_tool("xdotool", "key", "a")
        self.assertEqual(self.records()[0]["env"]["_W11_PASSTHROUGH"],
                         os.path.realpath(FAKE))

    def test_wayland_session_never_hands_over(self):
        """The whole point of the ordering: a live compositor socket wins even
        though DISPLAY is set (Xwayland always sets it).

        `W11_PROXY=never` (out of `env()`, which sets it for every case in this
        file) because this file is about `maybe_exec_real` and the X11
        handover, and a Wayland session with the original installed now has a
        SECOND handover below it -- the wrapper's, through the proxy
        (xw11/wrap.py, tests/test_xw11_wrap.py).  Without it the fake below
        would run for that other reason and this case would stop being about
        the session ordering at all; the case underneath it is the next
        test."""
        open(os.path.join(self.runuser, "wayland-0"), "w").close()
        env = self.env(XDG_SESSION_TYPE="wayland",         # W11_PROXY=never: see env()
                       XDG_RUNTIME_DIR=self.runuser, WAYLAND_DISPLAY="wayland-0")
        p, out, err = self.run_tool("xdotool", "version", env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("xdotool version 4.", out)        # ours, not the fake's
        self.assertEqual(self.records(), [])

    def test_a_wayland_session_with_the_original_installed_hands_over_below(self):
        """...and the same command without that variable is this batch's whole
        claim, in a real process: on a Wayland session with the original on
        PATH, `xdotool version` is the ORIGINAL's version string, because the
        original is what will run every other command there (design section
        8.1).  `version` is a help request (passthrough.py:763), so it reaches
        the original with no proxy started at all -- a version string opens no
        display."""
        open(os.path.join(self.runuser, "wayland-0"), "w").close()
        env = self.env(XDG_SESSION_TYPE="wayland", W11_PROXY="auto",
                       XDG_RUNTIME_DIR=self.runuser, WAYLAND_DISPLAY="wayland-0")
        p, out, err = self.run_tool("xdotool", "version", env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("fake-real-tool xdotool", out)
        self.assertEqual(self.records()[0]["argv"], ["version"])
        # and no DISPLAY of the proxy's in it: there is no proxy.
        self.assertEqual(self.records()[0]["env"]["DISPLAY"], ":0")

    def test_escape_hatch_keeps_our_own_code(self):
        """`W11_PASSTHROUGH=never` — what the test suite and the
        parity oracle rely on, on an X11 box with the real tools installed."""
        p, out, err = self.run_tool("xdotool", "version",
                               env=self.env(W11_PASSTHROUGH="never"))
        self.assertEqual(p.returncode, 0)
        self.assertIn("xdotool version 4.", out)
        self.assertEqual(self.records(), [])
        p, out, err = self.run_tool("xdotool", "version",
                               env=self.env(WDOTOOL_PASSTHROUGH="never"))
        self.assertIn("xdotool version 4.", out)

    def test_forced_passthrough_on_a_wayland_box(self):
        open(os.path.join(self.runuser, "wayland-0"), "w").close()
        env = self.env(XDG_SESSION_TYPE="wayland", XDG_RUNTIME_DIR=self.runuser,
                       WAYLAND_DISPLAY="wayland-0",
                       W11_PASSTHROUGH="always")
        p, out, err = self.run_tool("xdotool", "key", "a", env=env)
        self.assertEqual(len(self.records()), 1)


class NoOriginal(Tree):
    def test_the_127_line_names_this_boxs_own_package_manager(self):
        """U14, the subprocess half: four real processes, four exit 127s, each naming the package the box it
        is running on actually has.

        The line said `apt install ...` everywhere, on every distribution: a NixOS box with no apt on it was
        told to run apt, and Fedora and Arch were named packages that do not exist there [M recon2/nixos.md,
        fedora.md, arch.md].  The expected text comes from `w11common.distro`, whose seam is a module constant
        no subprocess can be handed, so what this pins is the whole path -- PATH walk, refusal, exit status --
        on whatever family the runner is; the byte-exact table for the other four families is
        tests/test_passthrough.py:MissingOriginalPerDistro.

        xprop is not here: it has an X11 client of its own and never exits 127 (the test below)."""
        from w11common import distro
        for tool in ("xdotool", "wmctrl", "xrandr"):
            with self.subTest(tool=tool):
                args = {"xdotool": ("key", "a"), "wmctrl": ("-l",), "xrandr": ("--query",)}[tool]
                p, out, err = self.run_tool(tool, *args, env=self.env(real=False))
                self.assertEqual(p.returncode, 127, err)
                self.assertEqual(out, "")
                self.assertIn("(%s)" % distro.hint(tool), err)

    def test_missing_original_is_127_with_a_useful_message(self):
        p, out, err = self.run_tool("xdotool", "key", "a", env=self.env(real=False))
        self.assertEqual(p.returncode, 127)
        self.assertIn(distro.hint("xdotool"), err)
        self.assertIn("WDOTOOL_REAL_XDOTOOL", err)
        self.assertEqual(out, "")

    def test_the_reason_named_at_127_is_the_one_that_applies(self):
        """The line says why the original is wanted, and a forced handover is not an X11 session.

        Reached with W11_PASSTHROUGH=always (and by `wxrandr --backend x11`) on a Wayland
        desktop, where "this is an X11 session" would simply be false."""
        p, out, err = self.run_tool("xdotool", "key", "a", env=self.env(real=False))
        self.assertEqual(p.returncode, 127)
        self.assertIn("this is an X11 session", err)

        open(os.path.join(self.runuser, "wayland-0"), "w").close()
        env = self.env(real=False, XDG_SESSION_TYPE="wayland",
                       XDG_RUNTIME_DIR=self.runuser, WAYLAND_DISPLAY="wayland-0",
                       W11_PASSTHROUGH="always")
        p, out, err = self.run_tool("xdotool", "key", "a", env=env)
        self.assertEqual(p.returncode, 127)
        self.assertNotIn("this is an X11 session", err)
        self.assertIn("a handover to the real tool was asked for", err)
        self.assertIn(distro.hint("xdotool"), err)

    def test_help_and_version_still_answer(self):
        """M3: a help request must never exit 127."""
        for args, want in ((["--version"], "xdotool version 4."),
                           (["version"], "xdotool version 4."),
                           (["--help"], "Available commands:")):
            p, out, err = self.run_tool("xdotool", *args, env=self.env(real=False))
            self.assertEqual(p.returncode, 0, (args, err))
            self.assertIn(want, out + err, args)
            self.assertNotIn(distro.hint("xdotool"), err, args)
        p, out, err = self.run_tool("wmctrl", "--help", env=self.env(real=False))
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("wmctrl", out)
        # xrandr --version needs a display, like the original: our own code
        # answers (whatever it then says), the passthrough does not intercede
        p, out, err = self.run_tool("xrandr", "--version", env=self.env(real=False))
        self.assertNotEqual(p.returncode, 127)
        self.assertNotIn(distro.hint("xdotool"), err)

    def test_wxprop_falls_back_to_its_own_x11_client(self):
        """wxprop has a real X11 client of its own, so it keeps working with
        no x11-utils installed — the one tool that never says 127."""
        env = self.env(real=False, DISPLAY=":123")      # nothing listening
        p, out, err = self.run_tool("xprop", "-root", "WM_CLASS", env=env)
        self.assertNotEqual(p.returncode, 127)
        self.assertNotIn(distro.hint("xdotool"), err)
        self.assertIn("xprop:", err)

    def test_override_beats_path(self):
        other = os.path.join(self.tmp, "elsewhere-xdotool")
        os.symlink(FAKE, other)
        p, out, err = self.run_tool("xdotool", "key", "a",
                               env=self.env(WDOTOOL_REAL_XDOTOOL=other))
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(len(self.records()), 1)
        # the shebang mechanism replaces argv[0] with the script path, so what
        # the fake logs as argv0 names the file we actually exec'd
        self.assertEqual(self.records()[0]["argv0"], other)

    def test_unusable_override_is_a_distinct_error(self):
        dud = os.path.join(self.tmp, "not-executable")
        open(dud, "w").close()
        p, out, err = self.run_tool("xdotool", "key", "a",
                               env=self.env(WDOTOOL_REAL_XDOTOOL=dud))
        self.assertEqual(p.returncode, 127)
        self.assertIn("WDOTOOL_REAL_XDOTOOL", err)
        self.assertNotIn(distro.hint("xdotool"), err)    # not the "nothing found" one


class Recursion(Tree):
    """Two *copies* of the clone under two names in two PATH directories: the
    samefile, realpath-name and head-sniff guards all miss it (that is what
    they cost), so the environment guard has to stop it — and stop it at the
    second process, not the eighth."""

    def clone_copy(self, path, counter):
        # deliberately hides the marker words from the head sniff: this is
        # the case the environment guard exists for.
        src = ("#!/usr/bin/env python3\n"
               "import os, sys\n"
               "open(%r, 'a').write('x')\n"
               "sys.path.insert(0, %r)\n"
               "mod = __import__('wdo' 'tool.cli', fromlist=['main'])\n"
               "sys.exit(mod.main())\n" % (counter, ROOT))
        with open(path, "w") as f:
            f.write(src)
        os.chmod(path, 0o755)

    def test_refuses_to_loop(self):
        counter = os.path.join(self.tmp, "count")
        os.remove(os.path.join(self.local, "xdotool"))
        os.remove(os.path.join(self.bin, "xdotool"))
        self.clone_copy(os.path.join(self.local, "xdotool"), counter)
        self.clone_copy(os.path.join(self.bin, "xdotool"), counter)
        t0 = time.time()
        p, out, err = self.run_tool("xdotool", "key", "a")
        self.assertLess(time.time() - t0, 10)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("_W11_PASSTHROUGH", err)
        self.assertIn("loop", err)
        with open(counter) as f:
            self.assertEqual(len(f.read()), 2)           # exactly two


class BackendFlag(Tree):
    """`wxrandr --backend NAME` decides the handover, which happens before
    a single option is parsed.  So the hook looks ahead in argv -- and here
    it is a real process doing it, with a real `execve` on the other side."""

    def wayland_env(self, **extra):
        open(os.path.join(self.runuser, "wayland-0"), "w").close()
        return self.env(XDG_SESSION_TYPE="wayland",
                        XDG_RUNTIME_DIR=self.runuser,
                        WAYLAND_DISPLAY="wayland-0", **extra)

    def test_a_wayland_backend_on_x11_keeps_our_own_code(self):
        p, out, err = self.run_tool("xrandr", "--backend", "sway", "--query")
        self.assertEqual(self.records(), [])            # nothing handed over
        self.assertEqual((p.returncode, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend sway is not available in "
                              "this session: no sway or i3 IPC socket "
                              "($SWAYSOCK)\n")

    def test_the_kwin_backend_on_an_x11_session_is_refused(self):
        """A Plasma X11 session is an X11 session: `kwin_x11` owns
        `org.kde.KWin` on the bus, but the KDE display protocols live in
        `kwin_wayland` and there is no socket to reach them on. Asking for
        the backend by name is a one-line refusal, not a fallback and not a
        traceback."""
        p, out, err = self.run_tool("xrandr", "--backend", "kwin", "--query")
        self.assertEqual(self.records(), [])            # nothing handed over
        self.assertEqual((p.returncode, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend kwin is not available in "
                              "this session: no wayland socket\n")

    def test_x11_on_a_wayland_session_hands_over(self):
        p, out, err = self.run_tool("xrandr", "--backend", "x11", "--query",
                                    env=self.wayland_env())
        self.assertEqual(p.returncode, 0, err)
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        # our own flag is not passed on: the original has no such option
        self.assertEqual(recs[0]["argv"], ["--query"])
        self.assertEqual(recs[0]["pid"], p.pid)         # execve, not a child
        # ...where without it the same session runs our code
        os.remove(self.log)
        p, out, err = self.run_tool("xrandr", "--query",
                                    env=self.wayland_env())
        self.assertEqual(self.records(), [])

    def test_x11_on_an_x11_session_hands_over_without_the_flag(self):
        p, out, err = self.run_tool("xrandr", "--backend=x11", "--verbose")
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([r["argv"] for r in self.records()], [["--verbose"]])

    def test_an_output_named_like_the_flag_is_a_value(self):
        """The look-ahead walks argv with xrandr's own arities: this is an
        output called `--backend`, and the whole argv goes to the original
        untouched."""
        argv = ["--output", "--backend", "--off"]
        p, out, err = self.run_tool("xrandr", *argv)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([r["argv"] for r in self.records()], [argv])

    def test_the_variable_may_also_ask_for_the_real_xrandr(self):
        """`WXRANDR_BACKEND=x11` is the same request as `--backend x11`, and
        it is the one thing that variable gets to say about the handover:
        read any later and it could only ask this process to be something it
        can no longer become."""
        env = self.wayland_env(WXRANDR_BACKEND="x11")
        p, out, err = self.run_tool("xrandr", "--query", env=env)
        self.assertEqual(p.returncode, 0, err)
        recs = self.records()
        self.assertEqual([r["argv"] for r in recs], [["--query"]])
        self.assertEqual(recs[0]["pid"], p.pid)         # execve, not a child
        # a Wayland name in it keeps its older behaviour: an X11 session
        # still hands over, and nothing is pre-checked
        os.remove(self.log)
        p, out, err = self.run_tool("xrandr", "--query",
                                    env=self.env(WXRANDR_BACKEND="mutter"))
        self.assertEqual([r["argv"] for r in self.records()], [["--query"]])
        # ...and the flag beats the variable, in both directions
        os.remove(self.log)
        p, out, err = self.run_tool("xrandr", "--backend", "sway", "--query",
                                    env=self.env(WXRANDR_BACKEND="x11"))
        self.assertEqual(self.records(), [])
        self.assertIn("--backend sway is not available", err)

    def test_a_flag_with_no_value_is_answered_by_us(self):
        """Either session: the look-ahead keeps the flag's presence, so the
        bytes are our own xrandr-shaped `requires an argument`, not the
        original's `unrecognized option`."""
        for env in (self.env(), self.wayland_env()):
            if os.path.exists(self.log):
                os.remove(self.log)
            p, out, err = self.run_tool("xrandr", "--backend", env=env)
            self.assertEqual((p.returncode, out), (1, ""))
            self.assertEqual(err, "xrandr: --backend requires an argument\n"
                                  "Try 'xrandr --help' for more "
                                  "information.\n")
            self.assertEqual(self.records(), [])

    def test_the_informational_options_answer_on_an_x11_session(self):
        """`--print-backend` and `--backends` are about *us*; handing them
        to the original would only earn an `unrecognized option`."""
        p, out, err = self.run_tool("xrandr", "--print-backend")
        self.assertEqual((p.returncode, out, err), (0, "x11\n", ""))
        self.assertEqual(self.records(), [])
        p, out, err = self.run_tool("xrandr", "--backends")
        self.assertEqual(p.returncode, 0, err)
        rows = {ln[2:10].strip(): ln for ln in out.splitlines()}   # the name column is 8 wide
        self.assertEqual(sorted(rows), ["cinnamon", "hypr", "kwin", "mutter", "sway", "wlr",
                                        "x11"])
        self.assertTrue(rows["x11"].startswith("* x11"), out)
        self.assertIn("available", rows["x11"])
        self.assertIn(os.path.join(self.bin, "xrandr"), rows["x11"])
        self.assertIn("unavailable", rows["sway"])
        # the KDE backend is in the same boat on an X11 session, whatever
        # the desktop drawing it: no compositor socket, so no protocol
        self.assertIn("unavailable", rows["kwin"])
        self.assertIn("no wayland socket", rows["kwin"])
        self.assertEqual(self.records(), [])


class RoutedBeforeTheHandover(Tree):
    """Four things are answered by us on an X11 session, because the real
    xdotool has nothing to hand them to: `keys`, the hidden `__keymap`, and
    the leading `--layout` / `--vkbd` options, which are stripped and never
    reach the original. That ordering inside `wdotool.cli.main` is what the
    README promises for Plasma-on-Xorg and every other X11 session, and it
    is invisible from the outside until the original starts getting argv it
    cannot parse -- so it is pinned here, on the same real-process rig as
    the handover itself.
    """

    KEYMAP = os.path.join(FIXTURES, "keymaps", "de.xkb")

    def test_keys_is_ours_on_an_x11_session(self):
        p, out, err = self.run_tool("xdotool", "keys", "explain",
                                    "--keymap", self.KEYMAP, "@")
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("wdotool type '@'", out)
        self.assertEqual(self.records(), [])       # nothing handed over

    def test_the_hidden_keymap_dump_is_ours_on_an_x11_session(self):
        p, out, err = self.run_tool("xdotool", "__keymap",
                                    "--keymap", self.KEYMAP, "--chars", "@")
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("'@': key 16+level3", out)
        self.assertEqual(self.records(), [])

    def test_the_leading_options_are_stripped_and_the_rest_hands_over(self):
        """`--layout`/`--vkbd` are ours; what follows them is the original's,
        and it must arrive without them -- `xdotool --layout us key a` would
        be `unrecognized option` otherwise."""
        for opt, val in (("--layout", "us"), ("--vkbd", "off")):
            if os.path.exists(self.log):
                os.remove(self.log)
            p, out, err = self.run_tool("xdotool", opt, val, "key", "a")
            self.assertEqual(p.returncode, 0, (opt, err))
            recs = self.records()
            self.assertEqual(len(recs), 1, (opt, recs))
            self.assertEqual(recs[0]["argv"], ["key", "a"], opt)
            self.assertEqual(recs[0]["pid"], p.pid, opt)

    def test_a_bad_value_for_one_is_ours_too(self):
        p, out, err = self.run_tool("xdotool", "--layout", "wibble", "key", "a")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--layout: invalid argument", err)
        self.assertEqual(self.records(), [])

    def test_an_ordinary_command_still_hands_over(self):
        """The control: nothing above widened the set of commands we keep."""
        p, out, err = self.run_tool("xdotool", "key", "a")
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([r["argv"] for r in self.records()], [["key", "a"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
