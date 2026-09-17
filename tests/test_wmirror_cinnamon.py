#!/usr/bin/env python3
"""wmirror on Cinnamon (Muffin): the region/cross-size mirror over org.Cinnamon.Eval -- AGENTS.md route 2.

Cinnamon publishes neither wl-mirror capture protocol, and muffin 6.4 has the ScreenCast machinery compiled
out (measured -- see tests/test_wmirror_screencast.py and docs/WMIRROR.md). So the mirror does not capture at
all: it puts a black, clipped viewport the size of the target head on `global.stage` at its origin and, inside
it, a clone group scaled by `--scaling`'s exact ratio and centred, filled with one `Clutter.Clone` per
on-screen `Meta.WindowActor`/`Meta.BackgroundActor` (walked from `global.window_group` and
`global.top_window_group`) plus the `panel`, each offset by the region origin. Measured on
`resolute-cinnamon-wayland` (Cinnamon 6.4.13 / muffin 6.4.1): 2026-09-14, the 1:1-at-origin program of that
day, region `1000x700+0+0` of monitor 0 onto monitor 1's origin, `compare -metric AE` 0 / RMSE 0 against head
0's crop, live, over a native Wayland window, the XWayland desktop/wallpaper actors and the panel; 2026-09-17,
the scaled program, Virtual-2 at 1280x1024, fit 1280x896+0+64 / cover the head filled / exact
1000x700+140+162 at AE 0 (docs/WMIRROR.md).

This file pins the JS the backend sends (hacks/window/cinnamon_js.py's interpolation rule: only integers ever
go in) and the lifetime bookkeeping (start writes a kind/token record with no pid; stop destroys over Eval;
reap drops a mirror the compositor has dropped) the way tests/test_backend_cinnamon.py tests the window plane.
The live pixel proof is vm/live-smoke.d/cinnamon-wayland.sh.
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from fractions import Fraction as F
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["W11_PASSTHROUGH"] = "never"

from w11common import session  # noqa: E402
from wmirror import cli  # noqa: E402
from hacks.mirror import cinnamon, core  # noqa: E402


class _Out:
    """A stand-in for a wxcore.OutputState: the mirror only reads .x/.y (the target origin) and .rect()."""

    def __init__(self, name, x=0, y=0, w=1920, h=1080, active=True):
        self.name, self.x, self.y, self.w, self.h, self.active = name, x, y, w, h, active

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class FakeEval:
    """A muffin that remembers which tokens are live, driven from a class-level registry so start/stop/status
    round trips see one another the way the real `global.__w11m` does."""

    live = set()
    seq = 0
    scripts = []

    def __init__(self, bus=None):
        pass

    def eval(self, script):
        FakeEval.scripts.append(script)
        if "global.__w11m[tok]={grp" in script:            # build_scaled_program
            FakeEval.seq += 1
            FakeEval.live.add(FakeEval.seq)
            return FakeEval.seq
        m = re.search(r"var tok=(\d+);if\(global\.__w11m&&global\.__w11m\[tok\]\)\{var m", script)
        if m:                                              # destroy_program
            tok = int(m.group(1))
            if tok in FakeEval.live:
                FakeEval.live.discard(tok)
                return "ok"
            return "gone"
        raise AssertionError("unexpected eval: %s" % script[:80])

    def json(self, script):                                # status_program
        FakeEval.scripts.append(script)
        m = re.search(r"var tok=(\d+);return JSON\.stringify", script)
        assert m, script[:80]
        return int(m.group(1)) in FakeEval.live


#: The nine content boxes wl-mirror 0.18.5 actually draws, `(rw, rh, tw, th, mode, (sn, sd), (x, y, w, h))`
#: -- the unit table both paths answer to. Sampled from a screendump of the target head under sway 1.11
#: headless with `grim` (2026-09-17); the first three rows are the 1920x1080 -> 1280x1024 boxes docs/WMIRROR.md
#: has carried since the wl-mirror path was written. `Fraction`s compare equal to the ints, so a row's box is
#: written the exact way and `core.content_box`'s tuple matches it whole.
SCALE_TABLE = [
    (1920, 1080, 1280, 1024, "fit", (2, 3), (0, 152, 1280, 720)),
    (1920, 1080, 1280, 1024, "cover", (128, 135), (-F(2432, 9), 0, F(16384, 9), 1024)),
    (1920, 1080, 1280, 1024, "exact", (1, 2), (160, 242, 960, 540)),
    (800, 600, 1920, 1080, "fit", (9, 5), (240, 0, 1440, 1080)),
    (800, 600, 1920, 1080, "cover", (12, 5), (0, -180, 1920, 1440)),
    (800, 600, 1920, 1080, "exact", (1, 1), (560, 240, 800, 600)),        # 1x: it fits, it is not doubled
    (400, 300, 1920, 1080, "exact", (3, 1), (360, 90, 1200, 900)),        # 3x, not 4.8x: whole factors only
    (1920, 1080, 700, 480, "exact", (1, 3), (30, 60, 640, 360)),          # down: 1/ceil(1/fit), not 1/2
    (801, 601, 1920, 1080, "exact", (1, 1), (F(1119, 2), F(479, 2), 801, 601)),   # the half-pixel centre
]


class Programs(unittest.TestCase):
    """The JS strings, and the one rule they must obey: only integers are interpolated."""

    def test_scale_plan_is_the_measured_wl_mirror_table(self):
        """The nine boxes wl-mirror 0.18.5 draws, as exact ratios of the four integers. This is the oracle
        both paths answer to: the wl-mirror paths because wl-mirror computes them, this one because
        `core.scale_plan` has to reproduce them before `set_scale` can be trusted to draw them."""
        for rw, rh, tw, th, mode, plan, box in SCALE_TABLE:
            with self.subTest(region=(rw, rh), target=(tw, th), mode=mode):
                self.assertEqual(core.scale_plan(rw, rh, tw, th, mode), plan)
                self.assertEqual(core.content_box(rw, rh, tw, th, mode), box)

    def test_scale_plan_invariants(self):
        """What the three modes mean, apart from any one measurement: a target the size of the region is 1:1
        in all three, `cover` is never smaller than `fit`, `exact` is never larger, `exact` is a whole factor
        or the reciprocal of one, every plan is a pair of ints, and a mode or a side that cannot be drawn is a
        ValueError rather than a silently wrong picture."""
        for mode in core.SCALINGS:
            self.assertEqual(core.scale_plan(1280, 1024, 1280, 1024, mode), (1, 1), mode)
        for rw, rh, tw, th in ((1920, 1080, 1280, 1024), (800, 600, 1920, 1080), (801, 601, 1920, 1080),
                               (400, 300, 1920, 1080), (1920, 1080, 700, 480)):
            with self.subTest(region=(rw, rh), target=(tw, th)):
                fit = F(*core.scale_plan(rw, rh, tw, th, "fit"))
                cover = F(*core.scale_plan(rw, rh, tw, th, "cover"))
                exact = F(*core.scale_plan(rw, rh, tw, th, "exact"))
                self.assertGreaterEqual(cover, fit)
                self.assertLessEqual(exact, fit)
                self.assertTrue(exact.numerator == 1 or exact.denominator == 1, exact)
                for plan in (fit, cover, exact):
                    self.assertIsInstance(plan.numerator, int)
                    self.assertIsInstance(plan.denominator, int)
        with self.assertRaises(ValueError):
            core.scale_plan(800, 600, 1920, 1080, "linear")     # wl-mirror's filter half, not a scaling
        with self.assertRaises(ValueError):
            core.scale_plan(800, 0, 1920, 1080, "fit")

    def test_build_interpolates_ten_ints(self):
        prog = cinnamon.build_scaled_program((10, 20, 300, 400), (1280, 0, 1920, 1080), "fit")
        self.assertIn("RX=10,RY=20,RW=300,RH=400,TX=1280,TY=0,TW=1920,TH=1080,SN=27,SD=10;", prog)

    def test_build_coerces_through_int_and_leaves_no_format_holes(self):
        """The interpolation rule: floats are forced through int(), and nothing but numbers reaches the
        program -- so a program built from odd inputs is still all-integer JS, never a stray %d, a string or a
        decimal point (the scale itself is the pair SN/SD, divided inside the compositor)."""
        prog = cinnamon.build_scaled_program((1.9, 2.9, 300.0, 400.0), (1280.5, 0.0, 1920.0, 1080.0), "fit")
        self.assertIn("RX=1,RY=2,RW=300,RH=400,TX=1280,TY=0,TW=1920,TH=1080,", prog)
        self.assertNotIn("%d", prog)
        self.assertIsNone(re.search(r"\d\.\d", prog))

    def test_build_is_the_per_actor_walk_not_the_uigroup_shortcut(self):
        """The working pattern is one Clone per Meta actor found under window_group + top_window_group, plus
        the panel. The magnifier-style single `Clone` of `Main.uiGroup` does NOT mirror across heads (measured
        AE 700000/700000 on the rig -- only the panel comes through), so it must not be what we send. The
        viewport around it is the parity item: wl-mirror blacks out the whole target head, and the 1:1 group
        that used to be the only actor let the target's own desktop show around the picture."""
        prog = cinnamon.build_scaled_program((0, 0, 800, 600), (0, 0, 1280, 1024), "fit")
        for needle in ("global.window_group", "global.top_window_group",
                       "M.WindowActor", "M.BackgroundActor",
                       "get_transformed_position", "new C.Clone(",
                       "name==='panel'", "clip_to_allocation:true",
                       "background_color:new C.Color({red:0,green:0,blue:0,alpha:255})",
                       "grp.set_pivot_point(0,0);grp.set_scale(s,s);",
                       "vp.add_child(grp)", "global.__w11m[tok]={grp:vp,"):
            self.assertIn(needle, prog, needle)
        self.assertNotIn("C.Clone({source:Main.uiGroup", prog)

    def test_build_stays_live_over_structural_changes(self):
        """A Clone tracks its source's content on its own; the tree's SHAPE (windows opening, closing,
        restacking) is what needs a re-walk, so the program connects `restacked` and `window-created`."""
        prog = cinnamon.build_scaled_program((0, 0, 800, 600), (0, 0, 1280, 1024), "fit")
        self.assertIn("connect('restacked'", prog)
        self.assertIn("connect('window-created'", prog)

    def test_destroy_disconnects_the_handlers_and_the_token_is_an_int(self):
        prog = cinnamon.destroy_program(7)
        self.assertIn("var tok=7;", prog)
        self.assertIn("global.display.disconnect(m.h1)", prog)
        self.assertIn("global.display.disconnect(m.h2)", prog)
        self.assertIn("m.grp.destroy()", prog)
        self.assertEqual(cinnamon.destroy_program("7"), prog)   # coerced through int()

    def test_status_reads_whether_the_group_is_still_parented(self):
        prog = cinnamon.status_program(3)
        self.assertIn("var tok=3;", prog)
        self.assertIn("get_parent()", prog)
        self.assertIn("JSON.stringify", prog)


