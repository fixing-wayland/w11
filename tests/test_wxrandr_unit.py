"""wxrandr unit tests: option-parse byte parity, the pending-layout resolver
(which has no overlap rule: overlapping layouts are the compositor's call,
not ours), transform mapping (against the XWayland-verified table), modeline
math, and query rendering against oracle capture bytes. No compositor
needed."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wxrandr import cli
from hacks.display import core
from hacks.display import kwin, mutter
from hacks.display.core import (Mode, OutputState, Stanza, State,
                          build_targets, resolve_positions)

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
    return code, out.getvalue(), err.getvalue()


def mk_state():
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    os.unlink(f.name)
    return State("test", path=f.name)


def mk_output(name, w, h, x=0, y=0, active=True, transform="normal",
              scale=1.0, refresh=59860, modes=None):
    o = OutputState(name=name, active=active, x=x, y=y, scale=scale,
                    transform=transform)
    if active:
        lw, lh = core.logical_size(w, h, transform, scale)
        o.w, o.h = lw, lh
    if modes is None:
        modes = [(w, h, refresh)]
    o.modes = [Mode(w=mw, h=mh, refresh_mhz=mr) for mw, mh, mr in modes]
    o.virtual_modes = False
    if o.modes:
        o.modes[0].preferred = True
        if active:
            o.current = o.modes[0]
    return o


class ParseErrors(unittest.TestCase):
    """stderr bytes straight from the oracle error captures."""

    CASES = [
        (["--zorp"], "xrandr: unrecognized option '--zorp'\n"),
        (["--mode", "800x600"],
         "xrandr: --mode must be used after --output\n"),
        (["--left-of", "HEADLESS-1"],
         "xrandr: --left-of must be used after --output\n"),
        (["--output", "X", "--pos", "nope"],
         "xrandr: failed to parse 'nope' as a position\n"),
        (["--output", "X", "--gamma", "banana"],
         "xrandr: --gamma: invalid argument 'banana'\n"),
        (["--output", "X", "--gamma", "0:1:1"],
         "xrandr: gamma correction factors must be positive\n"),
        (["--output", "X", "--brightness", "dim"],
         "xrandr: --brightness: invalid argument 'dim'\n"),
        (["--output", "X", "--rotate", "sideways"],
         "xrandr: --rotate: invalid argument 'sideways'\n"),
        (["--output", "X", "--reflect", "z"],
         "xrandr: --reflect: invalid argument 'z'\n"),
        (["--output", "X", "--scale", "big"],
         "xrandr: failed to parse 'big' as a scaling factor\n"),
        (["--output", "X", "--scale", "0"],
         "xrandr: scaling factors must be positive\n"),
        (["--output", "X", "--scale-from", "wide"],
         "xrandr: failed to parse 'wide' as a scale-from size\n"),
        (["--fb", "big"],
         "xrandr: failed to parse 'big' as a framebuffer size\n"),
        (["--fbmm", "big"],
         "xrandr: failed to parse 'big' as a physical size\n"),
        (["--output"], "xrandr: --output requires an argument\n"),
        (["--addmode", "X"], "xrandr: --addmode requires two arguments\n"),
        (["--delmode", "X"], "xrandr: --delmode requires two arguments\n"),
        (["--setmonitor", "a", "b"],
         "xrandr: --setmonitor requires three argument\n"),  # sic (oracle)
        (["--screen", "-2"],
         "xrandr: --screen argument must be nonnegative\n"),
        (["-s", "-4"], "xrandr: --size argument must be nonnegative\n"),
        (["-o", "diagonal"], "xrandr: -o: invalid argument 'diagonal'\n"),
        (["--rate", "fast"],
         "xrandr: failed to parse 'fast' as a number\n"),
    ]

    def test_argerr_bytes(self):
        tail = "Try 'xrandr --help' for more information.\n"
        for argv, want in self.CASES:
            code, out, err = run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(err, want + tail, argv)
            self.assertEqual(out, "", argv)

    def test_help_stdout_exit0(self):
        code, out, err = run_cli("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("usage: xrandr [options]\n"))
        self.assertIn("--setmonitor <name> {auto|<w>/<mmw>x<h>/<mmh>", out)
        self.assertEqual(out.count("\n"), 62)
        code2, out2, _ = run_cli("-help")
        self.assertEqual((code2, out2), (0, out))


class UnnamedOptions(unittest.TestCase):
    """The seventeen xrandr options no test named.

    Every one is a real-xrandr option wxrandr accepts. Three are swallowed
    in silence, four are warned about and dropped, ten do something -- and
    all of them share one thing that has to keep working: `_ARITY`, the
    look-ahead that decides where an X11 session's argv gets split before it
    is handed to the real xrandr, has to agree with parse() about how many
    arguments each one eats. The sentinel `--output LAST` at the end of
    every case is what proves that: if an option ate one argument too few or
    too many, the sentinel would not be the last stanza (or would not parse
    at all).

    `opts` is what the parse result must carry, `stanza` what the *first*
    stanza must carry, `warn` a fragment of the line it prints on stderr.
    """

    OUT = ["--output", "X"]

    #: (argv fragment, {Opts attribute: value}, {Stanza attribute: value}, warning)
    CASES = [
        # accepted, and quietly does nothing (xrandr's own no-ops)
        (["--nograb"], {}, {}, ""),
        (["--q12"], {}, {}, ""),
        (OUT + ["--crtc", "0"], {}, {}, ""),
        # accepted, and says on stderr that Wayland has nowhere to put it
        (OUT + ["--transform", "1,0,0,0,1,0,0,0,1"], {}, {},
         "--transform is not supported on Wayland; ignoring\n"),
        (OUT + ["--panning", "1920x1080"], {}, {},
         "--panning is not supported on Wayland; ignoring\n"),
        (["--setprovideroutputsource", "1", "2"], {"action": True}, {},
         "--setprovideroutputsource is not supported on Wayland; ignoring\n"),
        (["--setprovideroffloadsink", "1", "2"], {"action": True}, {},
         "--setprovideroffloadsink is not supported on Wayland; ignoring\n"),
        # accepted, and acted on
        (["--current"], {"current": True}, {}, ""),
        (["--q1"], {"query_1": True}, {}, ""),
        (["--prop"], {"props": True}, {}, ""),
        (["--properties"], {"props": True}, {}, ""),
        (["--orientation", "left"], {"rot": 1, "setit": True}, {}, ""),
        (["--refresh", "75"], {"rate": 75.0, "setit": True}, {}, ""),
        (["--listactivemonitors"], {"monitor_op": ("listactive",)}, {}, ""),
        (["--delmonitor", "M"], {"monitor_op": ("del", "M")}, {}, ""),
        (OUT + ["--preferred"], {}, {"preferred": True}, ""),
        (OUT + ["--filter", "bilinear"], {}, {"props": [("__filter", "bilinear")]}, ""),
    ]

    def test_each_is_accepted_and_consumes_its_own_arguments(self):
        for argv, opts, stanza, warn in self.CASES:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                o = cli.parse(argv + ["--output", "LAST"])
            for attr, want in opts.items():
                self.assertEqual(getattr(o, attr), want, (argv, attr))
            if stanza:
                for attr, want in stanza.items():
                    self.assertEqual(getattr(o.stanzas[0], attr), want,
                                     (argv, attr))
            self.assertEqual(err.getvalue(),
                             ("xrandr: " + warn) if warn else "", argv)
            # the sentinel survived: the option ate exactly its own arguments
            self.assertEqual(o.stanzas[-1].name, "LAST", argv)

    def test_the_table_agrees_with_the_arity_table(self):
        for argv, _o, _s, _w in self.CASES:
            opt = argv[len(self.OUT)] if argv[:len(self.OUT)] == self.OUT else argv[0]
            rest = argv[argv.index(opt) + 1:]
            self.assertEqual(len(rest), cli._ARITY.get(opt, 0), opt)

    def test_every_option_appears_once(self):
        named = [argv[len(self.OUT)] if argv[:len(self.OUT)] == self.OUT
                 else argv[0] for argv, _o, _s, _w in self.CASES]
        self.assertEqual(len(named), len(set(named)))
        self.assertEqual(len(named), 17)


class TransformMapping(unittest.TestCase):
    """The sway<->RandR table was verified against XWayland's own RandR
    translation (sway 90 shows as `right`, flipped as `normal X axis`,
    flipped-90 as `right X axis`, ...)."""

    VERIFIED = {  # sway transform -> what real xrandr printed for it
        "normal": ("normal", "normal"), "90": ("right", "normal"),
        "180": ("inverted", "normal"), "270": ("left", "normal"),
        "flipped": ("normal", "x"), "flipped-90": ("right", "x"),
        "flipped-180": ("inverted", "x"), "flipped-270": ("left", "x"),
    }

    def test_randr_view(self):
        self.assertEqual(core.RANDR_VIEW, self.VERIFIED)

    def test_sway_transform_roundtrip(self):
        for tf, (rot, refl) in self.VERIFIED.items():
            self.assertEqual(core.sway_transform(rot, refl), tf)

    def test_all_16_combos_map_to_equivalent_transform(self):
        # reflect y == reflect x + rotate 180; reflect xy == rotate 180
        self.assertEqual(core.sway_transform("normal", "y"), "flipped-180")
        self.assertEqual(core.sway_transform("right", "y"), "flipped-270")
        self.assertEqual(core.sway_transform("inverted", "y"), "flipped")
        self.assertEqual(core.sway_transform("left", "y"), "flipped-90")
        self.assertEqual(core.sway_transform("normal", "xy"), "180")
        self.assertEqual(core.sway_transform("right", "xy"), "270")
        self.assertEqual(core.sway_transform("inverted", "xy"), "normal")
        self.assertEqual(core.sway_transform("left", "xy"), "90")

    def test_logical_size(self):
        # observed sway/wlroots rounding: truncation after transform swap
        self.assertEqual(core.logical_size(1280, 720, "normal", 1.5),
                         (853, 480))
        self.assertEqual(core.logical_size(1111, 666, "normal", 1.5),
                         (740, 444))
        self.assertEqual(core.logical_size(1281, 721, "normal", 2), (640, 360))
        self.assertEqual(core.logical_size(1280, 720, "270", 2), (360, 640))
        self.assertEqual(core.logical_size(1280, 720, "flipped-90", 1),
                         (720, 1280))


class SharedModeResolver(unittest.TestCase):
    """One transform table and one mode resolver serve both D-Bus backends.
    The single thing that differs between them stays a parameter: Mutter's
    mode list says whether a mode is interlaced, and KWin's does not."""

    def test_the_spec_table_is_the_sway_table_permuted(self):
        """1<->3 and 5<->7, and nothing else, between the two numberings."""
        swap = {0: 0, 1: 3, 2: 2, 3: 1, 4: 4, 5: 7, 6: 6, 7: 5}
        for n, view in core.WL_SPEC_RANDR_VIEW.items():
            sway_name = core.WL_TRANSFORM_NAME[swap[n]]
            self.assertEqual(core.RANDR_VIEW[sway_name], view, n)
        self.assertIs(mutter.MUTTER_RANDR_VIEW, core.WL_SPEC_RANDR_VIEW)
        self.assertIs(kwin.KWIN_RANDR_VIEW, core.WL_SPEC_RANDR_VIEW)

    def real_modes(self, flagged: bool):
        """Two real 1920x1080 modes, one of them interlaced -- carrying the
        flag only where the compositor reports one."""
        return [Mode(w=1920, h=1080, refresh_mhz=60000, mode_id="p"),
                Mode(w=1920, h=1080, refresh_mhz=59940, mode_id="i",
                     flags=("interlace",) if flagged else ())]

    def custom(self):
        """A `--newmode 1920x1080i ... Interlace` mode: no compositor id."""
        return Mode(w=1920, h=1080, refresh_mhz=59940, custom=True,
                    name="1920x1080i", flags=("interlace",))

    def target(self, modes, mode):
        o = OutputState(name="DP-1", active=True, modes=modes)
        return core.Target(output=o, stanza=None, mode=mode)

    def test_a_flagged_list_resolves_a_custom_interlaced_mode_onto_one(self):
        m = core.resolve_real_mode(self.target(self.real_modes(True),
                                               self.custom()),
                                   mk_state(), interlace_known=True)
        self.assertEqual(m.mode_id, "i")

    def test_a_flagged_list_never_answers_with_a_progressive_mode(self):
        """Mutter reports the flag, so an interlaced request that only a
        progressive mode could satisfy is `cannot find mode`, not a silent
        substitution."""
        t = self.target([Mode(w=1920, h=1080, refresh_mhz=59940,
                              mode_id="p")], self.custom())
        with self.assertRaises(core.Fatal) as e:
            core.resolve_real_mode(t, mk_state(), interlace_known=True)
        self.assertEqual(str(e.exception), "cannot find mode 1920x1080i\n")

    def test_a_flagless_list_matches_on_size_and_rate_alone(self):
        """KWin's modes carry no flag at all, so comparing against one would
        find nothing and turn every custom mode into an error. The flag is
        left out of the match there, and the size/rate twin wins."""
        m = core.resolve_real_mode(self.target(self.real_modes(False),
                                               self.custom()),
                                   mk_state(), interlace_known=False)
        self.assertEqual(m.mode_id, "i")           # 59.940, the nearest rate

    def test_the_same_flagless_list_under_the_mutter_rule_finds_nothing(self):
        """The divergence itself: identical inputs, one parameter apart."""
        t = self.target(self.real_modes(False), self.custom())
        with self.assertRaises(core.Fatal):
            core.resolve_real_mode(t, mk_state(), interlace_known=True)

    def test_each_backend_asks_for_the_rule_that_fits_its_wire(self):
        """resolve_mode() uses no instance state, so the wiring can be read
        off the unbound methods."""
        t = self.target(self.real_modes(False), self.custom())
        self.assertEqual(
            kwin.KwinOutputs.resolve_mode(None, t, mk_state()).mode_id, "i")
        t2 = self.target(self.real_modes(False), self.custom())
        with self.assertRaises(core.Fatal):
            mutter.MutterOutputs.resolve_mode(None, t2, mk_state())


