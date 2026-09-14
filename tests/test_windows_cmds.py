#!/usr/bin/env python3
"""window/desktop command unit tests against a fake in-memory
backend — output byte-parity, arg consumption, stack semantics, exit codes.
No compositor needed."""

import contextlib
import io
import os
import signal
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

from w11common.errors import CmdError
from wdotool import cli, window_cmds
from hacks.window import backend
from hacks.window.backend import Window, WindowBackend
from wdotool.ctx import Context, NoSessionError, SoftCmdError


# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class FakeBackend(WindowBackend):
    name = "fake"

    def __init__(self, windows):
        self.windows = {w.id: w for w in windows}
        self.calls = []
        self.desktop = 0
        self.ndesktops = 2

    def list(self):
        return list(self.windows.values())

    def activate(self, wid):
        self.calls.append(("activate", wid))
        for w in self.windows.values():
            w.focused = w.id == wid

    def close(self, wid):
        self.calls.append(("close", wid))
        del self.windows[wid]

    def move_window(self, wid, x, y):
        self.calls.append(("move", wid, x, y))
        self.windows[wid].x, self.windows[wid].y = x, y

    def resize(self, wid, w, h):
        self.calls.append(("resize", wid, w, h))
        self.windows[wid].w, self.windows[wid].h = w, h

    def minimize(self, wid):
        self.windows[wid].visible = False

    def map(self, wid):
        self.windows[wid].visible = True

    def unmap(self, wid):
        self.windows[wid].visible = False

    def set_state(self, wid, state, action):
        self.calls.append(("state", wid, state, action))
        if state not in ("FULLSCREEN", "HIDDEN"):
            raise CmdError("windowstate %s is not supported" % state)

    def get_desktop(self):
        return self.desktop

    def set_desktop(self, n):
        self.desktop = n

    def num_desktops(self):
        return self.ndesktops

    def set_window_desktop(self, wid, n):
        self.windows[wid].desktop = n

    def display_size(self):
        return 1280, 720


def make_backend():
    return FakeBackend([
        Window(id=11, title="Alpha One", class_="alpha", pid=101,
               x=5, y=6, w=100, h=200, focused=True, visible=True, desktop=0),
        Window(id=22, title="Beta Two", class_="beta", pid=202,
               x=7, y=8, w=300, h=400, focused=False, visible=True, desktop=1),
        Window(id=33, title="Hidden Beta", class_="beta", pid=303,
               x=0, y=0, w=10, h=10, focused=False, visible=False, desktop=-1),
    ])


def run(argv, backend=None):
    """Run a chain like cli.main but with the fake backend pre-installed."""
    ctx = Context()
    ctx._backend = backend or make_backend()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.run_chain(ctx, "wdotool", argv)
    return (rc if rc else ctx.exit_code), out.getvalue(), err.getvalue(), ctx