class Lifetime(unittest.TestCase):
    def setUp(self):
        FakeEval.live = set()
        FakeEval.seq = 0
        FakeEval.scripts = []
        p = mock.patch.object(cinnamon, "Eval", FakeEval)
        p.start()
        self.addCleanup(p.stop)

    def test_start_writes_a_kind_token_record_with_no_pid(self):
        recs = {}
        err = cinnamon.start(recs, "A", "B", (0, 0, 800, 600), _Out("B", x=1280))
        self.assertIsNone(err)
        rec = recs["B"]
        self.assertEqual(rec["kind"], "cinnamon")
        self.assertEqual(rec["token"], 1)
        self.assertEqual(rec["source"], "A")
        self.assertEqual(rec["region"], [0, 0, 800, 600])
        self.assertNotIn("pid", rec)
        self.assertNotIn("helper_pid", rec)
        # and the mode the mirror is drawing, resolved by the caller: `fit` here as on every other path, so
        # `--list` says what the scaled group does rather than what the flag happened to be.
        self.assertEqual(rec["scaling"], "fit")

    def test_start_records_the_mode_it_was_asked_for(self):
        recs = {}
        cinnamon.start(recs, "A", "B", (0, 0, 800, 600), _Out("B", x=1280), scaling="cover")
        self.assertEqual(recs["B"]["scaling"], "cover")
        built = [s for s in FakeEval.scripts if "global.__w11m[tok]={grp" in s]
        self.assertIn("SN=12,SD=5;", built[0])         # cover of 800x600 onto 1920x1080: max(12/5, 9/5)

    def test_start_places_the_viewport_at_the_target_origin_and_size(self):
        """The viewport is the target head, not the region: that is what blacks the head out the way
        wl-mirror does and what the scaled group is centred inside."""
        recs = {}
        cinnamon.start(recs, "A", "B", (0, 0, 800, 600), _Out("B", x=1280, y=40))
        built = [s for s in FakeEval.scripts if "global.__w11m[tok]={grp" in s]
        self.assertEqual(len(built), 1)
        self.assertIn("TX=1280,TY=40,TW=1920,TH=1080", built[0])

    def test_alive_then_stop_then_not_alive(self):
        recs = {}
        cinnamon.start(recs, "A", "B", (0, 0, 800, 600), _Out("B", x=1280))
        rec = recs["B"]
        self.assertTrue(cinnamon.alive(rec))
        self.assertTrue(cinnamon.stop_record(rec))
        self.assertFalse(cinnamon.alive(rec))
        self.assertFalse(cinnamon.stop_record(rec))     # already gone: not an error, just False

    def test_reap_drops_a_mirror_the_compositor_has_dropped(self):
        recs = {}
        cinnamon.start(recs, "A", "B", (0, 0, 800, 600), _Out("B", x=1280))
        cinnamon.start(recs, "A", "C", (0, 0, 800, 600), _Out("C", x=1280))
        FakeEval.live.discard(recs["B"]["token"])          # muffin dropped B (a restart, a closed head)
        self.assertTrue(cinnamon.reap(recs))
        self.assertNotIn("B", recs)
        self.assertIn("C", recs)

    def test_reap_of_no_cinnamon_records_opens_no_bus(self):
        """A wl-mirror-only session must pay nothing: reap returns False without ever constructing an Eval
        client when there is no cinnamon record to check."""
        with mock.patch.object(cinnamon, "Eval",
                               side_effect=AssertionError("must not open a bus")):
            self.assertFalse(cinnamon.reap({"B": {"pid": 1, "helper_pid": 2}}))

    def test_fmt_record_names_the_route_and_token_not_a_pid(self):
        rec = {"kind": "cinnamon", "source": "A", "region": [0, 0, 800, 600],
               "scaling": "cover", "token": 5, "route": cinnamon.ROUTE}
        line = cinnamon.fmt_record("B", rec)
        self.assertIn("B <- A", line)
        self.assertIn("region 800x600+0+0", line)
        self.assertIn("scaling cover", line)
        self.assertIn("tok 5", line)
        self.assertIn("Eval", line)
        self.assertNotIn("wl-mirror", line)

    def test_fmt_record_of_a_record_with_no_mode_says_1_to_1(self):
        """A record with no `scaling` key was written by a tree whose program placed the clone group 1:1 at
        the target origin -- it is still on disk after an upgrade, and the line has to say what THAT mirror
        draws, not what the flag would mean today."""
        rec = {"kind": "cinnamon", "source": "A", "region": [0, 0, 800, 600],
               "token": 5, "route": cinnamon.ROUTE}
        line = cinnamon.fmt_record("B", rec)
        self.assertIn("scaling 1:1", line)


