#!/usr/bin/env python3
"""U20: wxrandr's Hyprland backend -- `j/monitors` to read, `keyword monitor` to write.

Why there is a backend here at all is one measurement [M recon2/hyprland.md §4, repeated on 0.56.2 in
recon2/arch.md]: Hyprland advertises `zwlr_output_manager_v1` version 4 and takes exactly ONE apply per
session through it. The second times out at 10 s with nothing changed and no `[COutputConfiguration] Applying
configuration` in Hyprland's own log; with a second output present even the first one hangs; and `wlr-randr`,
the reference client, hangs for ever on the same request. Its own `keyword monitor` applied at once on a
session that had never touched the protocol -- so that is the route, and U08 (tests/test_wxrandr_hostile.py)
is the sentence `--backend wlr` gets on such a session.

The double is `support.FakeHypr` replaying the recorded `j/monitors` (a 1920x1080@74.998 Virtual-1 and a
scale-2.0 HEADLESS-2 at +1920+0), plus one thing no recon report recorded and this file has to model: what
`j/monitors` says AFTER a `keyword monitor`. `_ApplyingHypr` below is that, and its `deaf` mood is the
measured post-wedge Hyprland -- `ok` to a `keyword monitor` it then ignores, three times in a row.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from w11common import session
from wdotool.hypr_ipc import HyprIPC
from wxrandr import cli, core, hypr

#: `NAME,WxH@Hz,XxY,SCALE` and the keyword pairs after it
_LINE = re.compile(r"^([^,]+),(.*)$")


class _ApplyingHypr(support.FakeHypr):
    """`support.FakeHypr` that lets a `keyword monitor` change what `j/monitors` says next.

    The recorded double replays one session and answers `ok` to every keyword, which is all a request-log
    assertion needs; an apply that has to be VERIFIED needs the other half. Only the four positional fields
    and the two keyword pairs this backend writes are understood, because they are the only ones it writes.

    `deaf=True` answers `ok` and changes nothing -- Hyprland after its wlr-output-management path has wedged,
    measured three times in a row on one session [M recon2/hyprland.md §4]."""

    def __init__(self, mode="ok", deaf=False, conf_dir=None, **kw):
        self.deaf = deaf
        #: Hyprland's config directory as this double sees it: `reload` applies what the files
        #: hyprland.conf sources say, which is the route-2 half of the backend.  None is a compositor
        #: whose config this run cannot see, and its `reload` changes nothing -- exactly what a reload
        #: does when hyprland.conf never learned to source our file.
        self.conf_dir = conf_dir
        self.keywords = []
        self.reloads = 0
        super().__init__(mode, **kw)

    def reply_for(self, req: str) -> bytes:
        if req.startswith("keyword monitor "):
            self.keywords.append(req[len("keyword monitor "):])
            if not self.deaf:
                self._apply(self.keywords[-1])
            return b"ok"
        if req.strip() == "reload":
            self.reloads += 1
            for spec in self.sourced_rules():
                self._apply(spec)
            return b"ok"
        return super().reply_for(req)

    def sourced_rules(self) -> list:
        """Every `monitor =` argument in the files hyprland.conf sources, in config order.

        The rule Hyprland keeps is the LAST matching one, which is why wxrandr appends its `source =` line
        rather than putting it anywhere else, and why this double applies them in order."""
        out = []
        if not self.conf_dir:
            return out
        try:
            with open(os.path.join(self.conf_dir, "hyprland.conf"), encoding="utf-8") as fh:
                conf = fh.read()
        except OSError:
            return out
        for line in conf.splitlines():
            m = re.match(r"\s*source\s*=\s*(.+?)\s*$", line)
            if not m:
                continue
            try:
                with open(m.group(1), encoding="utf-8") as fh:
                    body = fh.read()
            except OSError:
                continue
            for rule in body.splitlines():
                m2 = re.match(r"\s*monitor\s*=\s*(.+?)\s*$", rule)
                if m2:
                    out.append(m2.group(1))
        return out

    def _rows_key(self) -> str:
        """Whichever key this double is serving its monitor rows out of.  `support.FakeHypr` answers
        `j/monitors all` off `monitors` unless a test planted `monitors all` itself, and a real `keyword
        monitor` changes what BOTH questions answer -- so the apply has to land on the one being served or
        the re-read that verifies it would see the row it started with."""
        return "monitors all" if "monitors all" in self.payloads else "monitors"

    def _apply(self, spec: str):
        m = _LINE.match(spec)
        name, rest = m.group(1), m.group(2)
        key = self._rows_key()
        rows = json.loads(json.dumps(self.payloads[key]))
        row = next((r for r in rows if r["name"] == name), None)
        if row is None:
            return
        fields = rest.split(",")
        if fields[0] == "disable":
            row["disabled"] = True
        else:
            row["disabled"] = False
            size, rate = (fields[0].split("@") + [None])[:2]
            if "x" in size:
                row["width"], row["height"] = (int(v) for v in size.split("x"))
            if rate:
                row["refreshRate"] = float(rate)
            row["x"], row["y"] = (int(v) for v in fields[1].split("x"))
            row["scale"] = float(fields[2])
            row["transform"] = 0
            row["mirrorOf"] = "none"
            for i in range(3, len(fields) - 1, 2):
                if fields[i] == "transform":
                    row["transform"] = int(fields[i + 1])
                elif fields[i] == "mirror":
                    row["mirrorOf"] = fields[i + 1]
        self.payloads[key] = rows


class Base(unittest.TestCase):
    """A FakeHypr whose socket is where `session.find_hypr_socket()` looks."""

    DOUBLE = _ApplyingHypr

    def hypr(self, **kw):
        srv = self.DOUBLE(**kw)
        self.addCleanup(srv.close)
        self.srv = srv
        return srv

    def plant(self, srv=None, sig="sig0"):
        """`<runtime dir>/hypr/<sig>/.socket.sock` pointing at the double's own socket.

        A symlink, because the double picks its own temp directory: `find_hypr_socket()` stats the path
        (os.path.exists and the owner check both follow it) and connect() follows it too, so what is being
        exercised is the real finder and not a stub of it."""
        srv = srv or self.hypr()
        self.tmp = tempfile.mkdtemp(prefix="wxr-hypr-rt-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        d = os.path.join(self.tmp, "hypr", sig)
        os.makedirs(d)
        os.symlink(srv.path, os.path.join(d, ".socket.sock"))
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = self.tmp
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)
        return srv

    def outputs(self, srv=None) -> "hypr.HyprOutputs":
        srv = srv or self.hypr()
        return hypr.HyprOutputs(ipc=HyprIPC(srv.path))

    def state(self) -> core.State:
        d = tempfile.mkdtemp(prefix="wxr-hypr-st-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return core.State(os.path.join(d, "key"))

    def run_cli(self, *argv, **env):
        """The real cli.Session, over the planted socket: the branch in wxrandr/cli.py that builds
        HyprOutputs is part of what U20 has to prove, so nothing here is stubbed."""
        base = dict(XDG_RUNTIME_DIR=self.tmp, HYPRLAND_INSTANCE_SIGNATURE=None,
                    WAYLAND_DISPLAY=None, DISPLAY=None, WXRANDR_BACKEND=None,
                    XDG_SESSION_TYPE="wayland", SWAYSOCK=None, I3SOCK=None)
        base.update(env)
        out, err = io.StringIO(), io.StringIO()
        with support.env(**base):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()


class Snapshot(Base):
    """`j/monitors` -> OutputState, and the `--query` block it renders."""

    def test_the_query_block_is_the_recorded_session(self):
        """74.998 Hz prints as 75.00 because xrandr's refresh column is `%6.2f`, and the scale-2.0 head is
        960x540 of layout at +1920+0 where the compositor drives 1920x1080 of pixels."""
        self.plant()
        code, out, err = self.run_cli("--query")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0],
                         "Screen 0: minimum 16 x 16, current 2880 x 1080, maximum 32767 x 32767")
        self.assertEqual(lines[1], "Virtual-1 connected 1920x1080+0+0 "
                         "(normal left inverted right x axis y axis) 480mm x 270mm")
        self.assertEqual(lines[2], "   1920x1080     75.00*+  60.00    50.00  ")
        head = [ln for ln in lines if ln.startswith("HEADLESS-2")]
        self.assertEqual(head, ["HEADLESS-2 connected 960x540+1920+0 "
                                "(normal left inverted right x axis y axis) 0mm x 0mm"])
        self.assertEqual(lines[lines.index(head[0]) + 1], "   1920x1080      0.06*+")

    def test_make_model_serial_and_the_mode_list(self):
        outs = {o.name: o for o in self.outputs().snapshot(self.state())}
        v = outs["Virtual-1"]
        # an empty serial reads `Unknown`, which is what xrandr prints for an output that has none
        self.assertEqual((v.make, v.model, v.serial), ("Red Hat, Inc.", "QEMU Monitor", "Unknown"))
        self.assertEqual(len(v.modes), 26)
        self.assertEqual((v.current.w, v.current.h, v.current.refresh_mhz), (1920, 1080, 75000))
        self.assertTrue(v.current.preferred)
        h = outs["HEADLESS-2"]
        self.assertEqual((h.make, h.model), ("Unknown", "Unknown"))
        self.assertEqual((h.mm_w, h.mm_h, h.scale), (0, 0, 2.0))

    def test_the_current_mode_is_the_advertised_one_nearest_the_live_rate(self):
        """`refreshRate` is 74.998 and `availableModes` says `1920x1080@75.00Hz`: one mode row, not two
        reading 75.00 apiece."""
        v = [o for o in self.outputs().snapshot(self.state()) if o.name == "Virtual-1"][0]
        same = [m for m in v.modes if (m.w, m.h) == (1920, 1080)]
        self.assertEqual([m.refresh_mhz for m in same], [75000, 60000, 50000])
        self.assertIs(v.current, same[0])

    def test_a_mode_the_list_does_not_carry_is_still_reported(self):
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[0]["width"], mons[0]["height"] = 1234, 567
        srv.payloads["monitors"] = mons
        v = [o for o in self.outputs(srv).snapshot(self.state()) if o.name == "Virtual-1"][0]
        self.assertEqual((v.current.w, v.current.h), (1234, 567))
        self.assertEqual((v.w, v.h), (1234, 567))

    def test_a_disabled_monitor_is_inactive_and_has_no_rectangle(self):
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[1]["disabled"] = True
        srv.payloads["monitors"] = mons
        h = [o for o in self.outputs(srv).snapshot(self.state()) if o.name == "HEADLESS-2"][0]
        self.assertFalse(h.active)
        self.assertEqual((h.x, h.y, h.w, h.h), (0, 0, 0, 0))

    def test_the_transform_is_the_wlroots_numbering_and_not_the_spec_reading(self):
        """Measured: `wxrandr --output Virtual-1 --rotate left` over the protocol put sway transform "270" on
        the wire and `hyprctl` reported `transform: 3` [M recon2/hyprland.md §4]. That is core.WL_TRANSFORM's
        numbering (3 == 270 == xrandr `left`); core.from_wl_spec_transform reads 3 as `right`, which is how
        Mutter and KWin number the same enum, and using it here would report every rotated Hyprland head
        ninety degrees out."""
        self.assertEqual(core.from_wl_spec_transform(3), "90")     # the numbering NOT used here
        srv = self.hypr()
        for number, sway_tf, word, wh in ((3, "270", "left", (1080, 1920)),
                                          (1, "90", "right", (1080, 1920)),
                                          (2, "180", "inverted", (1920, 1080))):
            mons = json.loads(json.dumps(srv.payloads["monitors"]))
            mons[0]["transform"] = number
            srv.payloads["monitors"] = mons
            v = [o for o in self.outputs(srv).snapshot(self.state()) if o.name == "Virtual-1"][0]
            self.assertEqual(v.transform, sway_tf, number)
            self.assertEqual((v.w, v.h), wh, number)
            self.assertIn(" " + word + " ", core.render_output_header(v, None))

    def test_a_mirror_is_remembered_for_the_apply(self):
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[1]["mirrorOf"] = "Virtual-1"
        srv.payloads["monitors"] = mons
        out = self.outputs(srv)
        out.snapshot(self.state())
        self.assertEqual(out.mirrors, {"HEADLESS-2": "Virtual-1"})

    def test_the_mirror_is_remembered_by_name_when_hyprland_answers_with_an_id(self):
        """What a real Hyprland puts in `mirrorOf` is the mirrored monitor's numeric `id` as a string, not
        its name.  Measured live on resolute-hypr (0.53.3) 2026-09-09:

            $ hyprctl keyword monitor "Virtual-3,1920x1080@60,3840x0,1,mirror,Virtual-1"   -> ok
            $ hyprctl -j monitors all | ...                        -> Virtual-3 mirrorOf "0"

        and `wxrandr --output Virtual-3 --same-as Virtual-1` produced the same "0".  Both spellings apply
        (`,mirror,0` and `,mirror,Virtual-2` were each accepted in the same session), so the reason to
        translate is what happens next: `self.mirrors` is re-emitted on a LATER apply that does not mention
        the mirror, and an id is a position in Hyprland's own list that a hotplug moves."""
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        self.assertEqual(str(mons[0]["id"]), "0")           # the recording's own ids
        mons[1]["mirrorOf"] = "0"                           # what the compositor really answers
        srv.payloads["monitors"] = mons
        out = self.outputs(srv)
        out.snapshot(self.state())
        self.assertEqual(out.mirrors, {"HEADLESS-2": "Virtual-1"})

    def test_an_id_that_names_no_monitor_is_kept_as_it_came(self):
        """A row that vanished between the read and the translation is not worth inventing a name for: the
        value goes back out as Hyprland gave it, which is a spelling Hyprland takes."""
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[1]["mirrorOf"] = "7"
        srv.payloads["monitors"] = mons
        out = self.outputs(srv)
        out.snapshot(self.state())
        self.assertEqual(out.mirrors, {"HEADLESS-2": "7"})

    def test_a_monitors_answer_that_is_not_a_list_is_one_line(self):
        srv = self.hypr(payloads={"monitors": {"oops": 1}})
        with self.assertRaises(core.Fatal) as cm:
            self.outputs(srv).snapshot(self.state())
        self.assertEqual(str(cm.exception),
                         "the compositor's j/monitors answer is not a list of monitors\n")


