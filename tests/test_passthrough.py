#!/usr/bin/env python3
"""Unit tests for w11common.passthrough: session detection, finding the real
tool, the argv conventions of the four main()s, help/version, environment
repair — and the guard that keeps this very test suite from exec'ing itself
away on an X11 development box.

Everything here is hermetic: `session_kind()` reads nothing but its three
seam directories (`_X11_SOCK_DIR`, `_LOGIND_DIR`, `_RUN_USER_DIR`) and the
environment dict it is handed, so these results are the same on a Wayland
box, on an X11 box and on a headless server. Nothing here execs; the real
handover is `tests/test_passthrough_exec.py`.
"""

import ast
import collections
import errno
import contextlib
import importlib
import gc
import io
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from w11common import distro, passthrough
from wdotool import cli as wdotool_cli
from wwmctl import cli as wwmctl_cli
from wxprop import cli as wxprop_cli
from wxrandr import cli as wxrandr_cli

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SHIM = os.path.join(FIXTURES, "w11_shim.py")
FAKE = os.path.join(FIXTURES, "fake_real_tool.py")


class ExecCalled(BaseException):
    """Raised by the stubbed os.execve instead of replacing this process.

    BaseException, not Exception: `execve` does not return, so the six
    `main()`s wrap the handover in the same "never print a traceback"
    guard as everything else, and an Exception here would be caught by it
    and reported as a one-line failure instead of standing for a process
    that has been replaced."""

    def __init__(self, path, argv, env):
        super().__init__(path)
        self.path, self.argv, self.env = path, list(argv), dict(env)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11_pt_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uid = os.getuid()
        self.x11 = self.mkdir("x11")
        self.logind = self.mkdir("logind")
        self.runuser = self.mkdir("run-user")
        for name, value in (("_X11_SOCK_DIR", self.x11),
                            ("_LOGIND_DIR", self.logind),
                            ("_RUN_USER_DIR", self.runuser)):
            p = mock.patch.object(passthrough, name, value)
            p.start()
            self.addCleanup(p.stop)
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)

    # -- fixture helpers ---------------------------------------------------
    def mkdir(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def touch(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def xsock(self, num):
        """An X server socket (only its existence is ever looked at)."""
        return self.touch(os.path.join(self.x11, "X%d" % num))

    def runtime_dir(self, uid=None):
        return self.mkdir("run-user", str(self.uid if uid is None else uid))

    def wsock(self, uid=None, name="wayland-0"):
        return self.touch(os.path.join(self.runtime_dir(uid), name))

    def logind_file(self, sid="1", **fields):
        rec = {"UID": str(self.uid), "USER": "test", "ACTIVE": "1",
               "STATE": "active", "REMOTE": "0", "CLASS": "user"}
        rec.update({k: str(v) for k, v in fields.items()})
        body = "# This is private data. Do not parse.\n" + \
            "".join("%s=%s\n" % kv for kv in rec.items())
        self.touch(os.path.join(self.logind, sid), body)
        # logind also keeps <id>.ref, a FIFO: reading it would block forever
        os.mkfifo(os.path.join(self.logind, sid + ".ref"))
        return rec

    def kind(self, tool=None, **env):
        passthrough.reset_cache()
        return passthrough.session_kind(tool, env)

    # -- a fake install tree ----------------------------------------------
    def install_tree(self, ours=("xdotool",), real=("xdotool",)):
        """`<tmp>/local` holds us, `<tmp>/bin` holds the originals; returns
        the PATH. Mirrors the shipped shape: /usr/local/bin/xdotool is a
        symlink to the clone, /usr/bin/xdotool is the distribution's."""
        local = self.mkdir("local")
        real_dir = self.mkdir("bin")
        for n in ours:
            os.symlink(SHIM, os.path.join(local, n))
        for n in real:
            os.symlink(FAKE, os.path.join(real_dir, n))
        return os.pathsep.join([local, real_dir])

    def stub_execve(self):
        """Stand in for the process replacement, and undo what it leaves.

        `exec_real()` resets SIGPIPE and SIGXFSZ to SIG_DFL immediately before
        `execve`, because an *ignored* disposition survives the exec and the
        original has to die of the signal the way it always did. The real call
        never comes back, so nothing there needs to put them back -- but this
        stub does come back, into a runner that keeps going with SIGPIPE now
        fatal. The next test anywhere in the process that writes down a closed
        pipe then kills the run outright (status 141, no summary). One process
        per file hides that; `python3 -m unittest discover -s tests` does not.
        """
        for name in ("SIGPIPE", "SIGXFSZ"):
            sig = getattr(signal, name, None)
            if sig is not None:
                self.addCleanup(signal.signal, sig, signal.getsignal(sig))

        def fake_execve(path, argv, env):
            raise ExecCalled(path, argv, env)
        p = mock.patch.object(passthrough.os, "execve", fake_execve)
        p.start()
        self.addCleanup(p.stop)


# ---------------------------------------------------------------------------


class Detection(Base):
    """The ordered rule of session_kind(). The trap this table exists for:
    $DISPLAY is set on a Wayland session too (Xwayland), and a display
    manager's greeter can own a Wayland socket on an X11 box."""

    def test_wayland_in_session_even_with_display(self):
        rd = self.runtime_dir()
        self.wsock()
        self.xsock(0)       # Xwayland: DISPLAY is set on Wayland too
        self.assertEqual(self.kind(XDG_RUNTIME_DIR=rd, WAYLAND_DISPLAY="wayland-0",
                                   DISPLAY=":0", XDG_SESSION_TYPE="wayland"),
                         "wayland")
        # ...and even when the environment lies about the type
        self.assertEqual(self.kind(XDG_RUNTIME_DIR=rd, WAYLAND_DISPLAY="wayland-0",
                                   DISPLAY=":0", XDG_SESSION_TYPE="x11"),
                         "wayland")

    def test_absolute_wayland_display(self):
        sock = self.touch(os.path.join(self.tmp, "abs", "wayland-9"))
        self.assertEqual(self.kind(WAYLAND_DISPLAY=sock), "wayland")

    def test_stale_wayland_display_is_not_a_session(self):
        """WAYLAND_DISPLAY left over in a script's environment with no socket
        behind it: the session type decides, not the variable."""
        rd = self.runtime_dir()
        self.xsock(0)
        self.assertEqual(self.kind(XDG_RUNTIME_DIR=rd, WAYLAND_DISPLAY="wayland-0",
                                   XDG_SESSION_TYPE="x11", DISPLAY=":0"), "x11")

    def test_session_type_decides(self):
        self.assertEqual(self.kind(XDG_SESSION_TYPE="wayland"), "wayland")
        self.assertEqual(self.kind(XDG_SESSION_TYPE="x11"), "x11")
        self.assertIsNone(self.kind(XDG_SESSION_TYPE="tty"))

    def test_sudo_ignores_a_stale_session_type(self):
        """`sudo xdotool ...` keeps root's XDG_SESSION_TYPE from an `ssh
        root@box` login (tty), and even XDG_RUNTIME_DIR=/run/user/0. logind's
        record of the *target* user is the truth."""
        self.logind_file("5", TYPE="x11", DISPLAY=":0")
        self.assertEqual(
            self.kind(XDG_SESSION_TYPE="tty", SUDO_UID=str(self.uid),
                      XDG_RUNTIME_DIR=os.path.join(self.runuser, "0")),
            "x11")
        shutil.rmtree(self.logind)
        os.makedirs(self.logind)
        self.logind_file("5", TYPE="wayland")
        self.assertEqual(
            self.kind(XDG_SESSION_TYPE="tty", SUDO_UID=str(self.uid)),
            "wayland")

    def test_sudo_uid_zero_is_not_a_session_owner(self):
        """`ssh root@box` then `sudo -i xdotool ...`: sudo leaves SUDO_UID=0
        behind, and taking that as the target sends the cookie search into
        /root and hands the original an environment with no authority at all
        (measured on a real Xfce box: "Authorization required, but no
        authorization protocol specified"). Root owns no graphical session,
        so 0 means "unknown" from either source."""
        self.logind_file("2", TYPE="x11", DISPLAY=":0")
        rd = self.runtime_dir()
        cookie = self.touch(os.path.join(rd, "xauth_user"), "yes")
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            self.assertIsNone(passthrough.target_uid({"SUDO_UID": "0"}))
            self.assertEqual(passthrough.session_uid({"SUDO_UID": "0"}),
                             self.uid)
            env = passthrough.child_env("xdotool", FAKE, {"SUDO_UID": "0"})
        self.assertEqual(env["XAUTHORITY"], cookie)
        self.assertEqual(env["DISPLAY"], ":0")
        # ...and it is still a sudo environment, so XDG_SESSION_TYPE (the
        # *invoking* login's) does not get to decide
        self.assertEqual(
            self.kind(SUDO_UID="0", XDG_SESSION_TYPE="wayland"), "x11")
        # root running `sudo -u test`: SUDO_UID=0, but our own uid is real
        self.assertEqual(passthrough.target_uid({"SUDO_UID": "0"}), self.uid)

    def test_pkexec_uid_counts_as_sudo(self):
        self.logind_file("5", TYPE="x11", DISPLAY=":0")
        self.assertEqual(self.kind(XDG_SESSION_TYPE="wayland",
                                   PKEXEC_UID=str(self.uid)), "x11")

    def test_logind_ignores_greeters_and_ssh(self):
        self.logind_file("1", TYPE="wayland", CLASS="greeter",
                         UID=self.uid)
        self.logind_file("2", TYPE="wayland", REMOTE="1")
        self.assertIsNone(self.kind())
        self.logind_file("3", TYPE="x11", DISPLAY=":0")
        self.assertEqual(self.kind(), "x11")

    def test_logind_prefers_the_active_session(self):
        self.logind_file("1", TYPE="wayland", STATE="online", ACTIVE="0")
        self.logind_file("2", TYPE="x11", STATE="active", DISPLAY=":0")
        self.assertEqual(self.kind(), "x11")

    def test_greeter_wayland_socket_of_another_user(self):
        """An Xfce box whose display manager runs a Wayland greeter: the
        greeter's socket exists under *its* uid; ours is an X11 session."""
        self.wsock(uid=self.uid + 4242)     # not the target user's
        self.xsock(0)
        self.assertEqual(self.kind(SUDO_UID=str(self.uid)), "x11")

    def test_greeter_socket_with_no_target_uid(self):
        """Root over ssh, no SUDO_UID: only a *real user's* socket counts, so
        a system account's (gdm, uid 125) never wins."""
        self.wsock()
        self.xsock(0)
        with mock.patch.object(passthrough, "_owner", lambda p: 125):
            self.assertIsNone(passthrough.find_wayland_socket({}, None))
        self.assertIsNotNone(passthrough.find_wayland_socket({}, None))

    def test_socket_scan_last_resort(self):
        """cron/systemd unit: no environment at all."""
        self.wsock()
        self.assertEqual(self.kind(), "wayland")

    def test_x_socket_last_resort(self):
        self.xsock(1)
        self.assertEqual(self.kind(), "x11")

    def test_forwarded_display(self):
        """`ssh -X box xdotool ...`: no local socket, but the real tool works
        over the forwarded display, so we must hand over."""
        self.assertEqual(self.kind(DISPLAY="localhost:10.0"), "x11")

    def test_nothing_anywhere(self):
        self.assertIsNone(self.kind())
        self.assertIsNone(self.kind(DISPLAY=""))

    def test_stale_display_is_not_a_session(self):
        self.assertIsNone(self.kind(DISPLAY=":7"))

    def test_override(self):
        rd = self.runtime_dir()
        self.wsock()
        wl = dict(XDG_RUNTIME_DIR=rd, WAYLAND_DISPLAY="wayland-0")
        self.assertEqual(self.kind(**wl), "wayland")
        self.assertEqual(self.kind(W11_PASSTHROUGH="always", **wl), "x11")
        self.assertEqual(self.kind(XDG_SESSION_TYPE="x11"), "x11")
        self.assertEqual(
            self.kind(W11_PASSTHROUGH="never", XDG_SESSION_TYPE="x11"),
            "wayland")

    def test_override_is_skippable_for_callers_that_never_hand_over(self):
        """`respect_override=False` for `warandr` & co: those variables say
        what to do about the *handover*, not what the session is."""
        for value, overridden in (("never", "wayland"), ("always", "x11")):
            passthrough.reset_cache()
            env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0",
                   "W11_PASSTHROUGH": value}
            self.assertEqual(passthrough.session_kind(env=env), overridden)
            self.assertEqual(
                passthrough.session_kind(env=env, respect_override=False),
                "x11", value)
            self.assertEqual(passthrough.passthrough_mode(env=env), value)
        # ...and with nothing set the two agree
        passthrough.reset_cache()
        env = {"XDG_SESSION_TYPE": "x11"}
        self.assertEqual(passthrough.passthrough_mode(env=env), "auto")
        self.assertEqual(passthrough.session_kind(env=env), "x11")
        self.assertEqual(
            passthrough.session_kind(env=env, respect_override=False), "x11")

    def test_per_tool_override_beats_the_global_one(self):
        env = dict(W11_PASSTHROUGH="always", WDOTOOL_PASSTHROUGH="never",
                   XDG_SESSION_TYPE="wayland")
        self.assertEqual(self.kind("xdotool", **env), "wayland")
        self.assertEqual(self.kind("wdotool", **env), "wayland")
        self.assertEqual(self.kind("wmctrl", **env), "x11")
        self.assertEqual(self.kind("xprop", **env), "x11")

    def test_cache_is_keyed_on_the_environment(self):
        # ...and on respect_override, which asks a different question of the
        # same environment
        env = {"XDG_SESSION_TYPE": "x11", "W11_PASSTHROUGH": "never"}
        self.assertEqual(passthrough.session_kind(env=env), "wayland")
        self.assertEqual(passthrough.session_kind(env=env,
                                                  respect_override=False), "x11")
        self.assertEqual(passthrough.session_kind(env=env), "wayland")
        self.assertEqual(passthrough.session_kind(env={"XDG_SESSION_TYPE": "x11"}),
                         "x11")
        self.assertEqual(passthrough.session_kind(env={"XDG_SESSION_TYPE": "wayland"}),
                         "wayland")
        self.assertEqual(passthrough.session_kind(env={"XDG_SESSION_TYPE": "x11"}),
                         "x11")


class TheProxyVariable(Base):
    """`W11_PROXY`, the second question about the same handover.

    `W11_PASSTHROUGH` says whether our own code may be replaced at all;
    `W11_PROXY` says whether a Wayland session replaces it with the ORIGINAL
    running on the xw11 display (design section 8.3).  Two variables and not
    one because the two answers are independent: a box that wants the clone on
    Wayland still wants the original on X11, and `never` in the first has to
    keep meaning "our own code, no handover to anything" -- it is this suite's
    escape hatch, and every test file in it relies on that one line.

    Nothing in `passthrough.py` ACTS on `W11_PROXY`: the handover it describes
    is `xw11/wrap.py`'s.  The reader lives here because the spellings, the
    per-tool table and `real_name()` do."""

    def test_the_three_answers_and_both_spellings_of_each(self):
        for value in passthrough._NEVER:
            self.assertEqual(passthrough.proxy_mode(env={"W11_PROXY": value}),
                             "never", value)
        for value in passthrough._ALWAYS:
            self.assertEqual(passthrough.proxy_mode(env={"W11_PROXY": value}),
                             "always", value)
        for value in ("", "auto", "  ", "sometimes"):
            self.assertEqual(passthrough.proxy_mode(env={"W11_PROXY": value}),
                             "auto", repr(value))
        self.assertEqual(passthrough.proxy_mode(env={}), "auto")

    def test_case_and_whitespace_are_the_passthrough_variables_own(self):
        self.assertEqual(passthrough.proxy_mode(env={"W11_PROXY": " NEVER "}), "never")
        self.assertEqual(passthrough.proxy_mode(env={"W11_PROXY": "Always"}), "always")

    def test_the_per_tool_variable_beats_the_global_one(self):
        env = {"W11_PROXY": "auto", "WDOTOOL_PROXY": "never",
               "WXRANDR_PROXY": "always"}
        self.assertEqual(passthrough.proxy_mode("xdotool", env), "never")
        self.assertEqual(passthrough.proxy_mode("wdotool", env), "never")
        self.assertEqual(passthrough.proxy_mode("xrandr", env), "always")
        self.assertEqual(passthrough.proxy_mode("wmctrl", env), "auto")

    def test_the_two_variables_do_not_read_each_other(self):
        """The one that would be silently wrong: a `proxy_mode()` that fell
        back to `W11_PASSTHROUGH`, or a `passthrough_mode()` that read
        `W11_PROXY`, would tie two independent answers together -- and the
        second of those would turn `W11_PROXY=never` into "do not hand over on
        X11 either", which is the regression this whole module exists to
        prevent."""
        self.assertEqual(passthrough.proxy_mode(env={"W11_PASSTHROUGH": "always"}),
                         "auto")
        self.assertEqual(passthrough.passthrough_mode(env={"W11_PROXY": "never"}),
                         "auto")
        self.assertEqual(passthrough.proxy_mode(
            "xdotool", {"WDOTOOL_PASSTHROUGH": "never"}), "auto")

    def test_every_tool_with_a_passthrough_variable_has_a_proxy_one(self):
        """Four originals, four pairs.  A table that grew a fifth tool in one
        dictionary and not the other would leave that tool with no way to say
        "not through the proxy"."""
        self.assertEqual(sorted(passthrough.PROXY_MODE_VAR),
                         sorted(passthrough._MODE_VAR))
        for tool, var in passthrough.PROXY_MODE_VAR.items():
            with self.subTest(tool):
                self.assertEqual(var, passthrough._MODE_VAR[tool]
                                 .replace("_PASSTHROUGH", "_PROXY"))


class TheProxyIsOneOfUs(Base):
    """`xw11` in `OUR_NAMES`, and what that buys.

    The wrapper asks `real_tool()` for the original it is about to run through
    the proxy, and `real_tool()` walks PATH: an installed `xw11` that the "not
    us" guards did not recognise would be a candidate `xdotool` the moment
    somebody copied it under that name, and `xw11/wrap.py:_exec_plan` would
    refuse to re-exec the very script it is looking for."""

    def test_it_is_in_our_names(self):
        self.assertIn("xw11", passthrough.OUR_NAMES)
        self.assertEqual(passthrough.real_name("xw11"), "xw11",
                         "xw11 clones nothing, so it has no original")

    def test_the_generated_console_script_is_us_under_an_originals_name(self):
        """`pip install .` and dpkg both write this file: a setuptools shim
        that imports `xw11.cli` and nothing else.  It is called `xdotool` here
        and not `xw11` on purpose -- under its own name guard 2 answers on the
        basename alone and never opens the file, so the question this batch
        owed (batch-8.md, "measurements to take": does the HEAD SNIFF accept
        the installed shim?) would go unasked.  Copied under an original's
        name, the sniff is the only guard left, and `_OUR_MODULES` is what it
        reads: measured 2026-09-11, `is_us()` on this file is True with `xw11`
        in OUR_NAMES and False without it, so this line is the whole of what
        keeps a PATH walk from exec'ing the proxy as `xdotool`."""
        d = self.mkdir("xw11bin")
        script = self.touch(os.path.join(d, "xdotool"),
                            "#!/usr/bin/python3\nimport sys\n"
                            "from xw11.cli import main\nsys.exit(main())\n")
        os.chmod(script, 0o755)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertTrue(passthrough.is_us(script))
            # ...and therefore not a candidate original, which is the point:
            # real_tool() walks PATH for `xdotool` and must not find the proxy.
            self.assertIsNone(passthrough.real_tool("xdotool", {"PATH": d}))

    def test_no_cli_imports_the_wrapper_at_module_level(self):
        """What makes "an X11 session never even imports the proxy" possible
        (design section 8.1 rule 3): the import is inside `main()`, after the
        handover has already left.  A top-level `from xw11 import wrap` would
        also break the four zipapps, which carry no `xw11/` at all
        (scripts/build-pyz.sh) -- the ImportError guard around that import is
        what covers them, and it can only guard an import it wraps.

        tests/test_xw11_wrap.py proves the consequence in a process of its own;
        this is the property, read off the source."""
        for name in ("wdotool", "wwmctl", "wxprop", "wxrandr"):
            path = os.path.join(ROOT, name, "cli.py")
            with self.subTest(name), open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in tree.body:
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for mod in names:
                    self.assertNotEqual(mod.split(".")[0], "xw11",
                                        "%s imports %s at module level" % (path, mod))


class RealTool(Base):
    """`real_tool()`: the next binary of that name on PATH that is not us."""

    def test_path_walk_skips_us(self):
        path = self.install_tree()
        found = passthrough.real_tool("xdotool", {"PATH": path})
        self.assertEqual(os.path.realpath(found), os.path.realpath(FAKE))
        self.assertTrue(found.startswith(os.path.join(self.tmp, "bin")))
        self.assertEqual(passthrough.real_tool("wdotool", {"PATH": path}), found)

    def test_not_being_first_on_path(self):
        """`sys.argv[0]` with no slash has to be resolved against PATH to know
        which file we are — but if the original is ahead of us there, that
        resolution answers with *it*, and mistaking it for ourselves would
        leave nothing to hand over to."""
        local, real_dir = self.install_tree().split(os.pathsep)
        path = os.pathsep.join([real_dir, local])
        with mock.patch.object(sys, "argv", ["xdotool"]), \
                mock.patch.dict(os.environ, {"PATH": path}):
            found = passthrough.real_tool("xdotool", {"PATH": path})
        self.assertEqual(os.path.realpath(found), os.path.realpath(FAKE))

    def test_nothing_installed(self):
        local = self.mkdir("local")
        os.symlink(SHIM, os.path.join(local, "xdotool"))
        self.assertIsNone(passthrough.real_tool("xdotool", {"PATH": local}))
        self.assertIsNone(passthrough.real_tool("xprop", {"PATH": local}))

    def test_override_variable(self):
        path = self.install_tree()
        other = self.touch(os.path.join(self.tmp, "other-xdotool"), "#!/bin/sh\n")
        os.chmod(other, 0o755)
        self.assertEqual(
            passthrough.real_tool("xdotool", {"PATH": path,
                                              "WDOTOOL_REAL_XDOTOOL": other}),
            other)

    def test_override_variable_unusable_is_an_error(self):
        bad = self.touch(os.path.join(self.tmp, "not-exec"))
        for value in (bad, os.path.join(self.tmp, "nope"), self.tmp):
            with self.assertRaises(passthrough.RealToolError) as cm:
                passthrough.real_tool("xprop", {"PATH": "", "WXPROP_REAL_XPROP": value})
            self.assertIn("WXPROP_REAL_XPROP", str(cm.exception))

    def test_is_us_guards(self):
        # 1: the same file (a hardlink under another name). A hardlink cannot cross filesystems, and
        # the temp dir is on another one whenever /tmp is tmpfs or an overlay while the clone is not
        # (EXDEV): the guard is about inodes, so make the link beside the fixture in that case.
        link = os.path.join(self.tmp, "xdotool")
        try:
            os.link(SHIM, link)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            near = tempfile.mkdtemp(prefix="w11_pt_link_", dir=os.path.dirname(SHIM))
            self.addCleanup(shutil.rmtree, near, True)
            link = os.path.join(near, "xdotool")
            os.link(SHIM, link)
        with mock.patch.object(sys, "argv", [SHIM]):
            self.assertTrue(passthrough.is_us(link))
        # 2: resolves to a file named like one of our tools
        wdo = self.touch(os.path.join(self.tmp, "d2", "wdotool"), "#!/bin/sh\n:\n")
        os.chmod(wdo, 0o755)
        alias = os.path.join(self.tmp, "d2", "xdotool")
        os.symlink(wdo, alias)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertTrue(passthrough.is_us(alias))
            # 3: the head sniff (a pyz copy under a foreign name)
            pyz = self.touch(os.path.join(self.tmp, "d3", "xdotool"),
                             "#!/usr/bin/env python3\n# w11-clone: wdotool\n")
            os.chmod(pyz, 0o755)
            self.assertTrue(passthrough.is_us(pyz))
            # ...but the bare project word on its own is not the stamp
            near = self.touch(os.path.join(self.tmp, "d3b", "xdotool"),
                              "#!/bin/sh\n# a w11-adjacent wrapper\n")
            os.chmod(near, 0o755)
            self.assertFalse(passthrough.is_us(near))
            # a compiled binary is never us: we are pure Python
            self.assertFalse(passthrough.is_us("/bin/true"))
            self.assertFalse(passthrough.is_us(FAKE))

    def test_a_wrapper_that_merely_mentions_us_is_not_us(self):
        """The head sniff must match the build's *stamp*, not the bare project
        or tool name: a third-party wrapper that mentions us in a comment is
        somebody's real `wmctrl`, and skipping it would tell the user to
        install a package they already have."""
        d = self.mkdir("wrap")
        w = self.touch(os.path.join(d, "wmctrl"),
                       "#!/bin/sh\n# w11 fallback wrapper\n"
                       'exec /usr/bin/wmctrl-real "$@"\n')
        os.chmod(w, 0o755)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertFalse(passthrough.is_us(w))
            self.assertEqual(passthrough.real_tool("wmctrl", {"PATH": d}), w)

    def test_a_generated_console_script_is_us(self):
        """`pip install .` writes `from wdotool.cli import main`; a copy of
        that under an original's name is still us, and the sniff has to say
        so without falling back to bare-word matching."""
        d = self.mkdir("consolescript")
        cs = self.touch(os.path.join(d, "xdotool"),
                        "#!/usr/bin/python3\nimport re\nimport sys\n"
                        "from wdotool.cli import main\n"
                        "sys.exit(main())\n")
        os.chmod(cs, 0o755)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertTrue(passthrough.is_us(cs))
            self.assertIsNone(passthrough.real_tool("xdotool", {"PATH": d}))
        # ...but an import of something else is not
        other = self.touch(os.path.join(d, "xprop"),
                           "#!/usr/bin/python3\nimport sys\n"
                           "from myproject.cli import main\n")
        os.chmod(other, 0o755)
        with mock.patch.object(sys, "argv", ["/nowhere/else"]):
            self.assertFalse(passthrough.is_us(other))

    def test_built_pyz_carries_the_marker(self):
        """scripts/build-pyz.sh must stamp the marker into the first 4 KiB —
        the head sniff is the only guard that survives two *copies* of us
        under two names in two PATH directories."""
        dist = os.path.join(ROOT, "dist")
        built = [os.path.join(dist, n) for n in
                 ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr")]
        built = [p for p in built if os.path.exists(p)]
        if not built:
            self.skipTest("no dist/ build here (run scripts/build-pyz.sh)")
        for p in built:
            with open(p, "rb") as f:
                head = f.read(4096)
            self.assertIn(passthrough.STAMP, head, p)
            self.assertTrue(passthrough.is_us(p), p)


class ArgvConventions(Base):
    """H2: the four main()s do NOT agree about argv. wdotool.cli.main takes
    argv *including* argv[0]; wwmctl/wxprop/wxrandr take argv[1:]. Each hook
    has to hand the child the same command line the user typed."""

    def hook_env(self, **extra):
        env = {"W11_PASSTHROUGH": "always", "PATH": "",
               "WDOTOOL_REAL_XDOTOOL": FAKE, "WWMCTL_REAL_WMCTRL": FAKE,
               "WXPROP_REAL_XPROP": FAKE, "WXRANDR_REAL_XRANDR": FAKE}
        env.update(extra)
        return env

    def run_main(self, main, argv):
        """Call an entry point the way a real invocation does: no argv
        argument, the command line in sys.argv."""
        self.stub_execve()
        with mock.patch.dict(os.environ, self.hook_env(), clear=True), \
                mock.patch.object(sys, "argv", list(argv)):
            passthrough.reset_cache()
            with self.assertRaises(ExecCalled) as cm:
                main()
        return cm.exception

    def test_wdotool_argv_includes_argv0(self):
        e = self.run_main(wdotool_cli.main,
                          ["xdotool", "search", "--name", "x", "windowactivate"])
        self.assertEqual(e.argv, ["xdotool", "search", "--name", "x",
                                  "windowactivate"])
        self.assertEqual(e.path, FAKE)

    def test_wwmctl_argv_excludes_argv0(self):
        e = self.run_main(wwmctl_cli.main, ["wmctrl", "-l", "-G", "-p", "-x"])
        self.assertEqual(e.argv, ["wmctrl", "-l", "-G", "-p", "-x"])

    def test_wxprop_argv_excludes_argv0(self):
        e = self.run_main(wxprop_cli.main, ["xprop", "-root", "WM_CLASS"])
        self.assertEqual(e.argv, ["xprop", "-root", "WM_CLASS"])

    def test_wxrandr_argv_excludes_argv0(self):
        e = self.run_main(wxrandr_cli.main,
                          ["xrandr", "--output", "VGA-1", "--auto"])
        self.assertEqual(e.argv, ["xrandr", "--output", "VGA-1", "--auto"])

    def test_argv0_is_the_original_name(self):
        """The original prints argv[0] in its own messages: invoked under our
        own name (or `python3 -m`), it still has to call itself `xdotool`."""
        e = self.run_main(wdotool_cli.main, ["wdotool", "key", "a"])
        self.assertEqual(e.argv, ["xdotool", "key", "a"])
        e = self.run_main(wdotool_cli.main, ["/repo/wdotool/__main__.py", "key", "a"])
        self.assertEqual(e.argv, ["xdotool", "key", "a"])

    def test_explicit_argv_is_honoured(self):
        """A caller that does pass an argv still gets the right child command
        line (entry=True says the process really is this invocation)."""
        self.stub_execve()
        with mock.patch.dict(os.environ, self.hook_env(), clear=True), \
                mock.patch.object(sys, "argv", ["xdotool"]):
            passthrough.reset_cache()
            with self.assertRaises(ExecCalled) as cm:
                passthrough.maybe_exec_real("xdotool", ["mousemove", "1", "2"])
        self.assertEqual(cm.exception.argv, ["xdotool", "mousemove", "1", "2"])

    def test_daemon_reinvocation_never_hands_over(self):
        """`wdotool __daemon` is our own re-invocation of ourselves."""
        self.stub_execve()
        import wdotool.daemon as daemon
        with mock.patch.dict(os.environ, self.hook_env(), clear=True), \
                mock.patch.object(sys, "argv", ["xdotool", "__daemon"]), \
                mock.patch.object(daemon, "daemon_main", lambda: 7):
            passthrough.reset_cache()
            self.assertEqual(wdotool_cli.main(), 7)


class SuiteGuard(Base):
    """H1: an unguarded hook would `execve` the test runner. Two independent
    belts; this class proves both are live."""

    def test_the_escape_hatch_is_in_effect_right_now(self):
        self.assertEqual(os.environ.get("W11_PASSTHROUGH"), "never")
        self.assertEqual(
            passthrough.session_kind("xdotool", os.environ), "wayland",
            "the suite's escape hatch is not in effect")

    def test_conftest_sets_the_escape_hatch(self):
        """pytest imports conftest.py before collection; prove it is what
        sets the hatch, not just this file's own line."""
        import importlib
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        self.addCleanup(os.environ.__setitem__, "W11_PASSTHROUGH",
                        os.environ["W11_PASSTHROUGH"])
        del os.environ["W11_PASSTHROUGH"]
        import conftest
        importlib.reload(conftest)
        self.assertEqual(os.environ.get("W11_PASSTHROUGH"), "never")

    def test_every_test_file_sets_the_escape_hatch(self):
        """The suite is run file by file (`python3 tests/test_foo.py`), where
        conftest.py never loads — so every file carries the line itself, and
        every *new* file has to. A test that spawns one of our tools would
        otherwise watch it hand itself over on an X11 box."""
        tests = os.path.join(ROOT, "tests")
        missing = []
        for name in sorted(os.listdir(tests)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(tests, name)) as f:
                if 'W11_PASSTHROUGH"] = "never"' not in f.read():
                    missing.append(name)
        self.assertEqual(missing, [])

    # -- how the file is run, and whether that changes what runs -------------
    #
    # Three invocation forms are documented (docs/Technical.md section 9):
    # `python3 -m unittest discover -s tests`, `python3 tests/test_foo.py`,
    # and `python3 -m unittest tests/test_foo.py`. They put DIFFERENT things
    # on sys.path and collect DIFFERENT sets of tests, and both of those have
    # already hidden real tests from a real run.

    TESTS_DIR = os.path.join(ROOT, "tests")

    #: helpers imported by their bare name, which only resolves when the
    #: tests directory itself is on sys.path
    BARE_HELPERS = {"support", "wl_fake", "test_dbus_mini", "test_wxrandr_mutter",
                    "test_wwmctl_x11", "test_wxprop_x11", "test_backend_wlr"}

    #: what puts it there, in every file that needs it
    BOOTSTRAP = "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))"

    def test_files(self):
        return sorted(n for n in os.listdir(self.TESTS_DIR)
                      if n.startswith("test_") and n.endswith(".py"))

    def test_every_files_main_block_is_its_last_statement(self):
        """`if __name__ == "__main__": unittest.main()` runs the classes
        DEFINED SO FAR. A class written after it is invisible to
        `python3 tests/<file>.py` and visible to `-m unittest`, so the file
        silently tests less of the tree the way most people run it -- and the
        two counts disagree, which is how this was found:
        `python3 tests/test_overlap_consent.py` ran 60 tests where
        `-m unittest` ran 66, and the six in `WarandrNeverAsks` had never
        run at all. test_overlap_force.py hid four the same way."""
        self.maxDiff = None
        late = []
        for name in self.test_files():
            with open(os.path.join(self.TESTS_DIR, name)) as f:
                body = ast.parse(f.read(), filename=name).body
            guard = [i for i, node in enumerate(body)
                     if isinstance(node, ast.If)
                     and "__main__" in ast.dump(node.test)
                     and "__name__" in ast.dump(node.test)]
            if not guard:
                late.append("%s: no __main__ block" % name)
                continue
            hidden = [getattr(n, "name", type(n).__name__)
                      for n in body[guard[-1] + 1:]]
            if hidden:
                late.append("%s:%d hides %s"
                            % (name, body[guard[-1]].lineno, ", ".join(hidden)))
        self.assertEqual(late, [])

    def test_every_bare_helper_import_carries_the_tests_directory_bootstrap(self):
        """`import support` (not `tests.support`) resolves only when the
        tests directory is on sys.path. Running a file by path puts it there
        for free -- sys.path[0] is the script's own directory -- which is why
        these files have got away with the repository-root bootstrap alone.
        `python3 -m unittest tests/test_vkbd.py` puts only the working
        directory there, and every one of these files dies on the import
        before a single test is collected."""
        self.maxDiff = None
        missing = []
        for name in sorted(os.listdir(self.TESTS_DIR)):
            if not name.endswith(".py") or name == "conftest.py":
                continue
            with open(os.path.join(self.TESTS_DIR, name)) as f:
                src = f.read()
            tree = ast.parse(src, filename=name)
            bare = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    bare |= {a.name for a in node.names
                             if a.name.split(".")[0] in self.BARE_HELPERS}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    if node.module.split(".")[0] in self.BARE_HELPERS:
                        bare.add(node.module)
            if bare and self.BOOTSTRAP not in src:
                missing.append("%s (imports %s)" % (name, ", ".join(sorted(bare))))
        self.assertEqual(missing, [], "add `%s` beside the ROOT bootstrap" % self.BOOTSTRAP)

    # -- state that outlives the file that set it ----------------------------

    def test_the_process_environment_is_still_the_real_one(self):
        """`os.environ` is `os._Environ`, and every `os.environ[k] = v` in it
        calls `putenv`, which is what a child spawned with the INHERITED
        environment reads.  Replace the object with a plain dict and that stops
        happening silently: the tests that read `os.environ` back still pass and
        the ones that spawn a process stop seeing what they set.

        Found under `unittest discover -s tests`, where
        tests/test_live_smoke.py had `self.mod.os.environ = env` (its `os` is
        the interpreter's) and a cleanup that read `os.environ` after the
        assignment, so what it restored was the plain dict.  From there the run
        carried four failures in tests/test_wmirror_lifetime.py -- its
        wl-mirror stub never got `$WMIRROR_STUB_LOG` -- and two errors in
        tests/test_overlap_consent.py, both files green run alone.  CI never saw
        it because CI runs one file per job; the full-suite gate the release
        runs does."""
        self.assertIsInstance(os.environ, os._Environ)

    def test_a_module_taken_away_for_one_test_is_still_importable(self):
        """`gi`, the one module a test in this suite removes from
        `sys.modules` on purpose (tests/test_live_smoke.py's `without_gi`, so
        the gdbus-text branch of the Mutter reader runs on a host that has the
        bindings).  Putting a DIFFERENT object back is not putting it back:
        Debian's gi/__init__.py refuses a second load beside the static
        bindings and raises "you must not import static modules like gobject",
        which is how `from warandr import gui` failed in
        tests/test_overlap_consent.py under `unittest discover` and nowhere
        else.  ModuleNotFoundError is a box without python3-gi and is a skip;
        any other ImportError is this.

        A guard and not coverage: run as `python3 tests/test_passthrough.py`
        this can only pass, since nothing in the file touches `gi`.  It has
        teeth under `unittest discover`, where it runs after
        tests/test_live_smoke.py, and that is the run it is written for."""
        try:
            importlib.import_module("gi")
        except ModuleNotFoundError:
            raise unittest.SkipTest("python3-gi is not installed")

    def test_no_test_file_rebinds_environ_on_a_module(self):
        """The guard for the failure above, at the source rather than after the
        fact: an assignment to `<something>.environ` replaces the object for
        every module in the process, and there is no way to write one that is
        safe.  `mock.patch.dict(os.environ, ..., clear=True)` sets values in the
        object that is already there and puts back what it replaced, which is
        what the fixed test uses."""
        offenders = []
        for name in sorted(os.listdir(self.TESTS_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(self.TESTS_DIR, name)) as f:
                tree = ast.parse(f.read(), filename=name)
            for node in ast.walk(tree):
                targets = getattr(node, "targets", [])
                if isinstance(node, ast.AugAssign):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "environ":
                        offenders.append("%s:%d" % (name, node.lineno))
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "setattr"
                        and len(node.args) > 1
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == "environ"):
                    offenders.append("%s:%d (setattr)" % (name, node.lineno))
        self.assertEqual(offenders, [],
                         "use mock.patch.dict(os.environ, ...) instead")

    def test_the_module_path_invocation_form_imports_the_file(self):
        """The third documented form, end to end, on the file that has the
        most to lose from it: test_vkbd.py imports both `wl_fake` and
        `support`. Run from the repository root, `python3 -m unittest
        tests/test_vkbd.py` collected one test -- unittest's own
        `_FailedTest` standing for a ModuleNotFoundError -- and reported
        `Ran 1 test ... FAILED (errors=1)` where the file has 75.

        The claim is about the IMPORT, so that is all this reads: no
        ModuleNotFoundError, no `_FailedTest` (which is how unittest reports
        one, and it counts as a collected test), and a run that collected
        more than the single failure. A genuine failure inside test_vkbd
        belongs to test_vkbd; asserting rc == 0 here would have painted it on
        this guard instead."""
        p = subprocess.run([sys.executable, "-m", "unittest",
                            "tests/test_vkbd.py"],
                           cwd=ROOT, capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, W11_PASSTHROUGH="never"))
        self.assertNotIn("ModuleNotFoundError", p.stderr)
        self.assertNotIn("_FailedTest", p.stderr)
        ran = re.search(r"^Ran (\d+) tests? in ", p.stderr, re.M)
        self.assertIsNotNone(ran, p.stderr[-2000:])
        self.assertGreater(int(ran.group(1)), 1, p.stderr[-2000:])

    def test_in_process_main_never_execs(self):
        """The ~17 in-process `cli.main([...])` callers: an explicit argv
        means we are a library, and a library never replaces its caller's
        process — even on a box that really is X11."""
        self.stub_execve()
        env = {"W11_PASSTHROUGH": "always", "PATH": "",
               "WDOTOOL_REAL_XDOTOOL": FAKE, "WWMCTL_REAL_WMCTRL": FAKE,
               "WXPROP_REAL_XPROP": FAKE, "WXRANDR_REAL_XRANDR": FAKE,
               "DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}
        calls = [(wdotool_cli.main, ["wdotool", "version"]),
                 (wdotool_cli.main, ["wdotool", "key", "a"]),
                 (wwmctl_cli.main, ["--help"]),
                 (wwmctl_cli.main, ["-l"]),
                 (wxprop_cli.main, ["-version"]),
                 (wxprop_cli.main, ["-root", "WM_CLASS"]),
                 (wxrandr_cli.main, ["--version"]),
                 (wxrandr_cli.main, ["--query"])]
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["test_passthrough.py"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            passthrough.reset_cache()
            for main, argv in calls:
                try:
                    # must not raise ExecCalled; what our own code then does
                    # with no session at all is beside the point
                    rc = main(list(argv))
                except SystemExit as e:
                    rc = e.code
                self.assertIn(rc, (0, 1), "%s%r -> %r" % (main, argv, rc))
        # those runs reach real backend detection, which caches a session-bus
        # connection; drop it here rather than at interpreter exit, where
        # unittest's warning filter would print a ResourceWarning
        from wdotool import backend_detect
        backend_detect.reset()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()

    def test_parity_oracle_is_protected(self):
        """tests/test_cli_parity.py shells a shim named `xdotool` while the
        real one is on PATH: without the escape hatch, on an X11 box it would
        compare the real xdotool with itself and pass tautologically."""
        with open(os.path.join(ROOT, "tests", "test_cli_parity.py")) as f:
            src = f.read()
        head = src.split("compare(")[0]
        self.assertIn("W11_PASSTHROUGH", head)
        self.assertIn("never", head)


class HelpAndVersion(Base):
    """M3: `xdotool --help` must never answer 127."""

    def env(self, **extra):
        e = {"XDG_SESSION_TYPE": "x11", "PATH": self.mkdir("empty")}
        e.update(extra)
        return e

    def test_help_falls_back_to_our_own_when_nothing_is_installed(self):
        for args in ([], ["--help"], ["-h"], ["--version"], ["-v"], ["version"],
                     ["help"], ["-hv"], ["-vh"]):
            self.assertIsNone(
                passthrough.maybe_exec_real("xdotool", args, env=self.env()),
                args)

    def test_real_work_without_the_original_is_127(self):
        err = []
        with mock.patch.object(sys, "stderr") as fake:
            fake.write = err.append
            rc = passthrough.maybe_exec_real("xdotool", ["key", "a"],
                                             env=self.env())
        self.assertEqual(rc, 127)
        msg = "".join(err)
        self.assertIn(distro.hint("xdotool"), msg)
        self.assertIn("WDOTOOL_REAL_XDOTOOL", msg)
        self.assertIn("X11 session", msg)

    def test_help_goes_to_the_original_when_there_is_one(self):
        """Installed as `xdotool`, even the version string has to be theirs."""
        self.stub_execve()
        with self.assertRaises(ExecCalled) as cm:
            passthrough.maybe_exec_real(
                "xdotool", ["--version"],
                env=self.env(WDOTOOL_REAL_XDOTOOL=FAKE))
        self.assertEqual(cm.exception.argv, ["xdotool", "--version"])

    def test_help_request_table(self):
        """Each original's own spellings, exactly. The `no` column is the
        point: `-v` is *verbose* in wmctrl and unknown to xprop, so reading
        it as help would run our Wayland-only code and print a Wayland error
        where the sibling `wmctrl -l` exits 127 saying what to install."""
        yes = {
            # xdotool: getopt_long(argc, argv, "+hv", ...), plus the bare
            # words; `-vh key a` prints the version (verified vs xdotool 3.x)
            "xdotool": [[], ["--help"], ["-h"], ["-v"], ["--version"],
                        ["-hv"], ["-vh", "key", "a"], ["help"], ["version"]],
            "wmctrl": [[], ["-h"], ["-V"], ["--help"], ["--version"]],
            "xprop": [[], ["-help"], ["-version"], ["-grammar"]],
            "xrandr": [[], ["-help"], ["--help"], ["-v"], ["--version"]],
        }
        no = {
            "xdotool": [["key", "a"], ["-x"], ["search", "--help"], ["-"],
                        ["--sync"], ["-id", "5"], ["-ver"], ["--h"],
                        ["HELP"], ["VERSION"]],
            # -v is "be verbose"; --help/--version are special-cased by
            # wmctrl only when they are the whole command line
            "wmctrl": [["-v", "-l"], ["-vv"], ["-v"], ["-l"], ["-m"],
                       ["--help", "-l"], ["--version", "-l"], ["-help"]],
            "xprop": [["-v"], ["-root"], ["--help"], ["--version"],
                      ["-id", "5"]],
            "xrandr": [["--verbose"], ["--query"], ["-q"], ["--v"],
                       ["--output", "DP-1", "--auto"]],
        }
        for tool, cases in yes.items():
            for args in cases:
                self.assertTrue(passthrough._is_help_request(tool, args),
                                (tool, args))
        for tool, cases in no.items():
            for args in cases:
                self.assertFalse(passthrough._is_help_request(tool, args),
                                 (tool, args))

    def test_a_verbose_flag_is_not_a_help_request(self):
        """`wmctrl -v -l` with no wmctrl installed: 127 and the install line,
        the same answer `wmctrl -l` gets — not our own Wayland error."""
        err = []
        with mock.patch.object(sys, "stderr") as fake:
            fake.write = err.append
            rc = passthrough.maybe_exec_real("wmctrl", ["-v", "-l"],
                                             env=self.env())
        self.assertEqual(rc, 127)
        self.assertIn(distro.hint("wmctrl"), "".join(err))

    def test_wxprop_falls_back_to_its_native_x11_path(self):
        """wxprop is the one tool with a real X11 client of its own, so a box
        with no x11-utils installed keeps working instead of exiting 127."""
        self.assertIsNone(passthrough.maybe_exec_real(
            "xprop", ["-root", "WM_CLASS"], fallback_native=True,
            env=self.env()))


class EnvRepair(Base):
    """The sudo/ssh/cron payoff: `sudo xdotool key a` works *through* us
    where the original alone fails, because we inject the session's DISPLAY
    and XAUTHORITY into the environment we exec with."""

    def test_injects_display_and_xauthority(self):
        self.xsock(99)
        rd = self.runtime_dir()
        cookie = self.touch(os.path.join(rd, "xauth_abc"), "cookie")
        env = passthrough.child_env("xdotool", FAKE,
                                    {"XDG_RUNTIME_DIR": rd, "PATH": "/bin"})
        self.assertEqual(env["DISPLAY"], ":99")
        self.assertEqual(env["XAUTHORITY"], cookie)

    def test_logind_display_wins_over_a_guess(self):
        self.xsock(1)
        self.logind_file("7", TYPE="x11", DISPLAY=":0")
        env = passthrough.child_env("xdotool", FAKE, {})
        self.assertEqual(env["DISPLAY"], ":0")

    def test_working_values_are_never_touched(self):
        self.xsock(0)
        self.xsock(3)
        cookie = self.touch(os.path.join(self.tmp, "mycookie"))
        env = passthrough.child_env("xdotool", FAKE,
                                    {"DISPLAY": ":3", "XAUTHORITY": cookie})
        self.assertEqual(env["DISPLAY"], ":3")
        self.assertEqual(env["XAUTHORITY"], cookie)
        # a forwarded display is not ours to second-guess
        env = passthrough.child_env("xdotool", FAKE, {"DISPLAY": "otherbox:0"})
        self.assertEqual(env["DISPLAY"], "otherbox:0")

    def test_dead_display_is_replaced(self):
        self.xsock(0)
        env = passthrough.child_env("xdotool", FAKE, {"DISPLAY": ":42"})
        self.assertEqual(env["DISPLAY"], ":0")

    def test_root_gets_the_users_cookie_not_the_greeters(self):
        """`ssh root@box xprop -root`: no SUDO_UID, so only logind knows whose
        session this is. Without that, the runtime-dir scan (lowest uid
        first) hands the original the *display manager's* cookie, which
        authorises nothing, and it dies where it should have worked."""
        greeter = self.mkdir("run-user", "125")
        self.touch(os.path.join(greeter, "xauth_greeter"), "no")
        mine = self.runtime_dir()
        cookie = self.touch(os.path.join(mine, "xauth_user"), "yes")
        self.xsock(0)
        self.logind_file("3", TYPE="x11", DISPLAY=":0")
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            self.assertEqual(passthrough.session_uid({}), self.uid)
            env = passthrough.child_env("xprop", FAKE, {})
        self.assertEqual(env["XAUTHORITY"], cookie)
        self.assertEqual(env["DISPLAY"], ":0")

    def test_a_second_x_server_on_the_box_is_not_ours(self):
        """Two X servers (a display manager's on :0, the session's on :1) and
        logind recording no `DISPLAY=`: the child must get the *target user's*
        display, which takes the uid logind knows — "the lowest socket number
        there is" is a coin toss."""
        self.xsock(0)
        self.xsock(1)
        self.logind_file("4", TYPE="x11")       # no DISPLAY= in the record
        owners = {os.path.join(self.x11, "X0"): 125,
                  os.path.join(self.x11, "X1"): self.uid}
        with mock.patch.object(passthrough, "_owner",
                               lambda p: owners.get(p, self.uid)), \
                mock.patch.object(passthrough.os, "getuid", lambda: 0):
            # what an un-qualified search answers, and why it is not enough
            self.assertEqual(passthrough.find_x_display({}, None), ":0")
            env = passthrough.child_env("xdotool", FAKE, {})
        self.assertEqual(env["DISPLAY"], ":1")

    def test_a_system_accounts_cookie_is_never_the_answer(self):
        """The same trap with no logind either (a container, a box with the
        session records gone): an unknown target uid takes a real user's
        runtime dir, never a system account's — the rule
        find_wayland_socket() already applies to sockets."""
        greeter = self.mkdir("run-user", "125")
        self.touch(os.path.join(greeter, "xauth_greeter"), "no")
        user = self.mkdir("run-user", str(self.uid + 4242))
        cookie = self.touch(os.path.join(user, "xauth_user"), "yes")
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            self.assertIsNone(passthrough.session_uid({}))
            self.assertEqual(passthrough.find_xauthority({}, None), cookie)
        # with a target uid it is that user's dir and nobody else's (the
        # last-resort session.find_xauthority() reads the real box, so it is
        # stubbed out here rather than left to the machine the tests run on)
        with mock.patch("w11common.session.find_xauthority", lambda *a, **k: None):
            self.assertIsNone(passthrough.find_xauthority({}, 126))

    def test_gdms_cookie_one_directory_down_beats_a_stale_home_one(self):
        """GDM keeps an X11 session's cookie at `<runtime dir>/gdm/Xauthority`, one directory below the two
        names this scan had. Without it the scan found nothing in the runtime dir and answered `~/.Xauthority`
        -- a file that exists on any box that has ever run an X server of its own and that authorises nothing
        on this one, and which is *ahead* of the `session.find_xauthority()` fallback that does glob GDM's
        name. noble-gnome-x11's seated session really does carry XAUTHORITY=/run/user/1000/gdm/Xauthority
        [M this rig, 2026-09-09]."""
        rd = self.runtime_dir()
        home = self.mkdir("home")
        stale = self.touch(os.path.join(home, ".Xauthority"), "old")
        os.utime(stale, (1.0, 1.0))
        cookie = self.touch(os.path.join(rd, "gdm", "Xauthority"), "live")
        pw = collections.namedtuple("pw", "pw_dir")(home)
        with mock.patch("pwd.getpwuid", lambda u: pw):
            self.assertEqual(passthrough.find_xauthority({}, self.uid), cookie)

    def test_the_newest_of_the_three_runtime_dir_cookies_wins(self):
        """The three names are one mtime comparison and not a priority list: a session that has been through
        both a Wayland and an X11 login leaves more than one behind, and the live one is the newest. This is
        the claim the arm added for GDM must not have broken -- an appended glob that skipped the comparison
        would make gdm/Xauthority win even when it is the older file."""
        rd = self.runtime_dir()
        old = self.touch(os.path.join(rd, "gdm", "Xauthority"), "old")
        os.utime(old, (1.0, 1.0))
        new = self.touch(os.path.join(rd, ".mutter-Xwaylandauth.ABC123"), "new")
        os.utime(new, (2.0, 2.0))
        self.assertEqual(passthrough.find_xauthority({}, self.uid), new)
        os.utime(old, (3.0, 3.0))
        self.assertEqual(passthrough.find_xauthority({}, self.uid), old)

    def test_dead_xauthority_is_dropped_not_forwarded(self):
        """A stale `$XAUTHORITY` we cannot better is worse than none: left in
        place it *suppresses* the original's own ~/.Xauthority default."""
        with mock.patch.object(passthrough, "find_xauthority",
                               lambda *a, **k: None):
            env = passthrough.child_env("xdotool", FAKE,
                                        {"XAUTHORITY": "/nonexistent/xa",
                                         "PATH": "/bin"})
            self.assertNotIn("XAUTHORITY", env)
            # nothing to drop, nothing added
            env = passthrough.child_env("xdotool", FAKE, {"PATH": "/bin"})
            self.assertNotIn("XAUTHORITY", env)

    def test_guard_variable_records_the_handover(self):
        env = passthrough.child_env("xdotool", FAKE, {})
        self.assertEqual(env[passthrough.GUARD_VAR], os.path.realpath(FAKE))
        env2 = passthrough.child_env("xprop", FAKE, env)
        self.assertEqual(env2[passthrough.GUARD_VAR].split(os.pathsep),
                         [os.path.realpath(FAKE)] * 2)

    def test_guard_stops_a_loop(self):
        """We were exec'd as somebody's "real tool": one more handover would
        just do it again."""
        with mock.patch.object(sys, "argv", [SHIM]):
            self.assertTrue(passthrough._handover_loop(
                {passthrough.GUARD_VAR: os.path.realpath(SHIM)}))
            self.assertFalse(passthrough._handover_loop(
                {passthrough.GUARD_VAR: os.path.realpath(FAKE)}))
            self.assertFalse(passthrough._handover_loop({}))
            # backstop: an identity we cannot recognise still terminates
            self.assertTrue(passthrough._handover_loop(
                {passthrough.GUARD_VAR: os.pathsep.join(
                    "/nope/%d" % i for i in range(passthrough.GUARD_DEPTH))}))

    def test_loop_reports_instead_of_exec(self):
        err = []
        self.stub_execve()
        with mock.patch.object(sys, "argv", [SHIM]), \
                mock.patch.object(sys, "stderr") as fake:
            fake.write = err.append
            rc = passthrough.maybe_exec_real(
                "xdotool", ["key", "a"],
                env={"XDG_SESSION_TYPE": "x11", "PATH": self.install_tree(),
                     passthrough.GUARD_VAR: os.path.realpath(SHIM)})
        self.assertEqual(rc, 127)
        self.assertIn(passthrough.GUARD_VAR, "".join(err))


class WarandrBackend(Base):
    """warandr picks a command; it never execs (it is not a clone of an X11
    binary we are installed over)."""

    def test_choose_follows_session_kind(self):
        from warandr import randr
        rd = self.runtime_dir()
        self.wsock(name="wayland-1")
        passthrough.reset_cache()
        b = randr.choose({"XDG_RUNTIME_DIR": rd, "WAYLAND_DISPLAY": "wayland-1",
                          "PATH": "/nonexistent"})
        self.assertTrue(b.wayland)
        self.assertEqual(b.word, "wxrandr")
        passthrough.reset_cache()
        # the same variable with no compositor behind it: X11
        b = randr.choose({"WAYLAND_DISPLAY": "wayland-4", "PATH": "/nonexistent",
                          "XDG_SESSION_TYPE": "x11"})
        self.assertFalse(b.wayland)
        self.assertEqual(b.argv, ["xrandr"])

    def test_the_escape_hatch_does_not_reach_warandr(self):
        """W11_PASSTHROUGH is about handing over to the original, and
        warandr never hands over. `never` — which the README documents and
        every tests/test_*.py exports — must not make an X11 box select
        wxrandr, whose every Apply would then say "Can't open display"."""
        from warandr import randr
        for value in ("never", "always", "auto", ""):
            passthrough.reset_cache()
            b = randr.choose({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0",
                              "PATH": "/nonexistent",
                              "W11_PASSTHROUGH": value})
            self.assertEqual(b.argv, ["xrandr"], value)
            self.assertFalse(b.wayland, value)
            self.assertEqual(b.word, "xrandr", value)
        # ...and a real Wayland session still picks wxrandr with it set
        rd = self.runtime_dir()
        self.wsock(name="wayland-1")
        for value in ("never", "always"):
            passthrough.reset_cache()
            b = randr.choose({"XDG_RUNTIME_DIR": rd,
                              "WAYLAND_DISPLAY": "wayland-1",
                              "PATH": "/nonexistent",
                              "W11_PASSTHROUGH": value})
            self.assertTrue(b.wayland, value)
            self.assertEqual(b.word, "wxrandr", value)

    def test_the_x11_runner_gets_the_sessions_display_and_cookie(self):
        """`warandr --command` / `--save` from `ssh root@box`, `sudo` or cron.

        The X11 runner is a *child*, not a handover, so it never went through
        child_env(): warandr ran a bare `xrandr` with the environment as
        found, which on a root shell has no $DISPLAY -- exit 1, `xrandr
        failed (1): Can't open display`, in the very shell where wdotool,
        wwmctl, wxprop and wxrandr all worked. Now it gets the same repair."""
        from warandr import randr
        self.xsock(0)
        rd = self.runtime_dir()
        cookie = self.touch(os.path.join(rd, "xauth_abc"), "cookie")
        self.logind_file("2", TYPE="x11", DISPLAY=":0")
        for forced in (None, "x11"):
            passthrough.reset_cache()
            b = randr.choose({"PATH": "/nonexistent", "XDG_SESSION_TYPE": "x11"},
                             forced=forced)
            self.assertFalse(b.wayland, forced)
            self.assertEqual(b.argv, ["xrandr"], forced)
            self.assertEqual(b.env["DISPLAY"], ":0", forced)
            self.assertEqual(b.env["XAUTHORITY"], cookie, forced)

    def test_the_wayland_runner_is_left_alone(self):
        """wxrandr finds the session for itself and talks to no X server:
        a DISPLAY injected there would only be a lie in its environment."""
        from warandr import randr
        self.xsock(0)
        rd = self.runtime_dir()
        self.touch(os.path.join(rd, "xauth_abc"), "cookie")
        self.wsock(name="wayland-1")
        passthrough.reset_cache()
        b = randr.choose({"XDG_RUNTIME_DIR": rd, "WAYLAND_DISPLAY": "wayland-1",
                          "PATH": "/nonexistent"})
        self.assertTrue(b.wayland)
        self.assertNotIn("DISPLAY", b.env)
        self.assertNotIn("XAUTHORITY", b.env)

    def test_the_repair_never_overrules_a_working_display(self):
        """A $DISPLAY that opens, and arandr's own --randr-display, are
        answers -- the repair only fills in what is missing or dead."""
        from warandr import randr
        self.xsock(0)
        self.xsock(7)
        self.logind_file("2", TYPE="x11", DISPLAY=":0")
        passthrough.reset_cache()
        b = randr.choose({"DISPLAY": ":7", "PATH": "/nonexistent",
                          "XDG_SESSION_TYPE": "x11"})
        self.assertEqual(b.env["DISPLAY"], ":7")
        b.set_display("localhost:10.0")
        self.assertEqual(b.env["DISPLAY"], "localhost:10.0")



class EmptyPathElement(unittest.TestCase):
    """An empty PATH element ("PATH=:/usr/bin", a trailing colon, os.defpath)
    means the current directory. We re-resolve the real tool *inside* the
    process, long after the user chose how to invoke us, so honouring it
    would execve a file out of whatever directory they had cd'd into."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.plant = os.path.join(self.d, "xdotool")
        with open(self.plant, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.plant, 0o755)
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.addCleanup(os.chdir, self.cwd)

    def test_the_cwd_is_never_searched(self):
        for path in (":/nonexistent", "/nonexistent:", "/nonexistent::/x"):
            self.assertIsNone(passthrough.real_tool("xdotool", {"PATH": path}),
                              "PATH=%r searched the cwd" % path)
            self.assertIsNone(passthrough._which_any("xdotool", {"PATH": path}),
                              "PATH=%r searched the cwd" % path)
        # os.defpath (the PATH-unset fallback) leads with an empty element
        # too; whatever it finds must come from a real directory on it
        for got in (passthrough.real_tool("xdotool", {"PATH": ""}),
                    passthrough._which_any("xdotool", {"PATH": ""})):
            if got is not None:
                self.assertFalse(got.startswith(self.d), got)
                self.assertTrue(os.path.isabs(got), got)

    def test_a_real_directory_on_PATH_still_wins(self):
        self.assertEqual(passthrough.real_tool("xdotool", {"PATH": self.d}),
                         self.plant)


class RootWithNoSession(unittest.TestCase):
    """Root with no SUDO_UID and no logind record (root cron, `ssh root@box`)
    falls back to scanning /tmp/.X11-unix, which is world-writable. Pairing
    the lowest-numbered socket there with a cookie found by a separate scan
    could hand a planted X server the real user's MIT-MAGIC-COOKIE, which is
    full access to their session."""

    def test_the_cookie_owner_follows_the_display_we_chose(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        xd = os.path.join(d, "X11-unix")
        os.makedirs(xd)
        rd = os.path.join(d, "run-user")
        # "somebody else" has to be a uid that is not the one running the suite, or the cookie
        # would legitimately belong to the socket's owner and the assertion below would be wrong
        # (it was, for every runner that is uid 1000).
        victim = "1000" if os.getuid() != 1000 else "1001"
        os.makedirs(os.path.join(rd, victim))
        with open(os.path.join(rd, victim, "xauth_victim"), "wb") as f:
            f.write(b"cookie")
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(os.path.join(xd, "X0"))          # "the attacker's" :0
        saved = (passthrough._X11_SOCK_DIR, passthrough._LOGIND_DIR,
                 passthrough._RUN_USER_DIR)
        passthrough._X11_SOCK_DIR = xd
        passthrough._LOGIND_DIR = os.path.join(d, "none")
        passthrough._RUN_USER_DIR = rd
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)

        def restore():
            (passthrough._X11_SOCK_DIR, passthrough._LOGIND_DIR,
             passthrough._RUN_USER_DIR) = saved
        self.addCleanup(restore)

        # session.find_xauthority() is the last resort and reads the real box: under xvfb-run (or on
        # any X desktop) it finds this uid's own server cookie, which is a legitimate answer for a
        # socket this uid owns and not what this test is about, so it is taken out of the picture.
        with mock.patch.object(passthrough.os, "getuid", lambda: 0), \
                mock.patch.object(passthrough.os, "geteuid", lambda: 0), \
                mock.patch("w11common.session.find_xauthority", lambda *a, **k: None):
            env = passthrough.repair_x_env({})
        self.assertEqual(env.get("DISPLAY"), ":0")
        # the socket is ours (the victim uid is somebody else here), so no cookie
        # from another uid's runtime dir may be handed to it
        self.assertIsNone(env.get("XAUTHORITY"))
        self.assertEqual(passthrough._display_owner_uid(":0"), os.getuid())
        self.assertIsNone(passthrough._display_owner_uid("host:0"))

class MeasuredSessions(Base):
    """U33: the five session shapes recon2 measured, each answered through the seam directories alone.

    `session_kind()` is the decision the whole product hangs off: it runs before backend detection, so it is
    what keeps a desktop that owns a promising bus name -- or a promising IPC socket -- from being handled by
    a Wayland backend on a session where the X server is authoritative.  Every case below is a shape somebody
    booted: Cinnamon on X11 [M recon2/cinnamon.md §3.1], i3 with `$I3SOCK` in the environment [M
    recon2/i3.md §2a], Xfce on Wayland [M recon2/xfce-wayland.md], LXQt/Openbox under SDDM [M
    recon2/openbox.md §2], and GDM's Wayland greeter in front of an Xorg session [M recon2/gnome-xorg.md
    §3]."""

    GREETER_UID = 125            # gdm on the measured Ubuntu box

    def x11_session(self):
        """The seam directories of a plain X11 session: an X socket, and logind saying so."""
        self.xsock(0)
        self.logind_file("1", TYPE="x11", DISPLAY=":0")
        self.runtime_dir()       # the user has a runtime dir, with no wayland socket in it

    def test_a_cinnamon_x11_session_is_x11_although_the_bus_says_cinnamon(self):
        """An X11 Cinnamon session owns `org.Cinnamon` *and* `org.cinnamon.Muffin.DisplayConfig` -- the two
        names that select the Cinnamon window backend and the Muffin display backend.  Both are on the bus
        here, and the answer is still x11, because the bus is not consulted at all."""
        from test_dbus_mini import MockBus
        from w11common.dbus_mini import Bus
        from wdotool import backend_detect
        self.x11_session()
        mock_bus = MockBus()
        self.addCleanup(mock_bus.srv.close)
        holders = []
        for name in ("org.Cinnamon", "org.cinnamon.Muffin.DisplayConfig"):
            conn = Bus(mock_bus.address)
            self.addCleanup(conn.close)
            self.assertEqual(conn.request_name(name), 1)
            holders.append(conn)
        env = dict(DISPLAY=":0", XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="X-Cinnamon",
                   DBUS_SESSION_BUS_ADDRESS=mock_bus.address)
        self.assertEqual(self.kind(**env), "x11")
        # ...and the bus really would have offered the backend, so the line above is not passing by accident
        with mock.patch.dict(os.environ, env, clear=False):
            backend_detect.reset()
            self.addCleanup(backend_detect.reset)
            self.assertIn("org.Cinnamon", backend_detect.session_names() or [])

    def test_i3sock_does_not_defeat_the_handover(self):
        """`$I3SOCK` (and `WDOTOOL_BACKEND=sway` with it) names a compositor we can drive; on i3 that
        compositor is running under an X server that owns the layout, the input and the property store, and
        the handover is settled before any of it is asked [M recon2/i3.md §2a: `WDOTOOL_BACKEND=sway
        getactivewindow` printed the X id, i.e. the real xdotool answered]."""
        from w11common import session as wsession
        self.x11_session()
        sockpath = os.path.join(self.runtime_dir(), "ipc-socket.4242")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sockpath)
        srv.listen(1)
        self.addCleanup(srv.close)
        env = {"PATH": "", "WDOTOOL_REAL_XDOTOOL": FAKE, "DISPLAY": ":0",
               "I3SOCK": sockpath, "WDOTOOL_BACKEND": "sway",
               "XDG_RUNTIME_DIR": self.runtime_dir()}
        # the socket is findable: there really is a backend here to be defeated
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(wsession.find_sway_socket(), sockpath)
        self.assertEqual(self.kind(**env), "x11")
        self.stub_execve()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["xdotool", "getactivewindow"]):
            passthrough.reset_cache()
            with self.assertRaises(ExecCalled) as cm:
                wdotool_cli.main()
        self.assertEqual(cm.exception.argv, ["xdotool", "getactivewindow"])

    def test_xfce_on_wayland_is_wayland(self):
        """`startxfce4` on labwc exports `XDG_SESSION_TYPE=wayland` and `XDG_CURRENT_DESKTOP=XFCE` with both
        a live Wayland socket and a live `$DISPLAY` (Xwayland).  Reading the desktop name -- or the DISPLAY --
        as evidence of X11 would hand Xfce-on-Wayland to the real xdotool, which is the regression this pins
        [M recon2/xfce-wayland.md]."""
        rd = self.runtime_dir()
        self.wsock()
        self.xsock(0)
        self.assertEqual(self.kind(XDG_RUNTIME_DIR=rd, WAYLAND_DISPLAY="wayland-0",
                                   XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="XFCE",
                                   DESKTOP_SESSION="xfce", DISPLAY=":0"),
                         "wayland")

    def test_lxqt_under_sddm_is_x11_from_an_empty_environment(self):
        """Lubuntu 26.04 is LXQt 2.3 on Openbox under SDDM.  `ssh root@box` there has no DISPLAY, no
        XAUTHORITY and no XDG_SESSION_TYPE at all; logind's record of the user's session is the only truth,
        and all four tools have to reach the original through it [M recon2/openbox.md §2]."""
        self.x11_session()
        self.assertEqual(self.kind(), "x11")
        self.assertEqual(self.kind(XDG_SESSION_TYPE="tty",
                                   XDG_RUNTIME_DIR=os.path.join(self.runuser, "0")),
                         "x11")

    def greeter_socket(self):
        """GDM's own `wayland-0` under uid 125, and the ownership the real one has.

        The seam directories are made by this test as its own user, so the owner is patched: without it the
        greeter's socket looks like a real user's and the scan below would take it, which is exactly the bug
        the ownership rule exists to prevent."""
        greeter = self.touch(os.path.join(self.mkdir("run-user", str(self.GREETER_UID)), "wayland-0"))
        real_owner = passthrough._owner
        p = mock.patch.object(passthrough, "_owner",
                              lambda path: self.GREETER_UID if path == greeter else real_owner(path))
        p.start()
        self.addCleanup(p.stop)
        return greeter

    def test_a_wayland_greeter_in_front_of_xorg_is_x11_four_ways(self):
        """GDM's default on 24.04 when the user picks "Ubuntu on Xorg": `/run/user/125/wayland-0` is the
        *greeter's*, and the user's session is X11.  Measured through these seams in all four ways a tool is
        invoked [M recon2/gnome-xorg.md §3]."""
        self.x11_session()
        self.greeter_socket()
        # in-session
        self.assertEqual(self.kind(DISPLAY=":0", XDG_SESSION_TYPE="x11",
                                   XDG_RUNTIME_DIR=self.runtime_dir()), "x11")
        # sudo from the session: root's own XDG_SESSION_TYPE is the tty it came in on
        self.assertEqual(self.kind(XDG_SESSION_TYPE="tty", SUDO_UID=str(self.uid)), "x11")
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            # root over ssh, empty environment: no target uid at all, logind readable
            self.assertEqual(self.kind(), "x11")
            # ...and with logind unreadable, the socket scan alone -- where the greeter's socket is the only
            # Wayland socket on the box and must still lose to the X one
            shutil.rmtree(self.logind)
            os.makedirs(self.logind)
            self.assertEqual(self.kind(), "x11")

    def test_a_real_users_socket_in_the_same_tree_wins(self):
        """The control for the arm above: the scan does find a Wayland session when there is one, so the four
        answers are about whose socket it is and not about the scan finding nothing."""
        self.x11_session()
        self.greeter_socket()
        shutil.rmtree(self.logind)
        os.makedirs(self.logind)
        self.wsock()                     # the user's own, in the user's own runtime dir
        with mock.patch.object(passthrough.os, "getuid", lambda: 0):
            self.assertEqual(self.kind(), "wayland")


class MissingOriginalPerDistro(Base):
    """U14: exit 127's line, byte for byte, per `/etc/os-release` family.

    Every "install the original" message this project prints named a Debian package and `apt`, on every
    distribution: `apt install xdotool` printed on a NixOS box with no apt [M recon2/nixos.md], `x11-utils`
    named on Fedora where the package is called `xprop` and `x11-utils` does not exist [M recon2/fedora.md],
    and `x11-xserver-utils` on Arch where it is `xorg-xrandr` [M recon2/arch.md].

    The family comes from `w11common.distro`, whose seam is a module constant rather than an environment
    variable, so these plant os-release files and point the constant at them.  The subprocess half of the
    handover (a real exit 127 through a real PATH) is tests/test_passthrough_exec.py:NoOriginal, which runs
    on whatever family the box really is."""

    #: os-release bodies, as the four distributions write them [M the four recon reports]
    RELEASES = {
        "debian": 'ID=debian\nID_LIKE=""\n',
        "ubuntu": 'ID=ubuntu\nID_LIKE=debian\n',
        "fedora": "ID=fedora\nVERSION_ID=44\n",
        "arch": "ID=arch\n",
        "nixos": 'ID=nixos\nVERSION="26.05"\n',
    }
    #: family -> tool -> the whole parenthesis
    WANT = {
        "debian": {"xdotool": "apt install xdotool", "wmctrl": "apt install wmctrl",
                   "xprop": "apt install x11-utils", "xrandr": "apt install x11-xserver-utils"},
        "ubuntu": {"xdotool": "apt install xdotool", "wmctrl": "apt install wmctrl",
                   "xprop": "apt install x11-utils", "xrandr": "apt install x11-xserver-utils"},
        "fedora": {"xdotool": "dnf install xdotool", "wmctrl": "dnf install wmctrl",
                   "xprop": "dnf install xprop", "xrandr": "dnf install xrandr"},
        "arch": {"xdotool": "pacman -S xdotool", "wmctrl": "pacman -S wmctrl",
                 "xprop": "pacman -S xorg-xprop", "xrandr": "pacman -S xorg-xrandr"},
        "nixos": {"xdotool": "nix-env -iA nixpkgs.xdotool",
                  "wmctrl": "nix-env -iA nixpkgs.wmctrl",
                  "xprop": "nix-env -iA nixpkgs.xorg.xprop",
                  "xrandr": "nix-env -iA nixpkgs.xorg.xrandr"},
    }

    @contextlib.contextmanager
    def plant(self, body, name="os-release"):
        """Point `distro.OS_RELEASE` at a file holding `body` for the duration of the `with`; None means the
        file is not there at all.

        A context manager rather than an addCleanup: the table below plants twenty times inside one test, and
        the obvious way to undo a plant mid-test -- `self.doCleanups()` -- also runs setUp's cleanups, which
        deletes `self.tmp` (the next `touch()` silently recreates it and nothing removes it again: one
        /tmp/w11_pt_* per run, 16 of them on this box) and drops the `_X11_SOCK_DIR`/`_LOGIND_DIR` patches for
        every remaining subTest."""
        from w11common import distro
        path = os.path.join(self.tmp, name)
        if body is None:
            path = os.path.join(self.tmp, "no-such-os-release")
        else:
            self.touch(path, body)
        with contextlib.ExitStack() as stack:
            for attr, value in (("OS_RELEASE", path),
                                ("NIXOS_MARKER", os.path.join(self.tmp, "no-such-NIXOS"))):
                stack.enter_context(mock.patch.object(distro, attr, value))
            yield path

    def test_every_family_names_its_own_package_for_all_four_tools(self):
        self.maxDiff = None
        for family, body in self.RELEASES.items():
            for tool in ("xdotool", "wmctrl", "xprop", "xrandr"):
                with self.subTest(family=family, tool=tool):
                    with self.plant(body, name="os-release." + family):
                        self.assertEqual(
                            passthrough._missing_message(tool),
                            "%s: this is w11's clone and this is an X11 session, but no real %s was "
                            "found on PATH -- install it (%s) or set %s=/path/to/%s\n"
                            % (tool, tool, self.WANT[family][tool],
                               passthrough._OVERRIDE[tool], tool))

    def test_no_os_release_at_all_keeps_the_debian_bytes(self):
        """A distribution we cannot identify gets what every box got before this table existed, which is why
        no older test of this line moved."""
        with self.plant(None):
            self.assertIn("(apt install x11-utils)", passthrough._missing_message("xprop"))

    def test_the_other_half_of_the_line_is_untouched(self):
        """The only thing keyed on the family is the parenthesis: the reason, the tool name and the override
        variable are the same sentence they always were, including on a forced handover."""
        with self.plant(self.RELEASES["arch"]):
            msg = passthrough._missing_message("xrandr", x11=False)
        self.assertIn("a handover to the real tool was asked for", msg)
        self.assertNotIn("this is an X11 session", msg)
        self.assertIn("(pacman -S xorg-xrandr)", msg)
        self.assertTrue(msg.endswith("set WXRANDR_REAL_XRANDR=/path/to/xrandr\n"), msg)


class WarandrGtkHintPerDistro(Base):
    """U14's fourth consumer: warandr's "install GTK 3 for Python" line, per family.

    warandr is the one tool in the set that never hands over -- there is nothing to hand over to, arandr is
    not installed beside it -- so this line is all a user with no python3-gi has, and it named
    `sudo apt install python3-gi gir1.2-gtk-3.0` on Fedora, Arch and NixOS alike.  The packages are
    `python3-gobject gtk3` [M recon2/fedora.md], `python-gobject gtk3` [M recon2/arch.md] and
    `nixpkgs.python3Packages.pygobject3 nixpkgs.gtk3` [M recon2/nixos.md] -- and nix-env is per-user, so
    NixOS is the one family whose line carries no `sudo`.

    Kept beside the exit-127 table above because it is the same table (`w11common.distro`) and the same claim;
    the `GTK_HINT` constant tests/test_warandr_model.py pins is the Debian rendering and does not move."""

    #: family -> the whole line, with the error text already in it
    WANT = {
        "debian": "warandr: GTK 3 for Python is not available (no module named gi) - on Ubuntu/Debian: "
                  "sudo apt install python3-gi gir1.2-gtk-3.0\n",
        "fedora": "warandr: GTK 3 for Python is not available (no module named gi) - on Fedora: "
                  "sudo dnf install python3-gobject gtk3\n",
        "arch": "warandr: GTK 3 for Python is not available (no module named gi) - on Arch: "
                "sudo pacman -S python-gobject gtk3\n",
        "nixos": "warandr: GTK 3 for Python is not available (no module named gi) - on NixOS: "
                 "nix-env -iA nixpkgs.python3Packages.pygobject3 nixpkgs.gtk3\n",
    }
    ERR = "no module named gi"

    def test_every_family_gets_its_own_installer_and_packages(self):
        from warandr import cli as warandr_cli
        self.maxDiff = None
        for family, want in self.WANT.items():
            with self.subTest(family=family):
                self.assertEqual(warandr_cli.gtk_hint(self.ERR, family), want)

    def test_only_nixos_is_printed_without_sudo(self):
        """`nix-env -iA` run as root installs into root's profile and not the user's, which is why the sudo
        is per family and not a constant in the sentence."""
        from warandr import cli as warandr_cli
        for family in ("debian", "fedora", "arch"):
            self.assertIn(": sudo ", warandr_cli.gtk_hint(self.ERR, family), family)
        self.assertNotIn("sudo", warandr_cli.gtk_hint(self.ERR, "nixos"))

    def test_a_box_we_cannot_identify_still_gets_the_debian_bytes(self):
        """No family argument and no readable os-release: the answer is the constant every existing test of
        this line pins, so an unknown distribution keeps today's behaviour rather than a worse guess."""
        from warandr import cli as warandr_cli
        with self.plant_release(None):
            self.assertEqual(warandr_cli.gtk_hint(self.ERR), warandr_cli.GTK_HINT % self.ERR)
            self.assertEqual(warandr_cli.gtk_hint(self.ERR), self.WANT["debian"])

    def test_the_family_comes_off_os_release_when_none_is_passed(self):
        """The production call site passes no family: `/etc/os-release` is what picks the line, which is the
        whole point of the change (an Arch box printed the Debian one)."""
        from warandr import cli as warandr_cli
        with self.plant_release("ID=arch\n"):
            self.assertEqual(warandr_cli.gtk_hint(self.ERR), self.WANT["arch"])

    @contextlib.contextmanager
    def plant_release(self, body):
        """`distro.OS_RELEASE` pointed at a planted file, or at nothing when `body` is None."""
        from w11common import distro
        path = os.path.join(self.tmp, "os-release")
        if body is None:
            path = os.path.join(self.tmp, "no-such-os-release")
        else:
            self.touch(path, body)
        with contextlib.ExitStack() as stack:
            for attr, value in (("OS_RELEASE", path),
                                ("NIXOS_MARKER", os.path.join(self.tmp, "no-such-NIXOS"))):
                stack.enter_context(mock.patch.object(distro, attr, value))
            yield


if __name__ == "__main__":
    unittest.main(verbosity=2)
