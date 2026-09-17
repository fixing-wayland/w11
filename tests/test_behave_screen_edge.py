#!/usr/bin/env python3
"""behave_screen_edge: the edge/corner geometry, the dwell/cooldown state machine, and the poll loop wiring.

No compositor and no clock: `EdgeMachine` is handed `(x, y, now)` samples with a scripted time, and the loop is
handed a finite pointer source with a scripted clock, so every timing rule is exact and nothing sleeps."""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["W11_PASSTHROUGH"] = "never"
os.environ.setdefault("WDOTOOL_LAYOUT", "us")

from wdotool import cli, input_cmds
from wdotool.ctx import Context
from hacks.input.edges import EDGES, EdgeMachine, in_edge
from hacks.window.backend import Window, WindowBackend

BOX = (0, 0, 200, 100)  # x, y, w, h -> right column x==199, bottom row y==99


class InEdgeTest(unittest.TestCase):
    def test_every_edge_and_corner_is_the_outermost_row_or_column(self):
        cases = {
            "left": [((0, 50), True), ((1, 50), False), ((0, 0), True), ((0, 99), True)],
            "right": [((199, 50), True), ((198, 50), False)],
            "top": [((100, 0), True), ((100, 1), False)],
            "bottom": [((100, 99), True), ((100, 98), False)],
            "top-left": [((0, 0), True), ((0, 1), False), ((1, 0), False)],
            "top-right": [((199, 0), True), ((199, 1), False)],
            "bottom-left": [((0, 99), True), ((1, 99), False)],
            "bottom-right": [((199, 99), True), ((198, 99), False)],
        }
        self.assertEqual(set(cases), EDGES)
        for edge, points in cases.items():
            for (x, y), want in points:
                self.assertEqual(in_edge(edge, BOX, x, y), want, (edge, x, y))

    def test_left_column_does_not_stretch_past_the_box_vertically(self):
        # x is on the left column but y is below the box: not the left edge (nor the corner).
        self.assertFalse(in_edge("left", BOX, 0, 500))

    def test_an_empty_box_is_never_an_edge(self):
        self.assertFalse(in_edge("left", (0, 0, 0, 0), 0, 0))

    def test_a_negative_origin_moves_the_edges_with_the_box(self):
        # Two heads laid out as Virtual-1 at (-1920,0) and Virtual-2 at (0,0), the shape the wlroots rig
        # runs (tests/test_vptr.py's BBOX).  The box's own origin is where `left` is, and x==1919 -- the last
        # column that exists -- is `right`; x==3839 is off the layout entirely.  `_screen_box` in
        # wdotool/input_cmds.py is what hands this function the origin.
        box = (-1920, 0, 3840, 1080)
        self.assertTrue(in_edge("right", box, 1919, 10))
        self.assertFalse(in_edge("right", box, 1918, 10))
        self.assertTrue(in_edge("left", box, -1920, 10))
        self.assertFalse(in_edge("left", box, 0, 10))
        self.assertTrue(in_edge("top-left", box, -1920, 0))
        # And the same samples against the origin-less box the command used to build: the right edge moves to
        # x==3839, a column this layout does not have, so it never fires, while `left` swallows every x <= 0,
        # i.e. the whole left-hand head.  (x==3839 is still "right" of the shifted box above: `>=` is
        # deliberate in edges.py, for a pointer query that reports the boundary pixel itself.)
        self.assertFalse(in_edge("right", (0, 0, 3840, 1080), 1919, 10))
        self.assertTrue(in_edge("left", (0, 0, 3840, 1080), 0, 10))


class EdgeMachineTest(unittest.TestCase):
    def test_a_bare_entry_fires_at_once(self):
        m = EdgeMachine("left", BOX)
        self.assertTrue(m.feed(0, 50, now=0.0))

    def test_leaving_the_edge_does_not_fire(self):
        m = EdgeMachine("left", BOX)
        self.assertFalse(m.feed(100, 50, now=0.0))

    def test_an_unknown_pointer_is_not_on_the_edge(self):
        m = EdgeMachine("left", BOX)
        self.assertFalse(m.feed(None, None, now=0.0))

    def test_delay_needs_the_pointer_to_dwell(self):
        m = EdgeMachine("left", BOX, delay_ms=500)
        self.assertFalse(m.feed(0, 50, now=0.0))        # just arrived
        self.assertFalse(m.feed(0, 50, now=0.3))        # 300 ms < 500
        self.assertTrue(m.feed(0, 50, now=0.5))         # 500 ms dwell reached

    def test_leaving_resets_the_delay_timer(self):
        m = EdgeMachine("left", BOX, delay_ms=500)
        self.assertFalse(m.feed(0, 50, now=0.0))
        self.assertFalse(m.feed(0, 50, now=0.4))
        self.assertFalse(m.feed(100, 50, now=0.45))     # left the edge: timer reset
        self.assertFalse(m.feed(0, 50, now=0.5))        # re-entered, dwell starts over
        self.assertTrue(m.feed(0, 50, now=1.0))         # 500 ms after re-entry

    def test_quiesce_does_not_refire_a_resting_pointer_after_the_cooldown(self):
        # A resting pointer never refires, cooldown or no -- only a leave-and-return does, and then only past
        # the cooldown.  (The old sample-refire machine fired at 2.5 s here; xdotool needs the return.)
        m = EdgeMachine("left", BOX, quiesce_ms=2000)
        self.assertTrue(m.feed(0, 50, now=0.0))         # first entry fires
        self.assertFalse(m.feed(0, 50, now=1.0))        # resting, 1 s < 2 s cooldown
        self.assertFalse(m.feed(0, 50, now=2.5))        # resting, past the cooldown: still no refire
        self.assertFalse(m.feed(100, 50, now=3.0))      # left the edge
        self.assertTrue(m.feed(0, 50, now=3.1))         # re-entry past the cooldown: fires

    def test_a_resting_pointer_fires_once_per_entry(self):
        # A pointer that sits on the edge fires exactly once; only leaving and returning fires it again.
        m = EdgeMachine("left", BOX, quiesce_ms=0)
        self.assertTrue(m.feed(0, 50, now=0.0))         # entry fires
        self.assertFalse(m.feed(0, 50, now=0.01))       # still resting: no refire
        self.assertFalse(m.feed(0, 50, now=0.5))
        self.assertFalse(m.feed(0, 50, now=3.0))
        self.assertFalse(m.feed(100, 50, now=3.1))      # left the edge
        self.assertTrue(m.feed(0, 50, now=3.2))         # re-entry fires again


