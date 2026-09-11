#!/usr/bin/env python3
"""The generic wlroots apply, against a compositor that rearranges the layout it just accepted.

CI run 34308982263 (commit 4d6fd40) failed the same two display checks on all four labwc flavors --
resolute-labwc, resolute-xfce-wayland, resolute-budgie, resolute-lxqt-wayland -- with numbers that grew by
one head's width per apply:

    FAIL display: --right-of puts Virtual-3 to the right of Virtual-2 [Virtual-2 at 5760,0, Virtual-3 at 3840,0]
    FAIL display: --below puts Virtual-3 under Virtual-2   [Virtual-2 at 7680,1080, Virtual-3 at 5760,1080]
    FAIL mirror:  ... the region 800x600+100+100 is not inside Virtual-3, which is 1920x1080+9600+1080

Run down on the rig on 2026-09-09 (resolute-labwc golden, `vmctl start --heads 3 --mem 3G`, labwc 0.9.3 on
wlroots 0.19.2).  The whole cascade starts at `--output Virtual-3 --off` followed by `--output Virtual-3
--auto`: xrandr brings an output back at 0,0 -- X is the oracle and this repo does what X did -- so the
configuration asks for Virtual-3 on top of Virtual-1, and labwc answers `succeeded` and then lays the three
heads out itself (Virtual-3 0,0 / Virtual-1 1920,0 / Virtual-2 3840,0).  From then on every configuration of
that session comes back with one head where labwc chose:

    asked  V-1 0,0     V-2 1920,0    V-3 3840,0     read back  V-1 0,0  V-3 3840,0  V-2 5760,0

The three set_position requests were read off our own wire in the guest (a probe of WlrOutputs.send) and they
were exactly those numbers, and `wlr-randr` 0.4.1 -- the reference client, and the flavor's own oracle --
produces the identical layout from the identical starting state.  Re-sending the same configuration lands it
exactly, first retry, every time it was tried.  So `WlrOutputs.apply` reads the layout back and sends the same
configuration once more when a head is not where it was put; this file is that behaviour, at the wire, against
wl_fake.OutputManagerServer with the rearrangement armed.
"""

import os
import sys
import tempfile
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py.  This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common.wayland_mini import WlConn
from wxrandr import core
from wxrandr.core import Fatal, State, WlrOutputs, build_targets, snapshot_wlr


