#!/usr/bin/env python3
"""behave: the per-window watch loops (focus/blur off events(), mouse-enter/leave off a pointer poll), the
click gap that names its rung, and the selectwindow pickers the wlr/cosmic floors and Cinnamon grew.

No compositor: the event stream is a finite iterator, the pointer is a scripted source, and the Cinnamon picker
is a scripted Eval, so every event and every edge is exact and nothing blocks."""

import io
import contextlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["W11_PASSTHROUGH"] = "never"
os.environ.setdefault("WDOTOOL_LAYOUT", "us")

from w11common.errors import CmdError
from wdotool import cli, window_cmds
from wdotool.ctx import Context
from hacks.window import backend as backend_mod
from hacks.window.backend import Window, WindowBackend, poll_diff_events
from hacks.window.backend_wlr import XPlaneViews


class _RecordActions:
    """Swap cli.run_behave_action for a recorder, so the loops can be watched without running a real chain."""

    def __enter__(self):
        self.fires = []
        self._orig = cli.run_behave_action
        cli.run_behave_action = lambda ctx, tokens, wid=None: self.fires.append((tuple(tokens), wid))
        return self

    def __exit__(self, *exc):
        cli.run_behave_action = self._orig


ACTION = ["key", "a"]


class FocusLoopTest(unittest.TestCase):
    def loop(self, event, stream, focused=0, targets=(22,)):
        with _RecordActions() as rec:
            window_cmds._behave_focus_loop(None, list(targets), event, ACTION, iter(stream), focused)
        return rec.fires

    def test_focus_fires_when_the_target_takes_focus(self):
        fires = self.loop("focus", [(22, "focus")], focused=11)
        self.assertEqual(fires, [(tuple(ACTION), 22)])

    def test_focus_ignores_a_focus_event_for_another_window(self):
        self.assertEqual(self.loop("focus", [(33, "focus")], focused=11), [])

    def test_focus_does_not_fire_if_the_target_was_already_focused(self):
        # seeded as focused: no rising edge, so no fire (xdotool fires on the transition).
        self.assertEqual(self.loop("focus", [(22, "focus")], focused=22), [])

    def test_blur_fires_when_focus_leaves_the_target(self):
        fires = self.loop("blur", [(11, "focus")], focused=22)
        self.assertEqual(fires, [(tuple(ACTION), 22)])

    def test_blur_ignores_focus_moving_between_other_windows(self):
        self.assertEqual(self.loop("blur", [(33, "focus")], focused=11), [])

    def test_non_focus_changes_are_skipped(self):
        self.assertEqual(self.loop("focus", [(22, "title"), (22, "new")], focused=11), [])

    def test_focus_also_fires_when_the_target_loses_focus(self):
        # xdotool's bug, reproduced: `behave W focus` and `behave W blur` share one FocusChangeMask, so `focus`
        # fires on W's FocusOut too.  Target 22 held focus and 11 takes it -> the action runs against 22.
        self.assertEqual(self.loop("focus", [(11, "focus")], focused=22), [(tuple(ACTION), 22)])

    def test_blur_also_fires_when_the_target_gains_focus(self):
        # The mirror of the above: `blur` fires on the target's FocusIn as well.
        self.assertEqual(self.loop("blur", [(22, "focus")], focused=11), [(tuple(ACTION), 22)])


class _Snap:
    """A backend whose list()/pointer() walk scripted snapshots, one per poll."""

    def __init__(self, wins_seq, pos_seq):
        self.wins_seq, self.pos_seq = list(wins_seq), list(pos_seq)
        self.i_w = self.i_p = 0

    def list(self):
        w = self.wins_seq[min(self.i_w, len(self.wins_seq) - 1)]
        self.i_w += 1
        return w


class MouseLoopTest(unittest.TestCase):
    WIN = Window(id=22, x=100, y=100, w=100, h=100, visible=True, focused=True)  # covers 100..199

    def loop(self, event, positions):
        it = iter(positions)

        def source():
            return next(it)   # StopIteration ends the loop

        def list_fn():
            return [self.WIN]
        with _RecordActions() as rec:
            window_cmds._behave_mouse_loop(None, [22], event, ACTION, source, list_fn,
                                           poll=0.0, sleep=lambda _s: None)
        return rec.fires

    def test_enter_fires_on_the_outside_to_inside_transition(self):
        fires = self.loop("mouse-enter", [(0, 0), (150, 150)])
        self.assertEqual(fires, [(tuple(ACTION), 22)])

    def test_enter_does_not_fire_when_the_pointer_starts_inside(self):
        # the first sample only seeds; a window the pointer already sits in must not fire an enter.
        self.assertEqual(self.loop("mouse-enter", [(150, 150), (150, 150)]), [])

    def test_leave_fires_on_the_inside_to_outside_transition(self):
        fires = self.loop("mouse-leave", [(150, 150), (0, 0)])
        self.assertEqual(fires, [(tuple(ACTION), 22)])

    def test_leave_does_not_fire_while_the_pointer_stays_inside(self):
        self.assertEqual(self.loop("mouse-leave", [(150, 150), (160, 160)]), [])

    def test_an_unknown_pointer_reads_as_outside(self):
        # None sample -> hit_test target 0 -> a leave from inside fires.
        fires = self.loop("mouse-leave", [(150, 150), None])
        self.assertEqual(fires, [(tuple(ACTION), 22)])


class _FakeCtx(Context):
    def __init__(self, wins):
        super().__init__()
        self._backend = _Backend(wins)