class BackendShape(unittest.TestCase):
    """All four display backends are one shape, so the CLI can hold one of
    them and never ask which. sway and wlroots were module-level functions
    and a connection object until they were given the names Mutter and KWin
    already answered to."""

    BACKENDS = (core.SwayBackend, core.WlrOutputs,
                mutter.MutterOutputs, kwin.KwinOutputs)

    METHODS = {"snapshot": ["state"],
               "predicted_dims": ["t", "state"],
               "verify": ["state", "targets"],
               "apply": ["state", "targets", "persistent"],
               "close": []}

    def test_every_backend_answers_to_the_same_names(self):
        import inspect
        for cls in self.BACKENDS:
            for meth, params in sorted(self.METHODS.items()):
                fn = getattr(cls, meth, None)
                self.assertTrue(callable(fn), "%s.%s" % (cls.__name__, meth))
                got = [p for p in inspect.signature(fn).parameters
                       if p != "self"]
                self.assertEqual(got, params, "%s.%s" % (cls.__name__, meth))

    def test_the_name_is_the_compositor_the_query_prints(self):
        self.assertEqual([c.name for c in self.BACKENDS],
                         ["sway", "wlroots", "mutter", "kwin"])

    def test_the_wlr_wire_call_is_send_not_apply(self):
        """WlrOutputs.apply() used to be the zwlr configuration request; the
        backend method took that name, so the request is `send` now and
        nothing calls the old one by accident."""
        import inspect
        self.assertEqual(
            [p for p in inspect.signature(core.WlrOutputs.send).parameters
             if p != "self"], ["targets", "positions"])

    def test_the_two_wires_predict_different_logical_sizes(self):
        """The one thing the sway and wlroots backends really disagree on:
        `--scale 1.03` reaches sway as text and runs as 1.0333, and reaches
        zwlr as a wl_fixed and runs as 1.025."""
        o = OutputState(name="DP-1", active=True,
                        modes=[Mode(w=1920, h=1080, refresh_mhz=60000)])
        t = core.Target(output=o, stanza=None, mode=o.modes[0], scale=1.03,
                        enabled=True)
        sway = core.SwayBackend.predicted_dims(None, t, mk_state())
        wlr = core.WlrOutputs.predicted_dims(None, t, mk_state())
        self.assertEqual(sway, (1858, 1045))
        self.assertEqual(wlr, (1873, 1053))

    def test_the_sway_backend_closes_both_of_its_sockets(self):
        """The IPC socket and the zwlr connection the query enrichment
        opened -- and one that fails on the way out does not keep the other
        one open."""
        class Handle:
            def __init__(self, boom=False):
                self.closed, self.boom = 0, boom

            def close(self):
                self.closed += 1
                if self.boom:
                    raise OSError("already gone")
        ipc, wlr = Handle(boom=True), Handle()
        core.SwayBackend(ipc, wlr).close()
        self.assertEqual((ipc.closed, wlr.closed), (1, 1))
        ipc = Handle()
        core.SwayBackend(ipc).close()          # no enrichment connection
        self.assertEqual(ipc.closed, 1)

    def test_the_session_holds_one_handle(self):
        self.assertIsNone(cli.Session.impl)
        for gone in ("ipc", "wlr", "mutter", "kwin"):
            self.assertFalse(hasattr(cli.Session, gone), gone)