class KeywordLines(Base):
    """The apply's whole contract: one `keyword monitor` argument per output, byte for byte."""

    def test_mode_position_and_scale(self):
        out = self.outputs()
        state = self.state()
        outs = {o.name: o for o in out.snapshot(state)}
        t = core.Target(output=outs["Virtual-1"], stanza=None, enabled=True,
                        mode=core.Mode(w=1280, h=1024, refresh_mhz=60020), scale=1.0)
        self.assertEqual(out.monitor_line(t, {"Virtual-1": (0, 0)}),
                         "Virtual-1,1280x1024@60.02,0x0,1")
        t.scale = 1.5
        self.assertEqual(out.monitor_line(t, {"Virtual-1": (100, 200)}),
                         "Virtual-1,1280x1024@60.02,100x200,1.5")

    def test_a_whole_number_rate_carries_no_decimals(self):
        out = self.outputs()
        outs = {o.name: o for o in out.snapshot(self.state())}
        t = core.Target(output=outs["Virtual-1"], stanza=None,
                        mode=core.Mode(w=1920, h=1080, refresh_mhz=75000), scale=1.0)
        self.assertEqual(out.monitor_line(t, {}), "Virtual-1,1920x1080@75,0x0,1")

    def test_a_mode_with_no_rate_leaves_the_field_out(self):
        out = self.outputs()
        outs = {o.name: o for o in out.snapshot(self.state())}
        t = core.Target(output=outs["HEADLESS-2"], stanza=None,
                        mode=core.Mode(w=800, h=600), scale=1.0)
        self.assertEqual(out.monitor_line(t, {"HEADLESS-2": (0, 0)}), "HEADLESS-2,800x600,0x0,1")

    def test_transform_is_appended_as_the_wlroots_number(self):
        out = self.outputs()
        outs = {o.name: o for o in out.snapshot(self.state())}
        t = core.Target(output=outs["Virtual-1"], stanza=None,
                        mode=core.Mode(w=1920, h=1080, refresh_mhz=75000), scale=1.0, sway_tf="270")
        self.assertEqual(out.monitor_line(t, {"Virtual-1": (0, 0)}),
                         "Virtual-1,1920x1080@75,0x0,1,transform,3")

    def test_an_output_going_off_is_the_disable_word(self):
        out = self.outputs()
        outs = {o.name: o for o in out.snapshot(self.state())}
        t = core.Target(output=outs["HEADLESS-2"], stanza=None, enabled=False)
        self.assertEqual(out.monitor_line(t, {}), "HEADLESS-2,disable")

    def test_same_as_becomes_the_mirror_pair(self):
        out = self.outputs()
        outs = {o.name: o for o in out.snapshot(self.state())}
        st = core.Stanza(name="HEADLESS-2", relation=("same-as", "Virtual-1"))
        t = core.Target(output=outs["HEADLESS-2"], stanza=st,
                        mode=core.Mode(w=1920, h=1080, refresh_mhz=60), scale=1.0)
        self.assertEqual(out.monitor_line(t, {"HEADLESS-2": (0, 0)}),
                         "HEADLESS-2,1920x1080@0.06,0x0,1,mirror,Virtual-1")

    def test_an_existing_mirror_survives_a_line_that_does_not_mention_it(self):
        """A `keyword monitor` with no `mirror` field CLEARS one, so a `--pos` on a mirrored output would
        silently un-mirror it."""
        srv = self.hypr()
        mons = json.loads(json.dumps(srv.payloads["monitors"]))
        mons[1]["mirrorOf"] = "Virtual-1"
        srv.payloads["monitors"] = mons
        out = self.outputs(srv)
        outs = {o.name: o for o in out.snapshot(self.state())}
        t = core.Target(output=outs["HEADLESS-2"], stanza=None,
                        mode=core.Mode(w=1920, h=1080, refresh_mhz=60), scale=1.0)
        self.assertTrue(out.monitor_line(t, {"HEADLESS-2": (0, 0)}).endswith(",mirror,Virtual-1"))
        # ... and a relation that is not `same-as` drops it, because the user just placed it somewhere else
        t.stanza = core.Stanza(name="HEADLESS-2", relation=("right-of", "Virtual-1"))
        self.assertFalse(out.monitor_line(t, {"HEADLESS-2": (1920, 0)}).endswith(",mirror,Virtual-1"))