class _Backend(WindowBackend):
    name = "b"

    def __init__(self, wins):
        self.wins = list(wins)

    def list(self):
        return self.wins

    def activate(self, wid):
        pass


class CmdBehaveTest(unittest.TestCase):
    def run_cmd(self, argv):
        ctx = _FakeCtx([Window(id=11, focused=True), Window(id=22)])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run_chain(ctx, "wdotool", argv)
        return rc, err.getvalue()

    def test_mouse_click_names_route_four(self):
        rc, err = self.run_cmd(["behave", "11", "mouse-click", "getactivewindow"])
        self.assertEqual(rc, 1)
        self.assertIn("AGENTS.md route 4", err)
        self.assertIn("not yet wired", err)

    def test_unknown_event_is_named(self):
        rc, err = self.run_cmd(["behave", "11", "sideways", "getactivewindow"])
        self.assertEqual(rc, 1)
        self.assertIn("Unknown event name: sideways", err)


class PollDiffEventsTest(unittest.TestCase):
    """The wlr/cosmic events() vocabulary from a list() diff: new/focus/title/close."""

    def collect(self, snapshots, n):
        seq = list(snapshots)
        i = [0]

        def list_fn():
            s = seq[min(i[0], len(seq) - 1)]
            i[0] += 1
            return s

        got = []
        for ev in poll_diff_events(list_fn, timeout=None, poll=0.0):
            got.append(ev)
            if len(got) >= n:
                break
        return got

    def test_a_new_focused_window_gets_new_then_focus(self):
        got = self.collect([[], [Window(id=5, focused=True)]], 2)
        self.assertEqual(got, [(5, "new"), (5, "focus")])

    def test_a_title_change_is_reported(self):
        got = self.collect([[Window(id=5, title="a")], [Window(id=5, title="b")]], 1)
        self.assertEqual(got, [(5, "title")])

    def test_a_focus_change_and_a_close_are_reported(self):
        got = self.collect(
            [[Window(id=5, focused=False), Window(id=6)],
             [Window(id=5, focused=True)]], 2)
        self.assertIn((5, "focus"), got)
        self.assertIn((6, "close"), got)


class _Floor(XPlaneViews, WindowBackend):
    """A minimal foreign-toplevel floor: XPlaneViews.select_window over scripted list() snapshots."""

    name = "floor"

    def __init__(self, snapshots):
        self.snaps = list(snapshots)
        self.i = 0

    def list(self):
        s = self.snaps[min(self.i, len(self.snaps) - 1)]
        self.i += 1
        return s

    def activate(self, wid):
        pass


class FloorSelectWindowTest(unittest.TestCase):
    def setUp(self):
        self._sleep = backend_mod.time.sleep
        backend_mod.time.sleep = lambda *_a: None

    def tearDown(self):
        backend_mod.time.sleep = self._sleep

    def test_select_window_waits_for_the_next_activation(self):
        b = _Floor([[Window(id=5, focused=False)],
                    [Window(id=5, focused=True)]])
        self.assertEqual(b.select_window(), 5)

    def test_the_hint_is_to_focus_not_to_click(self):
        self.assertIn("focus", _Floor([[]]).select_window_hint)


class CinnamonPickerTest(unittest.TestCase):
    """selectwindow on Cinnamon: the Eval reactive-actor picker (validated live on resolute-cinnamon-wayland,
    where a QMP click at ~520,360 was read back as [519,359,1])."""

    def backend(self, poll_answers, list_wins):
        # poll_answers are the single-encoded JSON strings _eval returns for PICK_POLL (a JSON.stringify(...)
        # program), which select_window reads through _json; "null" decodes to None (still pending).
        from hacks.window.backend_cinnamon import CinnamonBackend
        from hacks.window import cinnamon_js as js
        b = object.__new__(CinnamonBackend)
        b.PICK_POLL = 0.0
        b.PICK_TIMEOUT = 5.0
        answers = iter(poll_answers)
        self.evals = []

        def fake_eval(script):
            self.evals.append(script)
            if script in (js.PICK_INSTALL, js.PICK_DESTROY):
                return "ok"
            if script == js.PICK_POLL:
                return next(answers)
            raise AssertionError("unexpected script")

        b._eval = fake_eval
        b.list = lambda: list_wins
        return b, js

    def test_a_click_returns_the_window_under_it(self):
        win = Window(id=7, x=100, y=200, w=200, h=200, visible=True, focused=True)  # covers x100..299 y200..399
        b, _js = self.backend(["null", "null", "[150,250,1]"], [win])
        self.assertEqual(b.select_window(), 7)

    def test_a_click_on_no_window_returns_zero(self):
        b, _js = self.backend(["[10,10,1]"], [])
        self.assertEqual(b.select_window(), 0)

    def test_ctrl_c_tears_the_actor_down(self):
        from hacks.window import cinnamon_js as js
        b, _js = self.backend([], [])

        def boom(script):
            self.evals.append(script)
            if script == js.PICK_POLL:
                raise KeyboardInterrupt
            return "ok"

        b._eval = boom
        with self.assertRaises(KeyboardInterrupt):
            b.select_window()
        self.assertIn(js.PICK_DESTROY, self.evals)

    def test_a_timeout_tears_the_actor_down_and_raises(self):
        from hacks.window import cinnamon_js as js
        b, _js = self.backend(["null"], [])
        b.PICK_TIMEOUT = 0.0
        with self.assertRaises(CmdError) as cm:
            b.select_window()
        self.assertIn("timed out", str(cm.exception))
        self.assertIn(js.PICK_DESTROY, self.evals)


if __name__ == "__main__":
    unittest.main()
