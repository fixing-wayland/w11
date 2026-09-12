#!/usr/bin/env python3
"""wwmctl CLI + core unit tests (no compositor needed).

Byte-parity checks follow wmctrl 1.07 main.c and the reference dumps; when
the real wmctrl binary is on PATH (nix develop) the help text is compared
byte-for-byte against `wmctrl --help`.

Documented deviations from wmctrl 1.07 (deliberate, see wwmctl/cli.py):
- -h/-V/--help/--version need no session (wmctrl needs an X display),
- backend-detection failure prints the detector's error, not
  "Cannot open display.",
- acting on a nonexistent window id exits 1 silently (wmctrl fires the
  ClientMessage into the void and exits 0)."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from w11common.errors import CmdError
from wdotool.backend import Window
from wdotool.backend_cosmic import CosmicBackend
from wdotool.backend_sway import SwayBackend
from wdotool.backend_wlr import WlrBackend
from wwmctl import cli, core
from wwmctl.cli import WMCTRL_VERSION

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class FakeSwayBackend:
    # sway's IPC has no picker: its select_window() waits for a focus change,
    # and its hint says so (see wdotool.backend.WindowBackend).
    select_window_hint = "focus the target window to select it"

    """Sway-shaped backend: offers _nodes() (raw tree view) and _msg()."""

    name = "sway"

    # the real backend's mapping, over this fake's raw GET_WORKSPACES rows
    workspaces = SwayBackend.workspaces

    def __init__(self, specs, workspaces=None, current=0):
        self.specs = specs
        self.calls = []
        self.ws_rows = workspaces or [
            {"num": 1, "name": "1", "focused": True,
             "rect": {"x": 0, "y": 0, "width": 1280, "height": 720}},
        ]
        self._cur = current
        self._select = None

    def _nodes(self):
        out = []
        for s in self.specs:
            node = {
                "id": s["node"],
                "name": s.get("title"),
                "app_id": s.get("app_id"),
                "window": s.get("xid"),
                "window_properties": s.get("wp"),
                "rect": {"x": s.get("x", 0), "y": s.get("y", 0),
                         "width": s.get("w", 0), "height": s.get("h", 0)},
            }
            win = Window(
                id=s["node"], title=s.get("title") or "",
                class_=s.get("app_id") or (s.get("wp") or {}).get("class", ""),
                pid=s.get("pid", 0), x=s.get("x", 0), y=s.get("y", 0),
                w=s.get("w", 0), h=s.get("h", 0),
                focused=s.get("focused", False),
                visible=s.get("visible", True),
                desktop=s.get("desktop", 0),
            )
            out.append((node, win, s.get("floating", False),
                        s.get("ws", "1")))
        return out

    def _msg(self, mtype):
        assert mtype == 1  # GET_WORKSPACES
        return self.ws_rows

    def move_to_current_desktop(self, wid):
        # no run(): like every backend but sway this says "move it by
        # number", and the caller falls back to get_desktop()
        return False

    def display_size(self):
        return (1280, 720)

    def _spec(self, wid):
        for s in self.specs:
            if s["node"] == wid:
                return s
        raise CmdError("window %d not found" % wid)

    def activate(self, wid):
        self._spec(wid)
        self.calls.append(("activate", wid))

    def close(self, wid):
        self._spec(wid)
        self.calls.append(("close", wid))

    def get_desktop(self):
        return self._cur

    def set_desktop(self, n):
        self.calls.append(("set_desktop", n))

    def num_desktops(self):
        return len(self.ws_rows)

    def set_window_desktop(self, wid, n):
        self._spec(wid)
        self.calls.append(("set_window_desktop", wid, n))

    def move_window(self, wid, x, y):
        if not self._spec(wid).get("floating"):
            raise CmdError(
                "sway: cannot move a tiled window to an absolute position "
                "(floating enable it first)")
        self.calls.append(("move", wid, x, y))

    def resize(self, wid, w, h):
        self._spec(wid)
        self.calls.append(("resize", wid, w, h))

    # a state this backend takes and quietly does not apply (KWin's shape)
    ignores = ()

    def set_state(self, wid, state, action):
        self._spec(wid)
        if state not in ("FULLSCREEN", "STICKY", "DEMANDS_ATTENTION",
                         "HIDDEN"):
            # the real backend's sentence cut to its first clause. Nothing in this file reads the route
            # half, so carrying it here would only be a second copy to keep in step; the one byte pin for
            # the whole sentence is tests/test_windows_i3.py's
            # `test_a_state_neither_dialect_has_says_what_would_close_it`, over the real backend.
            raise CmdError(
                "windowstate %s is not supported by the sway backend: sway has no such window "
                "state" % state)
        self.calls.append(("set_state", wid, state, action))
        if state in self.ignores:
            return ("windowstate %s: the compositor did not apply it to "
                    "window %d" % (state, wid))
        return None

    def select_window(self):
        return self._select

    def minimize(self, wid):
        self._spec(wid)
        self.calls.append(("minimize", wid))

    def lower(self, wid):
        self._spec(wid)
        self.calls.append(("lower", wid))


class FakeX11:
    """Implements the parts of the frozen x11_mini API that core uses."""

    def __init__(self, machines=None, wm_name="wlroots wm"):
        self.machines = machines or {}
        self.wm_name = wm_name
        self.calls = []
        self.atoms = {}
        self.states = {}          # win -> {atom} (_NET_WM_STATE)
        self.wm_honours_state = True

    def root(self):
        return 1

    def get_wm_class(self, win):
        return ("xterm", "XTerm")

    def get_client_machine(self, win):
        return self.machines.get(win, "xhost")

    def get_geometry(self, win):
        return (7, 8, 111, 222)

    def get_pid(self, win):
        return 999 if win != 99 else 0

    def get_prop_ints(self, win, name):
        if name == "_NET_SUPPORTING_WM_CHECK":
            return [99]
        if name == "_NET_WM_STATE":
            return sorted(self.states.get(win, ()))
        return []

    def get_prop_string(self, win, name):
        if (win, name) == (99, "_NET_WM_NAME"):
            return self.wm_name
        return ""

    def set_name(self, win, name, icon, long_, utf8=False):
        self.calls.append(("set_name", win, name, icon, long_, utf8))

    def atom(self, name, only_if_exists=False):
        return self.atoms.setdefault(name, 0x180 + len(self.atoms))

    def send_root_message(self, win, type_name, data):
        self.calls.append(("client_message", win, type_name, tuple(data)))
        if type_name == "_NET_WM_STATE" and self.wm_honours_state:
            # what a full EWMH window manager does with the message -- and
            # what the caller now reads back to tell "sent" from "applied"
            action, atom = data[0], data[1]
            have = self.states.setdefault(win, set())
            if action == 1 or (action == 2 and atom not in have):
                have.add(atom)
            else:
                have.discard(atom)


# standard fixture: one XWayland window, one native, one bare/N-A-ish
SPECS = [
    dict(node=5, xid=0x40000C, title="Mail inbox", pid=111,
         wp={"class": "XTerm", "instance": "xterm"},
         x=0, y=0, w=640, h=720, desktop=0),
    dict(node=6, app_id="footw", title="FootWin", pid=222,
         x=640, y=0, w=640, h=720, desktop=0, focused=True),
    dict(node=7, title=None, pid=0, x=-5, y=2, w=10, h=20, desktop=-1,
         ws="__i3_scratch"),
]


def run(argv, backend=None, x11=None, argv0="wmctrl", env=None):
    # WWMCTL_WMCTRL_GENERATION is forced to "1.07" unless the caller asked
    # for something else: without it `cli.help_text()` runs `_oracle_generation()`,
    # which shells out to whatever wmctrl is installed, and six of the tests below
    # compare against `cli.HELP`. Ubuntu 26.04 ships 1.07+git20240228 (its
    # --help carries "\n  -j "), so those six went red on this host and green
    # inside `nix develop`, where wmctrl is the 1.07 the clone is written against.
    # test_help_generation_follows_the_oracle passes "git"/"1.07" itself.
    env = dict(env or {})
    env.setdefault("WWMCTL_WMCTRL_GENERATION", "1.07")
    backend = backend if backend is not None else FakeSwayBackend(
        [dict(s) for s in SPECS])
    old_detect, old_x11 = core._detect_backend, core._x11_connect
    old_host, old_argv = core.hostname, sys.argv
    old_env = {}
    core._detect_backend = lambda: backend
    core._x11_connect = lambda: x11
    core.hostname = lambda: "testhost"
    sys.argv = [argv0]
    for k, v in env.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
    finally:
        core._detect_backend, core._x11_connect = old_detect, old_x11
        core.hostname, sys.argv = old_host, old_argv
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, out.getvalue(), err.getvalue(), backend


@contextlib.contextmanager
def _no_forced_generation():
    """Drop $WWMCTL_WMCTRL_GENERATION for the block: the one place that wants
    to see what `_oracle_generation()` makes of the wmctrl actually installed."""
    old = os.environ.pop("WWMCTL_WMCTRL_GENERATION", None)
    try:
        yield
    finally:
        if old is not None:
            os.environ["WWMCTL_WMCTRL_GENERATION"] = old


class UsageTest(unittest.TestCase):
    def test_help_structure(self):
        self.assertEqual(len(cli.HELP.encode()), 6801)
        self.assertTrue(cli.HELP.startswith(
            "wmctrl 1.07\nUsage: wmctrl [OPTION]...\nActions:\n"))
        self.assertTrue(cli.HELP.endswith("Copyright (C) 2003\n"))

    @unittest.skipUnless(shutil.which("wmctrl"), "real wmctrl not on PATH")
    def test_help_byte_parity_with_oracle(self):
        """`cli.help_text()` -- not `cli.HELP` -- is what the tool prints, and it
        follows the installed oracle. Ubuntu 26.04's wmctrl 1.07+git20240228 emits
        7179 bytes carrying "\n  -j "; the 1.07 in `nix develop` emits 6801 and
        does not. Branch the same way `_oracle_generation()` does, so this is a
        byte comparison on both boxes instead of a red test on one of them."""
        with _no_forced_generation():
            cli._GENERATION = None
            try:
                p = subprocess.run(["wmctrl", "--help"], capture_output=True,
                                   timeout=10)
                want = cli.HELP_GIT if b"\n  -j " in p.stdout else cli.HELP
                self.assertEqual(want.encode(), p.stdout)
                self.assertEqual(want, cli.help_text())
            finally:
                cli._GENERATION = None

    def test_no_args_help_to_stderr(self):
        rc, out, err, _b = run([])
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, cli.HELP)

    def test_positional_only_is_missing_option(self):
        rc, out, err, _b = run(["hello"])
        self.assertEqual((rc, out, err), (1, "", cli.HELP))

    def test_h_and_long_help(self):
        for argv in (["-h"], ["--help"]):
            rc, out, err, _b = run(argv)
            self.assertEqual((rc, out, err), (0, cli.HELP, ""))

    def test_version(self):
        # byte parity with the oracle: exactly "1.07", not an identity string
        for argv in (["-V"], ["--version"]):
            rc, out, _e, _b = run(argv)
            self.assertEqual((rc, out), (0, WMCTRL_VERSION + "\n"))
            self.assertEqual(out, "1.07\n")

    def test_invalid_option(self):
        rc, out, err, _b = run(["-q"])
        self.assertEqual((rc, out, err),
                         (1, "", "wmctrl: invalid option -- 'q'\n"))

    def test_missing_argument(self):
        rc, _o, err, _b = run(["-a"])
        self.assertEqual((rc, err),
                         (1, "wmctrl: option requires an argument -- 'a'\n"))

    def test_unknown_long_option(self):
        # plain getopt in the oracle: "--anything" is unknown option '-'
        rc, _o, err, _b = run(["--frob", "x"])
        self.assertEqual((rc, err),
                         (1, "wmctrl: invalid option -- '-'\n"))

    def test_help_generation_follows_the_oracle(self):
        """wwmctl-5: two oracle generations are in the field and both say
        "1.07" for -V. The newer one (1.07+git20240228 plus the distro's
        own -M/-L patches, Ubuntu 25.04+) documents eight more options and
        `-k toggle`; --help follows whichever wmctrl is installed here."""
        self.assertTrue(cli.HELP_GIT.startswith(
            "wmctrl 1.07\nUsage: wmctrl [OPTION]...\nActions:\n"))
        self.assertTrue(cli.HELP_GIT.endswith("Copyright (C) 2003\n"))
        self.assertEqual(len(cli.HELP_GIT.encode()), 7179)
        for opt in ("  -j  ", "  -S  ", "  -Y <WIN>", "  -r <WIN> -y <MVARG>",
                    "  -r <WIN> -M <PATH>", "  -r <WIN> -L",
                    "  -k (on|off|toggle)"):
            self.assertIn(opt, cli.HELP_GIT)
            self.assertNotIn(opt, cli.HELP)
        for argv in (["-h"], ["--help"]):
            rc, out, err, _b = run(argv, env={"WWMCTL_WMCTRL_GENERATION":
                                              "git"})
            self.assertEqual((rc, out, err), (0, cli.HELP_GIT, ""))
            rc, out, err, _b = run(argv, env={"WWMCTL_WMCTRL_GENERATION":
                                              "1.07"})
            self.assertEqual((rc, out, err), (0, cli.HELP, ""))

    def test_unknown_workaround(self):
        rc, _o, err, _b = run(["-w", "NOPE"])
        self.assertEqual((rc, err), (1, "Unknown workaround: NOPE\n"))
        rc, _o, err, b = run(["-w", "DESKTOP_TITLES_INVALID_UTF8"])
        self.assertEqual((rc, err), (0, ""))  # accepted, nothing to do

    def test_options_but_no_action(self):
        rc, out, err, b = run(["-p"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(b.calls, [])

    def test_envir_utf8(self):
        rc, _o, err, _b = run(["-v", "-p"], env={"LC_ALL": "C.UTF-8"})
        self.assertIn("envir_utf8: 1\n", err)
        rc, _o, err, _b = run(["-v", "-p"], env={"LC_ALL": "C",
                                                 "LC_CTYPE": "C",
                                                 "LANG": "C"})
        self.assertIn("envir_utf8: 0\n", err)
        rc, _o, err, _b = run(["-v", "-u", "-p"], env={"LC_ALL": "C",
                                                       "LC_CTYPE": "C",
                                                       "LANG": "C"})
        self.assertIn("envir_utf8: 1\n", err)


class ListTest(unittest.TestCase):
    def test_l_plain(self):
        rc, out, _e, _b = run(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 testhost Mail inbox",
            "0x00000006  0 testhost FootWin",
            "0x00000007 -1 testhost N/A",
        ])

    def test_lp(self):
        rc, out, _e, _b = run(["-lp"])
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 111    testhost Mail inbox",
            "0x00000006  0 222    testhost FootWin",
            "0x00000007 -1 0      testhost N/A",
        ])

    def test_lG(self):
        """The `-G` column is the ORIGINAL's arithmetic: `2x, 2y`, and the size untouched.

        wmctrl 1.07 feeds the window's own x,y into `XTranslateCoordinates` instead of 0,0, so the origin
        is counted twice on any window manager that does not reparent -- every wlroots xwm, and the shadow
        plane xw11 publishes. Measured 2026-09-12 on a headless labwc 0.9.3 on this guest, one xterm at
        398,215 484x316: pinned wmctrl 1.07 and Ubuntu's both printed `796 430 484 316`, through the proxy
        it was `796 430 484 316` again, and the pinned xdotool and xwininfo both read `398,215`
        [core.Core._geometry_column, goal2/requests-batch-12.md 2]. So the fixture's second window at
        640,0 prints 1280, and the third at -5,2 prints -10,4."""
        rc, out, _e, _b = run(["-lG"])
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 0    0    640  720  testhost Mail inbox",
            "0x00000006  0 1280 0    640  720  testhost FootWin",
            "0x00000007 -1 -10  4    10   20   testhost N/A",
        ])

    def test_lG_with_true_geometry_prints_the_rectangle_xwininfo_agrees_with(self):
        """The flag wmctrl never had, named the way `wxrandr --persistent` is. Same three windows, same
        sizes, the origins un-doubled -- 640,0 and -5,2, which is where the compositor really put them."""
        rc, out, _e, _b = run(["--true-geometry", "-lG"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 0    0    640  720  testhost Mail inbox",
            "0x00000006  0 640  0    640  720  testhost FootWin",
            "0x00000007 -1 -5   2    10   20   testhost N/A",
        ])

    def test_true_geometry_changes_nothing_but_the_two_origin_columns(self):
        """The size columns come straight off `XGetGeometry` in the original and are not doubled there, so
        they must not move here either -- and neither must the id, desktop, pid, class or title columns."""
        _rc, doubled, _e, _b = run(["-lGpx"])
        _rc, true_, _e, _b = run(["--true-geometry", "-lGpx"])
        for a, b in zip(doubled.splitlines(), true_.splitlines()):
            self.assertEqual(a.split()[:3], b.split()[:3], "id, desktop and pid")
            self.assertEqual(a.split()[5:], b.split()[5:], "width, height, class, machine, title")

    def test_L_on_one_window_doubles_the_same_way(self):
        """`-r <WIN> -L -G` prints one row through the very same builder, so the original's arithmetic has
        to reach it too: a script that reads `-lG` and one that reads `-L -G` must see the same numbers."""
        rc, out, _e, _b = run(["-i", "-r", "6", "-L", "-G"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["0x00000006  0 1280 0    640  720  testhost FootWin"])
        rc, out, _e, _b = run(["--true-geometry", "-i", "-r", "6", "-L", "-G"])
        self.assertEqual(out.splitlines(), ["0x00000006  0 640  0    640  720  testhost FootWin"])

    def test_an_x_row_whose_compositor_gives_no_workspace_prints_0_like_the_original(self):
        """wmctrl prints 0, never -1, for a window carrying neither `_NET_WM_DESKTOP` nor
        `_WIN_WORKSPACE` -- measured on Xvfb :79 with both properties removed by `xprop -remove`, and on
        the `resolute-cinnamon` golden where muffin's own X11 path publishes neither on `nemo-desktop`
        and real `wmctrl -l` printed `0x02a00030  0 ... Desktop` (2026-09-12). Our backends say -1 for
        "on all workspaces, or no workspace object", and printing that was the one red proxy check on
        resolute-cinnamon-wayland: six sticky background X windows, `-1` from us and `0` from the
        original [core.Core._desktop_column]."""
        specs = [dict(SPECS[0], desktop=-1)]
        rc, out, _e, _b = run(["-l"], backend=FakeSwayBackend(specs))
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["0x0040000c  0 testhost Mail inbox"])

    def test_a_native_row_keeps_the_minus_one_the_shadow_publishes(self):
        """The other half, and why the 0 is not a blanket. The only original that can see a native window
        reads it through the proxy, whose shadow publishes `_NET_WM_DESKTOP = 0xFFFFFFFF` for a row with
        no desktop (`xw11/shadow.py`) -- and wmctrl prints that as -1. Measured on a headless labwc on
        this guest, 2026-09-12: `wmctrl -l` through xw11 printed `0x00600001 -1 ... gfoot` for the very
        foot this column prints -1 for, while the XWayland xterm was `0` on both sides."""
        rc, out, _e, _b = run(["-l"])
        self.assertEqual(out.splitlines()[2], "0x00000007 -1 testhost N/A")

    def test_a_real_workspace_number_is_printed_as_it_is(self):
        """The guard: only a NEGATIVE desktop moves. A window muffin really put on desktop 2 prints 2 --
        which is what `wmctrl -t 2` then `wmctrl -l` printed for the xterm on the Cinnamon X11 golden."""
        specs = [dict(SPECS[0], desktop=2)]
        rc, out, _e, _b = run(["-l"], backend=FakeSwayBackend(specs))
        self.assertEqual(out.splitlines(), ["0x0040000c  2 testhost Mail inbox"])

    def test_lx(self):
        rc, out, _e, _b = run(["-lx"])
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 xterm.XTerm           testhost Mail inbox",
            "0x00000006  0 footw.footw           testhost FootWin",
            "0x00000007 -1 N/A                   testhost N/A",
        ])

    def test_lGpx_combo(self):
        rc, out, _e, _b = run(["-lGpx"])
        self.assertEqual(out.splitlines()[0],
                         "0x0040000c  0 111    0    0    640  720  "
                         "xterm.XTerm           testhost Mail inbox")

    def test_l_x_enrichment_and_machine_column_width(self):
        # X plane fills machine/class/geometry for the XWayland row; the
        # machine column is right-aligned to the LONGEST hostname in the
        # list. wmctrl 1.07 uses the last row's width instead -- a main.c
        # bug that is invisible on its creation-ordered list and would
        # re-flow ours (stacking order) on every raise.
        # The origins are the original's doubled ones (test_lG's paragraph): the X plane puts this window
        # at 7,8, so the column says 14,16.
        x11 = FakeX11(machines={0x40000C: "longmachine.example"})
        rc, out, _e, _b = run(["-lG"], x11=x11)
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 14   16   111  222  longmachine.example "
            "Mail inbox",
            "0x00000006  0 1280 0    640  720             testhost FootWin",
            "0x00000007 -1 -10  4    10   20              testhost N/A",
        ])

    def test_l_generic_backend(self):
        # non-sway backends have no _nodes(): everything is native-style
        class GenericBackend:
            name = "wlr"

            def list(self):
                return [Window(id=1000001, title="T", class_="app",
                               pid=3, x=1, y=2, w=3, h=4, desktop=-1)]

        rc, out, _e, _b = run(["-lx"], backend=GenericBackend())
        self.assertEqual(out.splitlines(), [
            "0x000f4241 -1 app.app               testhost T",
        ])


class DesktopTest(unittest.TestCase):
    def _backend(self):
        return FakeSwayBackend(
            [dict(s) for s in SPECS],
            workspaces=[
                {"num": 1, "name": "1", "focused": False,
                 "rect": {"x": 0, "y": 0, "width": 1280, "height": 690}},
                {"num": 2, "name": "web", "focused": True,
                 "rect": {"x": 0, "y": 0, "width": 1280, "height": 720}},
            ])

    def test_d_format(self):
        # VP is `0,0` on EVERY row, which is what real wmctrl prints against a
        # WM that publishes `_NET_DESKTOP_VIEWPORT` as one pair per desktop --
        # and what it prints through xw11, whose root now publishes exactly
        # that [M goal2/recon/gaps.md 3a, Xvfb :77, 2026-09-11]. It used to be
        # `N/A` on the non-current rows, which no X WM with viewports prints.
        rc, out, _e, _b = run(["-d"], backend=self._backend())
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0  - DG: 1280x720  VP: 0,0  WA: 0,0 1280x690  1",
            "1  * DG: 1280x720  VP: 0,0  WA: 0,0 1280x720  web",
        ])

    def test_d_viewport_is_the_origin_on_every_row_not_only_the_current_one(self):
        """The clone reads no X property here (no X plane in this rig), and the
        column is still the one real `wmctrl -d` prints through the proxy: the
        proxy publishes `[0, 0]` per desktop (xw11/shadow.py:root_props), so
        `VP: N/A` on a non-current row would be the clone alone inventing an
        absence. Asserted per row rather than on the whole line so that a WA or
        a name change cannot make this one pass by accident."""
        rc, out, _e, _b = run(["-d"], backend=self._backend())
        self.assertEqual(rc, 0)
        rows = out.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertEqual([ln.split("VP: ")[1].split()[0] for ln in rows],
                         ["0,0", "0,0"])

    def test_s(self):
        rc, _o, _e, b = run(["-s", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(b.calls, [("set_desktop", 1)])

    def test_s_invalid(self):
        rc, _o, err, b = run(["-s", "-1"])
        self.assertEqual((rc, err), (1, "Invalid desktop ID.\n"))
        self.assertEqual(b.calls, [])

    def test_s_atoi_garbage_is_desktop_zero(self):
        rc, _o, _e, b = run(["-s", "junk"])  # C atoi() -> 0
        self.assertEqual((rc, b.calls), (0, [("set_desktop", 0)]))

    def test_s_unicode_digits_are_not_digits(self):
        """atoi() is ASCII. Arabic-Indic "42" is junk to wmctrl, so it is
        junk here: desktop 0, not desktop 42."""
        rc, _o, _e, b = run(["-s", "\u0664\u0662"])
        self.assertEqual((rc, b.calls), (0, [("set_desktop", 0)]))


class WlrFloorBackend(FakeSwayBackend):
    """A listing backend wearing the wlr floor's identity.

    `name` and `wm_name` are read off the product class, not spelled again here, so this says something
    about `WlrBackend` rather than about itself."""

    name = WlrBackend.name
    wm_name = WlrBackend.wm_name


class CosmicFloorBackend(FakeSwayBackend):
    name = CosmicBackend.name
    wm_name = CosmicBackend.wm_name


class FramingXwmBackend(FakeSwayBackend):
    """The same three rows under a backend whose Xwayland window manager FRAMES its windows.

    Only `name` differs from `FakeSwayBackend`, and that is the whole point: the two quirks below are
    keyed on `core.NON_REPARENTING_XWM` / `core.NO_NET_WM_DESKTOP_XWM`, and `gnome` is in neither.
    Measured on `noble-gnome` (mutter 46.2, GNOME Wayland, 2026-09-12): an xterm at absolute 398,252 sat
    under `Parent window id: 0xa00004` against a root of `0x221`, both originals printed `412 301`
    (`absolute + parent-relative`, not `796 504`), and after `wmctrl -b add,sticky` it carried
    `_NET_WM_DESKTOP(CARDINAL) = 4294967295` and `wmctrl -l` printed `-1`
    [M goal2/recon/b17-review-measurements.md 1]."""

    name = "gnome"


class TheOriginalsQuirksAreConfinedToWhereTheyWereMeasured(unittest.TestCase):
    """Both of batch 17's parity quirks against a framing xwm, which is what mutter is.

    A blanket rule would have regressed the 13 GNOME flavors in opposite directions: `2x,2y` where the
    original prints `absolute + parent-relative` (398,252 -> 796,504 against the measured 412,301), and
    `0` where the original prints `-1` off a real `_NET_WM_DESKTOP = 0xFFFFFFFF`. Each test below is
    paired with the same row on a non-reparenting xwm, so neither is a tautology of its double."""

    def placed(self):
        """SPECS with the X row where the labwc measurement put its xterm: 398,215 484x316."""
        rows = [dict(s) for s in SPECS]
        rows[0].update(x=398, y=215, w=484, h=316)
        return rows

    def test_an_x_row_on_a_framing_xwm_prints_the_absolute_origin(self):
        """mutter's Xwayland windows are reparented, so the original's doubling collapses to the frame
        offset there and this column must not double. 398,215 stays 398,215."""
        rc, out, _e, _b = run(["-lG"], backend=FramingXwmBackend(self.placed()))
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0].split()[2:6], ["398", "215", "484", "316"])

    def test_the_same_x_row_doubles_on_a_non_reparenting_one(self):
        """The contrast, off the same fixture: on sway's xwm (parent == root, measured) the original
        printed `796 430 484 316` for exactly this rectangle and the clone owes it."""
        rc, out, _e, _b = run(["-lG"], backend=FakeSwayBackend(self.placed()))
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0].split()[2:6], ["796", "430", "484", "316"])

    def test_a_native_row_doubles_on_the_framing_xwm_too(self):
        """The native half does not turn off with the X half: the only original that can see a native
        window reads it through xw11, whose shadows are root children on every compositor. The FootWin
        row is at 640,0 and prints 1280 under both backends."""
        rc, out, _e, _b = run(["-lG"], backend=FramingXwmBackend(self.placed()))
        self.assertEqual(out.splitlines()[1].split()[2:4], ["1280", "0"])
        rc, out, _e, _b = run(["--true-geometry", "-lG"],
                              backend=FramingXwmBackend(self.placed()))
        self.assertEqual(out.splitlines()[1].split()[2:4], ["640", "0"],
                         "the flag is not a no-op on a framing xwm")

    def test_a_sticky_x_row_on_a_framing_xwm_keeps_the_minus_one_mutter_publishes(self):
        """mutter writes `_NET_WM_DESKTOP = 0xFFFFFFFF` for a window on all workspaces and wmctrl prints
        that as -1, which is what the gnome backend's own -1 already produced."""
        rc, out, _e, _b = run(["-l"], backend=FramingXwmBackend([dict(SPECS[0], desktop=-1)]))
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["0x0040000c -1 testhost Mail inbox"])

    def test_the_same_row_prints_0_where_the_xwm_publishes_no_property(self):
        """The contrast: the wlroots xwm never interns `_NET_WM_DESKTOP` (`no such atom on any window`
        on a headless sway 1.11 and labwc 0.9.3, 2026-09-12) and muffin publishes none either, so the
        original falls through to 0 on those."""
        rc, out, _e, _b = run(["-l"], backend=FakeSwayBackend([dict(SPECS[0], desktop=-1)]))
        self.assertEqual(out.splitlines(), ["0x0040000c  0 testhost Mail inbox"])