class Apply(Base):
    """The CLI end to end, over the real Session and the real finder."""

    def test_a_mode_change_is_one_keyword_and_the_re_read_agrees(self):
        srv = self.plant()
        code, out, err = self.run_cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(srv.keywords, ["Virtual-1,1280x1024@60.02,0x0,1"])
        self.assertEqual(srv.payloads["monitors"][0]["width"], 1280)

    def test_only_the_touched_outputs_are_written(self):
        srv = self.plant()
        self.run_cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual([k.split(",")[0] for k in srv.keywords], ["Virtual-1"])

    def test_off_and_auto(self):
        """`NAME,disable` off, and a plain mode line back on.

        A disabled `j/monitors` row publishes no `x`/`y` and no scale worth reading, so `--auto` puts the
        head back at 0x0 at scale 1 -- which is what `xrandr --auto` does with an output it is enabling and
        was given no position for, and it is verified by the re-read like every other apply."""
        srv = self.plant()
        code, _out, err = self.run_cli("--output", "HEADLESS-2", "--off")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(srv.keywords, ["HEADLESS-2,disable"])
        self.assertTrue(srv.payloads["monitors"][1]["disabled"])
        code, _out, err = self.run_cli("--output", "HEADLESS-2", "--auto")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(srv.keywords[1:], ["HEADLESS-2,1920x1080@0.06,0x0,1"])
        self.assertFalse(srv.payloads["monitors"][1]["disabled"])

    def test_the_reader_asks_for_all_and_never_the_plain_list(self):
        """`j/monitors all` and `j/monitors` are two different questions, and only the first one lists a
        head Hyprland has disabled.  Measured live on resolute-hypr (Hyprland 0.53.3) 2026-09-09, with
        Virtual-3 off:

            j/monitors      -> Virtual-1, Virtual-2
            j/monitors all  -> Virtual-1, Virtual-2, Virtual-3 (disabled: true)

        so asking the plain one made `--off` refuse with `Hyprland accepted the configuration for Virtual-3
        and then stopped listing it`, and left `--auto`/`--right-of`/`--below` warning `output Virtual-3 not
        found; ignoring` about an output that was there all along.  Asserted on the wire, because this is a
        claim about the REQUEST and the double answers both from one payload."""
        srv = self.plant()
        self.run_cli("--query")
        asked = [r for r in srv.requests if r.startswith("j/monitors")]
        self.assertTrue(asked, srv.requests)
        self.assertEqual(set(asked), {"j/monitors all"})

    def test_a_head_disabled_earlier_is_still_there_to_turn_back_on(self):
        """The live cascade, end to end, off the recorded three-head document: with Virtual-3 disabled,
        `--query` must still list it (xrandr keeps an `--off` output in the listing, and X is the oracle)
        and `--auto` must find it rather than warn it away."""
        rows = support.fixture_json("hypr", "monitors-all-one-disabled.json")
        # planted under `monitors all` and NOT under `monitors`, so the alias in support.FakeHypr does not
        # carry this test: a reader that went back to the plain question would get the two-head recording
        # instead of these three and go red here as well as in the wire test above
        srv = self.plant(self.hypr(payloads={"monitors all": rows}))
        code, out, err = self.run_cli("--query")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Virtual-3", out)
        code, _out, err = self.run_cli("--output", "Virtual-3", "--auto")
        self.assertEqual(code, 0, err)
        self.assertNotIn("not found", err)
        # 0x0 and not the 3840x0 the disabled row still publishes: `snapshot()` reads x/y only for an
        # active head, and `xrandr --auto` on an output it is enabling with no position given puts it at
        # the origin.  X is the oracle, so the friendlier answer would be a flag and not this line.
        self.assertEqual(srv.keywords, ["Virtual-3,1920x1080@75,0x0,1"])

    def test_a_rotation_goes_out_as_the_wlroots_number(self):
        srv = self.plant()
        code, _out, err = self.run_cli("--output", "Virtual-1", "--rotate", "left")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(srv.keywords, ["Virtual-1,1920x1080@75,0x0,1,transform,3"])
        self.assertEqual(srv.payloads["monitors"][0]["transform"], 3)

    def test_a_dryrun_sends_nothing(self):
        srv = self.plant()
        code, out, err = self.run_cli("--dryrun", "--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 0)
        self.assertEqual(srv.keywords, [])

    def deaf(self):
        """A Hyprland deaf to `keyword monitor` AND with no config this run can reach, so the route-2
        reload below it changes nothing either: what is left is the refusal, which is what these three
        assert.  `XDG_CONFIG_HOME` is a directory of this test's own -- the fallback writes a file, and
        the one place it may never write is the config of whoever is running the suite."""
        self.conf_root = tempfile.mkdtemp(prefix="wxr-hypr-cfg-")
        self.addCleanup(shutil.rmtree, self.conf_root, ignore_errors=True)
        return self.plant(self.hypr(deaf=True))

    def test_a_compositor_that_says_ok_and_does_nothing_is_one_line(self):
        """The measured post-wedge Hyprland: `hyprctl keyword monitor` accepted with `ok` and no change,
        three times in a row on one session [M recon2/hyprland.md §4], and every apply of a fresh 0.56.2
        session [M goal2/recon/flavors.md 3b]. Trusting the `ok` would report a layout that is not on the
        screen; the route-2 reload is tried first and this is what is left when that lands nothing."""
        self.deaf()
        code, _out, err = self.run_cli("--output", "Virtual-1", "--mode", "1280x1024",
                                       XDG_CONFIG_HOME=self.conf_root)
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[-1],
                         "xrandr: Hyprland accepted the mode 1280x1024 for Virtual-1 and did not "
                         "apply it (it reports 1920x1080)")

    def test_a_position_that_does_not_move_is_one_line(self):
        self.deaf()
        code, _out, err = self.run_cli("--output", "HEADLESS-2", "--pos", "3000x0",
                                       XDG_CONFIG_HOME=self.conf_root)
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[-1],
                         "xrandr: Hyprland accepted the position 3000x0 for HEADLESS-2 and did not "
                         "apply it (it reports 1920x0)")

    def test_an_output_that_will_not_go_off_is_one_line(self):
        self.deaf()
        code, _out, err = self.run_cli("--output", "HEADLESS-2", "--off",
                                       XDG_CONFIG_HOME=self.conf_root)
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines()[-1],
                         "xrandr: Hyprland accepted HEADLESS-2 off and did not apply it (it is still on)")

    def test_a_refused_keyword_is_hyprlands_own_words(self):
        srv = self.plant(self.hypr(payloads={}))
        srv.reply_for = lambda req: (b"Invalid keyword" if req.startswith("keyword")
                                     else support.FakeHypr.reply_for(srv, req))
        code, _out, err = self.run_cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual((code, err), (1, "xrandr: hypr: Invalid keyword\n"))

    def test_persistent_writes_the_file_where_the_live_keyword_landed(self):
        """`--persistent` on a session whose `keyword monitor` DOES apply (0.53.3, and the fast path
        everywhere): the layout is already on the screen, so the rule goes into the file for the next
        session and no `reload` request is sent.

        No request is not no reload, and the line this run prints says so: Hyprland watches the files it
        sources, so the write is one.  Measured on the arch-hypr golden (0.56.2, 2026-09-11): a bare
        rewrite of the sourced file with no `hyprctl reload` anywhere applied the new mode and put
        `general:gaps_out` from `40 40 40 40, set: true` back to `20 20 20 20, set: false`, and appending a
        line to hyprland.conf -- which is exactly what the first `--persistent` run does -- did the same."""
        root = tempfile.mkdtemp(prefix="wxr-hypr-cfg-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        conf = os.path.join(root, "hypr")
        os.makedirs(conf)
        with open(os.path.join(conf, "hyprland.conf"), "w", encoding="utf-8") as fh:
            fh.write("monitor = Virtual-1,preferred,0x0,1\n")
        srv = self.plant(self.hypr(conf_dir=conf))
        code, _out, err = self.run_cli("--persistent", "--output", "Virtual-1", "--mode", "1280x1024",
                                       XDG_CONFIG_HOME=root)
        self.assertEqual(code, 0, err)
        self.assertEqual(srv.keywords, ["Virtual-1,1280x1024@60.02,0x0,1"])
        self.assertEqual(srv.reloads, 0)
        with open(os.path.join(conf, "w11-monitors.conf"), encoding="utf-8") as fh:
            rules = [ln for ln in fh.read().splitlines() if ln.startswith("monitor =")]
        self.assertEqual(rules, ["monitor = Virtual-1,1280x1024@60.02,0x0,1"])
        rules_path = os.path.join(conf, "w11-monitors.conf")
        self.assertEqual(err.splitlines(), [
            "xrandr: added a `source = %s` line to %s" % (rules_path, os.path.join(conf, "hyprland.conf")),
            "xrandr: --persistent: the layout is in %s for the next session too, and writing that file is "
            "itself a reload, no `hyprctl reload` needed (the whole config is re-read: runtime "
            "`hyprctl keyword`s set since login are reset)" % rules_path,
        ])

class RouteTwo(Base):
    """The apply a fresh Hyprland 0.56.2 needs: the rules in a file hyprland.conf sources, and `reload`.

    Measured on the arch-hypr golden (Hyprland 0.56.2, 2026-09-11): `hyprctl keyword monitor` answered `ok`
    in 4 ms to three modes out of the head's own `availableModes` and to a scale, and changed NOTHING, while
    the same rules written into a `source =`d file plus `hyprctl reload` applied mode, scale, transform and
    disable -- all four [M goal2/recon/flavors.md 3b].  `_ApplyingHypr(deaf=True, conf_dir=...)` is that
    compositor: deaf to the keyword, obedient to the reload.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wxr-hypr-cfg-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.conf = os.path.join(self.root, "hypr")
        os.makedirs(self.conf)
        self.hyprconf = os.path.join(self.conf, "hyprland.conf")
        self.rules = os.path.join(self.conf, "w11-monitors.conf")
        # the rig's own config, which is what an appended `source =` has to come after: those four lines
        # are vm/build-image.sh's desktop_hypr, byte for byte
        with open(self.hyprconf, "w", encoding="utf-8") as fh:
            fh.write("monitor = , preferred, auto, 1\nmonitor = Virtual-1,preferred,0x0,1\n")

    def deafsrv(self):
        return self.plant(self.hypr(deaf=True, conf_dir=self.conf))

    def cli(self, *argv):
        return self.run_cli(*argv, XDG_CONFIG_HOME=self.root)

    def rule_lines(self) -> list:
        with open(self.rules, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.startswith("monitor =")]

    def source_lines(self) -> list:
        with open(self.hyprconf, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.startswith("source =")]

    def test_a_keyword_that_did_nothing_is_followed_by_the_file_and_a_reload(self):
        srv = self.deafsrv()
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 0, err)
        # the fast path was tried first and once: the file is the fallback, not the route
        self.assertEqual(srv.keywords, ["Virtual-1,1280x1024@60.02,0x0,1"])
        self.assertEqual(srv.reloads, 1)
        # and the mode really landed, through the reload's reading of the sourced file
        self.assertEqual(srv.payloads["monitors"][0]["width"], 1280)
        self.assertIn("xrandr: Hyprland accepted the live `keyword monitor` and did not apply it", err)
        self.assertIn(self.rules, err)

    def test_the_source_line_is_appended_after_the_config_and_only_once(self):
        self.deafsrv()
        self.assertEqual((self.cli("--output", "Virtual-1", "--mode", "1280x1024"))[0], 0)
        self.assertEqual(self.source_lines(), ["source = %s" % self.rules])
        with open(self.hyprconf, encoding="utf-8") as fh:
            body = fh.read()
        # after the rig's own rules, because Hyprland keeps the LAST matching one
        self.assertLess(body.index("monitor = Virtual-1,preferred,0x0,1"), body.index("source ="))
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1680x1050")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.source_lines(), ["source = %s" % self.rules])

    def test_a_run_without_persistent_keeps_the_rule_and_says_why(self):
        """The plan said a non-persistent apply takes its own line out again.  The measurement said
        otherwise and the measurement wins: Hyprland watches the files it sources, so on the arch-hypr
        golden (0.56.2, 2026-09-11) the rule applied on the WRITE, before `hyprctl reload` was sent, and
        writing the file back empty put Virtual-1 from 1024x768 to 1920x1080 again before the next read --
        removing the rule IS an apply.  So the rule stays, the layout stays with it, and the run says what
        to delete."""
        srv = self.deafsrv()
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rule_lines(), ["monitor = Virtual-1,1280x1024@60.02,0x0,1"])
        self.assertEqual(srv.payloads["monitors"][0]["width"], 1280)
        self.assertIn("those rules stay in %s, with or without --persistent" % self.rules, err)
        self.assertIn("Delete that file, or the `source =` line hyprland.conf carries for it", err)

    def test_persistent_does_not_repeat_what_the_run_already_had_to_do(self):
        """The same apply with the flag: the same file, the same rule -- and no line telling the user the
        rules stayed, because staying is what was asked for."""
        self.deafsrv()
        code, _out, err = self.cli("--persistent", "--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rule_lines(), ["monitor = Virtual-1,1280x1024@60.02,0x0,1"])
        self.assertNotIn("with or without --persistent", err)

    def test_persistent_keeps_the_rule_in_the_file(self):
        srv = self.deafsrv()
        code, _out, err = self.cli("--persistent", "--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rule_lines(), ["monitor = Virtual-1,1280x1024@60.02,0x0,1"])
        self.assertEqual(srv.reloads, 1)

    def test_a_second_output_does_not_take_the_first_ones_rule_with_it(self):
        """Two `--persistent` runs, one output each: the file is merged, not replaced."""
        self.deafsrv()
        self.assertEqual((self.cli("--persistent", "--output", "Virtual-1", "--mode", "1280x1024"))[0], 0)
        self.assertEqual((self.cli("--persistent", "--output", "HEADLESS-2", "--pos", "3000x0"))[0], 0)
        self.assertEqual(self.rule_lines(), ["monitor = Virtual-1,1280x1024@60.02,0x0,1",
                                             "monitor = HEADLESS-2,1920x1080@0.06,3000x0,2"])

    def test_a_failed_route_two_apply_leaves_no_rule_behind(self):
        """The reload landed nothing (this compositor's config is not the one this run can see), so the
        refusal is printed -- and the rule a non-persistent run wrote is still taken out, in the `finally`,
        so the next session does not come up in a layout that never worked."""
        srv = self.plant(self.hypr(deaf=True, conf_dir=None))
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 1)
        self.assertEqual(srv.reloads, 1)
        self.assertEqual(self.rule_lines(), [])
        self.assertEqual(err.splitlines()[-1],
                         "xrandr: Hyprland accepted the mode 1280x1024 for Virtual-1 and did not "
                         "apply it (it reports 1920x1080)")

    def test_a_config_that_is_not_there_is_not_created(self):
        """A hyprland.conf written by this tool would BE the next session's whole config -- Hyprland's own
        default file carries the keybinds -- so the one that is missing is named in a line instead."""
        os.unlink(self.hyprconf)
        self.plant(self.hypr(deaf=True, conf_dir=self.conf))
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.hyprconf))
        self.assertIn("there is no %s to source the rules from" % self.hyprconf, err)
        self.assertIn("add `source = %s` to the config this session started with" % self.rules, err)

    def test_no_reload_is_sent_when_nothing_sources_our_file(self):
        """The reload is not free -- `hyprctl keyword general:gaps_out 40` read back `40 40 40 40,
        set: true` and was `20 20 20 20, set: false` after one [M arch-hypr golden, 0.56.2, 2026-09-11] --
        so a config that does not mention our file does not get one sent: it would pay the whole cost of a
        reload for none of its effect.  And nothing claims a reload that was not sent."""
        os.unlink(self.hyprconf)
        srv = self.plant(self.hypr(deaf=True, conf_dir=self.conf))
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 1)
        self.assertEqual(srv.reloads, 0)
        self.assertNotIn("the config was reloaded", err)

    def test_the_reloaded_line_is_not_printed_by_a_route_two_apply_that_failed(self):
        """`WROTE_CONF_NOTE` says the layout went into the file and the config was reloaded.  Printed
        before the reload it claimed both while neither had happened yet -- and the `finally` below takes
        the rule back out, so after a failure neither is true at all."""
        srv = self.plant(self.hypr(deaf=True, conf_dir=None))
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1280x1024")
        self.assertEqual(code, 1)
        self.assertEqual(srv.reloads, 1)
        self.assertNotIn("the config was reloaded", err)
        self.assertNotIn("those rules stay in", err)

    def test_a_failed_apply_leaves_an_earlier_runs_rule_for_the_same_output_alone(self):
        """The rollback is the file as it was, not a pop of the names this run touched.

        A `--persistent` run had put Virtual-1 at 1280x1024 in the file and that layout is what the next
        login reads; a later run for the SAME output that does not land must not take it away with its own.
        Popping by name did exactly that -- one failed `--mode 1680x1050` and the persisted 1280x1024 was
        gone from a file nobody had asked to change."""
        self.deafsrv()
        self.assertEqual((self.cli("--persistent", "--output", "Virtual-1", "--mode", "1280x1024"))[0], 0)
        self.assertEqual(self.rule_lines(), ["monitor = Virtual-1,1280x1024@60.02,0x0,1"])
        # a compositor whose config this run cannot see: the reload lands nothing and the apply fails
        self.plant(self.hypr(deaf=True, conf_dir=None))
        code, _out, err = self.cli("--output", "Virtual-1", "--mode", "1680x1050")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.rule_lines(), ["monitor = Virtual-1,1280x1024@60.02,0x0,1"])


class Dryrun(Base):
    def test_the_dryrun_backend_hook_sends_nothing_at_all(self):
        out = self.outputs()
        self.assertIsNone(out.verify(self.state(), []))
        self.assertEqual([r for r in self.srv.requests if not r.startswith("j/")], [])


class Probe(Base):
    """`--backends`, `--print-backend` and the two spellings `--backend` takes."""

    def test_the_backends_row_names_the_socket(self):
        self.plant()
        code, out, _err = self.run_cli("--backends")
        self.assertEqual(code, 0)
        rows = {ln[2:10].strip(): ln for ln in out.splitlines()}
        self.assertEqual(rows["hypr"],
                         "* hypr      available    IPC socket %s"
                         % os.path.join(self.tmp, "hypr", "sig0", ".socket.sock"))

    def test_print_backend_verbose_names_the_version_and_the_protocol(self):
        self.plant()
        code, out, _err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "hypr",
            "session: wayland",
            "chosen by: detection",
            "compositor: Hyprland 0.53.3",
            "protocol: Hyprland IPC (hyprctl)",
            "available: yes",
        ])

    def test_the_alias_hyprland_resolves_and_works(self):
        self.plant()
        self.assertEqual(cli.canonical_backend("hyprland"), "hypr")
        code, out, err = self.run_cli("--backend", "hyprland", "--query")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("Screen 0:"))

    def test_the_environment_variable_takes_it_too(self):
        self.plant()
        code, out, err = self.run_cli("--print-backend", WXRANDR_BACKEND="hyprland")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "hypr")

    def test_hypr_is_probed_second_so_a_hyprland_box_never_touches_the_bus(self):
        """`sway` first (an i3/sway socket is the only thing above it), then `hypr` -- and detection stops
        there, so the two D-Bus probes are never made on a Hyprland session. Hyprland owns neither bus name
        [M recon2/hyprland.md §2, `busctl --user list`]."""
        self.assertEqual(cli.AUTO_ORDER, ("sway", "hypr", "kwin", "mutter", "cinnamon"))
        self.assertEqual(cli.AUTO_FALLBACK, "wlr")
        self.plant()
        with support.env(XDG_RUNTIME_DIR=self.tmp, HYPRLAND_INSTANCE_SIGNATURE=None,
                         SWAYSOCK=None, I3SOCK=None):
            probes = {}
            self.assertEqual(cli.detect_wayland(probes)[0], "hypr")
        self.assertEqual(sorted(probes), ["hypr", "sway"])
        for p in probes.values():
            p.close()

    def test_a_wedged_compositor_ends_the_probe_at_its_own_deadline(self):
        """The probe's deadline is not the backend's: `hypr` is second in AUTO_ORDER and `--backends` runs
        every probe, so a box whose compositor is wedged inside its own event loop must not pay the apply
        path's ten seconds on every wxrandr run.

        A wedged compositor is the only thing that can spend the deadline -- it accepts the connection (the
        kernel does that for it) and then says nothing, so nothing but our own clock ends the wait. Driven at
        0.3 s rather than the shipped 2, and the assertion that it is well under IPC_TIMEOUT is what says the
        probe used its own constant and not the backend's."""
        self.plant(self.hypr(mode="wedged"))
        with mock.patch.object(hypr, "PROBE_TIMEOUT", 0.3):
            start = time.monotonic()
            code, out, _err = self.run_cli("--backends")
        spent = time.monotonic() - start
        self.assertEqual(code, 0)
        row = [ln for ln in out.splitlines() if ln[2:10].strip() == "hypr"][0]
        self.assertIn("unavailable", row)
        self.assertIn("no answer from the compositor within 0.3s", row)
        self.assertLess(spent, hypr.IPC_TIMEOUT)

    def test_a_socket_whose_compositor_is_gone_is_a_row_and_not_a_traceback(self):
        """A stale instance directory whose compositor exited between the finder and the connect: one row
        carrying Hyprland's silence, and the sweep goes on to the next backend."""
        self.plant(self.hypr(mode="gone"))
        code, out, _err = self.run_cli("--backends")
        self.assertEqual(code, 0)
        row = [ln for ln in out.splitlines() if ln[2:10].strip() == "hypr"][0]
        self.assertIn("unavailable", row)
        self.assertIn("closed the IPC socket without answering `j/version`", row)

    def test_an_answer_that_is_not_hyprlands_is_refused_by_name(self):
        self.plant(self.hypr(payloads={"version": {"nope": True}}))
        code, out, _err = self.run_cli("--backends")
        row = [ln for ln in out.splitlines() if ln[2:10].strip() == "hypr"][0]
        self.assertIn("did not answer j/version like Hyprland", row)

    def test_no_socket_at_all_is_the_pinned_reason(self):
        tmp = tempfile.mkdtemp(prefix="wxr-hypr-none-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = tmp
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)
        with support.env(XDG_RUNTIME_DIR=tmp, HYPRLAND_INSTANCE_SIGNATURE=None):
            p = cli.probe_backend("hypr")
        self.assertEqual((p.available, p.reason),
                         (False, "no Hyprland IPC socket ($HYPRLAND_INSTANCE_SIGNATURE)"))


class TheTwoClients(Base):
    """`wxrandr.hypr.HyprIPC` is a second copy of `wdotool.hypr_ipc.HyprIPC`'s reader, and this is what keeps
    them from drifting.

    The copy is not laziness: `scripts/build-pyz.sh` builds `dist/wxrandr` out of `w11common` and `wxrandr`
    alone, and `tests/test_build_scripts.py:TheZipapps.test_no_display_tool_carries_the_input_stack` pins
    that, so a `from wdotool...` in this package would work from the .deb and quietly not from the second
    install route -- and on Hyprland "quietly" means falling back to a wlr path that cannot apply
    [M recon2/hyprland.md §4]. `wdotool/layoutbox.py` carries a copy of Mutter's logical-size rule for the
    same reason, pinned the same way (tests/test_scale_spaces.py).

    So: same bytes on the wire, same sentence for every failure, differing only in the exception type each
    package speaks."""

    def pair(self, srv, timeout=10.0):
        from wdotool.hypr_ipc import HyprIPC as WdotoolIPC
        return hypr.HyprIPC(srv.path, timeout=timeout), WdotoolIPC(srv.path, timeout=timeout)

    def test_no_module_under_wxrandr_imports_wdotool(self):
        """The zipapp guard, as a check rather than a comment."""
        offenders = []
        d = os.path.join(ROOT, "wxrandr")
        for name in sorted(os.listdir(d)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                code = "\n".join(ln for ln in fh.read().splitlines()
                                 if not ln.lstrip().startswith("#"))
            if re.search(r"^\s*(from|import)\s+wdotool\b", code, re.M):
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_both_clients_send_the_same_bytes(self):
        srv = self.hypr()
        mine, theirs = self.pair(srv)
        self.assertEqual(mine.json("monitors"), theirs.json("monitors"))
        mine.keyword("monitor Virtual-1,disable")
        theirs.keyword("monitor Virtual-1,disable")
        self.assertEqual(srv.requests, ["j/monitors", "j/monitors",
                                        "keyword monitor Virtual-1,disable",
                                        "keyword monitor Virtual-1,disable"])

    def test_both_derive_the_event_socket_the_same_way(self):
        mine, theirs = self.pair(self.hypr())
        for ipc in (mine, theirs):
            self.assertTrue(ipc.events_path.endswith(".socket2.sock"))
        self.assertEqual(mine.events_path, theirs.events_path)

    def test_every_failure_is_the_same_sentence(self):
        from w11common.errors import CmdError
        for mode, want in (
                ("gone", "hypr backend: the compositor closed the IPC socket without answering `j/monitors`"),
                ("badjson", "hypr backend: the compositor's answer to j/monitors is not JSON"),
                ("short", "hypr backend: the compositor's answer to j/monitors is not JSON")):
            srv = self.hypr(mode=mode)
            mine, theirs = self.pair(srv)
            with self.assertRaises(core.Fatal) as a:
                mine.json("monitors")
            with self.assertRaises(CmdError) as b:
                theirs.json("monitors")
            # the only difference is the newline wxrandr's Fatal carries, because xrandr's fatal() prints
            # the message as it is given
            self.assertEqual(str(a.exception), str(b.exception) + "\n", mode)
            self.assertTrue(str(b.exception).startswith(want), mode)

    def test_a_wedged_compositor_ends_at_the_same_deadline_in_both(self):
        from w11common.errors import CmdError
        srv = self.hypr(mode="wedged")
        mine, theirs = self.pair(srv, timeout=0.4)
        with self.assertRaises(core.Fatal) as a:
            mine.json("monitors")
        with self.assertRaises(CmdError) as b:
            theirs.json("monitors")
        self.assertEqual(str(a.exception),
                         "hypr backend: no answer from the compositor within 0.4s "
                         "(it is not responding)\n")
        self.assertEqual(str(a.exception), str(b.exception) + "\n")

    def test_a_refused_keyword_reads_the_same_in_both(self):
        from w11common.errors import CmdError
        srv = self.hypr(payloads={})
        srv.reply_for = lambda req: (b"Invalid keyword" if req.startswith("keyword")
                                     else support.FakeHypr.reply_for(srv, req))
        mine, theirs = self.pair(srv)
        with self.assertRaises(core.Fatal) as a:
            mine.keyword("monitor nonsense")
        with self.assertRaises(CmdError) as b:
            theirs.keyword("monitor nonsense")
        self.assertEqual(str(a.exception), "hypr: Invalid keyword\n")
        self.assertEqual(str(a.exception), str(b.exception) + "\n")

    def test_no_socket_at_all_is_the_same_sentence_in_both(self):
        from w11common.errors import CmdError
        from wdotool.hypr_ipc import HyprIPC as WdotoolIPC
        tmp = tempfile.mkdtemp(prefix="wxr-hypr-bare-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = tmp
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)
        with support.env(XDG_RUNTIME_DIR=tmp, HYPRLAND_INSTANCE_SIGNATURE=None):
            with self.assertRaises(core.Fatal) as a:
                hypr.HyprIPC()
            with self.assertRaises(CmdError) as b:
                WdotoolIPC()
        self.assertEqual(str(a.exception), str(b.exception) + "\n")
        self.assertEqual(str(b.exception),
                         "hypr backend: no Hyprland IPC socket found ($HYPRLAND_INSTANCE_SIGNATURE unset "
                         "and no hypr/*/.socket.sock in any runtime dir)")


if __name__ == "__main__":
    unittest.main()
