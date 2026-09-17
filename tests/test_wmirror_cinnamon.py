#!/usr/bin/env python3
"""wmirror on Cinnamon (Muffin): the region/cross-size mirror over org.Cinnamon.Eval -- AGENTS.md route 2.

Cinnamon publishes neither wl-mirror capture protocol, and muffin 6.4 has the ScreenCast machinery compiled
out (measured -- see tests/test_wmirror_screencast.py and docs/WMIRROR.md). So the mirror does not capture at
all: it puts a clipped `Clutter.Actor` on `global.stage` at the target head's origin and fills it with one
`Clutter.Clone` per on-screen `Meta.WindowActor`/`Meta.BackgroundActor` (walked from `global.window_group` and
`global.top_window_group`) plus the `panel`, each offset by the region origin. Measured on
`resolute-cinnamon-wayland` (Cinnamon 6.4.13 / muffin 6.4.1), 2026-09-14: region `1000x700+0+0` of monitor 0
onto monitor 1's origin compares byte-identical (`compare -metric AE` 0, RMSE 0) between the two heads, live,
over a native Wayland window, the XWayland desktop/wallpaper actors and the panel.

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
        if "global.__w11m[tok]={grp" in script:            # build_program
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


class Programs(unittest.TestCase):
    """The JS strings, and the one rule they must obey: only integers are interpolated."""

    def test_build_interpolates_region_then_target_origin_as_ints(self):
        prog = cinnamon.build_program((10, 20, 300, 400), 1280, 0)
        self.assertIn("RX=10,RY=20,RW=300,RH=400,TX=1280,TY=0", prog)

    def test_build_coerces_through_int_and_leaves_no_format_holes(self):
        """The interpolation rule: floats are forced through int(), and nothing but numbers reaches the
        program -- so a program built from odd inputs is still all-integer JS, never a stray %d or a string."""
        prog = cinnamon.build_program((1.9, 2.9, 300.0, 400.0), 1280.5, 0.0)
        self.assertIn("RX=1,RY=2,RW=300,RH=400,TX=1280,TY=0", prog)
        self.assertNotIn("%d", prog)

    def test_build_is_the_per_actor_walk_not_the_uigroup_shortcut(self):
        """The working pattern is one Clone per Meta actor found under window_group + top_window_group, plus
        the panel. The magnifier-style single `Clone` of `Main.uiGroup` does NOT mirror across heads (measured
        AE 700000/700000 on the rig -- only the panel comes through), so it must not be what we send."""
        prog = cinnamon.build_program((0, 0, 800, 600), 0, 0)
        for needle in ("global.window_group", "global.top_window_group",
                       "M.WindowActor", "M.BackgroundActor",
                       "get_transformed_position", "new C.Clone(",
                       "name==='panel'", "clip_to_allocation:true"):
            self.assertIn(needle, prog, needle)
        self.assertNotIn("C.Clone({source:Main.uiGroup", prog)

    def test_build_stays_live_over_structural_changes(self):
        """A Clone tracks its source's content on its own; the tree's SHAPE (windows opening, closing,
        restacking) is what needs a re-walk, so the program connects `restacked` and `window-created`."""
        prog = cinnamon.build_program((0, 0, 800, 600), 0, 0)
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
        # and no scaling mode: nothing on this path scales, so there is none to write down -- a `"scaling":
        # "fit"` here is what made `--list` claim a letterbox the Clutter group never did.
        self.assertNotIn("scaling", rec)

    def test_start_places_the_group_at_the_target_origin(self):
        recs = {}
        cinnamon.start(recs, "A", "B", (0, 0, 800, 600), _Out("B", x=1280, y=40))
        built = [s for s in FakeEval.scripts if "global.__w11m[tok]={grp" in s]
        self.assertEqual(len(built), 1)
        self.assertIn("TX=1280,TY=40", built[0])

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
        # the `"scaling": "fit"` key is what a record written while this path still recorded a scaling mode
        # looks like: it is ignored, not printed back, because the line has to say what the mirror does
        # (1:1, clipped) and not what was once asked for. The column itself stays, so the line keeps the
        # wl-mirror line's shape.
        rec = {"kind": "cinnamon", "source": "A", "region": [0, 0, 800, 600],
               "scaling": "fit", "token": 5, "route": cinnamon.ROUTE}
        line = cinnamon.fmt_record("B", rec)
        self.assertIn("B <- A", line)
        self.assertIn("region 800x600+0+0", line)
        self.assertIn("scaling 1:1", line)
        self.assertNotIn("scaling fit", line)
        self.assertIn("tok 5", line)
        self.assertIn("Eval", line)
        self.assertNotIn("wl-mirror", line)


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
        self.assertIn("RX=0,RY=0,RW=800,RH=600,TX=1280,TY=0", o)
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

    def test_an_explicit_scaling_is_refused_on_this_path(self):
        """The Clutter group is placed 1:1 at the target origin and clipped, so fit/cover/exact are not
        applied here. An explicit --scaling is refused with its route and its cost -- not recorded and then
        quietly dropped, which is what made a `--list` line claim a letterbox that never happened."""
        for mode in ("cover", "fit"):
            with self.subTest(mode=mode):
                FakeEval.live, FakeEval.seq, FakeEval.scripts = set(), 0, []
                rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0", "--scaling", mode])
                self.assertEqual(rc, 1)
                self.assertIn("--scaling %s" % mode, e)
                self.assertIn("not yet done on this path", e)
                self.assertIn("AGENTS.md route 2", e)
                self.assertEqual(FakeEval.scripts, [])         # nothing was built inside muffin
                self.assertEqual(FakeEval.live, set())
                self.assertEqual(core.records(core.load_state()), {})
                self.assertEqual(o, "")

    def test_an_explicit_scaling_is_refused_before_the_dry_run_prints_a_program(self):
        """--dry-run is not a way past it: the program it would print is the 1:1 one, so printing it under
        `--scaling cover` would be the same claim in another voice."""
        rc, o, e = self.run_cli(["A", "--to", "B", "--region", "800x600+0+0",
                                 "--scaling", "cover", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("not yet done on this path", e)
        self.assertNotIn("org.Cinnamon.Eval:", o)

    def test_list_reaps_a_mirror_the_compositor_dropped(self):
        self.run_cli(["A", "--to", "B", "--region", "800x600+0+0"])
        FakeEval.live.discard(1)                           # muffin dropped it out from under us
        rc, o, e = self.run_cli(["--list"])
        self.assertEqual(rc, 0, e)
        self.assertEqual(o.strip(), "")                    # gone, and the record with it
        self.assertEqual(core.records(core.load_state()), {})


if __name__ == "__main__":
    unittest.main()