class ModelineMath(unittest.TestCase):
    def test_refresh_formula(self):
        # the classic CVT 1280x720 modeline: 74.50MHz 1664x748 -> 59.86Hz
        hz = core.mode_refresh_hz(74.50, 1664, 748)
        self.assertEqual("%6.2f" % hz, " 59.86")

    def test_doublescan_and_interlace(self):
        base = core.mode_refresh_hz(74.50, 1664, 748)
        self.assertAlmostEqual(
            core.mode_refresh_hz(74.50, 1664, 748, ["doublescan"]),
            base / 2)
        self.assertAlmostEqual(
            core.mode_refresh_hz(74.50, 1664, 748, ["interlace"]), base * 2)

    def test_custom_mode_through_state(self):
        st = mk_state()
        st.modes()["fancy"] = {"clock": 74.50,
                               "h": [1280, 1344, 1472, 1664],
                               "v": [720, 723, 728, 748],
                               "flags": ["-hsync", "+vsync"]}
        st.addmodes()["OUT"] = ["fancy"]
        (m,) = st.modes_for_output("OUT")
        self.assertEqual((m.w, m.h, m.name, m.custom),
                         (1280, 720, "fancy", True))
        self.assertEqual("%6.2f" % m.refresh_hz, " 59.86")

    def test_mm_synthesis_matches_xwayland(self):
        # oracle listmonitors: 1280->339 720->190 1024->271 768->203
        #                      800->212 600->159 1920->508 1080->286
        for px, mm in ((1280, 339), (720, 190), (1024, 271), (768, 203),
                       (800, 212), (600, 159), (1920, 508), (1080, 286)):
            self.assertEqual(core.synth_mm(px), mm, px)
        # X screen mm truncate: 1280->338, 720->190 (oracle -s table)
        self.assertEqual(core.screen_mm(1280), 338)
        self.assertEqual(core.screen_mm(720), 190)


