#!/usr/bin/env python3
"""offline tests for wxprop.cli + wxprop.core.

Everything here runs without a compositor or X server: the X plane is
disabled via WXPROP_NO_X and the compositor is a fake sway backend injected
through core._detect_backend. Error strings and exit codes were captured
from real xprop 1.2.8; the deliberate
deviations (native plane, -version identity, -font, click-select) are
covered as their own contracts.
"""

import io
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import test_dbus_mini` resolve only with the tests
# directory itself on sys.path: running this file by path puts it there for
# free, `python3 -m unittest tests/<file>.py` does not (tests/test_passthrough.py
# fails over a file that imports one of them without this line).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from test_dbus_mini import MockBus
from wdotool.backend_cinnamon import CinnamonBackend
from wxprop import cli, core

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

USAGE_FIRST = "usage:  xprop [-options ...] [[format [dformat]] atom] ...\n"


class _CapStdout:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, s):
        self.buffer.write(s.encode("utf-8", "surrogateescape"))
        return len(s)

    def flush(self):
        pass

    def fileno(self):
        raise ValueError("no fileno in capture")

    def isatty(self):
        return False


class _FakeWin:
    def __init__(self, **kw):
        self.id = kw.get("id", 0)
        self.title = kw.get("title", "")
        self.class_ = kw.get("class_", "")
        self.pid = kw.get("pid", 0)
        self.x = self.y = self.w = self.h = 0
        self.focused = kw.get("focused", False)
        self.visible = kw.get("visible", True)
        self.desktop = kw.get("desktop", 0)


class _FakeSway:
    """Just enough of wdotool.backend_sway for the native plane."""

    # sway waits for a focus change rather than a click; the hint follows the
    # backend, not the tool (see wdotool.backend.WindowBackend).
    select_window_hint = "focus the target window to select it"

    def __init__(self):
        self.foot = {
            "id": 6, "app_id": "footw", "name": "WL-Foot", "pid": 4242,
            "fullscreen_mode": 0, "visible": True, "sticky": False,
        }
        self.xterm = {
            "id": 5, "app_id": None, "name": "XW-Xterm", "pid": 4243,
            "window": 0x40000C, "fullscreen_mode": 0, "visible": True,
            "sticky": False,
            "window_properties": {"instance": "xterm", "class": "XTerm"},
        }

    def _nodes(self):
        return [
            (self.xterm, _FakeWin(id=5, title="XW-Xterm", pid=4243,
                                  desktop=0), False, "1"),
            (self.foot, _FakeWin(id=6, title="WL-Foot", class_="footw",
                                 pid=4242, desktop=0, focused=True),
             False, "1"),
        ]

    def num_desktops(self):
        return 2

    def get_desktop(self):
        return 0

    def select_window(self):
        return 6


class CliTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {
            "WXPROP_NO_X": "1", "WXPROP_ARGV0": "xprop", "LC_ALL": "C"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COLORTERM", None)
        os.environ.pop("XPROPFORMATS", None)

    def run_cli(self, *args, backend="none", hostname="testhost"):
        if backend == "fake":
            fake = _FakeSway()

            def det():
                return fake
        elif backend == "real":
            # the real detector, patched over itself: the caller has already
            # arranged the session it should find (or not find)
            det = core._detect_backend
        else:
            def det():
                return None
        out = _CapStdout()
        err = io.StringIO()
        with mock.patch.object(core, "_detect_backend", det), \
                mock.patch.object(core, "hostname",
                                  lambda: hostname), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            code = cli.main(list(args))
        return code, out.buffer.getvalue(), err.getvalue()


class BackendOnAnX11SessionTest(unittest.TestCase):
    """The native fallback on a plain X11 session must not consult a
    compositor. We reach it only when no real xprop is installed, and the X
    server is authoritative there -- but the detector goes by the session
    bus, and KWin on Xorg owns org.kde.KWin exactly as KWin on Wayland
    does, so `-root` would answer with KWin's synthesized window list where
    the X root has the real one."""

    def setUp(self):
        from w11common import passthrough
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)

    def _detect(self, env):
        called = []

        def detector():
            called.append(True)
            return "a backend"

        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("wdotool.backend_detect.detect", detector):
            return core._detect_backend(), called

    def test_x11_session_never_detects(self):
        got, called = self._detect({"W11_PASSTHROUGH": "auto",
                                    "XDG_SESSION_TYPE": "x11",
                                    "DISPLAY": ":0"})
        self.assertIsNone(got)
        self.assertEqual(called, [])

    def test_wayland_session_detects_as_before(self):
        got, called = self._detect({"W11_PASSTHROUGH": "auto",
                                    "XDG_SESSION_TYPE": "wayland"})
        self.assertEqual(got, "a backend")
        self.assertEqual(called, [True])

    def test_the_escape_hatch_still_runs_our_own_code(self):
        """W11_PASSTHROUGH=never means "our own code whatever the
        session" -- including the compositor backends, on an X11 box. It is
        what the whole suite runs under."""
        got, called = self._detect({"W11_PASSTHROUGH": "never",
                                    "XDG_SESSION_TYPE": "x11",
                                    "DISPLAY": ":0"})
        self.assertEqual(got, "a backend")
        self.assertEqual(called, [True])


    # -- T36: the session the detector believes it is on ---------------------

    def _detect_without(self, env, drop=()):
        """`_detect()` with `drop` removed from the environment as well:
        mock.patch.dict cannot express "unset"."""
        saved = {k: os.environ.pop(k, None) for k in drop}
        try:
            return self._detect(env)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def _seams(self):
        """A private /tmp/.X11-unix, /run/user and /run/systemd/sessions, so
        the answer comes from the fixture and not from the box the suite runs
        on. Returns (x11 dir, run-user dir)."""
        import shutil
        import tempfile as _tempfile
        from w11common import passthrough
        d = _tempfile.mkdtemp(prefix="wxprop_sess_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        x11 = os.path.join(d, "X11-unix")
        run = os.path.join(d, "run-user")
        logind = os.path.join(d, "logind")
        for p in (x11, run, logind):
            os.makedirs(p)
        for name, value in (("_X11_SOCK_DIR", x11), ("_LOGIND_DIR", logind),
                            ("_RUN_USER_DIR", run)):
            p = mock.patch.object(passthrough, name, value)
            p.start()
            self.addCleanup(p.stop)
        passthrough.reset_cache()
        return x11, run

    def test_a_compositor_on_an_x11_session_is_still_never_asked(self):
        """The detector goes by the session bus, and a Wayland compositor's
        bus name is owned on its X11 build too -- KWin on Xorg owns
        org.kde.KWin, and a GNOME-on-Xorg session owns org.gnome.Shell. So
        the guard cannot be "is a compositor there": it is the session kind,
        checked before backend_detect is imported at all."""
        from w11common import passthrough
        with mock.patch.object(passthrough, "session_kind",
                               lambda tool=None, **kw: "x11"):
            got, called = self._detect({"W11_PASSTHROUGH": "auto"})
        self.assertIsNone(got)
        self.assertEqual(called, [])
        with mock.patch.object(passthrough, "session_kind",
                               lambda tool=None, **kw: "wayland"):
            got, called = self._detect({"W11_PASSTHROUGH": "auto"})
        self.assertEqual((got, called), ("a backend", [True]))

    def test_a_wayland_session_with_a_display_set_still_detects(self):
        """DISPLAY is set on every Wayland session that runs Xwayland, which
        is nearly all of them -- it says nothing about the session kind."""
        self._seams()
        got, called = self._detect({"W11_PASSTHROUGH": "auto",
                                    "XDG_SESSION_TYPE": "wayland",
                                    "DISPLAY": ":1"})
        self.assertEqual((got, called), ("a backend", [True]))

    def _kind(self, env, drop=()):
        """`passthrough.session_kind("xprop")` under the same environment
        `_detect_without()` would use. `_detect_backend()` only *refuses* on
        "x11", so it cannot be asked which of "wayland" and None it saw --
        and the difference is the whole claim of the two tests below."""
        from w11common import passthrough
        e = {k: v for k, v in os.environ.items() if k not in drop}
        e.update(env)
        passthrough.reset_cache()
        return passthrough.session_kind("xprop", env=e)

    def test_under_sudo_the_invoking_users_wayland_socket_decides(self):
        """`sudo wxprop` inherits XDG_SESSION_TYPE=tty (or nothing at all)
        and drops XDG_RUNTIME_DIR, so the environment says "no graphical
        session". SUDO_UID names the user whose session it really is, and a
        wayland-* socket in that user's runtime dir is the evidence.

        The session_kind assertions are what make the socket load-bearing:
        with an empty /run/user/1000 the answer is None, and None is not
        "x11", so `_detect_backend()` goes on to detect either way -- this
        test read as green with no socket at all until they were added."""
        _x11, run = self._seams()
        os.makedirs(os.path.join(run, "1000"))
        sock = os.path.join(run, "1000", "wayland-0")
        open(sock, "w").close()
        env = {"W11_PASSTHROUGH": "auto", "SUDO_UID": "1000",
               "XDG_SESSION_TYPE": "tty"}
        drop = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        self.assertEqual(self._kind(env, drop), "wayland")
        got, called = self._detect_without(env, drop=drop)
        self.assertEqual((got, called), ("a backend", [True]))
        with self.subTest("the socket taken away"):
            os.unlink(sock)
            self.assertIsNone(self._kind(env, drop))

    def test_under_sudo_with_only_an_x_socket_it_is_an_x11_session(self):
        """The same fixture with the wayland socket taken away and an X
        server socket in its place: an X11 session, and the native plane is
        the whole answer -- no compositor is consulted."""
        x11, run = self._seams()
        os.makedirs(os.path.join(run, "1000"))
        open(os.path.join(x11, "X0"), "w").close()
        got, called = self._detect_without(
            {"W11_PASSTHROUGH": "auto", "SUDO_UID": "1000",
             "XDG_SESSION_TYPE": "tty"},
            drop=("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"))
        self.assertIsNone(got)
        self.assertEqual(called, [])


class NoSessionErrorTest(CliTestBase):
    """T36: the error a tool prints when there is no session at all has to
    carry that tool's own name.

    `backend.set_program()` is process-global: the three CLIs set it at the
    top of main(), and a suite that runs two mains in one process (or a
    future `wwmctl` that shells out to a wdotool entry point) would otherwise
    print the previous tool's name at the user. The originals never do:
    wmctrl says "wmctrl:", xprop says "xprop:"."""

    def test_wxprop_names_itself_and_not_wdotool(self):
        """The cheap half: no backend and no X plane at all (WXPROP_NO_X=1
        from setUp), which is the "nothing to talk to" path through
        core.root_target()."""
        from wdotool import backend
        backend.set_program("wdotool")       # as a wdotool run leaves it
        code, out, err = self.run_cli("-root", "_NET_CLIENT_LIST")
        self.assertEqual((code, out), (1, b""))
        self.assertTrue(err.startswith("xprop: error: "), err)
        self.assertNotIn("wdotool", err)
        self.assertEqual(backend.program(), "xprop")

    def test_the_real_detectors_message_names_xprop(self):
        """The half that matters, driven through the real
        `wdotool.backend_detect.detect()`: a session bus that answers
        ListNames with no org.kde.KWin and no org.gnome.Shell, no sway/i3
        socket, and `_wlr()` refusing -- the state a bare Wayland compositor
        (or a GNOME session whose Shell has just crashed) leaves behind.

        `detect()` builds its NoSessionError with `wdotool.backend.program()`,
        a process-global that main() sets: so the message itself, not only
        the prefix wxprop puts in front of it, has to say "xprop". Seeded
        with "wdotool" here, as a previous in-process main() would leave it.
        Session.backend() swallows the exception into `backend_error`, and
        core.root_target() reports it as "cannot examine the root window"."""
        from w11common.errors import CmdError
        from w11common import session
        from wdotool import backend, backend_detect
        bus = MockBus()
        self.addCleanup(bus.close)
        backend_detect.reset()               # drop any bus this process kept
        self.addCleanup(backend_detect.reset)
        backend.set_program("wdotool")
        env = {"DBUS_SESSION_BUS_ADDRESS": bus.address,
               "XDG_SESSION_TYPE": "wayland"}
        saved = {k: os.environ.pop(k, None)
                 for k in ("SWAYSOCK", "I3SOCK", "WAYLAND_DISPLAY",
                           "WDOTOOL_BACKEND")}

        def no_wlr():
            raise CmdError("the compositor does not offer wlr-foreign-toplevel")

        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(backend_detect, "_wlr", no_wlr), \
                    mock.patch.object(session, "find_sway_socket",
                                      lambda: None), \
                    mock.patch.object(backend_detect.session,
                                      "find_sway_socket", lambda: None):
                # find_sway_socket is patched as well as unset: this host runs
                # a headless sway in the live files, and whether one happens to
                # be up must not decide what this test measures.
                code, out, err = self.run_cli("-root", "_NET_CLIENT_LIST",
                                              backend="real")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual((code, out), (1, b""))
        self.assertTrue(err.startswith("xprop: error: cannot examine the "
                                       "root window: "), err)
        self.assertIn("xprop: no Wayland session found", err)
        self.assertIn("no KWin, GNOME Shell or Cinnamon on the session D-Bus", err)
        self.assertNotIn("wdotool", err)
        self.assertEqual(backend.program(), "xprop")


class VersionHelpGrammarTest(CliTestBase):
    # The 1.2.8 RELEASE binary handles -grammar/-help/-version in the option
    # loop (NOT a pre-scan): single dash only, in argv order, after the
    # display would be opened. All assertions below were verified against the
    # real xprop 1.2.8 binary, which is the only authority here: the release
    # source and the option loop it actually runs disagree about the order.
    def test_help_goes_to_stderr_exit_0(self):
        code, out, err = self.run_cli("-help")
        self.assertEqual((code, out), (0, b""))
        self.assertTrue(err.startswith(USAGE_FIRST), err)
        self.assertIn("    -version                       "
                      "print program version\n", err)

    def test_double_dash_help_is_unrecognized(self):
        # oracle: `xprop --help` -> "unrecognized argument --help", exit 1
        code, out, err = self.run_cli("--help")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("xprop: unrecognized argument --help\n\n", err)

    def test_help_wins_over_everything(self):
        # xprop2-spy-usage: `-spy -id W -len 1 -help` -> usage, exit 0
        code, out, err = self.run_cli("-spy", "-id", "0x1", "-len", "1",
                                      "-help")
        self.assertEqual((code, out), (0, b""))
        self.assertTrue(err.startswith(USAGE_FIRST))

    def test_bad_flag_before_version_is_usage_error(self):
        # oracle: `xprop -badflag -version` -> the option loop hits -badflag
        # FIRST (argv order), so it's a usage error, NOT a version print
        code, out, err = self.run_cli("-badflag", "-version")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("xprop: unrecognized argument -badflag\n\n", err)

    def test_grammar_single_dash_only(self):
        code, out, err = self.run_cli("-grammar")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(b"Grammar for xprop:\n\n"))
        self.assertTrue(out.endswith(
            b"\tnormal char ::= <any char except a digit, $, ?, \\, or )>"
            b"\n\n"), out)
        # --grammar is NOT accepted (matches the oracle)
        code, _out, err = self.run_cli("--grammar")
        self.assertEqual(code, 1)
        self.assertIn("xprop: unrecognized argument --grammar\n\n", err)

    def test_version(self):
        # byte parity with the oracle: `xprop -version` -> "xprop 1.2.8"
        code, out, err = self.run_cli("-version")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b"xprop 1.2.8\n")

    def test_double_dash_version_is_unrecognized(self):
        code, out, err = self.run_cli("--version")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("xprop: unrecognized argument --version\n\n", err)

    def test_version_after_root(self):
        # -root is a select arg (consumed early); -version still fires in
        # the option loop -> version print, exit 0 (oracle: xprop -root
        # -version -> "xprop 1.2.8")
        code, out, err = self.run_cli("-root", "-version", backend="fake")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b"xprop 1.2.8\n")


class ArgErrorsTest(CliTestBase):
    def test_unrecognized_argument(self):
        code, _out, err = self.run_cli("-zorp")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(
            "xprop: unrecognized argument -zorp\n\n" + USAGE_FIRST), err)

    def test_missing_arguments(self):
        cases = (
            (("-id",), "-id requires an argument"),
            (("-name",), "-name requires an argument"),
            (("-display",), "-display requires an argument"),
            (("-id", "0x1", "-len"), "-len requires an argument"),
            (("-id", "0x1", "-fs"), "-fs requires an argument"),
            (("-id", "0x1", "-formats"), "-fs requires an argument"),
            (("-id", "0x1", "-font"), "-font requires an argument"),
            (("-id", "0x1", "-remove"), "-remove requires an argument"),
            (("-id", "0x1", "-set", "A"), "insufficient arguments for -set"),
            (("-id", "0x1", "-f"), "insufficient arguments for -format"),
            (("-id", "0x1", "-f", "A"), "insufficient arguments for "
                                        "-format"),
        )
        for args, msg in cases:
            code, _out, err = self.run_cli(*args)
            self.assertEqual(code, 1, args)
            self.assertTrue(err.startswith("xprop: %s\n\n" % msg),
                            (args, err))

    def test_spec_without_atom(self):
        code, _out, err = self.run_cli("-id", "0x1", "8s")
        self.assertEqual(code, 1)
        self.assertIn("xprop: format specified without atom\n\n", err)
        code, _out, err = self.run_cli("-id", "0x1", "8s", "$0\\n")
        self.assertEqual(code, 1)
        self.assertIn("xprop: dformat specified without atom\n\n", err)

    def test_invalid_window_id(self):
        for bad in ("zzz", "0", "0xzz"):
            code, _out, err = self.run_cli("-id", bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(
                err, "xprop: error: Invalid window id format: %s.\n" % bad)

    def test_partial_id_parses_like_sscanf(self):
        self.assertEqual(cli._parse_window_id("12abc"), 12)
        self.assertEqual(cli._parse_window_id("0x40000czz"), 0x40000C)
        self.assertEqual(cli._parse_window_id(" 7"), 7)

    def test_synthesized_atom_ids_cannot_be_mistaken_for_real_ones(self):
        """wxprop-3: the native plane's atom table numbers EWMH names
        itself. Numbered from 0x100 those ids sat inside the range a real X
        server hands out, so an id copied out of a native window's dump and
        fed to a real X tool named a plausible WRONG atom instead of
        failing."""
        atoms = core.NativeAtoms()
        for name, a in atoms.by_name.items():
            if a <= 68:                    # the predefined X atoms, 1..68
                self.assertEqual(atoms.by_id[a], name)
                continue
            self.assertGreaterEqual(a, 0x10000000, name)
        # ... and a name interned at runtime lands above them too
        self.assertGreaterEqual(atoms.intern("ZZ_BRAND_NEW", True), 0x10000000)

    def test_partial_property_survives_a_rendering_fatal(self):
        """wxprop-4: xprop writes each property as it renders it, so a
        fatal halfway through a value leaves the name line on stdout. We
        buffered the whole segment and dropped it on the way out."""
        code, out, err = self.run_cli("-id", "6", "32s", "_NET_WM_PID",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(out, b"_NET_WM_PID(CARDINAL)")
        self.assertEqual(err, "xprop: error: can't use format character "
                              "'s' with any size except 8.\n")

    def test_id_parsing_matches_sscanf_exactly(self):
        """wxprop-8: the "0x" of sscanf("0x%lx") is a literal -- it skips
        no whitespace and matches no uppercase X -- while the %lu fallback
        skips whitespace and takes a sign (strtoul)."""
        self.assertEqual(cli._parse_window_id("-5"), 0xFFFFFFFB)
        self.assertEqual(cli._parse_window_id("+7"), 7)
        self.assertEqual(cli._parse_window_id("  12"), 12)
        for bad in (" 0x20", "0X20", "\t0x20"):
            with self.assertRaises(cli.FatalError) as cm:
                cli._parse_window_id(bad)
            self.assertEqual(str(cm.exception),
                             "Invalid window id format: %s." % bad)

    def test_len_is_a_c_int(self):
        """wxprop-5/6: -len goes through atoi(), which returns an int, and
        the byte budget is compared as an unsigned long. So 4294967296
        truncates to 0 (nothing printed) while 2147483648 truncates to
        INT_MIN, whose negative word count reaches the server as a huge
        unsigned one and fetches everything."""
        empty = b"_NET_SUPPORTED(ATOM) = \n"
        for n in ("4294967296", "-1", "-5", "0"):
            code, out, _e = self.run_cli("-root", "-len", n, "_NET_SUPPORTED",
                                         backend="fake")
            self.assertEqual((code, out), (0, empty), n)
        code, full, _e = self.run_cli("-root", "_NET_SUPPORTED",
                                      backend="fake")
        self.assertEqual(code, 0)
        self.assertNotEqual(full, empty)
        for n in ("2147483648", "-2147483648"):
            code, out, _e = self.run_cli("-root", "-len", n, "_NET_SUPPORTED",
                                         backend="fake")
            self.assertEqual((code, out), (0, full), n)

    def test_bad_format_flag(self):
        code, _out, err = self.run_cli("-id", "0x1", "-f", "A", "s8")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Bad format: s8.\n")

    def test_font_without_an_x_server_is_a_clean_error(self):
        code, _out, err = self.run_cli("-font", "fixed")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Unable to open font fixed!\n")


class FontTest(CliTestBase):
    """wxprop-9: -font used to be an unconditional "cannot open it".
    XWayland serves the core fonts (xfonts-base), so it is implementable:
    OpenFont + QueryFont, xprop's FONT format table, and values with no
    type -- so no "(TYPE)" and an unmapped property printed as bare hex.
    Byte-identical to `xprop -font fixed` live on GNOME 46."""

    FIXED = [("FONTNAME_REGISTRY", 0x59), ("FOUNDRY", 0),
             ("PIXEL_SIZE", 13), ("POINT_SIZE", 120), ("FONT", 0)]

    class _Conn:
        def __init__(self, props):
            self.props = props
            self.names = {1000 + i: n for i, (n, _v) in enumerate(props)}
            self.names[2000] = "Misc"
            self.names[2001] = "-Misc-Fixed-Medium-R--13-120"
            self.opened = []

        def atom(self, name, only_if_exists=False):
            for a, n in self.names.items():
                if n == name:
                    return a
            return 0 if only_if_exists else 3000

        def get_atom_name(self, a):
            return self.names.get(a)

        def font_properties(self, name):
            self.opened.append(name)
            if name != "fixed":
                raise RuntimeError("BadName")
            vals = {"FOUNDRY": 2000, "FONT": 2001}
            return [(self.atom(n), vals.get(n, v)) for n, v in self.props]

    def run_font(self, *args):
        conn = self._Conn(self.FIXED)
        with mock.patch.object(core, "_x11_connect", lambda *a, **k: conn):
            with mock.patch.dict(os.environ, {"WXPROP_NO_X": ""}):
                os.environ.pop("WXPROP_NO_X")
                out = _CapStdout()
                err = io.StringIO()
                with mock.patch.object(core, "_detect_backend",
                                       lambda: None), \
                        mock.patch.object(sys, "stdout", out), \
                        mock.patch.object(sys, "stderr", err):
                    code = cli.main(list(args))
        return code, out.buffer.getvalue(), err.getvalue(), conn

    def test_full_dump(self):
        code, out, err, conn = self.run_font("-font", "fixed")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(conn.opened, ["fixed"])
        self.assertEqual(out, (
            b"FONTNAME_REGISTRY = 0x59\n"        # unmapped: xprop's "0x"
            b"FOUNDRY = Misc\n"                  # 32a: the atom's name
            b"PIXEL_SIZE = 13\n"                 # 32c
            b"POINT_SIZE = 120\n"
            b"FONT = -Misc-Fixed-Medium-R--13-120\n"))

    def test_named_properties_and_a_missing_one(self):
        code, out, err, _c = self.run_font("-font", "fixed", "FOUNDRY",
                                           "ZZ_NOPE", "POINT_SIZE")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (b"FOUNDRY = Misc\n"
                               b"ZZ_NOPE:  no such atom on any window.\n"
                               b"POINT_SIZE = 120\n"))

    def test_font_that_will_not_open(self):
        code, _out, err, _c = self.run_font("-font", "zzznosuch")
        self.assertEqual(code, 1)
        self.assertEqual(err,
                         "xprop: error: Unable to open font zzznosuch!\n")

    def test_set_and_remove_are_refused(self):
        for opt, args in (("-remove", ("-remove", "FOUNDRY")),
                          ("-set", ("-set", "FOUNDRY", "x"))):
            code, _out, err, _c = self.run_font("-font", "fixed", *args)
            self.assertEqual(code, 1)
            self.assertEqual(err, "xprop: error: %s works only on windows, "
                                  "not fonts\n" % opt)

    def test_spy_on_a_font_just_dumps(self):
        code, out, err, _c = self.run_font("-font", "fixed", "-spy",
                                           "FOUNDRY")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b"FOUNDRY = Misc\n")

    def test_explicit_display_that_cannot_open(self):
        code, _out, err = self.run_cli("-display", ":9313", "-root")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop:  unable to open display ':9313'\n")

    def test_mixed_set_and_specs(self):
        # -remove keeps its arity, so the trailing spec is diagnosed
        code, _out, err = self.run_cli("-id", "6", "-remove", "A",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(
            "xprop: unrecognized argument WM_CLASS\n\n"), err)

    def test_set_swallows_one_extra_token(self):
        # xprop.c:2052 bug, ported: `-set name value` eats the next arg,
        # so no "unrecognized argument" fires — the native -set error does
        code, _out, err = self.run_cli("-id", "6", "-set", "A", "b",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err, "xprop: error: -set cannot work on a native Wayland "
                 "window (it has no X property store)\n")

    def test_no_window_with_name(self):
        code, _out, err = self.run_cli("-name", "nosuchwindowname",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            "xprop: error: No window with name nosuchwindowname exists!\n")

    def test_unknown_id_without_x(self):
        code, _out, err = self.run_cli("-id", "0x999999", backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err, "xprop: error: window id # 0x999999 does not exists!\n")


NATIVE_DUMP = (
    b"_NET_WM_STATE(ATOM) = \n"
    b"_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n"
    b"_NET_WM_DESKTOP(CARDINAL) = 0\n"
    b"_NET_WM_PID(CARDINAL) = 4242\n"
    b'WM_CLIENT_MACHINE(STRING) = "testhost"\n'
    b'WM_CLASS(STRING) = "footw", "footw"\n'
    b'_NET_WM_NAME(UTF8_STRING) = "WL-Foot"\n'
    b'WM_NAME(STRING) = "WL-Foot"\n')


class NativePlaneTest(CliTestBase):
    def test_full_dump(self):
        code, out, err = self.run_cli("-id", "6", backend="fake")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, NATIVE_DUMP)

    def test_single_property_for_scripts(self):
        code, out, _err = self.run_cli("-id", "6", "WM_CLASS",
                                      backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_notype_and_len_apply(self):
        code, out, _err = self.run_cli("-notype", "-len", "3", "-id", "6",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS = "foo"\n')

    def test_dformat_spec(self):
        code, out, _err = self.run_cli(
            "-id", "6", "8s", " instance=$0 class=$1\\n", "WM_CLASS",
            backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) instance="footw" '
                              b'class="footw"\n')

    def test_no_such_atom_and_not_found(self):
        code, out, _err = self.run_cli("-id", "6", "NOSUCHATOM123", "ATOM",
                                       backend="fake")
        self.assertEqual(code, 0)  # exit stays 0, like the oracle
        self.assertEqual(
            out,
            b"NOSUCHATOM123:  no such atom on any window.\n"
            b"ATOM:  not found.\n")

    def test_f_interns_the_atom(self):
        # after -f NAME 8s the atom exists -> "not found" instead
        code, out, _err = self.run_cli("-id", "6", "-f", "NOSUCHATOM123",
                                       "8s", "NOSUCHATOM123",
                                       backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"NOSUCHATOM123:  not found.\n")

    def test_name_matches_title_then_app_id(self):
        for sel in ("WL-Foot", "footw"):
            code, out, _err = self.run_cli("-name", sel, "WM_CLASS",
                                          backend="fake")
            self.assertEqual(code, 0, sel)
            self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_xwayland_node_without_x_degrades_to_synthesis(self):
        code, out, _err = self.run_cli("-id", "5", "WM_CLASS",
                                      backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_remove_native_fails_cleanly(self):
        code, _out, err = self.run_cli("-id", "6", "-remove", "WM_NAME",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err, "xprop: error: -remove cannot work on a native Wayland "
                 "window (it has no X property store)\n")

    def test_root_without_x(self):
        code, out, err = self.run_cli("-root", backend="fake")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith(b"_NET_SUPPORTED(ATOM) = "))
        self.assertIn(b"_NET_CLIENT_LIST(WINDOW): window id # 0x5, 0x6",
                      lines)
        self.assertIn(b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x6",
                      lines)
        self.assertIn(b"_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 2", lines)
        self.assertIn(b"_NET_CURRENT_DESKTOP(CARDINAL) = 0", lines)
        self.assertIn(b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x0",
                      lines)

    def test_root_never_says_current_is_past_the_count(self):
        """sway-3: sway creates a workspace on demand and GET_WORKSPACES
        lists only the ones that exist, so the synthesized root published
        "current desktop 5, 2 desktops" -- which no EWMH reader can read."""
        with mock.patch.object(_FakeSway, "get_desktop", lambda self: 5):
            code, out, err = self.run_cli("-root", backend="fake")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertIn(b"_NET_CURRENT_DESKTOP(CARDINAL) = 5", lines)
        self.assertIn(b"_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 6", lines)

    def test_root_without_anything(self):
        code, _out, err = self.run_cli("-root", backend="none")
        self.assertEqual(code, 1)
        self.assertIn("cannot examine the root window", err)

    def test_click_select_uses_next_focus(self):
        code, out, err = self.run_cli("WM_CLASS", backend="fake")
        self.assertEqual(code, 0)
        self.assertIn("xprop: focus the target window to select it\n", err)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_frame_is_accepted(self):
        code, out, _err = self.run_cli("-frame", "-id", "6", "WM_CLASS",
                                       backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_fullscreen_hidden_sticky_states(self):
        fake = _FakeSway()
        fake.foot["fullscreen_mode"] = 1
        fake.foot["visible"] = False
        fake.foot["sticky"] = True
        out = _CapStdout()
        with mock.patch.object(core, "_detect_backend", lambda: fake), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = cli.main(["-id", "6", "_NET_WM_STATE"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.buffer.getvalue(),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN, "
            b"_NET_WM_STATE_HIDDEN, _NET_WM_STATE_STICKY\n")


def _state_names(node, win):
    """The `_NET_WM_STATE` atom names `NativeViewTarget` would publish for one
    node, decoded back through the same table that named them -- the code
    `wxprop -id` runs, reached without a session."""
    target = core.NativeViewTarget(None, core.NativeAtoms(), node, win)
    _type, _size, wire = target.fetch(b"_NET_WM_STATE")
    return [target.atom_name(a)
            for a in struct.unpack("<%dI" % (len(wire) // 4), wire)]


class ShadedIsAStateAgain(CliTestBase):
    """`_NET_WM_STATE_SHADED`, which only Cinnamon can be in.

    muffin kept shading after mutter dropped it and KWin 6 removed it -- its
    `Meta.Window` still answers `shade()`, `unshade()` and `is_shaded()`, and
    `wdotool/cinnamon_js.py:57` has been reading the flag into every window row
    for as long as that backend has existed.  Nothing consumed it: on
    resolute-cinnamon-wayland, 2026-09-09, `wwmctl -r :ACTIVE: -b add,shaded`
    really shaded the window (the shell's own `is_shaded()` said `true`) and
    `wxprop -id 1 _NET_WM_STATE` printed `_NET_WM_STATE_FOCUSED` and nothing
    else, because `wxprop/core.py` had no `shaded` arm and its atom table had
    no name to print [vm/live-smoke.d/cinnamon-wayland.sh:307-312,
    goal2/recon/gaps.md 1b #11].  Real xprop on a Cinnamon X11 session prints
    the atom -- muffin's `meta_window_x11_set_net_wm_state`
    (src/x11/window-x11.c, linuxmint/muffin master, read 2026-09-11) writes it
    first and HIDDEN with it -- and X is the oracle.

    The double is `support.FakeBackend` with `views()`, which is the shape
    every bridge backend hands wxprop; the flag travels the real path
    (`core._node_from_view` -> `NativeViewTarget._props`) and not a stub of
    it."""

    def backend(self, **flags):
        win = support.fake_window(6, title="WL-Foot", class_="footw", pid=4242,
                                  desktop=0, focused=flags.pop("focused", False))
        view = support.fake_view(win, app_id="footw", **flags)
        return support.FakeBackend(windows=[win], views=[view])

    def states(self, **flags):
        """The atom names `wxprop -id 6 _NET_WM_STATE` prints, as bytes."""
        fake = self.backend(**flags)
        out = _CapStdout()
        with mock.patch.object(core, "_detect_backend", lambda: fake), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = cli.main(["-id", "6", "_NET_WM_STATE"])
        self.assertEqual(code, 0)
        return out.buffer.getvalue()

    def test_a_shaded_window_says_so(self):
        """Both atoms, because muffin writes both: its HIDDEN arm is
        `if (!meta_window_showing_on_its_workspace (window) || window->shaded)`
        and a shaded window is still showing on its workspace -- it is mapped,
        just rolled up to the titlebar.  So real xprop prints the pair for it
        and a lone SHADED would be a dump X never produced."""
        self.assertEqual(
            self.states(shaded=True),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_SHADED, "
            b"_NET_WM_STATE_HIDDEN\n")

    def test_hidden_still_comes_from_visible_when_nobody_shaded_anything(self):
        """The other half of the `or`: an unmapped window is HIDDEN without
        being SHADED, which is the arm every non-Cinnamon backend uses and the
        one the shaded arm must not have swallowed."""
        self.assertEqual(self.states(hidden=True),
                         b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN\n")

    def test_a_window_nobody_shaded_does_not(self):
        """The control for the arm above: the same window with the flag off
        prints the empty array real xprop prints, not the atom."""
        self.assertEqual(self.states(), b"_NET_WM_STATE(ATOM) = \n")

    def test_it_comes_first_where_muffins_own_set_net_wm_state_puts_it(self):
        """muffin's `meta_window_x11_set_net_wm_state`
        (src/x11/window-x11.c, linuxmint/muffin master, read 2026-09-11)
        appends in this order: SHADED, MODAL, SKIP_PAGER, SKIP_TASKBAR,
        MAXIMIZED_HORZ, MAXIMIZED_VERT, FULLSCREEN, HIDDEN, ABOVE, BELOW,
        DEMANDS_ATTENTION, STICKY, FOCUSED.  That order is the byte order of
        the property, so a reader comparing our dump with the X plane's sees
        the same one -- including HIDDEN after the maximize pair, which the
        shaded window earns from muffin's `|| window->shaded`."""
        self.assertEqual(
            self.states(shaded=True, maximized_h=True, maximized_v=True,
                        focused=True),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_SHADED, "
            b"_NET_WM_STATE_MAXIMIZED_HORZ, _NET_WM_STATE_MAXIMIZED_VERT, "
            b"_NET_WM_STATE_HIDDEN, _NET_WM_STATE_FOCUSED\n")

    def test_the_atom_has_a_name_in_the_synthesized_table(self):
        """The other half of the 2026-09-09 failure, and what the table buys:
        `_p_atoms` would intern an unknown name at runtime, at an id that then
        depends on which properties this process happened to dump first.  In
        the table it has one id for every process and every dump, which is what
        an atom id copied out of one window's output and fed to another
        invocation needs."""
        atoms = core.NativeAtoms()
        self.assertIn("_NET_WM_STATE_SHADED", atoms.by_name)
        self.assertEqual(atoms.name(atoms.by_name["_NET_WM_STATE_SHADED"]),
                         "_NET_WM_STATE_SHADED")

    def test_the_root_advertises_it_with_the_other_view_states(self):
        """A reader consults `_NET_SUPPORTED` before it asks for a state, and
        muffin's own root lists this one."""
        fake = self.backend(shaded=True)
        out = _CapStdout()
        with mock.patch.object(core, "_detect_backend", lambda: fake), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = cli.main(["-root", "_NET_SUPPORTED"])
        self.assertEqual(code, 0)
        self.assertIn(b"_NET_WM_STATE_SHADED", out.buffer.getvalue())

    def test_the_cinnamon_row_carries_the_flag_all_the_way_to_the_array(self):
        """The route end to end, from the key the shell actually sends to the
        atoms wxprop prints, because nothing else in the tree reads it.
        `shaded:w.is_shaded()` has been in the LIST program since that backend
        was written (`wdotool/cinnamon_js.py:57`) and `CinnamonBackend._view`
        dropped it on the floor.  `_view` is a classmethod over a plain row, so
        this is the real parser and the real `_node_from_view`, no D-Bus and no
        paraphrase of either."""
        row = {"id": 3, "xid": 0, "title": "Rolled up", "cls": "Foo",
               "inst": "foo", "pid": 41, "ct": 0, "wt": "NORMAL",
               "x": 0, "y": 0, "w": 10, "h": 10, "ws": 0, "act": 0,
               "foc": False, "min": False, "max": 0, "full": False,
               "above": False, "shaded": True, "skipt": False, "dec": True}
        view = CinnamonBackend._view(row)
        self.assertTrue(view.shaded)
        self.assertIn("_NET_WM_STATE_SHADED",
                      _state_names(core._node_from_view(view), view.window))
        row["shaded"] = False
        self.assertNotIn("_NET_WM_STATE_SHADED",
                         _state_names(core._node_from_view(
                             CinnamonBackend._view(row)), view.window))

    def test_a_sway_tree_never_grows_the_state(self):
        """The arm is gated on the views() marker for the same reason every
        other rich state is: a sway node dict has no `shaded` key, sway has no
        shading, and a tree that grew the atom would be inventing one."""
        fake = _FakeSway()
        fake.foot["shaded"] = True          # a key sway would never write
        out = _CapStdout()
        with mock.patch.object(core, "_detect_backend", lambda: fake), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = cli.main(["-id", "6", "_NET_WM_STATE"])
        self.assertEqual(code, 0)
        self.assertEqual(out.buffer.getvalue(), b"_NET_WM_STATE(ATOM) = \n")


class IdCollisionTest(CliTestBase):
    """`-id N` is an X window id in every xprop manual there is, and a
    compositor id is not an X id: KWin mints its own, Mutter's are Mutter's,
    sway's are node ids. The two number spaces are not disjoint, so one
    window's compositor id can equal another window's X id -- and a single
    pass in the compositor's own listing order then answered about whichever
    of the two it happened to list first."""

    class _Collide(_FakeSway):
        """The native window is numbered with the XWayland window's X id,
        and the compositor lists it first."""

        XID = 0x40000C

        def __init__(self):
            super().__init__()
            self.xterm["window"] = self.XID
            self.foot["id"] = self.XID

        def _nodes(self):
            return [
                (self.foot, _FakeWin(id=self.XID, title="WL-Foot",
                                     class_="footw", pid=4242, desktop=0,
                                     focused=True), False, "1"),
                (self.xterm, _FakeWin(id=5, title="XW-Xterm", pid=4243,
                                      desktop=0), False, "1"),
            ]

    def run_collide(self, *args):
        fake = self._Collide()
        out = _CapStdout()
        err = io.StringIO()
        with mock.patch.object(core, "_detect_backend", lambda: fake), \
                mock.patch.object(core, "hostname", lambda: "testhost"), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            code = cli.main(list(args))
        return code, out.buffer.getvalue(), err.getvalue()

    def test_the_x_window_wins(self):
        code, out, err = self.run_collide("-id", str(self._Collide.XID),
                                          "WM_CLASS")
        self.assertEqual((code, err), (0, ""))
        # the XWayland window's, not the native one's ("footw", "footw")
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_a_compositor_id_nothing_else_claims_still_resolves(self):
        """The second pass: 5 is the XWayland node's compositor id and no
        window's X id, so it still answers, exactly as before."""
        code, out, err = self.run_collide("-id", "5", "WM_CLASS")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')


class FormatFileTest(CliTestBase):
    def _file(self, content: bytes) -> str:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".fmt")
        self.addCleanup(os.unlink, f.name)
        f.write(content)
        f.close()
        return f.name

    def test_fs_missing_file(self):
        code, _out, err = self.run_cli(
            "-fs", "/nonexistent-format-file", "-id", "6", "WM_CLASS",
            backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: unable to open file "
                              "/nonexistent-format-file for reading.\n")

    def test_fs_mapping_applies(self):
        path = self._file(b"WM_CLASS 8s ' inst=$0\\n'\n")
        code, out, _err = self.run_cli("-fs", path, "-id", "6", "WM_CLASS",
                                       backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) inst="footw"\n')

    def test_fs_default_dformat_and_line_continuation(self):
        # no quoted dformat -> the default " = $0+\n"; backslash-newline
        # inside quotes is a line continuation
        path = self._file(b"WM_CLASS 8s\nWM_NAME 8s ' a\\\nb\\n'\n")
        code, out, _err = self.run_cli("-fs", path, "-id", "6", "WM_CLASS",
                                       "WM_NAME", backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n'
                              b'WM_NAME(STRING) ab\n')

    def test_fs_bad_file(self):
        path = self._file(b"JUSTONE")
        code, _out, err = self.run_cli("-fs", path, "-id", "6",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Bad format file format.\n")

    def test_fs_unterminated_quote(self):
        path = self._file(b"A 8s 'oops")
        code, _out, err = self.run_cli("-fs", path, "-id", "6",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Bad format file: "
                              "Unexpected EOF.\n")

    def test_xpropformats_env(self):
        path = self._file(b"WM_CLASS 8s ' env=$0\\n'\n")
        with mock.patch.dict(os.environ, {"XPROPFORMATS": path}):
            code, out, _err = self.run_cli("-id", "6", "WM_CLASS",
                                          backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) env="footw"\n')


class HelperTest(CliTestBase):
    def test_atoi(self):
        self.assertEqual(cli._atoi("12"), 12)
        self.assertEqual(cli._atoi("  -5x"), -5)
        self.assertEqual(cli._atoi("abc"), 0)
        self.assertEqual(cli._atoi(""), 0)

    def test_strtoul(self):
        self.assertEqual(cli._strtoul("42"), 42)
        self.assertEqual(cli._strtoul("0x10"), 16)
        self.assertEqual(cli._strtoul("010"), 8)
        self.assertEqual(cli._strtoul("089"), 0)   # octal stops at 8
        self.assertEqual(cli._strtoul("abc"), 0)
        self.assertEqual(cli._strtoul("-1"), (1 << 64) - 1)

    def test_strtoul_saturates_on_overflow(self):
        # C strtoul returns ULONG_MAX (ERANGE) on overflow, NOT a mod-2^64
        # wrap. Verified: `-set 32c 18446744073709551617` stores 0xffffffff
        # via the oracle (ULONG_MAX low 32 bits), where a wrap would give 1.
        self.assertEqual(cli._strtoul("18446744073709551617"),
                         (1 << 64) - 1)
        self.assertEqual(cli._pack_ints([cli._strtoul(
            "18446744073709551617")], 32), struct.pack("<I", 0xFFFFFFFF))
        # a huge hex magnitude saturates too
        self.assertEqual(cli._strtoul("0x1" + "0" * 20), (1 << 64) - 1)
        # overflowing NEGATIVE wraps small (glibc: -ULONG_MAX == 1)
        self.assertEqual(cli._strtoul("-18446744073709551617"), 1)

    def test_len_atoi_jank(self):
        # -len abc -> atoi 0 -> everything truncates to no fields
        code, out, _err = self.run_cli("-len", "abc", "-id", "6",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"WM_CLASS(STRING) = \n")

    def test_lone_dash_stops_selection_scan(self):
        # dsimple's scans stop at "-"; the option loop then chokes on -root
        code, _out, err = self.run_cli("-", "-root", backend="fake")
        self.assertEqual(code, 1)
        self.assertIn("xprop: unrecognized argument -root\n\n", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