class SearchTest(unittest.TestCase):
    def test_basic_and_stack(self):
        rc, out, err, ctx = run(["search", "--class", "beta"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "22\n33\n")
        self.assertEqual(ctx.stack, [22, 33])
        self.assertEqual(err, "")

    def test_default_mask_warning(self):
        rc, out, err, _ = run(["search", "alpha"])
        self.assertEqual(
            err, "Defaulting to search window name, class, classname, and role\n"
        )
        self.assertEqual(out, "11\n")

    def test_name_only(self):
        rc, out, _e, _ = run(["search", "--name", "beta"])
        self.assertEqual(out, "22\n33\n")  # titles "Beta Two", "Hidden Beta"

    def test_case_insensitive(self):
        _rc, out, _e, _ = run(["search", "--name", "ALPHA"])
        self.assertEqual(out, "11\n")

    def test_onlyvisible(self):
        _rc, out, _e, _ = run(["search", "--onlyvisible", "--class", "beta"])
        self.assertEqual(out, "22\n")

    def test_desktop_filter(self):
        _rc, out, _e, _ = run(["search", "--desktop", "1", "--class", "beta"])
        self.assertEqual(out, "22\n")

    def test_pid_no_pattern(self):
        rc, out, _e, _ = run(["search", "--pid", "202"])
        self.assertEqual((rc, out), (0, "22\n"))

    def test_pid_any_vs_all(self):
        _rc, out, _e, _ = run(["search", "--all", "--pid", "202", "--class", "beta"])
        self.assertEqual(out, "22\n")
        _rc, out, _e, _ = run(["search", "--any", "--pid", "202", "--class", "alpha"])
        self.assertEqual(sorted(out.split()), ["11", "22"])

    def test_limit(self):
        _rc, out, _e, _ = run(["search", "--limit", "1", "--class", "beta"])
        self.assertEqual(out, "22\n")

    def test_shell_output_and_exit(self):
        rc, out, _e, _ = run(["search", "--shell", "--prefix", "P", "--class", "beta"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PWINDOWS=(22\n33\n)\n")
        rc, out, _e, _ = run(["search", "--shell", "--class", "nope"])
        self.assertEqual((rc, out), (0, "WINDOWS=()\n"))

    def test_no_match_aborts_chain_with_exit_1(self):
        rc, out, _e, ctx = run(["search", "--class", "nope"])
        self.assertEqual((rc, out, ctx.stack), (1, "", []))

    def test_not_last_is_silent(self):
        rc, out, _e, _ = run(["search", "--class", "alpha", "getwindowpid"])
        self.assertEqual((rc, out), (0, "101\n"))

    def test_title_deprecation(self):
        _rc, out, err, _ = run(["search", "--title", "alpha"])
        self.assertIn("This flag is deprecated. Assuming you mean --name "
                      "(the window name).\n", err)
        self.assertEqual(out, "11\n")  # matches title "Alpha One"

    def test_no_pattern_usage_error(self):
        rc, _o, err, _ = run(["search"])
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("Usage: xdotool search"))

    def test_bad_regex_matches_nothing(self):
        rc, out, err, _ = run(["search", "--class", "*bad"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("Failed to compile regex", err)



class QueryTest(unittest.TestCase):
    def test_getactivewindow_prints_when_last(self):
        rc, out, _e, ctx = run(["getactivewindow"])
        self.assertEqual((rc, out, ctx.stack), (0, "11\n", [11]))

    def test_getactivewindow_silent_mid_chain(self):
        _rc, out, _e, _ = run(["getactivewindow", "getwindowname"])
        self.assertEqual(out, "Alpha One\n")

    def test_getwindowfocus(self):
        rc, out, _e, _ = run(["getwindowfocus", "-f"])
        self.assertEqual((rc, out), (0, "11\n"))

    def test_getwindowname_arg_forms(self):
        self.assertEqual(run(["getwindowname", "22"])[1], "Beta Two\n")
        self.assertEqual(
            run(["search", "--class", "beta", "getwindowname", "%2"])[1],
            "Hidden Beta\n",
        )
        self.assertEqual(
            run(["search", "--class", "beta", "getwindowname", "%@"])[1],
            "Beta Two\nHidden Beta\n",
        )

    def test_getwindowpid_missing(self):
        b = make_backend()
        b.windows[11].pid = 0
        rc, _o, err, _ = run(["getwindowpid", "11"], backend=b)
        self.assertEqual(rc, 1)
        self.assertEqual(err, "window 11 has no pid associated with it.\n")

    def test_getwindowgeometry_plain(self):
        _rc, out, _e, _ = run(["getwindowgeometry", "22"])
        self.assertEqual(
            out,
            "Window 22\n  Position: 7,8 (screen: 0)\n  Geometry: 300x400\n",
        )

    def test_getwindowgeometry_shell_prefix(self):
        _rc, out, _e, _ = run(["getwindowgeometry", "--shell", "--prefix", "Q", "22"])
        self.assertEqual(
            out, "QWINDOW=22\nQX=7\nQY=8\nQWIDTH=300\nQHEIGHT=400\nQSCREEN=0\n"
        )

    def test_default_window_without_stack_errors(self):
        # Real xdotool: an omitted window argument defaults to %1, and an
        # empty stack makes that an error ("These would error: xdotool
        # windowactivate" -- manpage, COMMAND CHAINING).
        rc, out, err, _ = run(["getwindowclassname"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("There are no windows in the stack", err)



class ActionTest(unittest.TestCase):
    def test_windowactivate_stack_default(self):
        rc, _o, _e, ctx = run(["search", "--class", "beta", "windowactivate"])
        self.assertEqual(rc, 0)
        self.assertEqual(ctx._backend.calls[-1], ("activate", 22))

    def test_windowactivate_all(self):
        rc, _o, _e, ctx = run(["search", "--class", "beta",
                               "windowactivate", "%@"])
        acts = [c for c in ctx._backend.calls if c[0] == "activate"]
        self.assertEqual(acts, [("activate", 22), ("activate", 33)])

    def test_windowmove_consumption_and_chain(self):
        rc, out, _e, ctx = run(["windowmove", "22", "1", "2",
                                "getwindowgeometry", "22"])
        self.assertEqual(rc, 0)
        self.assertIn("Position: 1,2", out)

    def test_windowmove_x_literal(self):
        _rc, _o, _e, ctx = run(["windowmove", "22", "x", "99"])
        self.assertEqual(ctx._backend.calls[-1], ("move", 22, 7, 99))

    def test_windowmove_y_literal(self):
        _rc, _o, _e, ctx = run(["windowmove", "22", "99", "y"])
        self.assertEqual(ctx._backend.calls[-1], ("move", 22, 99, 8))

    def test_windowmove_relative(self):
        _rc, _o, _e, ctx = run(["windowmove", "--relative", "22", "10", "-2"])
        self.assertEqual(ctx._backend.calls[-1], ("move", 22, 17, 6))

    def test_windowmove_percent(self):
        _rc, _o, _e, ctx = run(["windowmove", "22", "50%", "50%"])
        self.assertEqual(ctx._backend.calls[-1], ("move", 22, 640, 360))

    def test_windowmove_percent_y_bug_compat(self):
        # cmd_windowmove.c checks argv[0] for '%' when flagging y as percent:
        # "10 50%" moves y to 50 *pixels*, not 50% of the screen.
        _rc, _o, _e, ctx = run(["windowmove", "22", "10", "50%"])
        self.assertEqual(ctx._backend.calls[-1], ("move", 22, 10, 50))

    def test_windowsize_pixels_and_percent(self):
        _rc, _o, _e, ctx = run(["windowsize", "22", "640", "50%"])
        self.assertEqual(ctx._backend.calls[-1], ("resize", 22, 640, 360))

    def test_windowsize_default_window(self):
        _rc, _o, _e, ctx = run(["search", "--class", "alpha",
                                "windowsize", "111", "222"])
        self.assertEqual(ctx._backend.calls[-1], ("resize", 11, 111, 222))

    def test_window_arg_not_taken_when_next_is_command(self):
        # windowsize 100 200 getwindowname: "getwindowname" at rest[2] is a
        # command, so no window argument is consumed (C window_get_arg).
        rc, out, _e, _ = run(["search", "--class", "alpha", "windowsize",
                              "100", "200", "getwindowname"])
        self.assertEqual((rc, out), (0, "Alpha One\n"))

    def test_windowclose(self):
        rc, _o, _e, ctx = run(["windowclose", "22"])
        self.assertNotIn(22, ctx._backend.windows)

    def test_windowstate_actions(self):
        _rc, _o, _e, ctx = run(["windowstate", "--add", "fullscreen", "22"])
        self.assertEqual(ctx._backend.calls[-1], ("state", 22, "FULLSCREEN", 1))
        _rc, _o, _e, ctx = run(["windowstate", "--remove", "FULLSCREEN", "22"])
        self.assertEqual(ctx._backend.calls[-1], ("state", 22, "FULLSCREEN", 0))
        _rc, _o, _e, ctx = run(["windowstate", "--toggle", "HIDDEN", "22"])
        self.assertEqual(ctx._backend.calls[-1], ("state", 22, "HIDDEN", 2))

    def test_windowstate_error(self):
        rc, _o, err, _ = run(["windowstate", "--add", "SHADED", "22"])
        self.assertEqual(rc, 1)
        self.assertIn("xdo_window_property reported an error on window 22\n", err)

    def test_windowstate_last_option_wins_like_xdotool(self):
        """Two --adds do not set two states: xdotool's cmd_windowstate.c has
        one action/arg_property pair and overwrites both in every getopt arm,
        so only the last option is applied. Pinned so a later reader does not
        `fix` it into a divergence."""
        _rc, _o, _e, ctx = run(["windowstate", "--add", "FULLSCREEN", "--add", "HIDDEN", "22"])
        self.assertEqual([c for c in ctx._backend.calls if c[0] == "state"], [("state", 22, "HIDDEN", 1)])
        _rc, _o, _e, ctx = run(["windowstate", "--add", "FULLSCREEN", "--remove", "HIDDEN", "22"])
        self.assertEqual([c for c in ctx._backend.calls if c[0] == "state"], [("state", 22, "HIDDEN", 0)])

    def test_windowstate_requires_action(self):
        rc, _o, err, _ = run(["windowstate", "22"])
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("Usage: windowstate"))

    def test_map_unmap_minimize(self):
        rc, _o, _e, ctx = run(["windowunmap", "22"])
        self.assertFalse(ctx._backend.windows[22].visible)
        rc, _o, _e, ctx = run(["windowmap", "33"])
        self.assertTrue(ctx._backend.windows[33].visible)

    def test_warn_and_succeed_cmds(self):
        for argv in (
            ["windowreparent", "11", "22"],
            ["set_window", "--name", "x", "11"],
            ["set_num_desktops", "4"],
            ["set_desktop_viewport", "3", "4"],
        ):
            rc, out, err, _ = run(argv)
            self.assertEqual(rc, 0, argv)
            self.assertEqual(out, "", argv)
            self.assertTrue(err.startswith("wdotool: "), (argv, err))
            self.assertEqual(err.count("\n"), 1, argv)

    def test_windowreparent_consumption(self):
        rc, out, _e, _ = run(["windowreparent", "11", "22", "getwindowname", "22"])
        self.assertEqual((rc, out), (0, "Beta Two\n"))

    def test_behave_unsupported(self):
        rc, _o, err, _ = run(["behave", "%@", "mouse-enter", "getmouselocation"])
        self.assertEqual(rc, 1)
        self.assertIn("behave is not supported", err)



class DesktopTest(unittest.TestCase):
    def test_get_set(self):
        self.assertEqual(run(["get_desktop"])[1], "0\n")
        self.assertEqual(run(["get_num_desktops"])[1], "2\n")
        rc, _o, _e, ctx = run(["set_desktop", "1", "get_desktop"])
        self.assertEqual(ctx._backend.desktop, 1)

    def test_set_desktop_relative_wraps(self):
        rc, _o, _e, ctx = run(["set_desktop", "--relative", "--", "-1"])
        self.assertEqual((rc, ctx._backend.desktop), (0, 1))
        b = make_backend()
        b.desktop = 1
        rc, _o, _e, ctx = run(["set_desktop", "--relative", "1"], backend=b)
        self.assertEqual(b.desktop, 0)

    def test_desktop_for_window(self):
        self.assertEqual(run(["get_desktop_for_window", "22"])[1], "1\n")
        rc, _o, _e, ctx = run(["set_desktop_for_window", "22", "0"])
        self.assertEqual(ctx._backend.windows[22].desktop, 0)

    def test_viewport(self):
        self.assertEqual(run(["get_desktop_viewport"])[1], "0 0\n")
        self.assertEqual(run(["get_desktop_viewport", "--shell"])[1], "X=0\nY=0\n")

    def test_get_desktop_consumes_nothing(self):
        rc, out, _e, _ = run(["get_desktop", "get_num_desktops"])
        self.assertEqual((rc, out), (0, "0\n2\n"))



class HelpTest(unittest.TestCase):
    def test_help_consumes_all_and_succeeds(self):
        rc, out, _e, _ = run(["windowmove", "--help", "these", "are", "eaten"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("Usage: windowmove"))

    def test_bad_option_message(self):
        rc, _o, err, _ = run(["windowraise", "--bogus"])
        self.assertEqual(rc, 1)
        self.assertTrue(
            err.startswith("windowraise: unrecognized option '--bogus'\n")
        )



class BoundedSyncTest(unittest.TestCase):
    """B3: no --sync wait runs for ever any more."""

    class _Stuck(FakeBackend):
        """Every action succeeds and changes nothing -- the shape Mutter has
        when it snaps a resize or defers a focus change."""

        def activate(self, wid):
            self.calls.append(("activate", wid))

        def resize(self, wid, w, h):
            self.calls.append(("resize", wid, w, h))

        def move_window(self, wid, x, y):
            self.calls.append(("move", wid, x, y))

        def minimize(self, wid):
            self.calls.append(("minimize", wid))

        def is_mapped(self, wid):
            return True

    def setUp(self):
        self.backup = os.environ.get("WDOTOOL_SYNC_TIMEOUT")
        os.environ["WDOTOOL_SYNC_TIMEOUT"] = "0.3"

    def tearDown(self):
        if self.backup is None:
            os.environ.pop("WDOTOOL_SYNC_TIMEOUT", None)
        else:
            os.environ["WDOTOOL_SYNC_TIMEOUT"] = self.backup

    def stuck(self):
        return self._Stuck([
            Window(id=11, title="xterm", class_="XTerm", pid=1,
                   x=0, y=0, w=496, h=392, focused=False, visible=True),
        ])

    def test_windowactivate_sync_gives_up(self):
        t0 = time.monotonic()
        rc, _o, err, _c = run(["windowactivate", "--sync", "11"], self.stuck())
        self.assertEqual(rc, 1)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(
            err, "wdotool: gave up waiting for window 11 to become active after 0.3s\n")

    def test_windowminimize_sync_gives_up(self):
        rc, _o, err, _c = run(["windowminimize", "--sync", "11"], self.stuck())
        self.assertEqual(rc, 1)
        self.assertIn("gave up waiting for window 11 to be minimized", err)

    class _Refuses(FakeBackend):
        """A backend whose move/resize say no. `soft` picks which no."""

        soft = False

        def move_window(self, wid, x, y):
            raise (SoftCmdError if self.soft else CmdError)("nope: move %d" % wid)

        def resize(self, wid, w, h):
            raise (SoftCmdError if self.soft else CmdError)("nope: resize %d" % wid)

    def test_windowstate_unknown_property_is_a_typo_not_a_gap(self):
        """`--add MAXIMISED` used to come back "not supported by the kwin
        backend", which reads as a missing compositor feature."""
        rc, _o, err, _c = run(["windowstate", "--add", "MAXIMISED", "11"])
        self.assertEqual(rc, 1)
        self.assertIn("no such property MAXIMISED", err)
        self.assertIn("MAXIMIZED_VERT", err)
        self.assertNotIn("not supported by", err)

    def test_windowstate_a_real_gap_still_says_so(self):
        rc, _o, err, _c = run(["windowstate", "--add", "SHADED", "11"])
        self.assertEqual(rc, 1)
        self.assertIn("not supported", err)

    def test_windowstate_names_are_still_case_insensitive(self):
        rc, _o, err, _c = run(["windowstate", "--add", "fullscreen", "11"])
        self.assertEqual((rc, err), (0, ""))

    def test_windowmove_hard_failure_exits_1(self):
        """A stale window id, a KWin script that failed, a compositor that
        said no: windowmove printed the error and exited 0, so `wdotool
        windowmove $id ... && echo moved` said moved."""
        b = self._Refuses([Window(id=11, x=0, y=0, w=10, h=10)])
        rc, _o, err, _c = run(["windowmove", "11", "5", "5"], b)
        self.assertEqual(rc, 1)
        self.assertIn("nope: move 11", err)
        self.assertIn("xdo_move_window reported an error while moving window 11",
                      err)

    def test_windowmove_soft_failure_warns_and_exits_0(self):
        """sway tiling a window is the desktop's shape, not a failed request:
        warn on stderr, carry on, rc 0 (unchanged behaviour)."""
        b = self._Refuses([Window(id=11, x=0, y=0, w=10, h=10)])
        b.soft = True
        rc, _o, err, _c = run(["windowmove", "11", "5", "5"], b)
        self.assertEqual(rc, 0)
        self.assertIn("xdo_move_window reported an error", err)

    def test_windowsize_hard_failure_still_exits_1(self):
        b = self._Refuses([Window(id=11, x=0, y=0, w=10, h=10)])
        rc, _o, err, _c = run(["windowsize", "11", "50", "50"], b)
        self.assertEqual(rc, 1)
        self.assertIn("xdo_set_window_size on window:11 reported an error", err)

    def test_windowsize_soft_failure_warns_and_exits_0(self):
        """The mirror of the windowmove case: a tiled sway resize."""
        b = self._Refuses([Window(id=11, x=0, y=0, w=10, h=10)])
        b.soft = True
        rc, _o, err, _c = run(["windowsize", "11", "50", "50"], b)
        self.assertEqual(rc, 0)
        self.assertIn("nope: resize 11", err)
        self.assertIn("xdo_set_window_size on window:11 reported an error", err)

    def test_windowmove_hard_failure_aborts_the_chain(self):
        b = self._Refuses([Window(id=11, x=0, y=0, w=10, h=10)])
        rc, out, _e, _c = run(["windowmove", "11", "5", "5",
                               "getwindowname", "11"], b)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")

    def test_windowmove_sync_gives_up(self):
        rc, _o, err, _c = run(["windowmove", "--sync", "11", "700", "800"],
                              self._Stuck([Window(id=11, x=0, y=0, w=10, h=10)]))
        self.assertEqual(rc, 1)
        self.assertIn("gave up waiting for window 11 to move", err)

    def test_windowsize_sync_accepts_a_snapped_size(self):
        """B3a: Mutter snaps an xterm asked for 497x392 to 496x392, so the
        size never changes and the old loop waited for ever."""
        t0 = time.monotonic()
        rc, _o, err, _c = run(["windowsize", "--sync", "11", "497", "392"],
                              self.stuck())
        self.assertEqual((rc, err), (0, ""))
        self.assertLess(time.monotonic() - t0, 0.3)

    def test_windowsize_sync_still_gives_up_on_a_refused_resize(self):
        rc, _o, err, _c = run(["windowsize", "--sync", "11", "1000", "900"],
                              self.stuck())
        self.assertEqual(rc, 1)
        self.assertIn("gave up waiting for window 11 to be resized", err)

    def test_a_wait_that_succeeds_is_untouched(self):
        rc, _o, err, _c = run(["windowsize", "--sync", "11", "800", "600"])
        self.assertEqual((rc, err), (0, ""))

    def test_zero_timeout_restores_the_unbounded_wait(self):
        os.environ["WDOTOOL_SYNC_TIMEOUT"] = "0"
        self.assertEqual(window_cmds._sync_timeout(), 0.0)
        os.environ["WDOTOOL_SYNC_TIMEOUT"] = "nonsense"
        self.assertEqual(window_cmds._sync_timeout(), window_cmds.SYNC_TIMEOUT)
        os.environ.pop("WDOTOOL_SYNC_TIMEOUT")
        self.assertEqual(window_cmds._sync_timeout(), window_cmds.SYNC_TIMEOUT)


class ClassNameSearchTest(unittest.TestCase):
    """B4: --classname matches the WM_CLASS *instance* of X/XWayland windows,
    --class the class part; native toplevels have no instance and keep
    matching their app_id through both."""

    def backend(self):
        return FakeBackend([
            Window(id=11, title="xterm", class_="XTerm", instance="myinst",
                   pid=1, x=0, y=0, w=10, h=10, visible=True),
            Window(id=22, title="calc", class_="org.gnome.Calculator",
                   pid=2, x=0, y=0, w=10, h=10, visible=True),
        ])

    def test_classname_finds_the_instance(self):
        rc, out, _e, _c = run(["search", "--classname", "myinst"], self.backend())
        self.assertEqual((rc, out), (0, "11\n"))

    def test_class_does_not_match_the_instance(self):
        rc, out, _e, _c = run(["search", "--class", "myinst"], self.backend())
        self.assertEqual((rc, out), (1, ""))

    def test_class_matches_the_class_part(self):
        rc, out, _e, _c = run(["search", "--class", "XTerm"], self.backend())
        self.assertEqual((rc, out), (0, "11\n"))

    def test_native_toplevel_matches_app_id_through_both(self):
        for flag in ("--class", "--classname"):
            rc, out, _e, _c = run(["search", flag, "gnome.Calc"], self.backend())
            self.assertEqual((rc, out), (0, "22\n"), flag)


class NoSessionExitCodeTest(unittest.TestCase):
    """B5: rc 2 for "no Wayland session", rc 1 for "nothing matched"."""

    class _NoSession(WindowBackend):
        name = "none"

    def run_without_backend(self, argv):
        ctx = Context()

        def boom():
            raise NoSessionError("wdotool: no Wayland session found: nothing here")

        ctx.backend = boom
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run_chain(ctx, "wdotool", argv)
        return (rc if rc else ctx.exit_code), out.getvalue(), err.getvalue()

    def test_no_session_is_rc_2(self):
        for argv in (["search", "--name", "x"], ["getactivewindow"],
                     ["windowactivate", "11"], ["get_num_desktops"]):
            rc, _o, err = self.run_without_backend(argv)
            self.assertEqual(rc, 2, argv)
            self.assertEqual(err, "wdotool: no Wayland session found: nothing here\n")

    def test_no_match_stays_rc_1(self):
        rc, out, err, _c = run(["search", "--name", "nothing-matches-this"])
        self.assertEqual((rc, out, err), (1, "", ""))

    def test_no_active_window_stays_rc_1(self):
        b = make_backend()
        for w in b.windows.values():
            w.focused = False
        rc, _o, err, _c = run(["getactivewindow"], b)
        self.assertEqual(rc, 1)
        self.assertEqual(err, "xdo_get_active_window reported an error\n")


class InvalidWindowIdTest(unittest.TestCase):
    """B8: a negative or out-of-range id is one line and rc 1, never a
    D-Bus marshalling traceback."""

    def test_bad_ids(self):
        for bad in ("-5", "0x10000000000000000", "18446744073709551616",
                    "notanumber"):
            rc, out, err, _c = run(["getwindowname", "--", bad])
            self.assertEqual(rc, 1, bad)
            self.assertEqual(out, "", bad)
            self.assertEqual(err, "Invalid window id '%s'\n" % bad, bad)

    def test_the_largest_valid_id_is_still_accepted(self):
        ctx = Context()
        self.assertEqual(ctx._resolve_one("18446744073709551615"), 2 ** 64 - 1)
        self.assertEqual(ctx._resolve_one("0"), 0)


class SetNumDesktopsTest(unittest.TestCase):
    """B9: actually ask the compositor; only a capability gap warns."""

    class _Counting(FakeBackend):
        def __init__(self, windows, fail=None):
            super().__init__(windows)
            self.fail = fail

        def set_num_desktops(self, n):
            self.calls.append(("set_num_desktops", n))
            if self.fail is not None:
                raise self.fail

    def test_calls_the_backend(self):
        b = self._Counting([])
        rc, _o, err, _c = run(["set_num_desktops", "3"], b)
        self.assertEqual(rc, 0)
        self.assertEqual(b.calls, [("set_num_desktops", 3)])
        self.assertEqual(err, "")

    def test_unsupported_only_warns(self):
        err_obj = CmdError("dynamic workspaces are enabled")
        err_obj.unsupported = True
        b = self._Counting([], fail=err_obj)
        rc, _o, err, _c = run(["set_num_desktops", "3"], b)
        self.assertEqual(rc, 0)
        self.assertEqual(
            err, "wdotool: set_num_desktops: dynamic workspaces are enabled; ignoring\n")

    def test_a_real_failure_fails(self):
        b = self._Counting([], fail=CmdError("SetNWorkspaces: no reply"))
        rc, _o, err, _c = run(["set_num_desktops", "3"], b)
        self.assertEqual(rc, 1)
        self.assertEqual(err, "SetNWorkspaces: no reply\n")

    def test_a_backend_without_the_capability_warns(self):
        rc, _o, err, _c = run(["set_num_desktops", "3"], FakeBackend([]))
        self.assertEqual(rc, 0)
        self.assertIn("not supported by the fake backend", err)
        self.assertTrue(err.endswith("; ignoring\n"))


class SearchUsageTest(unittest.TestCase):
    """B14: cmd_search.c is the one command that prints "Invalid usage"
    between getopt's message and the usage block."""

    def test_limit_without_an_argument(self):
        rc, _o, err, _c = run(["search", "--limit"])
        self.assertEqual(rc, 1)
        lines = err.splitlines()
        self.assertEqual(lines[0], "search: option '--limit' requires an argument")
        self.assertEqual(lines[1], "Invalid usage")
        self.assertEqual(lines[2], "Usage: xdotool search [options] regexp_pattern")

    def test_other_commands_do_not_print_it(self):
        rc, _o, err, _c = run(["windowsize", "--nope"])
        self.assertEqual(rc, 1)
        self.assertNotIn("Invalid usage", err)



class EmptyStackMessageTest(unittest.TestCase):
    """xdotool prints three things when the stack is empty and a window is
    needed: the message, the reference it could not resolve, and the command's
    own usage. Measured against xdotool 3.20160805.1, which a script grepping
    for "There are no windows in the stack" depends on."""

    def test_the_implicit_percent_1(self):
        rc, out, err, _ = run(["getwindowname"])
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err,
                         "There are no windows in the stack\n"
                         "Invalid window '%1'\n"
                         "Usage: getwindowname [window=%1]\n"
                         "If no window is given, %1 is used. "
                         "See WINDOW STACK in xdotool(1)\n")

    def test_an_explicit_reference_names_itself(self):
        for ref in ("%1", "%2", "%@", "%-1"):
            rc, _o, err, _ = run(["windowraise", ref])
            self.assertEqual(rc, 1, ref)
            self.assertEqual(err.splitlines()[:2],
                             ["There are no windows in the stack",
                              "Invalid window '%s'" % ref], ref)
            self.assertTrue(err.splitlines()[2].startswith("Usage: windowraise"),
                            err)

    def test_the_usage_is_this_command_s_own_not_the_last_one_s(self):
        rc, _o, err, _ = run(["getwindowpid"])
        self.assertIn("Usage: getwindowpid", err)
        self.assertNotIn("windowraise", err)


class BehaveHelpTest(unittest.TestCase):
    """B: help and a wrong argument count are ours to answer; only the thing
    the compositor cannot do is refused. behave_screen_edge already did this."""

    def test_help_prints_the_usage_and_succeeds(self):
        rc, out, err, _ = run(["behave", "--help"])
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("Usage: behave window event action"))
        self.assertIn("mouse-enter", out)

    def test_too_few_arguments_is_the_count_message_plus_usage(self):
        for argv in (["behave"], ["behave", "1"], ["behave", "1", "blur"]):
            rc, out, err, _ = run(argv)
            self.assertEqual((rc, out), (1, ""), argv)
            self.assertTrue(
                err.startswith("Invalid number of arguments (minimum is 3)\n"
                               "Usage: behave window event action"), err)

    def test_three_arguments_still_reach_the_wayland_refusal(self):
        rc, _o, err, _ = run(["behave", "1", "blur", "getactivewindow"])
        self.assertEqual(rc, 1)
        self.assertIn("not supported on Wayland", err)


class WindowreparentHelpTest(unittest.TestCase):
    """An upstream quirk of that one command's option table: -h is help,
    --help is an error. Measured against xdotool 3.20160805.1."""

    def test_short_h_is_stdout_and_zero(self):
        rc, out, err, _ = run(["windowreparent", "-h"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "Usage: windowreparent "
                              "[window_source=%1] window_destination\n")

    def test_long_help_is_stderr_and_one(self):
        for flag in ("--help", "--hel", "--he"):
            rc, out, err, _ = run(["windowreparent", flag])
            self.assertEqual((rc, out), (1, ""), flag)
            self.assertEqual(err, "Usage: windowreparent "
                                  "[window_source=%1] window_destination\n",
                             flag)



class UnexercisedActionTest(unittest.TestCase):
    """windowfocus, windowquit, windowkill and windowlower: four commands the
    registry knew about and nothing here ever ran. Each is a different shape
    -- an alias with a --sync predicate, an alias without one, the only
    command that leaves the compositor entirely, and one the backend refuses
    -- and none of those shapes was covered by the commands that were."""

    def test_windowfocus_is_activate_and_takes_sync(self):
        rc, _o, _e, ctx = run(["windowfocus", "22"])
        self.assertEqual(rc, 0)
        self.assertEqual(ctx._backend.calls[-1], ("activate", 22))
        # --sync waits on the same `focused` flag activate() sets, so it
        # returns rather than spinning
        rc, _o, _e, ctx = run(["windowfocus", "--sync", "22"])
        self.assertEqual(rc, 0)
        self.assertTrue(ctx._backend.windows[22].focused)

    def test_windowfocus_defaults_to_the_stack(self):
        rc, _o, _e, ctx = run(["search", "--class", "beta", "windowfocus"])
        self.assertEqual(rc, 0)
        self.assertEqual(ctx._backend.calls[-1], ("activate", 22))

    def test_windowquit_closes_politely(self):
        # Wayland has one way to close a window, so quit is close -- but it
        # is its own command with its own usage line.
        rc, _o, _e, ctx = run(["windowquit", "22"])
        self.assertEqual(rc, 0)
        self.assertEqual(ctx._backend.calls[-1], ("close", 22))
        self.assertNotIn(22, ctx._backend.windows)

    def test_windowkill_signals_the_pid(self):
        killed = []
        with mock.patch.object(backend.os, "kill",
                               lambda pid, sig: killed.append((pid, sig))):
            rc, _o, _e, _ctx = run(["windowkill", "22"])
        self.assertEqual(rc, 0)
        self.assertEqual(killed, [(202, signal.SIGKILL)])

    def test_windowkill_without_a_pid_is_one_line(self):
        b = make_backend()
        b.windows[22].pid = 0
        rc, _o, err, _ctx = run(["windowkill", "22"], backend=b)
        self.assertEqual((rc, err), (1, "no pid for window 22\n"))

    def test_windowlower_is_refused_by_a_backend_without_it(self):
        rc, _o, err, _ctx = run(["windowlower", "22"])
        self.assertEqual(rc, 1)
        self.assertEqual(err, "windowlower is not supported by the fake "
                              "backend\n")

    def test_the_four_answer_h_with_their_own_name(self):
        for cmd, usage in (("windowfocus", "Usage: windowfocus [window=%1]\n"),
                           ("windowquit", "Usage: windowquit [window=%1]\n"),
                           ("windowkill", "Usage: windowkill [window=%1]\n"),
                           ("windowlower", "Usage: windowlower [window=%1]\n")):
            rc, out, _e, _ctx = run([cmd, "-h"])
            self.assertEqual(rc, 0, cmd)
            self.assertTrue(out.startswith(usage), (cmd, out))

# ---------------------------------------------------------------------------
# From the window/chain torture pass: window stack reference edge cases,
# empty-stack defaults on destructive commands, and the sway backend's
# floating-resize position restore.


class StackRefTest(unittest.TestCase):
    """%N resolution matches xdotool's window_list(): negative refs count
    from the end (index = len + N), out-of-range refs are one-line errors,
    never IndexError tracebacks."""

    def _ctx(self, stack):
        ctx = Context()
        ctx.stack = list(stack)
        return ctx

    def test_negative_in_range(self):
        # xdotool: index = nwindows + N, then windows[index - 1].
        ctx = self._ctx([10, 20, 30])
        self.assertEqual(ctx.resolve_window("%-1"), 20)
        self.assertEqual(ctx.resolve_window("%-2"), 10)

    def test_negative_out_of_range_is_cmderror(self):
        # Used to raise IndexError (traceback) instead of a clean CmdError.
        ctx = self._ctx([10, 20])
        with self.assertRaises(CmdError):
            ctx.resolve_window("%-2")
        with self.assertRaises(CmdError):
            ctx.resolve_window("%-5")

    def test_negative_out_of_range_in_chain_is_one_line(self):
        rc, out, err, _ = run(["search", "--class", "beta",
                               "getwindowname", "%-2"])
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("Invalid window stack reference '%-2'", err)

    def test_positive_out_of_range(self):
        ctx = self._ctx([10])
        with self.assertRaises(CmdError):
            ctx.resolve_window("%2")
        with self.assertRaises(CmdError):
            ctx.resolve_window("%0")


class PathologicalRegexTest(unittest.TestCase):
    """re.compile raises RecursionError / OverflowError (not re.error) on
    some patterns; search must fail like a bad regex, not traceback."""

    def test_deeply_nested_groups(self):
        pattern = "(a" * 2000 + ")" * 2000
        rc, out, err, _ = run(["search", "--class", pattern])
        self.assertEqual(rc, 1)
        self.assertIn("Failed to compile regex", err)
        self.assertEqual(out, "")

    def test_huge_repetition_count(self):
        rc, _out, err, _ = run(["search", "--class", "a{99999999999}"])
        self.assertEqual(rc, 1)
        self.assertIn("Failed to compile regex", err)


class EmptyStackDefaultTest(unittest.TestCase):
    """An omitted window argument defaults to %1; with an empty stack that
    is an error, exactly like xdotool (manpage COMMAND CHAINING: 'These
    would error: xdotool windowactivate'). The old focused-window fallback
    made `wdotool windowclose` close the focused window."""

    def test_windowclose_without_stack_errors(self):
        backend = make_backend()
        rc, _out, err, _ = run(["windowclose"], backend=backend)
        self.assertEqual(rc, 1)
        self.assertIn("There are no windows in the stack", err)
        # Nothing was closed.
        self.assertNotIn(("close", 11), backend.calls)

    def test_windowclose_with_stack_uses_percent1(self):
        backend = make_backend()
        rc, _out, _err, _ = run(["search", "--class", "beta", "windowclose"],
                                backend=backend)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in backend.calls if c[0] == "close"],
                         [("close", 22)])

    def test_windowactivate_without_stack_errors(self):
        rc, _out, err, _ = run(["windowactivate"])
        self.assertEqual(rc, 1)
        self.assertIn("There are no windows in the stack", err)

    def test_resolve_window_explicit_id_still_works(self):
        ctx = Context()
        self.assertEqual(ctx.resolve_window("0x2a"), 42)


class WindowmapSyncTest(unittest.TestCase):
    """windowmap --sync must wait on map state, not visibility: a window on
    an unfocused workspace is mapped but not visible, and the old
    visibility-based predicate hung forever."""

    def test_windowmap_sync_mapped_but_not_visible(self):
        backend = make_backend()
        # Window 33 is visible=False (think: unfocused workspace) but the
        # backend reports it mapped.
        backend.is_mapped = lambda wid: True
        rc, _o, _e, _ = run(["windowmap", "--sync", "33"], backend=backend)
        self.assertEqual(rc, 0)  # returns instead of spinning forever

    def test_windowunmap_sync_uses_map_state(self):
        backend = make_backend()
        mapped = {11: True}
        backend.is_mapped = lambda wid: mapped[wid]
        real_unmap = backend.unmap

        def unmap(wid):
            real_unmap(wid)
            mapped[wid] = False

        backend.unmap = unmap
        rc, _o, _e, _ = run(["windowunmap", "--sync", "11"], backend=backend)
        self.assertEqual(rc, 0)
        self.assertFalse(mapped[11])

    def test_sway_is_mapped(self):
        from hacks.window.backend_sway import SCRATCHPAD_WS, SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b._node = lambda wid: ({}, None, False, SCRATCHPAD_WS)
        self.assertFalse(b.is_mapped(1))
        b._node = lambda wid: ({}, None, False, "2")
        self.assertTrue(b.is_mapped(1))


class SwayResizeRestoreTest(unittest.TestCase):
    """SwayBackend.resize keeps a floating window's top-left corner fixed
    (sway resizes floating containers around their center; X11 xdotool
    keeps the origin)."""

    def _backend(self, floating):
        from hacks.window.backend_sway import SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b.commands = []
        # `__new__` runs no constructor, so the dialect cache has to be planted: since 2026-09-09 the
        # refusals open with `dialect()` (an i3 session may not be told about sway), and an unset
        # `_dialect` would send this double down a GET_VERSION round trip it has no socket for.
        b._dialect = "sway"
        node = {"id": 7, "deco_rect": {"height": 0}}
        from hacks.window.backend import Window

        win = Window(id=7, x=100, y=50, w=400, h=300)
        b._node = lambda wid: (node, win, floating, "ws")
        b.run = lambda cmd: b.commands.append(cmd)
        return b

    def test_floating_resize_restores_position(self):
        b = self._backend(floating=True)
        b.resize(7, 500, 400)
        self.assertEqual(b.commands, [
            "[con_id=7] resize set 500 px 400 px",
            "[con_id=7] move absolute position 100 50",
        ])

    def test_tiled_resize_is_refused(self):
        """`resize set` on a tiled container moves the split ratio instead of
        sizing the window: one axis lands somewhere else, the other does not
        move at all, and nothing said so. Refused now, like a tiled move --
        and softly, so windowsize warns and exits 0."""
        from wdotool.ctx import SoftCmdError

        b = self._backend(floating=False)
        with self.assertRaises(SoftCmdError) as cm:
            b.resize(7, 500, 400)
        self.assertIn("floating enable", str(cm.exception))
        self.assertEqual(b.commands, [])


class SwayFullscreenGuardTest(unittest.TestCase):
    """sway ignores move/resize on fullscreen containers, which used to
    make windowsize/windowmove --sync spin forever; the backend now
    refuses with a CmdError instead."""

    def _backend(self):
        from hacks.window.backend import Window
        from hacks.window.backend_sway import SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b.commands = []
        b._dialect = "sway"                    # see SwayResizeRestoreTest._backend
        node = {"id": 7, "deco_rect": {"height": 0}, "fullscreen_mode": 1}
        win = Window(id=7, x=0, y=0, w=1280, h=720)
        b._node = lambda wid: (node, win, True, "3")
        b.run = lambda cmd: b.commands.append(cmd)
        return b

    def test_resize_fullscreen_refused(self):
        b = self._backend()
        with self.assertRaises(CmdError):
            b.resize(7, 500, 300)
        self.assertEqual(b.commands, [])

    def test_move_fullscreen_refused(self):
        b = self._backend()
        with self.assertRaises(CmdError):
            b.move_window(7, 10, 10)
        self.assertEqual(b.commands, [])


if __name__ == "__main__":
    unittest.main()