class WmInfoTest(unittest.TestCase):
    def test_m_on_the_wlr_backend_names_the_window_manager(self):
        """U31. With no X plane -- a root shell, or a session with no Xwayland -- `wm_info` falls back to
        the backend, and the wlr floor used to answer with its own token, `Name: wlr`. Every wlroots xwm
        publishes `wlroots wm` on its check window, byte-identical on labwc, Wayfire, river and sway
        [M recon2/labwc.md §3], so the in-session and out-of-session answers now agree."""
        rc, out, _e, _b = run(["-m"], backend=WlrFloorBackend([dict(s) for s in SPECS]))
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "Name: wlroots wm")

    def test_m_on_the_cosmic_backend_names_smithays_xwm(self):
        """`wwmctl -m` with cosmic-comp's Xwayland reachable printed `Name: Smithay X WM` off the check
        window [M recon2/cosmic.md §3]; the no-X-plane answer is now the same string."""
        rc, out, _e, _b = run(["-m"], backend=CosmicFloorBackend([dict(s) for s in SPECS]))
        self.assertEqual((rc, out.splitlines()[0]), (0, "Name: Smithay X WM"))

    def test_m_with_x_plane(self):
        rc, out, _e, _b = run(["-m"], x11=FakeX11())
        self.assertEqual(out, "Name: wlroots wm\n"
                              "Class: N/A\n"
                              "PID: N/A\n"
                              'Window manager\'s "showing the desktop" '
                              "mode: N/A\n")

    def test_m_compositor_only(self):
        rc, out, _e, _b = run(["-m"])
        self.assertEqual(out.splitlines()[0], "Name: sway")
        self.assertEqual(rc, 0)