class WlrootsScale(unittest.TestCase):
    """What sway 1.9 / wlroots 0.17 really does with a fractional scale,
    measured on a live headless sway (1920x1080, 1.00..3.00 by 0.01, both
    backends, 201 points each; every row below is a capture, and the model
    reproduces all 402 of them).

    Two single-precision steps: sway quantises the scale it is given to
    120ths -- fractional-scale-v1's unit -- and wlr_output_effective_
    resolution then divides the pixel size by that float and truncates.  What
    it is given differs per transport: the sway IPC gets the number as text,
    zwlr_output_management gets a wl_fixed that wayland_mini truncates to
    256ths, which is why `--scale 1.03` runs as 1.0333 on one and 1.025 on
    the other."""

    #      asked   what sway runs        logical 1920x1080
    FIXED = [
        (1.03, 1.0249999761581421, (1873, 1053)),
        (1.08, 1.0750000476837158, (1786, 1004)),
        (1.14, 1.1333333253860474, (1694, 952)),
        (1.20, 1.2000000476837158, (1599, 899)),
        (1.25, 1.25, (1536, 864)),
        (1.35, 1.3500000238418579, (1422, 800)),
        (1.50, 1.5, (1280, 720)),
        (1.60, 1.6000000238418579, (1200, 675)),
        (1.75, 1.75, (1097, 617)),
        (2.00, 2.0, (960, 540)),
        (2.67, 2.6666667461395264, (720, 405)),
        (3.00, 3.0, (640, 360)),
    ]
    TEXT = [
        (1.03, 1.0333333015441895, (1858, 1045)),
        (1.08, 1.0833333730697632, (1772, 996)),
        (1.14, 1.1416666507720947, (1681, 945)),
        (1.20, 1.2000000476837158, (1599, 899)),
        (1.50, 1.5, (1280, 720)),
        (1.60, 1.6000000238418579, (1200, 675)),
        (2.67, 2.6666667461395264, (720, 405)),
    ]

    def test_quantisation_matches_the_compositor(self):
        for wire, table in (("fixed", self.FIXED), ("text", self.TEXT)):
            for asked, runs, _dims in table:
                with self.subTest(wire=wire, scale=asked):
                    self.assertEqual(core.wlr_scale(asked, wire), runs)

    def test_logical_size_matches_the_compositor(self):
        for wire, table in (("fixed", self.FIXED), ("text", self.TEXT)):
            for asked, runs, dims in table:
                with self.subTest(wire=wire, scale=asked):
                    self.assertEqual(
                        core.logical_size(1920, 1080, "normal", runs), dims)
                    self.assertEqual(
                        core.logical_size(1920, 1080, "normal",
                                          core.wlr_scale(asked, wire)), dims)

    def test_a_double_division_is_not_good_enough(self):
        # 1920/1.6 is 1200 in float32 and 1199 in double: the pixel that
        # used to leave a gap between an output and its --right-of neighbour
        self.assertEqual(int(1920 / 1.6000000238418579), 1199)
        self.assertEqual(core.logical_size(1920, 1080, "normal",
                                           1.6000000238418579), (1200, 675))

    def test_the_transform_swap_still_comes_first(self):
        self.assertEqual(core.logical_size(1920, 1080, "90", 1.5), (720, 1280))

    def test_right_of_is_edge_to_edge_on_the_wlr_wire(self):
        st = mk_state()
        for asked, _runs, dims in self.FIXED:
            with self.subTest(scale=asked):
                a = mk_output("A", 1920, 1080)
                b = mk_output("B", 1920, 1080)
                targets = build_targets(
                    [a, b],
                    [Stanza(name="A", scale=(asked, asked), pos=(0, 0)),
                     Stanza(name="B", relation=("right-of", "A"))],
                    st, False)
                d = {t.name: core.predicted_dims(t, st, wire="fixed")
                     for t in targets if t.enabled}
                self.assertEqual(d["A"], dims)
                pos = resolve_positions(targets, d)
                self.assertEqual(pos["B"], (dims[0], 0))