SOCKET = "/run/user/1000/wayland-0"


class Cli(unittest.TestCase):
    """The wmirror command line on a Cinnamon session: no wl-mirror binary, `org.Cinnamon` on the bus, the
    mirror built and torn down over the (faked) Eval client."""

    def setUp(self):
        FakeEval.live = set()
        FakeEval.seq = 0
        FakeEval.scripts = []
        self.tmp = tempfile.mkdtemp(prefix="wmirror-cin-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = mock.Mock()
        self.outputs = [_Out("A", x=0, y=0, w=1280, h=800),
                        _Out("B", x=1280, y=0, w=1920, h=1080)]
        self._p = [
            mock.patch.object(cinnamon, "Eval", FakeEval),
            mock.patch.object(cinnamon, "available", return_value=True),
            mock.patch.object(core, "find_helper", return_value=None),
            mock.patch.object(core, "open_conn", return_value=self.conn),
            mock.patch.object(core, "read_outputs", return_value=self.outputs),
            mock.patch.object(session, "find_wayland_socket",
                              return_value=(1000, "user", SOCKET)),
        ]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, argv):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(argv)
        return rc, o.getvalue(), e.getvalue()

    def test_start_builds_a_cinnamon_mirror_and_prints_the_route(self):
        rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0"])
        self.assertEqual(rc, 0, e)
        self.assertIn("B <- A", o)
        self.assertIn("region 800x600+0+0", o)
        self.assertIn("Eval", o)
        self.assertEqual(FakeEval.live, {1})               # one live mirror inside muffin
        recs = core.records(core.load_state())
        self.assertEqual(recs["B"]["kind"], "cinnamon")
        self.assertEqual(recs["B"]["token"], 1)

    def test_dry_run_prints_the_eval_program_and_builds_nothing(self):
        rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0", "--dry-run"])
        self.assertEqual(rc, 0, e)
        self.assertIn("org.Cinnamon.Eval:", o)
        self.assertIn("RX=0,RY=0,RW=800,RH=600,TX=1280,TY=0,TW=1920,TH=1080", o)
        self.assertEqual(FakeEval.live, set())             # nothing built
        self.assertEqual(core.records(core.load_state()), {})

    def test_stop_destroys_over_eval(self):
        self.run_cli(["A", "--to", "B", "--region", "800x600+0+0"])
        self.assertEqual(FakeEval.live, {1})
        rc, o, e = self.run_cli(["--stop", "B"])
        self.assertEqual(rc, 0, e)
        self.assertIn("stopped", o)
        self.assertEqual(FakeEval.live, set())             # torn down inside muffin
        self.assertEqual(core.records(core.load_state()), {})

    def test_list_shows_the_live_cinnamon_record(self):
        self.run_cli(["A", "--to", "B", "--region", "800x600+0+0"])
        rc, o, e = self.run_cli(["--list"])
        self.assertEqual(rc, 0, e)
        self.assertIn("B <- A", o)
        self.assertIn("tok 1", o)

    def test_list_prints_the_mode(self):
        self.run_cli(["A", "--to", "B", "--region", "800x600+0+0", "--scaling", "cover"])
        rc, o, e = self.run_cli(["--list"])
        self.assertEqual(rc, 0, e)
        self.assertIn("scaling cover", o)

    def test_scaling_is_accepted_and_recorded(self):
        """`--scaling cover` on this path is the same picture wl-mirror draws: the group is scaled by the
        exact ratio `core.scale_plan` computes (800x600 onto B's 1920x1080: max(12/5, 9/5) = 12/5) and the
        mode is written into the record, so the start line and `--list` say what the mirror is drawing."""
        rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0", "--scaling", "cover"])
        self.assertEqual(rc, 0, e)
        self.assertIn("scaling cover", o)
        built = [s for s in FakeEval.scripts if "global.__w11m[tok]={grp" in s]
        self.assertEqual(len(built), 1)
        self.assertIn("SN=12,SD=5;", built[0])
        self.assertEqual(core.records(core.load_state())["B"]["scaling"], "cover")

    def test_dry_run_prints_the_scaled_program(self):
        """The dry run prints the program that would run, scale and all -- `exact` of 800x600 onto 1920x1080
        is 1x (whole factors only, and 2x would not fit), so SN/SD is 1/1 and the picture is centred."""
        rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0",
                                 "--scaling", "exact", "--dry-run"])
        self.assertEqual(rc, 0, e)
        self.assertIn("org.Cinnamon.Eval:", o)
        self.assertIn("SN=1,SD=1;", o)
        self.assertEqual(FakeEval.live, set())             # nothing built
        self.assertEqual(core.records(core.load_state()), {})

    def test_the_default_is_fit_like_the_wl_mirror_path(self):
        """No flag is `fit` here as everywhere else: 800x600 onto 1920x1080 letterboxes at 9/5."""
        rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0"])
        self.assertEqual(rc, 0, e)
        self.assertIn("scaling fit", o)
        built = [s for s in FakeEval.scripts if "global.__w11m[tok]={grp" in s]
        self.assertIn("SN=9,SD=5;", built[0])

    def test_list_reaps_a_mirror_the_compositor_dropped(self):
        self.run_cli(["A", "--to", "B", "--region", "800x600+0+0"])
        FakeEval.live.discard(1)                           # muffin dropped it out from under us
        rc, o, e = self.run_cli(["--list"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(o.strip(), "")                    # gone, and the record with it
        self.assertEqual(core.records(core.load_state()), {})


if __name__ == "__main__":
    unittest.main()