class SelectionTest(unittest.TestCase):
    def test_title_substring_case_insensitive(self):
        rc, _o, _e, b = run(["-a", "mail IN"])
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))

    def test_first_match_wins(self):
        specs = [dict(SPECS[0]), dict(SPECS[1])]
        specs[1]["title"] = "Mail inbox two"
        b = FakeSwayBackend(specs)
        rc, _o, _e, b = run(["-a", "mail"], backend=b)
        self.assertEqual(b.calls, [("activate", 5)])

    def test_F_exact_case_sensitive(self):
        rc, _o, _e, b = run(["-F", "-a", "mail inbox"])
        self.assertEqual((rc, b.calls), (1, []))
        rc, _o, _e, b = run(["-F", "-a", "Mail inbox"])
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))

    def test_x_matches_class(self):
        rc, _o, _e, b = run(["-x", "-a", "term.XT"])
        self.assertEqual(b.calls, [("activate", 5)])
        rc, _o, _e, b = run(["-x", "-F", "-a", "footw.footw"])
        self.assertEqual(b.calls, [("activate", 6)])

    def test_no_match_silent_exit_1(self):
        rc, out, err, b = run(["-a", "zzznope"])
        self.assertEqual((rc, out, err, b.calls), (1, "", "", []))

    def test_i_decimal_and_hex(self):
        rc, _o, _e, b = run(["-i", "-a", "6"])
        self.assertEqual(b.calls, [("activate", 6)])
        rc, _o, _e, b = run(["-i", "-a", "0x0040000c"])
        self.assertEqual(b.calls, [("activate", 5)])  # X id -> node 5
        rc, _o, _e, b = run(["-i", "-a", "4194316"])  # decimal X id
        self.assertEqual(b.calls, [("activate", 5)])

    def test_i_bad_number(self):
        rc, _o, err, _b = run(["-i", "-a", "nope"])
        self.assertEqual((rc, err), (1, "Cannot convert argument to "
                                        "number.\n"))

    def test_i_unknown_id_exits_1(self):
        """A title or class that matches nothing is wmctrl's silent exit 1.
        An -i id that names no window is not a search -- real wmctrl asks
        the X server and Xlib prints BadWindow -- so it says so."""
        rc, out, err, b = run(["-i", "-a", "0xdead"])
        self.assertEqual((rc, out, b.calls), (1, "", []))
        self.assertIn("no window with id 0x0000dead", err)

    def test_a_title_that_matches_nothing_is_still_silent(self):
        rc, out, err, b = run(["-a", "no such window anywhere"])
        self.assertEqual((rc, out, err, b.calls), (1, "", "", []))

    def test_active_magic(self):
        rc, _o, _e, b = run(["-a", ":ACTIVE:"])
        self.assertEqual(b.calls, [("activate", 6)])  # focused foot

    def test_select_magic(self):
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b._select = 5
        rc, _o, _e, b = run(["-a", ":SELECT:"], backend=b)
        self.assertEqual(b.calls, [("activate", 5)])

    def test_no_window_specified(self):
        rc, _o, err, _b = run(["-t", "1"])
        self.assertEqual((rc, err), (1, "No window was specified.\n"))

    def test_later_window_arg_wins(self):
        rc, _o, _e, b = run(["-r", "junk", "-a", "FootWin"])
        self.assertEqual(b.calls, [("activate", 6)])
        rc, _o, _e, b = run(["-a", "FootWin", "-r", "junk"])
        self.assertEqual((rc, b.calls), (1, []))

    def test_verbose_using_window(self):
        rc, _o, err, _b = run(["-v", "-i", "-a", "6"])
        self.assertIn("Using window: 0x00000006\n", err)