class Resolver(unittest.TestCase):
    def L(self):  # noqa: N802 - three outputs, side by side
        return [mk_output("A", 1280, 720),
                mk_output("B", 1024, 768, x=1280),
                mk_output("C", 800, 600, x=2304)]

    def dims(self, targets, state):
        return {t.name: core.predicted_dims(t, state)
                for t in targets if t.enabled}

    def test_chain_right_of_pending(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", relation=("right-of", "A")),
                   Stanza(name="C", relation=("right-of", "B"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "B": (1280, 0), "C": (2304, 0)})

    def test_l_shape_and_below(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", relation=("right-of", "A")),
                   Stanza(name="C", relation=("below", "A"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["C"], (0, 720))

    def test_left_of_goes_negative_then_normalizes(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="C",
                                         relation=("left-of", "A"))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        # C lands at -800, then the whole layout shifts right by 800
        self.assertEqual(pos, {"C": (0, 0), "A": (800, 0), "B": (2080, 0)})

    def test_relative_uses_rotated_dims(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", rotate="left",
                          relation=("right-of", "A")),
                   Stanza(name="C", relation=("right-of", "B"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["C"], (1280 + 768, 0))  # B is portrait now

    def test_same_as_mirrors(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B",
                                         relation=("same-as", "A"))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["B"], pos["A"])

    def test_relation_loop_is_fatal(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="A", relation=("right-of", "B")),
                   Stanza(name="B", relation=("right-of", "A"))]
        ts = build_targets(outs, stanzas, st)
        with self.assertRaises(core.Fatal) as cm:
            resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(cm.exception.args[0],
                         "loop in relative position specifications\n")

    def test_relative_to_unknown_output_fatal(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B",
                                         relation=("right-of", "NOPE"))], st)
        with self.assertRaises(core.Fatal) as cm:
            resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(cm.exception.args[0],
                         'cannot find output "NOPE"\n')

    def test_relative_to_disabled_lands_at_origin(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", off=True),
                   Stanza(name="C", relation=("right-of", "B"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["C"], (0, 0))
        self.assertNotIn("B", pos)

    def test_explicit_negative_pos_normalizes(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="A", pos=(-200, -100))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["A"], (0, 0))
        self.assertEqual(pos["B"], (1480, 100))

    def test_an_overlap_survives_the_resolver_untouched(self):
        """wxrandr has no geometry policy of its own: an overlapping --pos
        is resolved, normalised and pinned exactly as asked.  On sway and
        wlroots that is genuine partial mirroring -- both outputs are
        viewports onto one scene, and the shared region came back
        byte-identical on both heads (measured, sway 1.11)."""
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B", pos=(640, 0))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "B": (640, 0), "C": (2304, 0)})
        self.assertEqual(core.position_commands(ts, pos),
                         ["output A position 0 0", "output B position 640 0",
                          "output C position 2304 0"])
        # a full overlap (--same-as) and a rotation into a neighbour are the
        # same story: nothing here objects
        ts = build_targets(outs, [Stanza(name="B", pos=(0, 0)),
                                  Stanza(name="C", pos=(100, 100))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "B": (0, 0), "C": (100, 100)})

    def test_unknown_output_stanza_warns_not_fatal(self):
        outs = self.L()
        st = mk_state()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ts = build_targets(outs, [Stanza(name="GHOST", off=True)], st)
        self.assertEqual(err.getvalue(),
                         "warning: output GHOST not found; ignoring\n")
        self.assertTrue(all(t.enabled for t in ts))

    def test_off_and_hole_stays(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B", off=True)], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "C": (2304, 0)})  # hole kept

    def test_scale_changes_pending_dims(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="A", scale=(2.0, 2.0)),
                   Stanza(name="B", relation=("right-of", "A"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["B"], (640, 0))

    def test_mode_lookup(self):
        o = mk_output("A", 1280, 720,
                      modes=[(1280, 720, 59860), (1280, 720, 50000),
                             (800, 600, 59860)])
        t = build_targets([o], [Stanza(name="A", mode="800x600")], mk_state())
        self.assertEqual((t[0].mode.w, t[0].mode.h), (800, 600))
        # nearest-rate pick (xrandr find_mode has no threshold)
        t = build_targets([o], [Stanza(name="A", mode="1280x720",
                                       rate=49.0)], mk_state())
        self.assertEqual(t[0].mode.refresh_mhz, 50000)
        with self.assertRaises(core.Fatal) as cm:
            build_targets([o], [Stanza(name="A", mode="9999x9999")],
                          mk_state())
        self.assertEqual(cm.exception.args[0],
                         "cannot find mode 9999x9999\n")

    def test_virtual_output_accepts_any_mode(self):
        o = mk_output("A", 1280, 720)
        o.virtual_modes = True
        t = build_targets([o], [Stanza(name="A", mode="777x555")], mk_state())
        self.assertEqual((t[0].mode.w, t[0].mode.h, t[0].mode.custom),
                         (777, 555, True))


class AutoMode(unittest.TestCase):
    """--auto must switch to the PREFERRED mode, not no-op / remembered mode
    (review finding: build_targets --auto)."""

    def test_auto_switches_active_output_to_preferred(self):
        o = mk_output("A", 800, 600,
                      modes=[(800, 600, 59860), (1280, 720, 59860)])
        o.modes[0].preferred = False   # active at 800x600 ...
        o.modes[1].preferred = True    # ... but preferred is 1280x720
        o.current = o.modes[0]
        t = build_targets([o], [Stanza(name="A", auto=True)], mk_state())
        self.assertTrue(t[0].enabled)
        self.assertEqual((t[0].mode.w, t[0].mode.h), (1280, 720))

    def test_auto_keeps_explicit_mode(self):
        o = mk_output("A", 800, 600,
                      modes=[(800, 600, 59860), (1280, 720, 59860)])
        t = build_targets([o], [Stanza(name="A", auto=True, mode="800x600")],
                          mk_state())
        self.assertEqual((t[0].mode.w, t[0].mode.h), (800, 600))

    def test_global_auto_reenables_disabled_at_preferred(self):
        o = mk_output("A", 1280, 720, active=False,
                      modes=[(1280, 720, 59860)])
        ts = build_targets([o], [], mk_state(), global_auto=True)
        self.assertTrue(ts[0].enabled)
        self.assertTrue(ts[0].changed)
        self.assertEqual((ts[0].mode.w, ts[0].mode.h), (1280, 720))


class ScreenBound(unittest.TestCase):
    """xrandr's `screen cannot be larger than` fatal (set_screen_size)."""

    def _t(self, name):
        return type("T", (), {"name": name, "enabled": True})()

    def test_layout_too_wide_is_fatal(self):
        opts = cli.Opts()
        with self.assertRaises(core.Fatal) as cm:
            cli._check_screen_size(opts, [self._t("A")], {"A": (1280, 720)},
                                   {"A": (100000, 0)})
        self.assertEqual(cm.exception.args[0],
                         "screen cannot be larger than 32767x32767 "
                         "(desired size 101280x720)\n")

    def test_fb_too_large_is_fatal(self):
        opts = cli.Opts()
        opts.fb = (40000, 40000)
        with self.assertRaises(core.Fatal) as cm:
            cli._check_screen_size(opts, [], {}, {})
        self.assertEqual(cm.exception.args[0],
                         "screen cannot be larger than 32767x32767 "
                         "(desired size 40000x40000)\n")

    def test_within_bounds_ok(self):
        opts = cli.Opts()
        cli._check_screen_size(opts, [self._t("A")], {"A": (1280, 720)},
                               {"A": (0, 0)})  # no raise

    def test_output_scaled_below_the_minimum_is_fatal(self):
        # the Screen line advertises `minimum 16 x 16`; logical_size is
        # int(px / scale), so a big enough --scale truncates an output to
        # nothing and the compositor takes it
        opts = cli.Opts()
        for dims in ((0, 0), (15, 8), (1280, 15), (15, 720)):
            with self.subTest(dims=dims):
                with self.assertRaises(core.Fatal) as cm:
                    cli._check_screen_size(opts, [self._t("A")], {"A": dims},
                                           {"A": (0, 0)})
                self.assertEqual(cm.exception.args[0],
                                 "output A cannot be smaller than 16x16 "
                                 "(desired size %dx%d)\n" % dims)

    def test_exactly_the_minimum_is_allowed(self):
        opts = cli.Opts()
        cli._check_screen_size(opts, [self._t("A")], {"A": (16, 16)},
                               {"A": (0, 0)})  # no raise

    def test_a_disabled_output_is_not_measured(self):
        opts = cli.Opts()
        off = type("T", (), {"name": "B", "enabled": False})()
        cli._check_screen_size(opts, [off], {"B": (0, 0)}, {})  # no raise

    def test_scale_that_truncates_to_zero_is_caught(self):
        # the same thing spelled the way a user gets there
        out = mk_output("A", 1920, 1080)
        st = mk_state()
        targets = build_targets([out], [Stanza(name="A", scale=(99999.0,
                                                                99999.0))],
                                st, False)
        dims = {t.name: core.predicted_dims(t, st)
                for t in targets if t.enabled}
        self.assertEqual(dims["A"], (0, 0))
        with self.assertRaises(core.Fatal):
            cli._check_screen_size(cli.Opts(), targets, dims,
                                   resolve_positions(targets, dims))


class Rendering(unittest.TestCase):
    """Byte checks against the oracle captures (the 3-output L-shape)."""

    def l_shape(self):
        outs = [mk_output("HEADLESS-1", 1280, 720),
                mk_output("HEADLESS-2", 1024, 768, x=1280),
                mk_output("HEADLESS-3", 800, 600, y=720)]
        return outs

    def test_screen_line_l_shape(self):
        # oracle xrandr2-query-3out
        self.assertEqual(
            core.render_screen_line(0, self.l_shape()),
            "Screen 0: minimum 16 x 16, current 2304 x 1320, "
            "maximum 32767 x 32767")

    def test_output_headers_l_shape(self):
        st = mk_state()
        lines = core.render_query(self.l_shape(), st)
        self.assertEqual(lines[1], "HEADLESS-1 connected 1280x720+0+0 "
                         "(normal left inverted right x axis y axis) "
                         "0mm x 0mm")
        self.assertIn("HEADLESS-3 connected 800x600+0+720 "
                      "(normal left inverted right x axis y axis) 0mm x 0mm",
                      lines)

    def test_mode_table_bytes(self):
        o = mk_output("H", 1280, 720,
                      modes=[(1280, 720, 59860), (800, 600, 59860)])
        rows = core.render_mode_table(o)
        # oracle: current+preferred `*+`, others two trailing spaces
        self.assertEqual(rows[0], "   1280x720      59.86*+")
        self.assertEqual(rows[1], "   800x600       59.86  ")

    def test_mode_table_groups_same_name(self):
        o = mk_output("H", 1920, 1080,
                      modes=[(1920, 1080, 60000), (1920, 1080, 50000)])
        rows = core.render_mode_table(o)
        self.assertEqual(rows, ["   1920x1080     60.00*+  50.00  "])

    def test_monitors_l_shape_bytes(self):
        # oracle xrandr2-listmonitors-3out, byte for byte
        st = mk_state()
        self.assertEqual(core.render_monitors(self.l_shape(), st), [
            "Monitors: 3",
            " 0: +HEADLESS-1 1280/339x720/190+0+0  HEADLESS-1",
            " 1: +HEADLESS-2 1024/271x768/203+1280+0  HEADLESS-2",
            " 2: +HEADLESS-3 800/212x600/159+0+720  HEADLESS-3",
        ])

    def test_monitors_primary_star(self):
        st = mk_state()
        st.primary = "HEADLESS-2"
        lines = core.render_monitors(self.l_shape(), st)
        self.assertIn(" 1: +*HEADLESS-2 1024/271x768/203+1280+0  HEADLESS-2",
                      lines)

    def test_primary_and_rotation_header(self):
        st = mk_state()
        st.primary = "HEADLESS-1"
        o = mk_output("HEADLESS-1", 1280, 720, transform="270")
        line = core.render_output_header(o, st.primary)
        self.assertEqual(line, "HEADLESS-1 connected primary 720x1280+0+0 "
                         "left (normal left inverted right x axis y axis) "
                         "0mm x 0mm")

    def test_reflection_header_prints_rotation_word(self):
        o = mk_output("H", 1280, 720, transform="flipped")
        self.assertEqual(
            core.render_output_header(o, None),
            "H connected 1280x720+0+0 normal X axis "
            "(normal left inverted right x axis y axis) 0mm x 0mm")

    def test_disabled_output_header(self):
        o = mk_output("H", 1280, 720, active=False)
        self.assertEqual(core.render_output_header(o, None),
                         "H connected (normal left inverted right x axis "
                         "y axis)")

    def test_disabled_everything_screen_current_zero(self):
        o = mk_output("H", 1280, 720, active=False)
        self.assertEqual(
            core.render_screen_line(0, [o]),
            "Screen 0: minimum 16 x 16, current 0 x 0, "
            "maximum 32767 x 32767")

    def test_verbose_custom_modeline_bytes(self):
        st = mk_state()
        st.modes()["fancy"] = {"clock": 74.50,
                               "h": [1280, 1344, 1472, 1664],
                               "v": [720, 723, 728, 748],
                               "flags": ["-hsync", "+vsync"]}
        st.addmodes()["OUT"] = ["fancy"]
        (m,) = st.modes_for_output("OUT")
        m.preferred = True
        lines = core.render_verbose_mode(m, {}, True)
        # oracle print_verbose_mode bytes (xrandr-verbose.out shape)
        self.assertEqual(lines[0],
                         "  fancy (0x0) 74.500MHz -HSync +VSync *current "
                         "+preferred")
        self.assertEqual(lines[1],
                         "        h: width  1280 start 1344 end 1472 total "
                         "1664 skew    0 clock  44.77KHz")
        self.assertEqual(lines[2],
                         "        v: height  720 start  723 end  728 total "
                         " 748           clock  59.86Hz")

    def test_verbose_gamma_falls_back_when_holder_stale(self):
        # a holder record whose starttime no longer matches (killed/recycled)
        # must not keep reporting the old brightness — the compositor restored
        # the neutral ramp (review finding: render_verbose_block gamma record)
        st = mk_state()
        o = mk_output("H", 1280, 720)
        st.gamma()["H"] = {"pid": os.getpid(), "start": "0",
                           "brightness": 0.5, "gamma": [1.2, 1.0, 0.9]}
        lines = core.render_verbose_block(o, st, 0)
        self.assertIn("\tGamma:      1.0:1.0:1.0", lines)
        self.assertIn("\tBrightness: 1.0", lines)

    def test_verbose_gamma_reports_live_holder(self):
        from w11common import procs
        st = mk_state()
        o = mk_output("H", 1280, 720)
        pid = os.getpid()
        st.gamma()["H"] = {"pid": pid, "start": procs.proc_starttime(pid),
                           "brightness": 0.5, "gamma": [1.0, 1.0, 1.0]}
        lines = core.render_verbose_block(o, st, 0)
        self.assertIn("\tBrightness: 0.50", lines)

    def test_providers(self):
        lines = core.render_providers(self.l_shape(), "sway")
        self.assertEqual(lines[0], "Providers: number : 1")
        self.assertEqual(lines[1],
                         "Provider 0: id: 0x1 cap: 0xb, Source Output, "
                         "Sink Output, Sink Offload crtcs: 3 outputs: 3 "
                         "associated providers: 0 name:sway")

    def test_q1_table_bytes(self):
        # oracle xrandr2-dryrun-size shape (single row here)
        outs = [mk_output("H", 1280, 720)]
        lines = core.render_q1(outs, mk_state())
        self.assertEqual(lines[0],
                         " SZ:    Pixels          Physical       Refresh")
        self.assertEqual(lines[1],
                         "*0   1280 x 720    ( 338mm x 190mm )  *60  ")
        self.assertEqual(core.render_q1_state(outs[0]), [
            "Current rotation - normal",
            "Current reflection - none",
            "Rotations possible - normal left inverted right ",
            "Reflections possible - X Axis Y Axis",
        ])


class StateFile(unittest.TestCase):
    def test_roundtrip_and_isolation(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        st = State("sock-a", path=f.name)
        st.primary = "OUT-1"
        st.modes()["m"] = {"clock": 1.0, "h": [1, 2, 3, 4],
                           "v": [5, 6, 7, 8], "flags": []}
        st.save()
        again = State("sock-a", path=f.name)
        self.assertEqual(again.primary, "OUT-1")
        self.assertIn("m", again.modes())
        other = State("sock-b", path=f.name)
        self.assertIsNone(other.primary)

    def test_corrupt_file_ignored(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                        mode="w")
        f.write("{not json")
        f.close()
        self.addCleanup(os.unlink, f.name)
        st = State("k", path=f.name)
        self.assertIsNone(st.primary)

    def _tmp(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        f.close()
        os.unlink(f.name)
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        self.addCleanup(lambda: os.path.exists(f.name + ".lock")
                        and os.unlink(f.name + ".lock"))
        return f.name

    def test_concurrent_same_key_writes_merge(self):
        # two "processes" writing different outputs' gamma under one key must
        # not drop each other's record (read-modify-write race fix)
        path = self._tmp()
        a = State("k", path=path)
        b = State("k", path=path)          # both loaded the (empty) file
        a.gamma()["A"] = {"pid": 1, "start": "sa"}
        b.gamma()["B"] = {"pid": 2, "start": "sb"}
        a.save()
        b.save()                           # must merge onto A's write
        c = State("k", path=path)
        self.assertIn("A", c.gamma())
        self.assertIn("B", c.gamma())

    def test_concurrent_delete_survives_merge(self):
        path = self._tmp()
        seed = State("k", path=path)
        seed.gamma()["A"] = {"pid": 1, "start": "sa"}
        seed.save()
        a = State("k", path=path)          # sees A
        b = State("k", path=path)          # sees A
        b.gamma()["B"] = {"pid": 2, "start": "sb"}
        b.save()                           # disk now A + B
        a.gamma().pop("A")                 # a removes A
        a.save()                           # A gone, B kept
        c = State("k", path=path)
        self.assertNotIn("A", c.gamma())
        self.assertIn("B", c.gamma())

    def test_other_key_preserved_across_writers(self):
        path = self._tmp()
        a = State("k1", path=path)
        a.primary = "OUT-1"
        a.save()
        b = State("k2", path=path)
        b.primary = "OUT-2"
        b.save()
        self.assertEqual(State("k1", path=path).primary, "OUT-1")
        self.assertEqual(State("k2", path=path).primary, "OUT-2")

    #: holds LOCK_EX on the file it is given and says so, then sits on it
    LOCK_HOLDER = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "sys.stdout.write('held\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n")

    #: one State.save(), in a process of its own so a wait can be timed out
    SAVER = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from hacks.display.core import State\n"
        "st = State('k', path=sys.argv[2])\n"
        "st.gamma()['HDMI-1'] = {'pid': 1, 'start': '?'}\n"
        "st.save()\n"
        "sys.stdout.write('saved\\n')\n")

    def _lock_holder(self, lockpath):
        p = subprocess.Popen([sys.executable, "-c", self.LOCK_HOLDER, lockpath],
                             stdout=subprocess.PIPE)
        self.addCleanup(self._stop, p)
        self.assertEqual(p.stdout.readline(), b"held\n")
        return p

    def _stop(self, p):
        if p.poll() is None:
            p.kill()
            p.wait(5)
        p.stdout.close()

    def _save_elsewhere(self, path, timeout):
        return subprocess.run([sys.executable, "-c", self.SAVER, ROOT, path],
                              capture_output=True, timeout=timeout)

    def test_the_lock_is_taken_when_nobody_holds_it(self):
        """The control the one below needs: the same subprocess, the same
        `State.save()`, with the lock free -- it returns at once and the record
        is on disk."""
        path = self._tmp()
        done = self._save_elsewhere(path, 10)
        self.assertEqual((done.returncode, done.stdout), (0, b"saved\n"))
        self.assertIn("HDMI-1", State("k", path=path).gamma())

    @unittest.expectedFailure
    def test_save_is_bounded_when_another_process_holds_the_lock(self):
        """T49; no fix number covers it, so it is written to fail.

        `State.save()` takes `LOCK_EX` on `<state>.lock` with a plain
        `fcntl.flock`, which has no deadline: another process holding that lock
        stops this one for as long as it holds it, with nothing printed and
        nothing to interrupt but the command itself.  The lock is on a file in
        the runtime directory, which with no `$XDG_RUNTIME_DIR` -- every `sudo`
        run, and cron -- is a private directory or, failing that, shared /tmp
        under a guessable name, so the holder need not be a wxrandr at all.

        Every other wait in this tree is bounded (sway IPC 10 s, Mutter's
        MonitorsChanged 5 s, KWin's apply 10 s, the gamma holder's start 10 s)
        and the state file is a cache that is never load-bearing: the failure
        this asks for is `LOCK_NB` with a retry and then carrying on without
        the lock, exactly as an unopenable lock file is already handled two
        lines further down.

        The wait is 3 s and the saver is killed at the deadline, so this cannot
        hang the suite -- it can only fail."""
        path = self._tmp()
        self._lock_holder(path + ".lock")
        started = time.monotonic()
        try:
            done = self._save_elsewhere(path, 3.0)
        except subprocess.TimeoutExpired:
            self.fail("State.save() was still waiting for the lock after %.1fs"
                      % (time.monotonic() - started))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_corrupt_custom_mode_returns_none(self):
        st = mk_state()
        st.modes()["empty"] = {}
        st.modes()["short"] = {"clock": 1.0, "h": [1, 2], "v": [3, 4, 5, 6]}
        st.modes()["wrongtype"] = {"clock": 1.0, "h": 5, "v": 6}
        self.assertIsNone(st.custom_mode("empty"))
        self.assertIsNone(st.custom_mode("short"))
        self.assertIsNone(st.custom_mode("wrongtype"))
        self.assertIsNone(st.custom_mode("missing"))


class VersionAndMisc(unittest.TestCase):
    def test_version_program_line(self):
        # the full two-line --version needs a compositor; the program line
        # prints before connecting and must match the oracle bytes
        self.assertEqual("xrandr program version       "
                         + core.PROGRAM_VERSION,
                         "xrandr program version       1.5.4")

    def test_gamma_ramp_math(self):
        from hacks.display.gamma import compute_ramp
        import struct as st

        ramp = compute_ramp(4, 1.0, (1.0, 1.0, 1.0))
        vals = st.unpack("=12H", ramp)
        self.assertEqual(vals[:4], (0, 21845, 43690, 65535))  # linear
        self.assertEqual(vals[:4], vals[4:8])  # per-channel identical
        ramp = compute_ramp(4, 0.5, (1.0, 1.0, 1.0))
        vals = st.unpack("=12H", ramp)
        self.assertEqual(vals[3], 32767)  # brightness scales the top
        # gamma exponent: xrandr uses (i/(size-1))^(1/gamma)*brightness
        ramp = compute_ramp(4, 1.0, (2.0, 1.0, 1.0))
        vals = st.unpack("=12H", ramp)
        self.assertEqual(vals[1], int((1 / 3) ** 0.5 * 65535))

    def test_negative_brightness_clamps_to_black(self):
        # xrandr accepts a negative --brightness and applies the (black) ramp,
        # exit 0; without the low clamp struct.pack('=H') would raise
        from hacks.display.gamma import compute_ramp
        import struct as st
        ramp = compute_ramp(4, -0.5, (1.0, 1.0, 1.0))
        self.assertEqual(st.unpack("=12H", ramp)[:4], (0, 0, 0, 0))



class StateFileOwnership(unittest.TestCase):
    """The state file decides which pid `--brightness` signals, what
    `--newmode` lines exist and which output is primary. Under sudo it lives
    in world-writable /tmp under a guessable name, so a file that is not
    ours must be ignored rather than obeyed."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "state.json")
        with open(self.path, "w") as f:
            json.dump({"k": {"primary": "OUT-1"}}, f)

    def test_our_own_file_is_read(self):
        self.assertEqual(State("k", path=self.path).primary, "OUT-1")

    def test_a_file_owned_by_someone_else_is_ignored(self):
        real = os.fstat

        def fake(fd):
            st = real(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev,
                                   st.st_nlink, st.st_uid + 1, st.st_gid,
                                   st.st_size, 0, 0, 0))
        with mock.patch.object(os, "fstat", fake):
            self.assertIsNone(State("k", path=self.path).primary)

    def test_a_world_writable_file_is_ignored(self):
        os.chmod(self.path, 0o666)
        self.assertIsNone(State("k", path=self.path).primary)

    def test_a_symlink_is_never_followed(self):
        link = os.path.join(self.d, "link.json")
        os.symlink(self.path, link)
        self.assertIsNone(State("k", path=link).primary)

if __name__ == "__main__":
    unittest.main(verbosity=2)