class Live(unittest.TestCase):
    """One fake compositor and one connected WlrOutputs per test."""

    def outputs(self, **kw):
        self.server = wl_fake.OutputManagerServer(**kw)
        self.addCleanup(self.server.close)
        conn = WlConn(self.server.path)
        self.addCleanup(conn.close)
        wlr = WlrOutputs(conn)
        self.addCleanup(wlr.close)
        return wlr

    def state(self):
        """A State on a file that is never written: nothing here reads a saved mode or a primary."""
        fh = tempfile.NamedTemporaryFile(prefix="wxrandr-wlr-state-", suffix=".json", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return State("wlroots", path=fh.name)


class TheLayoutIsReadBackAndReSent(Live):
    """The measured labwc 0.9.3 sequence, and what the backend does with it."""

    def stanza(self, name, **kw):
        return core.Stanza(name=name, **kw)

    def plan(self, wlr, stanzas):
        st = self.state()
        return st, build_targets(snapshot_wlr(wlr, st), stanzas, st)

    def test_one_apply_when_the_compositor_keeps_the_layout(self):
        """The retry costs nothing on a compositor that does what it was asked: one configuration, no more."""
        wlr = self.outputs()
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("below", "Virtual-2"))])
        wlr.apply(st, targets)
        self.assertEqual(len(self.server.applies), 1)
        self.assertEqual(self.server.layout(),
                         {"Virtual-1": (0, 0), "Virtual-2": (1920, 0), "Virtual-3": (1920, 1080)})

    def test_a_rearranged_head_is_re_sent_and_lands(self):
        """The CI failure itself: --right-of asks V-1 0,0 / V-2 1920,0 / V-3 3840,0, labwc puts Virtual-2 at
        5760,0 beside Virtual-3, and the second identical configuration lands all three."""
        wlr = self.outputs()
        self.server.stray_once("Virtual-2")
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("right-of", "Virtual-2"))])
        fresh = wlr.apply(st, targets)
        self.assertEqual(len(self.server.applies), 2, "the rearranged layout was not re-sent")
        self.assertEqual(self.server.applies[0], self.server.applies[1],
                         "the retry asked for a different layout than the apply it is retrying")
        self.assertEqual(self.server.layout(),
                         {"Virtual-1": (0, 0), "Virtual-2": (1920, 0), "Virtual-3": (3840, 0)})
        by = {o.name: (o.x, o.y) for o in fresh}
        self.assertEqual(by["Virtual-2"], (1920, 0), "apply returned the layout it did not get")
        self.assertEqual(by["Virtual-3"], (3840, 0))

    def test_the_5760_of_the_ci_run_is_what_the_fake_produces(self):
        """The fake is not free to be wrong about the number: with the retry taken out of the picture (one
        raw send), Virtual-2 lands on 5760,0 -- the byte CI printed."""
        wlr = self.outputs()
        self.server.stray_once("Virtual-2")
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("right-of", "Virtual-2"))])
        dims = {t.name: core.predicted_dims(t, st, wire="fixed") for t in targets if t.enabled}
        pos = core.resolve_positions(targets, dims)
        self.assertEqual(pos, {"Virtual-1": (0, 0), "Virtual-2": (1920, 0), "Virtual-3": (3840, 0)})
        wlr.send({t.name: t for t in targets}, pos)
        wlr.conn.roundtrip()
        self.assertEqual(self.server.layout(),
                         {"Virtual-1": (0, 0), "Virtual-2": (5760, 0), "Virtual-3": (3840, 0)})

    def test_a_compositor_that_ignores_the_retry_too_gets_a_sentence(self):
        """Two applies and no third: the numbers reach the user instead of a layout nobody asked for."""
        wlr = self.outputs()
        original = wlr.send

        def send(targets, positions):
            self.server.stray_once("Virtual-2")
            return original(targets, positions)
        wlr.send = send
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", relation=("right-of", "Virtual-2"))])
        with self.assertRaises(Fatal) as caught:
            wlr.apply(st, targets)
        self.assertEqual(str(caught.exception),
                         "the compositor accepted the position 1920,0 for Virtual-2 twice and put it "
                         "at 5760,0 both times\n")
        self.assertEqual(len(self.server.applies), 2)

    def test_the_off_then_auto_pair_asks_for_the_overlap_x_asks_for(self):
        """xrandr --off then --auto brings the output back at 0,0, on top of whatever is there. That is what
        X does and what the rig's display phase leans on, so the backend must ask for it -- and the head that
        is turned off must not be counted as a stray on the way."""
        wlr = self.outputs()
        st, targets = self.plan(wlr, [self.stanza("Virtual-3", off=True)])
        wlr.apply(st, targets)
        self.assertEqual(self.server.layout(), {"Virtual-1": (0, 0), "Virtual-2": (1920, 0)})
        self.assertEqual(len(self.server.applies), 1, "a disabled head was mistaken for a stray one")
        wlr2 = self.outputs_again()
        st, targets = self.plan(wlr2, [self.stanza("Virtual-3", auto=True)])
        wlr2.apply(st, targets)
        self.assertEqual(self.server.applies[-1]["Virtual-3"], (0, 0, True))
        self.assertEqual(self.server.layout()["Virtual-3"], (0, 0))

    def outputs_again(self):
        """A second client on the SAME fake: every wxrandr invocation is its own process and its own
        connection, and the layout it reads is whatever the previous one left."""
        conn = WlConn(self.server.path)
        self.addCleanup(conn.close)
        wlr = WlrOutputs(conn)
        self.addCleanup(wlr.close)
        return wlr


class DroppingServer(wl_fake.OutputManagerServer):
    """A compositor that DROPS the head it is asked to turn off, instead of keeping it disabled.

    Measured twice, and it is the shape this backend has to survive: river 0.4.8 -- `wxrandr --output
    Virtual-3 --off` works, and the next process finds no such head, so `--auto` answered `warning: output
    Virtual-3 not found; ignoring` with exit 0 [M goal2/recon/flavors.md §2, arch-river 2026-09-11]; and
    cosmic-comp 1.7.0, where `--auto` brought back 0 of 3 heads while 1.6.0 has no such problem [§5].  The
    `finished` event is sent and the head leaves the manager's list, so a client that binds afterwards is
    never told about it -- which is exactly what `wlr-randr` sees there."""

    def __init__(self, *a, **kw):
        self.dropped = []
        super().__init__(*a, **kw)

    def _apply(self, conn, state, cid):
        super()._apply(conn, state, cid)
        for h in [x for x in self.heads if not x["enabled"]]:
            self._send(conn, h["id"], 9)          # zwlr_output_head_v1.finished
            self.heads.remove(h)
            self.dropped.append(h["name"])