class _Clock:
    def __init__(self, times):
        self.times = list(times)
        self.i = 0

    def __call__(self):
        t = self.times[min(self.i, len(self.times) - 1)]
        self.i += 1
        return t


class EdgeLoopTest(unittest.TestCase):
    def setUp(self):
        self.fires = []
        self._orig = cli.run_behave_action
        cli.run_behave_action = lambda ctx, tokens, wid=None: self.fires.append((tuple(tokens), wid)) or 0

    def tearDown(self):
        cli.run_behave_action = self._orig

    def run_loop(self, positions, times, machine):
        it = iter(positions)
        clock = _Clock(times)
        input_cmds._behave_edge_loop(
            None, lambda: next(it), machine, ["key", "a"],
            poll=0.0, clock=clock, sleep=lambda _s: None)

    def test_the_loop_runs_the_action_on_every_fire_and_ends_with_the_source(self):
        # left edge, no delay, no cooldown: two samples on the edge fire twice, the middle one off does not.
        m = EdgeMachine("left", BOX, quiesce_ms=0)
        self.run_loop([(0, 50), (100, 50), (0, 50)], [0.0, 0.1, 0.2], m)
        self.assertEqual(self.fires, [(("key", "a"), None), (("key", "a"), None)])

    def test_an_unknown_sample_does_not_fire(self):
        m = EdgeMachine("left", BOX, quiesce_ms=0)
        self.run_loop([None, (0, 50)], [0.0, 0.1], m)
        self.assertEqual(self.fires, [(("key", "a"), None)])


class ActionSubstitutionTest(unittest.TestCase):
    """`run_behave_action` runs the action as a chain, pushing the triggering window as the whole stack so the
    chain's `%1`/`%@` name it -- the same substitution behave and behave_screen_edge share."""

    def _ctx(self):
        ctx = Context()
        ctx.prog = "wdotool"
        ctx._backend = _NamedBackend()
        return ctx

    def test_percent_one_resolves_to_the_triggering_window(self):
        ctx = self._ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.run_behave_action(ctx, ["getwindowname", "%1"], wid=22)
        self.assertEqual((rc, out.getvalue()), (0, "Beta Two\n"))

    def test_percent_at_resolves_to_the_triggering_window(self):
        ctx = self._ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.run_behave_action(ctx, ["getwindowname", "%@"], wid=11)
        self.assertEqual(out.getvalue(), "Alpha One\n")

    def test_the_stack_is_restored_after_the_action(self):
        ctx = self._ctx()
        ctx.stack = [999]
        with contextlib.redirect_stdout(io.StringIO()):
            cli.run_behave_action(ctx, ["getwindowname", "%1"], wid=22)
        self.assertEqual(ctx.stack, [999])


class _NamedBackend(WindowBackend):
    name = "named-fake"

    def list(self):
        return [Window(id=11, title="Alpha One"), Window(id=22, title="Beta Two")]

    def activate(self, wid):
        pass


class NoPointerTest(unittest.TestCase):
    def test_a_query_backend_with_a_position_does_not_refuse(self):
        # sanity: the refusal is only for a backend that publishes NO position -- one that answers a pointer
        # does not hit EDGE_NO_POINTER (the loop would run forever, so this only checks the pre-loop guard by
        # constructing the machine path up to the first sample via _edge_sample).
        ctx = Context()
        ctx._backend = _PointerBackend()
        self.assertEqual(input_cmds._edge_sample(ctx), (10, 20))


class _PointerBackend(WindowBackend):
    name = "ptr"

    def list(self):
        return []

    def activate(self, wid):
        pass

    def pointer(self):
        return (10, 20)


if __name__ == "__main__":
    unittest.main()