class ActionTest(unittest.TestCase):
    def test_close(self):
        rc, _o, _e, b = run(["-c", "FootWin"])
        self.assertEqual((rc, b.calls), (0, [("close", 6)]))

    def test_t_move_to_desktop(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "3"])
        self.assertEqual((rc, b.calls), (0, [("set_window_desktop", 6, 3)]))

    def test_t_minus_one_is_current(self):
        b = FakeSwayBackend([dict(s) for s in SPECS], current=0)
        b._cur = 4
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual(b.calls, [("set_window_desktop", 6, 4)])

    def test_t_atoi(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "2abc"])
        self.assertEqual(b.calls, [("set_window_desktop", 6, 2)])

    def test_R_current_desktop_then_activate(self):
        rc, _o, _e, b = run(["-R", "FootWin"])
        self.assertEqual((rc, b.calls),
                         (0, [("set_window_desktop", 6, 0),
                              ("activate", 6)]))


class MoveResizeTest(unittest.TestCase):
    def _floating(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        return FakeSwayBackend(specs)

    def test_e_full(self):
        rc, _o, err, b = run(["-r", "Mail", "-e", "0,10,20,300,200"],
                             backend=self._floating())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("resize", 5, 300, 200),
                                   ("move", 5, 10, 20)])

    def test_e_resize_only(self):
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0,-1,-1,300,200"],
                            backend=self._floating())
        self.assertEqual(b.calls, [("resize", 5, 300, 200)])

    def test_e_move_only_fills_current(self):
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0,10,-1,-1,-1"],
                            backend=self._floating())
        self.assertEqual(b.calls, [("move", 5, 10, 0)])  # y from rect

    def test_e_tiled_move_warns_but_succeeds(self):
        rc, _o, err, b = run(["-r", "Mail", "-e", "0,10,20,-1,-1"])
        self.assertEqual(rc, 0)
        self.assertIn("; ignoring", err)
        self.assertEqual(b.calls, [])

    def test_e_parse_error(self):
        rc, _o, err, b = run(["-r", "Mail", "-e", "1,2,3"])
        self.assertEqual((rc, err), (1, 'The -e option expects a list of '
                                        'comma separated integers: '
                                        '"gravity,X,Y,width,height"\n'))
        self.assertEqual(b.calls, [])

    def test_e_negative_gravity(self):
        rc, _o, err, _b = run(["-r", "Mail", "-e", "-1,2,3,4,5"])
        self.assertEqual((rc, err),
                         (1, "Value of gravity mustn't be negative. Use zero"
                             " to use the default gravity of the window.\n"))

    def test_e_uses_move_resize_when_the_backend_has_one(self):
        """KWin: a resize and a move a few ms apart race a Wayland client's
        configure ack, and the move re-requests the size it read back. A
        backend that can take both at once gets one call; sway, which
        cannot, keeps the two."""
        b = self._floating()

        def move_resize(wid, x, y, w, h):
            b.calls.append(("move_resize", wid, x, y, w, h))
        b.move_resize = move_resize
        rc, _o, err, b = run(["-r", "Mail", "-e", "0,10,20,300,200"],
                             backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("move_resize", 5, 10, 20, 300, 200)])

    def test_e_move_only_still_moves_with_move_resize(self):
        b = self._floating()
        b.move_resize = lambda *a: b.calls.append(("move_resize",) + a)
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0,10,-1,-1,-1"], backend=b)
        self.assertEqual(b.calls, [("move", 5, 10, 0)])

    def test_e_verbose_grflags(self):
        rc, _o, err, _b = run(["-v", "-r", "Mail", "-e", "5,10,-1,300,-1"],
                              backend=self._floating())
        # grav 5 | x(1<<8) | w(1<<10)
        self.assertIn("grflags: %d\n" % (5 | 256 | 1024), err)