class TheDisabledOutputIsKept(Live):
    """`--off` then `--auto` on a compositor that drops the head: what is OURS and what is not.

    X's `xrandr` keeps a disabled output in `--query` and `--auto` turns it back on; two wlroots
    compositors here take the head away instead.  Keeping the output in the listing is ours and is done
    here; the compositor taking the head back is its own, and the refusal below says so and names the rung
    rather than exiting 0 in silence."""

    def dropping(self):
        self.server = DroppingServer()
        self.addCleanup(self.server.close)
        return self.server

    def client(self):
        """One wxrandr invocation: its own connection, its own snapshot, the layout the last one left."""
        conn = WlConn(self.server.path)
        self.addCleanup(conn.close)
        wlr = WlrOutputs(conn)
        self.addCleanup(wlr.close)
        return wlr

    def setUp(self):
        self.dropping()
        self.st = self.state()

    def turn_off(self, name="Virtual-3"):
        wlr = self.client()
        targets = build_targets(snapshot_wlr(wlr, self.st), [core.Stanza(name=name, off=True)], self.st)
        wlr.apply(self.st, targets)
        return wlr

    def test_the_head_the_compositor_dropped_is_still_an_output(self):
        self.turn_off()
        self.assertEqual(self.server.dropped, ["Virtual-3"])
        outs = {o.name: o for o in snapshot_wlr(self.client(), self.st)}
        self.assertIn("Virtual-3", outs, "the listing lost the output the last command disabled")
        gone = outs["Virtual-3"]
        self.assertFalse(gone.active)
        # the modes and the millimetres came out of the record, because the head is not there to ask
        self.assertEqual([(m.w, m.h, m.refresh_mhz) for m in gone.modes], [(1920, 1080, 74998)])
        self.assertTrue(gone.modes[0].preferred)
        self.assertEqual((gone.mm_w, gone.mm_h), (480, 270))
        self.assertEqual((gone.make, gone.model), ("Red Hat, Inc.", "QEMU Monitor"))

    def test_auto_finds_it_and_says_what_stopped_it_rather_than_ignoring_it(self):
        """`build_targets` warns `output X not found; ignoring` and exits 0 for a name it does not know --
        which is what a lost output got.  Now it is known, the stanza is a target, and what is left is the
        compositor: not yet, rung 6, and the sentence names it."""
        self.turn_off()
        wlr = self.client()
        outs = snapshot_wlr(wlr, self.st)
        targets = build_targets(outs, [core.Stanza(name="Virtual-3", auto=True)], self.st)
        hit = [t for t in targets if t.name == "Virtual-3"]
        self.assertEqual(len(hit), 1, "the stanza matched no output")
        self.assertTrue(hit[0].enabled and hit[0].changed)
        self.assertEqual((hit[0].mode.w, hit[0].mode.h), (1920, 1080), "--auto found no preferred mode")
        with self.assertRaises(Fatal) as caught:
            wlr.apply(self.st, targets)
        self.assertIn("Virtual-3 was turned off and the compositor stopped announcing it",
                      str(caught.exception))
        self.assertIn("AGENTS.md route 6", str(caught.exception))
        self.assertEqual(len(self.server.applies), 1, "a configuration went out for a head that is gone")

    def test_a_head_that_is_announced_again_is_listed_once_and_forgotten(self):
        """A compositor that DOES take the head back (or a re-plug): the record must not leave a second
        Virtual-3 in the listing for ever."""
        self.turn_off()
        # the head comes back: this is the same fake with the row restored, which is what a re-announce
        # looks like to a client that binds afterwards
        self.server.heads.append({"name": "Virtual-3", "x": 3840, "y": 0, "enabled": True})
        wlr = self.client()
        outs = snapshot_wlr(wlr, self.st)
        self.assertEqual([o.name for o in outs].count("Virtual-3"), 1)
        targets = build_targets(outs, [core.Stanza(name="Virtual-1", auto=True)], self.st)
        wlr.apply(self.st, targets)
        self.assertEqual(dict(self.st.offheads()), {}, "the record outlived the head coming back")