class WindowStateTest(unittest.TestCase):
    def test_b_add_fullscreen(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "add,fullscreen"])
        self.assertEqual((rc, b.calls),
                         (0, [("set_state", 6, "FULLSCREEN", 1)]))

    def test_b_remove_and_toggle(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "remove,hidden"])
        self.assertEqual(b.calls, [("set_state", 6, "HIDDEN", 0)])
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "toggle,sticky"])
        self.assertEqual(b.calls, [("set_state", 6, "STICKY", 2)])

    def test_b_two_props(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "add,sticky,hidden"])
        self.assertEqual(b.calls, [("set_state", 6, "STICKY", 1),
                                   ("set_state", 6, "HIDDEN", 1)])

    def test_b_unsupported_warns_but_succeeds(self):
        rc, _o, err, b = run(
            ["-r", "FootWin", "-b", "add,maximized_vert,maximized_horz"])
        self.assertEqual(rc, 0)
        self.assertEqual(err.count("; ignoring"), 2)
        self.assertIn("MAXIMIZED_VERT", err)
        self.assertEqual(b.calls, [])

    def test_b_errors(self):
        argerr = ('The -b option expects a list of comma separated '
                  'parameters: "(remove|add|toggle),<PROP1>[,<PROP2>]"\n')
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "nocomma"])
        self.assertEqual((rc, err), (1, argerr))
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "frob,fullscreen"])
        self.assertEqual((rc, err),
                         (1, "Invalid action. Use either remove, add or "
                             "toggle.\n"))
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,fullscreen,"])
        self.assertEqual((rc, err), (1, "Invalid zero length property.\n"))
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,"])
        self.assertEqual((rc, err), (1, "Invalid zero length property.\n"))

    def test_b_verbose_state2_before_state1(self):
        rc, _o, err, _b = run(
            ["-v", "-r", "FootWin", "-b", "toggle,sticky,hidden"])
        lines = [ln for ln in err.splitlines() if ln.startswith("State")]
        self.assertEqual(lines, ["State 2: _NET_WM_STATE_HIDDEN",
                                 "State 1: _NET_WM_STATE_STICKY"])


class SetTitleTest(unittest.TestCase):
    def test_N_I_T_on_x_window(self):
        for mode, icon, long_ in (("N", False, True), ("I", True, False),
                                  ("T", True, True)):
            x11 = FakeX11()
            rc, _o, err, _b = run(["-r", "Mail", "-%s" % mode, "New"],
                                  x11=x11,
                                  env={"LC_ALL": "C", "LC_CTYPE": "C",
                                       "LANG": "C"})
            self.assertEqual((rc, err), (0, ""), mode)
            self.assertEqual(x11.calls,
                             [("set_name", 0x40000C, "New", icon, long_,
                               False)])

    def test_title_on_native_warns_but_succeeds(self):
        rc, _o, err, _b = run(["-r", "FootWin", "-N", "New"], x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_title_without_x_plane_warns(self):
        rc, _o, err, _b = run(["-r", "Mail", "-N", "New"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)


class UnnamedFlagsTest(unittest.TestCase):
    """What -S, -I and -y do beyond what GitGenerationOptionsTest and
    SetTitleTest already pin.

    The plan listed these three as never named, from a search for their
    literal spelling; all three were in fact covered, as "-lS", as
    `"-%s" % mode` in a N/I/T loop, and at test_y_is_e_then_activate. What
    was missing is the edge of each: -S with no listing to reorder, -I with
    no window, and -y whose move half is refused."""

    def test_S_alone_is_an_option_with_nothing_to_do(self):
        # like a bare `-p`: options, but no action -- rc 0 and silence
        self.assertEqual(run(["-S"])[:3], (0, "", ""))

    def test_S_survives_the_other_list_flags(self):
        self.assertEqual(run(["-lGpx"])[:3], run(["-lGpxS"])[:3])

    def test_I_needs_a_window(self):
        rc, _o, err, _b = run(["-I", "Short"])
        self.assertEqual((rc, err), (1, "No window was specified.\n"))

    def test_y_activates_even_when_the_move_is_refused(self):
        # -e on a tiled sway container is a soft refusal; 1.07+git's -y is
        # "-e, then activate", and the activate is not conditional on it.
        b = FakeSwayBackend([dict(s) for s in SPECS])
        rc, _o, err, b = run(["-r", "Mail", "-y", "0,10,20,300,200"], backend=b)
        self.assertEqual(rc, 0, err)
        self.assertIn(("activate", 5), b.calls)


class XpmTest(unittest.TestCase):
    """wwmctl-5: `-M <PATH>` turns an XPM into _NET_WM_ICON, the way the
    distro's wmctrl patch does — colours through the X server's own
    XParseColor, "None" and anything the server does not know as 0."""

    XPM = b'''/* XPM */
static char *icon[] = {
/* columns rows colors chars-per-pixel */
"2 2 3 1",
"  c None",
". c #FF0000",
"X c navy blue",
/* pixels */
". ",
"X.",
};
'''

    class _X:
        """The server only ever sees NAMES: XParseColor resolves the
        numeric forms in the client."""

        def lookup_color(self, name):
            table = {"navy blue": (0, 0, 0x8080)}
            if name not in table:
                raise ValueError("BadName")
            return table[name]

    def test_read_xpm(self):
        with tempfile.NamedTemporaryFile(suffix=".xpm", delete=False) as fh:
            fh.write(self.XPM)
            path = fh.name
        self.addCleanup(os.unlink, path)
        w, h, pixels = core._read_xpm(path, self._X())
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(pixels, [0xFFFF0000, 0,            # ". "
                                  0xFF000080, 0xFFFF0000])  # "X."

    def test_unknown_colour_is_transparent_like_the_oracle(self):
        with tempfile.NamedTemporaryFile(suffix=".xpm", delete=False) as fh:
            fh.write(self.XPM.replace(b"navy blue", b"zzznosuch"))
            path = fh.name
        self.addCleanup(os.unlink, path)
        _w, _h, pixels = core._read_xpm(path, self._X())
        self.assertEqual(pixels[2], 0)

    def test_numeric_colour_specs_are_parsed_in_the_client(self):
        for spec, want in (("#F00", (0xF000, 0, 0)),
                           ("#FF0000", (0xFF00, 0, 0)),
                           ("#FFF000000", (0xFFF0, 0, 0)),
                           ("#FFFF00000000", (0xFFFF, 0, 0)),
                           ("rgb:ff/0/8080", (0xFF00, 0, 0x8080))):
            self.assertEqual(core._parse_color(spec), want, spec)
        for spec in ("navy blue", "#FF00", "#ZZZZZZ", "rgb:1/2"):
            self.assertIsNone(core._parse_color(spec), spec)

    def test_a_file_that_is_not_an_xpm_raises(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"not an xpm at all")
            path = fh.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(Exception):
            core._read_xpm(path, self._X())


class PlaceAxisTest(unittest.TestCase):
    """core._place_axis, the -e gravity arithmetic, as a table. Frame edge
    100, frame size 640, asked for a 300-wide client in a 300-wide frame
    (zero extents) unless the row says otherwise."""

    F_OLD, FS_OLD, CS_NEW, FS_NEW = 100, 640, 300, 300

    def place(self, pos, req, keep_anchor=True, static=False, lead=0,
              fs_new=None):
        return core._place_axis(pos, static, req, lead, self.F_OLD,
                                self.FS_OLD, self.CS_NEW,
                                self.FS_NEW if fs_new is None else fs_new,
                                keep_anchor)

    def test_explicit_coordinate_places_the_gravity_point(self):
        for pos, want in ((0, 500), (1, 500), (2, 500)):
            self.assertEqual(self.place(pos, 500), want, pos)
        # a frame wider than the client shifts the leading edge back
        self.assertEqual(self.place(2, 500, fs_new=320), 480)
        self.assertEqual(self.place(1, 500, fs_new=320), 490)
        self.assertEqual(self.place(0, 500, fs_new=320), 500)

    def test_bare_resize_keeps_the_gravity_point(self):
        # both coordinates -1: the reference point does not move
        self.assertEqual(self.place(0, -1), 100)          # left edge
        self.assertEqual(self.place(1, -1), 270)          # centre 420
        self.assertEqual(self.place(2, -1), 440)          # right edge 740

    def test_a_kept_axis_next_to_a_given_one_keeps_its_edge(self):
        # wwmctl-4: Mutter's rule when the request carries a coordinate
        for pos in (0, 1, 2):
            self.assertEqual(self.place(pos, -1, keep_anchor=False), 100,
                             pos)

    def test_static_gravity_addresses_the_client(self):
        self.assertEqual(self.place(0, 500, static=True, lead=37), 463)
        self.assertEqual(self.place(2, 500, static=True, lead=37), 463)
        for keep in (True, False):
            self.assertEqual(
                self.place(2, -1, static=True, lead=37, keep_anchor=keep),
                100, keep)


class GitGenerationOptionsTest(unittest.TestCase):
    """wwmctl-5: the options wmctrl 1.07+git20240228 added. We accept them
    on every flavor -- rejecting `wmctrl -j` on 24.04 would only make a
    26.04 script fail later and more confusingly."""

    def test_j_prints_the_current_desktop(self):
        rc, out, err, _b = run(["-j"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "0 \n")   # printf("%-2d\n")
        b = FakeSwayBackend([dict(s) for s in SPECS], current=12)
        rc, out, _e, _b = run(["-j"], backend=b)
        self.assertEqual(out, "12\n")

    def test_S_is_accepted_and_our_list_is_already_stacking(self):
        rc, plain, err, _b = run(["-l"])
        self.assertEqual((rc, err), (0, ""))
        rc, stacked, err, _b = run(["-lS"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(plain, stacked)

    def test_Y_iconifies(self):
        rc, _o, err, b = run(["-Y", "Mail"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("minimize", 5)])

    def test_z_lowers(self):
        rc, _o, err, b = run(["-z", "Mail"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("lower", 5)])

    def test_E_prints_the_title(self):
        rc, out, err, _b = run(["-E", "Mail"])
        self.assertEqual((rc, out, err), (0, "Mail inbox\n", ""))
        # a titleless window prints an empty line, like the oracle's
        # get_window_title fallback
        rc, out, _e, _b = run(["-i", "-E", "7"])
        self.assertEqual((rc, out), (0, "\n"))

    def test_y_is_e_then_activate(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        b = FakeSwayBackend(specs)
        rc, _o, err, b = run(["-r", "Mail", "-y", "0,10,20,300,200"],
                             backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("resize", 5, 300, 200),
                                   ("move", 5, 10, 20),
                                   ("activate", 5)])

    def test_L_prints_this_window_s_list_row(self):
        rc, out, err, _b = run(["-r", "Mail", "-L"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "0x0040000c  0 testhost Mail inbox\n")
        # the -l flags shape the row, and the machine column is sized from
        # this window alone (display_window's max_client_machine_len == 0)
        rc, out, _e, _b = run(["-Gp", "-r", "Mail", "-L"], x11=None)
        self.assertEqual(out, "0x0040000c  0 111    0    0    640  720  "
                              "testhost Mail inbox\n")

    def test_M_needs_an_x_window_and_a_readable_xpm(self):
        rc, _o, err, _b = run(["-r", "FootWin", "-M", "/nope.xpm"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("native window", err)
        rc, _o, err, _b = run(["-r", "Mail", "-M", "/nope.xpm"], x11=None)
        self.assertEqual((rc, err), (0, "wwmctl: cannot reach the XWayland "
                                        "server to set the window icon; "
                                        "ignoring\n"))

    def test_unknown_ids_still_exit_1(self):
        for opt in ("-Y", "-z", "-E"):
            rc, out, err, b = run(["-i", opt, "0x999999"])
            self.assertEqual((rc, out), (1, ""))
            self.assertIn("no window with id 0x00999999", err)
            self.assertEqual(b.calls, [])


class DesktopOrderTest(unittest.TestCase):
    """sway-2: wwmctl -d printed sway's GET_WORKSPACES order, which is
    creation order. Real wmctrl -d is always ascending and positionally
    indexed -- "the third line is desktop 2" is what a caller reads."""

    class _Sway(FakeSwayBackend):
        rows = ()

        def _msg(self, _kind):
            return list(self.rows)

    def _rows(self, rows):
        b = self._Sway([dict(s) for s in SPECS])
        b.rows = rows
        rc, out, err, _b = run(["-d"], backend=b)
        self.assertEqual((rc, err), (0, ""))
        return [ln.split() for ln in out.splitlines()]

    def test_out_of_order_workspaces_are_sorted(self):
        rows = self._rows([
            {"num": 3, "name": "3", "focused": False, "rect": {}},
            {"num": 1, "name": "1", "focused": True, "rect": {}},
            {"num": 2, "name": "2", "focused": False, "rect": {}},
        ])
        self.assertEqual([r[0] for r in rows], ["0", "1", "2"])
        # the star stays on the focused one after the sort
        self.assertEqual([i for i, r in enumerate(rows) if r[1] == "*"], [0])

    def test_named_workspaces_sort_after_the_numbered_ones(self):
        rows = self._rows([
            {"num": -1, "name": "scratch", "focused": False, "rect": {}},
            {"num": 2, "name": "two", "focused": False, "rect": {}},
            {"num": 1, "name": "one", "focused": True, "rect": {}},
        ])
        self.assertEqual([r[0] for r in rows], ["0", "1", "-1"])
        self.assertEqual([i for i, r in enumerate(rows) if r[1] == "*"], [0])


class XStateFallbackTest(unittest.TestCase):
    """wwmctl-6: a state the compositor backend refuses is retried on the
    X plane for an XWayland window, which is where real wmctrl sends it."""

    def test_refused_state_is_retried_on_the_x_plane(self):
        x = FakeX11()
        rc, _o, err, b = run(["-r", "Mail", "-b", "add,below"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([c for c in x.calls if c[0] == "client_message"],
                         [("client_message", 0x40000C, "_NET_WM_STATE",
                           (1, x.atom("_NET_WM_STATE_BELOW"), 0, 0, 0))])
        self.assertEqual(b.calls, [])   # the backend refused it

    def test_without_an_x_plane_the_refusal_still_warns(self):
        rc, _o, err, _b = run(["-r", "Mail", "-b", "add,below"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_accepted_and_ignored_is_retried_on_the_x_plane(self):
        """kde-1: KWin takes a state and does nothing with it (a window rule,
        or size hints a fullscreen cannot satisfy) and the backend said so
        while returning success, so wwmctl never reached for the X route --
        which is the one real wmctrl uses and which works."""
        x = FakeX11()
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b.ignores = ("FULLSCREEN",)
        rc, _o, err, _b = run(["-r", "Mail", "-b", "add,fullscreen"],
                              backend=b, x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([c for c in x.calls if c[0] == "client_message"],
                         [("client_message", 0x40000C, "_NET_WM_STATE",
                           (1, x.atom("_NET_WM_STATE_FULLSCREEN"), 0, 0, 0))])

    def test_accepted_and_ignored_on_a_native_window_warns(self):
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b.ignores = ("FULLSCREEN",)
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,fullscreen"],
                              backend=b, x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertIn("did not apply it", err)

    def test_a_client_message_the_wm_drops_is_not_success(self):
        """kde-1 (b): _x_set_state answered True for a message that was only
        *sent*. KWin 6 drops the shaded one, and the warning the caller would
        have printed was suppressed by that False success."""
        x = FakeX11()
        x.wm_honours_state = False
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b.ignores = ("FULLSCREEN",)
        rc, _o, err, _b = run(["-r", "Mail", "-b", "add,fullscreen"],
                              backend=b, x11=x)
        self.assertEqual(rc, 0)
        self.assertIn("did not apply it", err)
        self.assertTrue([c for c in x.calls if c[0] == "client_message"])

    def test_a_refused_state_the_wm_drops_still_warns(self):
        x = FakeX11()
        x.wm_honours_state = False
        rc, _o, err, _b = run(["-r", "Mail", "-b", "add,below"], x11=x)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_remove_and_toggle_are_read_back_too(self):
        x = FakeX11()
        run(["-r", "Mail", "-b", "add,below"], x11=x)
        self.assertIn(x.atom("_NET_WM_STATE_BELOW"), x.states[0x40000C])
        rc, _o, err, _b = run(["-r", "Mail", "-b", "toggle,below"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertNotIn(x.atom("_NET_WM_STATE_BELOW"), x.states[0x40000C])
        rc, _o, err, _b = run(["-r", "Mail", "-b", "remove,below"], x11=x)
        self.assertEqual((rc, err), (0, ""))

    def test_a_native_window_never_gets_a_client_message(self):
        x = FakeX11()
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,below"], x11=x)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)
        self.assertEqual([c for c in x.calls if c[0] == "client_message"], [])


class WarnAndSucceedTest(unittest.TestCase):
    def test_k(self):
        rc, _o, err, _b = run(["-k", "maybe"])
        # `toggle` is our extension, but the sentence stays wmctrl's, to the
        # byte: a script matching on it must not have to know which tool ran.
        self.assertEqual((rc, err), (1, 'The argument to the -k option must '
                                        'be either "on" or "off"\n'))
        for arg in ("on", "off", "toggle"):
            rc, _o, err, _b = run(["-k", arg])
            self.assertEqual(rc, 0)
            self.assertIn("ignoring", err)

    def test_o(self):
        rc, _o, err, _b = run(["-o", "12"])
        self.assertEqual((rc, err), (1, "The -o option expects two integers "
                                        "separated with a comma.\n"))
        rc, _o, err, _b = run(["-o", "0,0"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_n(self):
        rc, _o, err, _b = run(["-n", "junk"])
        self.assertEqual((rc, err), (1, "The -n option expects an "
                                        "integer.\n"))
        rc, _o, err, _b = run(["-n", "4"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_g(self):
        rc, _o, err, _b = run(["-g", "1x2"])
        self.assertEqual((rc, err), (1, "The -g option expects two integers "
                                        "separated with a comma.\n"))
        rc, _o, err, _b = run(["-g", "1280,720"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)


class BackendErrorTest(unittest.TestCase):
    def test_backend_failure_prints_message(self):
        def boom():
            raise CmdError("no compositor found")
        old = core._detect_backend
        core._detect_backend = boom
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["-l"])
        finally:
            core._detect_backend = old
        self.assertEqual((rc, err.getvalue()), (1, "no compositor found\n"))


class HelpersTest(unittest.TestCase):
    def test_parse_win_id(self):
        self.assertEqual(core._parse_win_id("0x40000c"), 0x40000C)
        self.assertEqual(core._parse_win_id("0X40000C"), 0x40000C)
        self.assertEqual(core._parse_win_id("42"), 42)
        self.assertEqual(core._parse_win_id(" 42xyz"), 42)  # sscanf prefix
        self.assertIsNone(core._parse_win_id("xyz"))
        self.assertIsNone(core._parse_win_id(""))

    def test_atoi(self):
        self.assertEqual(core._atoi("12"), 12)
        self.assertEqual(core._atoi("-1"), -1)
        self.assertEqual(core._atoi("  8x"), 8)
        self.assertEqual(core._atoi("x8"), 0)
        # ASCII classes, like C in the "C" locale: neither an Arabic-Indic
        # digit nor U+00A0 as leading space is one.
        self.assertEqual(core._atoi("\u0664\u0662"), 0)
        self.assertEqual(core._atoi("4\u0662"), 4)
        self.assertEqual(core._atoi("\xa042"), 0)

    def test_dot_class(self):
        self.assertEqual(core._dot_class("a", "B"), "a.B")
        self.assertEqual(core._dot_class(None, "B"), "B")
        self.assertEqual(core._dot_class("a", None), "a")
        self.assertIsNone(core._dot_class(None, None))


class BuildScriptTest(unittest.TestCase):
    """scripts/build-pyz.sh also emits dist/wwmctl (in a temp copy so a
    parallel test run cannot race on the repo's dist/.stage)."""

    def test_build_emits_both_zipapps(self):
        with tempfile.TemporaryDirectory(prefix="wwmctl-build-") as tmp:
            for d in ("w11common", "wdotool", "wwmctl", "wxprop", "wxrandr",
                      "warandr", "wmirror", "xw11", "scripts"):
                shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                                ignore=shutil.ignore_patterns("__pycache__"))
            p = subprocess.run(["sh", os.path.join(tmp, "scripts",
                                                   "build-pyz.sh")],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            for name in ("wdotool", "wwmctl"):
                path = os.path.join(tmp, "dist", name)
                self.assertTrue(os.path.exists(path), name)
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wwmctl"),
                                "--version"],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual((p.returncode, p.stdout),
                             (0, WMCTRL_VERSION + "\n"))
            # 6801 is the 1.07 help; the zipapp is a real subprocess, so unlike
            # run() it reads the ambient oracle and would print HELP_GIT's 7179
            # bytes on Ubuntu 26.04 (wmctrl 1.07+git20240228). Pin the generation
            # for it the same way.
            genv = dict(os.environ, WWMCTL_WMCTRL_GENERATION="1.07")
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wwmctl"), "-h"],
                               capture_output=True, text=True, timeout=30,
                               env=genv)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(len(p.stdout.encode()), 6801)
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wwmctl"), "-h"],
                               capture_output=True, text=True, timeout=30,
                               env=dict(os.environ,
                                        WWMCTL_WMCTRL_GENERATION="git"))
            self.assertEqual((p.returncode, len(p.stdout.encode())), (0, 7179))
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wdotool"),
                                "version"],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)



# ---------------------------------------------------------------------------
# From the wwmctl torture pass, oracle-diffed against the real wmctrl 1.07
# binary on a live sway+XWayland sandbox. Bugs fixed and pinned here:
# - `python -m wwmctl -q` printed "__main__.py: invalid option" (prog name),
# - unknown long options printed "unrecognized option '--x'"; plain getopt in
#   the oracle prints "invalid option -- '-'",
# - -V/--version printed an identity string instead of the oracle's "1.07",
# - a broken/closed stdout tracebacked ("Exception ignored ... BrokenPipeError"
#   or AttributeError on sys.stdout=None) instead of a quiet exit,
# - -o/-g/-n rejected negative integers that sscanf("%lu") accepts (the oracle
#   exits 0; we warn+succeed like the other desktop no-ops),
# - -s with a negative desktop and -t to a negative desktop leaked sway's
#   off-by-one "Invalid workspace number '-4'" error (now: the -s message is
#   wmctrl's own "Invalid desktop ID.", -t warns and succeeds),
# - -R / -t -1 while a *named* (numberless) sway workspace was focused
#   mis-filed the window onto a workspace literally called "0" instead of the
#   current workspace,
# - the -lx class / machine columns padded by characters, not bytes (printf
#   %-20s counts bytes; only visible with non-ASCII classes/hostnames).


class ProgNameTest(unittest.TestCase):
    def test_python_dash_m_does_not_leak_main_py(self):
        rc, _o, err, _b = run(["-q"], argv0="/x/wwmctl/__main__.py")
        self.assertEqual((rc, err), (1, "wwmctl: invalid option -- 'q'\n"))

    def test_argv0_is_printed_verbatim(self):
        """Measured against wmctrl 1.07 on Plasma 6.6: getopt prints argv[0]
        as it was given -- "/usr/bin/wmctrl: invalid option -- 'q'" for the
        absolute path, "./wmctrl: ..." from the directory. basename()'ing it
        made a drop-in print a different line from the tool it replaces."""
        rc, _o, err, _b = run(["-q"], argv0="/usr/local/bin/wmctrl")
        self.assertEqual(
            (rc, err), (1, "/usr/local/bin/wmctrl: invalid option -- 'q'\n"))
        rc, _o, err, _b = run(["-q"], argv0="./wmctrl")
        self.assertEqual((rc, err), (1, "./wmctrl: invalid option -- 'q'\n"))
        rc, _o, err, _b = run(["-q"], argv0="wmctrl")
        self.assertEqual((rc, err), (1, "wmctrl: invalid option -- 'q'\n"))


class LongOptionParityTest(unittest.TestCase):
    """The oracle uses plain getopt: any unknown --long option is the
    unknown short option '-' (verified byte-for-byte against wmctrl 1.07)."""

    def test_unknown_long_option(self):
        for argv in (["--frob"], ["--frob", "x"], ["-l", "--help"],
                     ["--help", "-l"], ["--version", "x"]):
            rc, _o, err, _b = run(argv)
            self.assertEqual((rc, err),
                             (1, "wmctrl: invalid option -- '-'\n"), argv)

    def test_double_dash_alone_is_end_of_options(self):
        rc, _o, err, _b = run(["--"])
        self.assertEqual((rc, err), (1, cli.HELP))


class BrokenStdoutTest(unittest.TestCase):
    class _BrokenPipe(io.StringIO):
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    def test_broken_pipe_exits_1_quietly(self):
        from wwmctl import core
        old_detect = core._detect_backend
        core._detect_backend = lambda: FakeSwayBackend(
            [dict(s) for s in SPECS])
        old_x11, core._x11_connect = core._x11_connect, lambda: None
        old_out, sys.stdout = sys.stdout, self._BrokenPipe()
        err = io.StringIO()
        old_err, sys.stderr = sys.stderr, err
        try:
            rc = cli.main(["-l"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            core._detect_backend, core._x11_connect = old_detect, old_x11
        self.assertEqual((rc, err.getvalue()), (1, ""))

    def test_stdout_none_help_exits_0(self):
        # fd 1 closed before Python starts -> sys.stdout is None
        old_out, sys.stdout = sys.stdout, None
        try:
            rc = cli.main(["-h"])
            opened = sys.stdout
        finally:
            sys.stdout = old_out
        self.assertEqual(rc, 0)
        if opened is not None and opened is not old_out:
            opened.close()

    def test_stdout_none_list_exits_0(self):
        from wwmctl import core
        old_detect = core._detect_backend
        core._detect_backend = lambda: FakeSwayBackend(
            [dict(s) for s in SPECS])
        old_x11, core._x11_connect = core._x11_connect, lambda: None
        old_out, sys.stdout = sys.stdout, None
        try:
            rc = cli.main(["-l"])
            opened = sys.stdout
        finally:
            sys.stdout = old_out
            core._detect_backend, core._x11_connect = old_detect, old_x11
        self.assertEqual(rc, 0)
        if opened is not None and opened is not old_out:
            opened.close()


class NoSessionErrorTest(unittest.TestCase):
    """T36: with no compositor to be found, nothing wwmctl says names another
    tool.

    `backend.set_program()` is process-global and every main() sets it first
    thing, so a process that has already run a wdotool main -- the suite does,
    and so would any in-process dispatcher -- must not leak that name into
    wmctrl's diagnostics. wmctrl itself never says another program's name."""

    def _run_with(self, exc):
        from wdotool import backend
        backend.set_program("wdotool")           # as a wdotool run leaves it

        def detect():
            raise exc

        old = core._detect_backend
        core._detect_backend = detect
        old_argv, sys.argv = sys.argv, ["wmctrl"]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["-l"])
        finally:
            core._detect_backend, sys.argv = old, old_argv
        return rc, out.getvalue(), err.getvalue(), backend.program()

    def test_the_detectors_own_error_is_printed_verbatim(self):
        """A CmdError out of the detector is the documented deviation from
        the oracle: wmctrl prints "Cannot open display." and we print the
        reason instead (which in the real thing already names the backend, as
        in "kwin backend: ..."). What matters here is that the reason arrives
        whole and unattributed to a tool that did not produce it."""
        rc, out, err, prog = self._run_with(
            CmdError("no session: nothing owns a compositor bus name"))
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err,
                         "no session: nothing owns a compositor bus name\n")
        self.assertNotIn("wdotool", err)
        self.assertEqual(prog, "wwmctl")

    def test_an_unexpected_error_is_prefixed_with_wmctrl(self):
        """Anything that is not a CmdError goes through main()'s one-line
        guard, which prefixes argv[0] -- "wmctrl", never the name a previous
        run left in backend.set_program()."""
        rc, out, err, prog = self._run_with(RuntimeError("boom"))
        self.assertEqual((rc, out, err), (1, "", "wmctrl: boom\n"))
        self.assertNotIn("wdotool", err)
        self.assertEqual(prog, "wwmctl")


class InterruptExitTest(unittest.TestCase):
    """Ctrl-C during a listing exits 130, a gone reader still exits 1 (fix 30).

    main() caught `(BrokenPipeError, KeyboardInterrupt)` in one handler and
    returned 1 for both, while wdotool/cli.py and wxprop/cli.py already
    returned 130 for the interrupt -- so of the three tools in one package
    `wwmctl -l` alone reported a Ctrl-C to the shell as a plain failure. The
    oracle dies of SIGINT, which bash reports as 130 as well."""

    class _Raising(FakeSwayBackend):
        """A backend whose window enumeration is where the signal lands: the
        long part of `wmctrl -l` is the tree walk, not the printing."""

        def __init__(self, exc):
            super().__init__([dict(s) for s in SPECS])
            self.exc = exc

        def _nodes(self):
            raise self.exc

    def test_keyboard_interrupt_is_130_and_prints_nothing(self):
        rc, out, err, _b = run(["-l"], backend=self._Raising(KeyboardInterrupt()))
        self.assertEqual((rc, out, err), (130, "", ""))

    def test_broken_pipe_is_still_1_and_prints_nothing(self):
        rc, out, err, _b = run(
            ["-l"], backend=self._Raising(BrokenPipeError(32, "Broken pipe")))
        self.assertEqual((rc, out, err), (1, "", ""))


class DebugTracebackTest(unittest.TestCase):
    """$DEBUG re-raises instead of printing the one-liner (fix 31).

    Every main() here turns an unexpected exception into `<prog>: <msg>` and
    rc 1, which is right for users and useless for a bug report: the line
    names neither the file nor the frame. DEBUG set to anything at all --
    including the empty string, hence `is not None` -- propagates it."""

    class _Boom(FakeSwayBackend):
        def __init__(self):
            super().__init__([dict(s) for s in SPECS])

        def _nodes(self):
            raise AttributeError("boom")

    def test_without_debug_one_line_and_rc_1(self):
        rc, out, err, _b = run(["-l"], backend=self._Boom())
        self.assertEqual((rc, out, err), (1, "", "wmctrl: boom\n"))

    def test_with_debug_the_exception_propagates(self):
        for val in ("1", ""):
            with self.subTest(DEBUG=val):
                with self.assertRaises(AttributeError) as cm:
                    run(["-l"], backend=self._Boom(),
                        env={"DEBUG": val})
                self.assertEqual(str(cm.exception), "boom")


class NegativeIntParityTest(unittest.TestCase):
    """sscanf("%lu") accepts a sign (strtoul wraps): the oracle exits 0 on
    negative -o/-g/-n arguments. We accept them and warn+succeed."""

    def test_o_g_negative(self):
        for flag in ("-o", "-g"):
            rc, _o, err, _b = run([flag, "-1,-1"])
            self.assertEqual(rc, 0, flag)
            self.assertIn("ignoring", err)

    def test_n_negative(self):
        rc, _o, err, _b = run(["-n", "-3"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_garbage_still_rejected(self):
        rc, _o, err, _b = run(["-o", "-,3"])
        self.assertEqual((rc, err), (1, "The -o option expects two integers "
                                        "separated with a comma.\n"))


class NegativeDesktopTest(unittest.TestCase):
    def test_s_any_negative_is_invalid_desktop(self):
        # never leak sway's off-by-one "Invalid workspace number '-4'"
        for arg in ("-1", "-5", "-2147483648"):
            rc, _o, err, b = run(["-s", arg])
            self.assertEqual((rc, err), (1, "Invalid desktop ID.\n"), arg)
            self.assertEqual(b.calls, [])

    def test_t_negative_warns_and_succeeds(self):
        # the oracle fires _NET_WM_DESKTOP=huge into the void and exits 0
        rc, _o, err, b = run(["-r", "FootWin", "-t", "-5"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)
        self.assertEqual(b.calls, [])


class RunCapableFake(FakeSwayBackend):
    """Sway-shaped fake that, like the real SwayBackend, offers run()."""

    move_to_current_desktop = SwayBackend.move_to_current_desktop

    def run(self, command):
        self.calls.append(("run", command))

    def window_desktop(self, wid):
        return self._spec(wid).get("desktop", 0)


class CurrentDesktopViaRunTest(unittest.TestCase):
    """-R / -t -1 on sway go through `move container to workspace current`,
    which is correct even when the focused workspace is named (no number).
    The numeric route used to send the window to a workspace called "0"."""

    def _backend(self):
        return RunCapableFake([dict(s) for s in SPECS])

    def test_t_minus_one_uses_workspace_current(self):
        b = self._backend()
        b._cur = -1  # focused workspace is named: get_desktop() == -1
        rc, _o, err, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            b.calls, [("run", "[con_id=6] move container to workspace "
                              "current")])

    def test_R_uses_workspace_current_then_activates(self):
        b = self._backend()
        b._cur = -1
        rc, _o, err, b = run(["-R", "FootWin"], backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            b.calls, [("run", "[con_id=6] move container to workspace "
                              "current"),
                      ("activate", 6)])

    def test_vanished_window_is_clean_error(self):
        b = self._backend()

        def gone(wid):
            raise CmdError("window %d not found" % wid)
        b.window_desktop = gone
        rc, _o, err, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual((rc, err), (1, "window 6 not found\n"))

    def test_generic_backend_still_uses_numbers(self):
        # no run(): fall back to get_desktop() + set_window_desktop()
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b._cur = 3
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual((rc, b.calls), (0, [("set_window_desktop", 6, 3)]))


class BytePaddingTest(unittest.TestCase):
    """printf %-20s / %*s count bytes; ours must too (visible only with
    non-ASCII WM_CLASS or hostnames)."""

    def test_lx_class_column_pads_bytes(self):
        specs = [dict(SPECS[1])]
        specs[0]["app_id"] = "föö"  # föö: 3 chars, 5 bytes
        rc, out, _e, _b = run(["-lx"], backend=FakeSwayBackend(specs))
        line = out.splitlines()[0]
        head = "0x00000006  0 "
        cls = "föö.föö"  # 7 chars, 11 bytes
        pad = " " * (20 - 11)
        self.assertEqual(line, head + cls + pad + "  testhost FootWin")

    def test_machine_column_pads_bytes(self):
        x11 = FakeX11(machines={0x40000C: "hôst"})  # 4 chars, 5 bytes
        specs = [dict(SPECS[0]), dict(SPECS[1])]
        rc, out, _e, _b = run(["-l"], backend=FakeSwayBackend(specs),
                              x11=x11)
        lines = out.splitlines()
        # width = byte length of the last machine ("testhost", 8): the höst
        # row right-pads to 8 BYTES = 3 spaces before the 5-byte name
        self.assertEqual(lines[0], "0x0040000c  0    hôst Mail inbox")
        self.assertEqual(lines[1], "0x00000006  0 testhost FootWin")


class EnrichmentRaceTest(unittest.TestCase):
    """An XWayland window can die between the tree read and the X property
    reads: every enrichment failure must degrade to compositor data."""

    class _DyingX11:
        def _boom(self, *a, **k):
            raise ConnectionResetError("X connection lost")
        get_wm_class = get_client_machine = get_geometry = get_pid = _boom
        root = get_prop_ints = get_prop_string = set_name = _boom

    def test_listing_survives_x_failures(self):
        rc, out, _e, _b = run(["-lGpx"], x11=self._DyingX11())
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.splitlines()[0],
            "0x0040000c  0 111    0    0    640  720  "
            "xterm.XTerm           testhost Mail inbox")

    def test_set_title_survives_x_failure(self):
        rc, _o, err, _b = run(["-r", "Mail", "-N", "x"],
                              x11=self._DyingX11())
        self.assertEqual(rc, 0)
        self.assertIn("; ignoring", err)


class SelectionLiteralTest(unittest.TestCase):
    """<WIN> is a literal casefolded substring, never a regex."""

    def _backend(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["title"] = "a.*b [1] (main)"
        return FakeSwayBackend(specs)

    def test_metachars_match_literally(self):
        rc, _o, _e, b = run(["-a", "a.*b [1]"], backend=self._backend())
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))

    def test_metachars_do_not_glob(self):
        rc, _o, _e, b = run(["-a", "a.b"], backend=self._backend())
        self.assertEqual((rc, b.calls), (1, []))

    def test_empty_needle_matches_first_window(self):
        # strstr(title, "") matches: the oracle picks the first listed window
        rc, _o, _e, b = run(["-a", ""])
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))


class SscanfEdgeTest(unittest.TestCase):
    def test_e_space_before_comma_rejected(self):
        # sscanf: a literal ',' does not skip whitespace
        rc, _o, err, _b = run(["-r", "Mail", "-e", "0 ,10,20,300,200"])
        self.assertEqual(rc, 1)
        self.assertIn("The -e option expects", err)

    def test_e_space_after_comma_ok(self):
        # %ld skips leading whitespace
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0, 10, 20, 300, 200"],
                            backend=FakeSwayBackend(specs))
        self.assertEqual(rc, 0)
        self.assertEqual(b.calls, [("resize", 5, 300, 200),
                                   ("move", 5, 10, 20)])

    def test_e_trailing_junk_after_five_ints_ok(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        rc, _o, _e, _b = run(["-r", "Mail", "-e", "0,-1,-1,300,200junk"],
                             backend=FakeSwayBackend(specs))
        self.assertEqual(rc, 0)

    def test_b_third_comma_joins_prop2(self):
        # strchr splitting: "add,a,b,c" -> PROP2 is "b,c" (oracle-verified)
        rc, _o, err, _b = run(["-v", "-r", "Mail", "-b", "add,a,b,c"])
        self.assertEqual(rc, 0)
        self.assertIn("State 2: _NET_WM_STATE_B,C\n", err)
        self.assertIn("State 1: _NET_WM_STATE_A\n", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