class TheApplyIsReadBack(Live):
    """A `succeeded` that changed nothing is not a success.

    Measured on resolute-hypr 2026-09-09: after a `keyword monitor` apply, Hyprland stopped timing the wlr
    path out and started ANSWERING it -- `wxrandr --backend wlr --output Virtual-1 --mode 1920x1080` on a
    head sitting at 1280x1024 gave rc 0 in 0.64 s, empty stderr, and the head exactly where it was, three
    times [M vm/live-smoke.d/hypr.sh display phase, the xwant at :566].  `xrandr` on X says `Configure crtc
    failed` rather than nothing, and X is the oracle."""

    def test_a_transform_the_compositor_accepted_and_ignored_is_a_sentence(self):
        """The fake takes `set_transform` and reports `transform 0` for every head, which is precisely a
        compositor that accepts and changes nothing -- so the rotation asks, the read-back disagrees, and
        the run says so instead of exiting 0."""
        wlr = self.outputs()
        st = self.state()
        targets = build_targets(snapshot_wlr(wlr, st),
                                [core.Stanza(name="Virtual-1", rotate="left")], st)
        with self.assertRaises(Fatal) as caught:
            wlr.apply(st, targets)
        self.assertEqual(str(caught.exception),
                         "the compositor accepted the transform 270 for Virtual-1 and did not apply it "
                         "(it reports normal)\n")

    def test_a_mode_that_did_not_change_is_named_with_both_numbers(self):
        """The hypr.sh:566 shape itself, at the level `_stray_head` is tested at: asked 1920x1080, reports
        1280x1024."""
        o = core.OutputState(name="Virtual-1", active=True, ident=1)
        o.current = core.Mode(w=1280, h=1024, refresh_mhz=60020)
        t = core.Target(output=o, stanza=None, enabled=True,
                        mode=core.Mode(w=1920, h=1080, refresh_mhz=74998), scale=1.0)
        t.changed = True
        with self.assertRaises(Fatal) as caught:
            WlrOutputs._verify_applied([t], [o])
        self.assertEqual(str(caught.exception),
                         "the compositor accepted the mode 1920x1080 for Virtual-1 and did not apply it "
                         "(it reports 1280x1024)\n")

    def test_an_output_that_did_not_come_on_is_named_too(self):
        o = core.OutputState(name="Virtual-3", active=False, ident=1)
        t = core.Target(output=o, stanza=None, enabled=True, scale=1.0)
        t.changed = True
        with self.assertRaises(Fatal) as caught:
            WlrOutputs._verify_applied([t], [o])
        self.assertEqual(str(caught.exception),
                         "the compositor accepted Virtual-3 on and did not apply it (it is still off)\n")

    def test_an_untouched_output_is_not_verified_against_a_plan_it_was_not_in(self):
        """`changed` is the whole gate: a head nobody named keeps whatever the compositor has it at, which
        is what every other backend's re-read does too."""
        o = core.OutputState(name="Virtual-2", active=False, ident=1)
        t = core.Target(output=o, stanza=None, enabled=True, scale=1.0)
        self.assertIsNone(WlrOutputs._verify_applied([t], [o]))


class StrayHead(unittest.TestCase):
    """`_stray_head` alone: which output a sentence names, and which mismatches are not one."""

    def out(self, name, x, y, active=True):
        o = core.OutputState(name=name, active=active, ident=1)
        o.x, o.y = x, y
        return o

    def test_snapshot_order_decides_which_output_is_named(self):
        """Two heads adrift: the one the compositor lists first is the one the message is about, so the
        sentence does not change between runs with the dict's iteration order."""
        fresh = [self.out("Virtual-3", 9, 9), self.out("Virtual-2", 8, 8)]
        pos = {"Virtual-2": (1920, 0), "Virtual-3": (3840, 0)}
        self.assertEqual(core._stray_head(pos, fresh), ("Virtual-3", (3840, 0), (9, 9)))

    def test_a_head_that_is_off_is_not_a_stray(self):
        fresh = [self.out("Virtual-3", 0, 0, active=False)]
        self.assertIsNone(core._stray_head({"Virtual-3": (3840, 0)}, fresh))

    def test_an_output_with_no_planned_position_is_not_a_stray(self):
        fresh = [self.out("Virtual-1", 4096, 0)]
        self.assertIsNone(core._stray_head({}, fresh))

    def test_the_layout_that_landed_is_no_stray_at_all(self):
        fresh = [self.out("Virtual-2", 1920, 0), self.out("Virtual-1", 0, 0)]
        self.assertIsNone(core._stray_head({"Virtual-1": (0, 0), "Virtual-2": (1920, 0)}, fresh))


if __name__ == "__main__":
    unittest.main()
