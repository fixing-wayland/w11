#!/usr/bin/env python3
"""The GNOME overlap extension, executed.

gnome/w11-overlap@w11/extension.js is the dangerous half of
`--unsafe-gnome-overlap`: it loads a type description pinned to a private
struct layout, walks gnome-shell's heap through it and writes eight bytes into
it.  Until now nothing had ever run the file.  tests/test_gnome_overlap.py runs
`rules.js` (which has no `gi` imports) under plain node and greps extension.js
for strings, and `FakeOverlap` in that same file stood in for its replies --
two implementations of one protocol, held together by nobody.

node 22 runs the shipped file, at its own path, through
tests/fixtures/gjs/loader.mjs (support.js_harness): every `gi://` namespace and
every shell resource resolves to a recording double under
tests/fixtures/gjs/stubs/, exactly as tests/test_bridge_js.py does for the
bridge.  What is proved here is behaviour of the file install-overlap.sh
installs -- every refusal of the guard chain with its sentence pinned whole, the
nine ways an apply can end with the scripted heap read back after each one to
prove the rollback, `FakeOverlap` held to the real replies field for field, and
the text contract hacks/display/gnome_overlap.py has with those replies.

WHAT THIS FILE CANNOT CATCH.  The scripted world answers `dup_cfg`, `dup_node`,
`dup_lmc`, `dup_mc` and `dup_ms` with records; the offsets inside those records
are the constants extension.js passes to `copy()` (a 24-byte GList node, a
40-byte MetaLogicalMonitorConfig, a 24-byte monitor config, a 32-byte monitor
spec) and the field names the shipped .gir declares.  A wrong offset therefore
reads the same answer here as a right one.  Offsets stay the rig's job --
vm/live-smoke.d/gnome-wayland.sh measures them on a live session -- and
test_gnome_overlap.py's
test_the_shipped_tail_offsets_follow_the_record_the_sentinel_proved holds the
descriptions to the table.  What is proved here is the *use* of an offset: that
the two words written are x and y of the monitor the caller named, that they go
back when anything downstream refuses, and that every check reads what it says
it reads.

THE TWO SHAPES OF A REFUSAL.  extension.js:944-951 copies `g.checks` into the
answer only after the whole chain returned, so a refusal from `version()`,
`typelib()`, `sentinel()`, `noPendingDialog()` or `read()` carries `checks: []`
-- it cannot report the checks it did pass.  A refusal from `_apply()` happens
after that copy and carries all six.  Both shapes are asserted, per case.

Every case is one node process, so the module state starts clean.
"""

import hashlib
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what covers
# `python3 tests/<file>.py`, where conftest is not loaded, and it reaches every
# subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support                                              # noqa: E402
from test_gnome_overlap import FakeOverlap                  # noqa: E402
from hacks.display import gnome_overlap                     # noqa: E402

#: the library the scripted /proc/self/maps has mapped, and its build id.  The
#: id is FakeOverlap's own (tests/test_gnome_overlap.py) so that the double and
#: the extension can be held to one number.
LIBMUTTER = "/usr/lib/x86_64-linux-gnu/libmutter-18.so.0"
BUILD = "0f3a1b2c3d4e5f60718293a4b5c6d7e8f9012345"

#: the layout the scripted heap is in, as the caller read it out of DisplayConfig
EXPECT = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
          {"connectors": ["Virtual-2"], "x": 1920, "y": 0}]

#: the six passes of a probe that refuses nothing, in the order they are pushed
SIX = [("shell-version", "GNOME Shell 50.1, libmutter-18.so.0 (build 0f3a1b2c3d4e)"),
       ("typelib", "W11Overlap18, MetaMonitorsConfig 80 bytes as declared"),
       ("sentinel", "switch_config round-tripped at the declared offset"),
       ("pending-dialog",
        'nothing holds a modal grab, so GNOME is not asking "Keep changes?"'),
       ("bounded-read", "2 logical monitors, every address range-checked"),
       ("public-view",
        "identical to Mutter's public view (global.display + "
        "get_monitor_for_connector on the requested names)")]

PASSES = [{"name": n, "ok": True, "detail": d} for n, d in SIX]

# ---------------------------------------------------------------------------
# The world a case builds before it calls a method.
#
# It is a Python string rather than a file under tests/fixtures/gjs/stubs/ for
# the reason tests/test_bridge_js.py gives about its own: the stubs are shared
# between the two extensions and carry nothing overlap-shaped.  This is the
# overlap extension's own scaffolding -- a heap whose records the shipped
# description's five `dup_*` entry points hand back, a /proc/self/maps that
# holds it, and a Mutter whose public view agrees with it.
#
# `KNOB` is how a case breaks one thing: every knob is applied after the world
# is whole and before the extension is constructed, so a case says what is
# wrong rather than restating everything that is right.

WORLD = r"""
import fs from 'node:fs';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import GIRepository from 'gi://GIRepository';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as H from '@STUBS@/harness.mjs';
import {installGlobals, installImports, makeLib} from '@STUBS@/gjs-globals.mjs';

const Ext = (await import('@MODULE@/extension.js')).default;

const ARG = JSON.parse(process.argv[2] ?? 'null') || {};
const KNOB = ARG.knob || {};

const LIBMUTTER = '/usr/lib/x86_64-linux-gnu/libmutter-18.so.0';
const BUILD = '0f3a1b2c3d4e5f60718293a4b5c6d7e8f9012345';
const EXPECT = [{connectors: ['Virtual-1'], x: 0, y: 0},
                {connectors: ['Virtual-2'], x: 1920, y: 0}];

// ---- the files ----------------------------------------------------------
// generations.json is the shipped one, read off disk: a table written here
// would be a test of a table nobody installs.
GLib.setFile('@MODULE@/generations.json',
             fs.readFileSync('@MODULE@/generations.json', 'utf8'));
// All four, because a forced run picks its namespace by struct size and the
// one it picks is not the one this shell's soname names.
for (const ns of ['W11Overlap14', 'W11Overlap17', 'W11Overlap18', 'W11Overlap51'])
    GLib.setFile(`@MODULE@/typelib/${ns}-1.0.typelib`, 'elf');
const HEAP_LO = 0x55d0c0000000;
GLib.setFile('/proc/self/maps',
  '55d0c0000000-55d0c1000000 rw-p 00000000 00:00 0 [heap]\n' +
  '7f1200000000-7f1200400000 r-xp 00000000 08:02 393217 ' + LIBMUTTER + '\n');
// The same directory savedConfigDigest() builds monitors.xml under, so the
// real answer's `saved_config.path` is the fake's `/home/u/.config/monitors.xml`
// and the two can be compared without an exclusion.
GLib.userConfigDir = '/home/u/.config';
GObject.setType('MetaMonitorsConfig', 80);
Config.setPackageVersion('50.1');
Main.setModalCount(0);
GIRepository.configure({
    metaVersion: '18',
    sharedLibraries: {W11Overlap14: [], W11Overlap17: [],
                      W11Overlap18: [], W11Overlap51: []},
    records: {'W11Overlap14.ConfigN': 72, 'W11Overlap17.ConfigN': 80,
              'W11Overlap18.ConfigN': 80, 'W11Overlap51.ConfigN': 80},
});
// The inode the maps line says that mapping came from: equal, so the library
// on disk is the one this session has mapped and the ELF is read.
Gio.setFileAttribute(LIBMUTTER, 393217);
// monitors.xml is ABSENT unless a case asks for it.

// ---- the heap the Reader walks ------------------------------------------
// Every address is inside [heap], so maps.holds() clears it; each copy() kind
// answers the scripted record at that address.  The twins are at +0x100.
const CFG = HEAP_LO + 0x1000, NODE0 = HEAP_LO + 0x2000, LMC0 = HEAP_LO + 0x3000;
const MNODE0 = HEAP_LO + 0x4000, MC0 = HEAP_LO + 0x5000, MS0 = HEAP_LO + 0x6000;
const CONN0 = HEAP_LO + 0x7000;
const NODE1 = HEAP_LO + 0x2100, LMC1 = HEAP_LO + 0x3100;
const MNODE1 = HEAP_LO + 0x4100, MC1 = HEAP_LO + 0x5100, MS1 = HEAP_LO + 0x6100;
const CONN1 = HEAP_LO + 0x7100;
const THROWAWAY = HEAP_LO + 0x8000;

const lmc = {[LMC0]: {x: 0, y: 0, width: 1920, height: 1080, scale: 1,
                      transform: 0, is_primary: 1, monitor_configs: MNODE0},
             [LMC1]: {x: 1920, y: 0, width: 1920, height: 1080, scale: 1,
                      transform: 0, is_primary: 0, monitor_configs: MNODE1}};
const nodes = {[NODE0]: {data: LMC0, next: NODE1}, [NODE1]: {data: LMC1, next: 0}};
const mnodes = {[MNODE0]: {data: MC0, next: 0}, [MNODE1]: {data: MC1, next: 0}};
const mcs = {[MC0]: {monitor_spec: MS0}, [MC1]: {monitor_spec: MS1}};
const mss = {[MS0]: {connector: CONN0}, [MS1]: {connector: CONN1}};
const strings = {[CONN0]: 'Virtual-1', [CONN1]: 'Virtual-2'};
let switchConfig = 0;

const lib = makeLib({impl: {
    dup_cfg: (p, n) => (p === THROWAWAY
        ? {switch_config: switchConfig, layout_mode: 1, logical_monitor_configs: 0}
        : {switch_config: 0, layout_mode: 1, logical_monitor_configs: NODE0}),
    dup_node: p => nodes[p] ?? mnodes[p] ?? null,
    dup_lmc: p => lmc[p] ?? null,
    dup_mc: p => mcs[p] ?? null,
    dup_ms: p => mss[p] ?? null,
    strn: (p, n) => (p === 0 ? null : strings[p] ?? null),
    addr: o => (o === 'throwaway' ? THROWAWAY : CFG),
    unref_addr: () => null,
    type_name: () => 'MetaMonitorsConfig',
    get_config_manager: () => 'cm',
    get_current: () => 'cfg',
    create_linear: () => 'throwaway',
    set_switch_config: (o, v) => { switchConfig = v; return null; },
    get_switch_config: () => switchConfig,
    // Mutter's validator, refusing the way it refuses a layout whose monitors
    // share no edge.  The wording is the double's, so that what the caller
    // prints can be compared against what Mutter said.
    verify: () => { throw new Error('Logical monitors not adjacent'); },
    apply: () => true,
    wr: (p, bytes) => {
        // the write lands in the scripted lmc record, little-endian
        const v = new DataView(new Uint8Array(bytes).buffer).getInt32(0, true);
        for (const [a, r] of Object.entries(lmc)) {
            if (+a === p) r.x = v;
            if (+a + 4 === p) r.y = v;
        }
        return null;
    },
}});
for (const ns of ['W11Overlap14', 'W11Overlap17', 'W11Overlap18', 'W11Overlap51'])
    installImports(ns, lib);

// ---- global.display / global.backend ------------------------------------
// get_monitors() is deliberately absent: this is the GNOME 49/50 shape where
// connector names are resolved one at a time from the names the caller read
// out of DisplayConfig, which is the branch the public-view detail names.
// A name nobody asks about answers 1 the way a two-head Mutter would answer
// for its second head; `strictConnectors` is the other branch, where Mutter
// says it has never heard of the name at all.
const mm = {get_monitor_for_connector: c => (c === 'Virtual-1' ? 0
    : (KNOB.strictConnectors && c !== 'Virtual-2') ? -1 : 1)};
const geo = [{x: 0, y: 0, width: 1920, height: 1080},
             {x: 1920, y: 0, width: 1920, height: 1080}];
installGlobals({
    backend: {get_monitor_manager: () => mm},
    display: {
        get_primary_monitor: () => 0,
        get_n_monitors: () => geo.length,
        get_monitor_geometry: i => geo[i],
        get_monitor_scale: () => 1,
    },
});

// ---- what this case breaks ----------------------------------------------
if (KNOB.shell) Config.setPackageVersion(KNOB.shell);
if (KNOB.modal !== undefined) Main.setModalCount(KNOB.modal);
if (KNOB.maps !== undefined) GLib.setFile('/proc/self/maps', KNOB.maps);
if (KNOB.noTypelib) GLib.setFile('@MODULE@/typelib/W11Overlap18-1.0.typelib', null);
if (KNOB.noTable) GLib.setFile('@MODULE@/generations.json', null);
if (KNOB.badTable) GLib.setFile('@MODULE@/generations.json', KNOB.badTable);
if (KNOB.size !== undefined) {
    GObject.unsetType('MetaMonitorsConfig');
    if (KNOB.size) GObject.setType('MetaMonitorsConfig', KNOB.size);
}
if (KNOB.sharedLib) GIRepository.configure({sharedLibraries: {W11Overlap18: KNOB.sharedLib}});
if (KNOB.declared !== undefined)
    GIRepository.configure({records: {'W11Overlap18.ConfigN': KNOB.declared}});
if (KNOB.metaVersion !== undefined) GIRepository.configure({metaVersion: KNOB.metaVersion});
if (KNOB.dropSymbol) installImports('W11Overlap18', makeLib({drop: [KNOB.dropSymbol]}));
if (KNOB.sentinelDead) lib.set_switch_config = () => null;
if (KNOB.wildPointer) nodes[NODE0].data = 0x10;
// An inode that is not the one the maps line carries is dpkg having written a
// new library over the name: `replaced`, a note, and no build id.
if (KNOB.inode !== undefined) Gio.setFileAttribute(LIBMUTTER, KNOB.inode);
if (!KNOB.noElf) Gio.setElf(LIBMUTTER, BUILD);
if (KNOB.saved === 'present') GLib.setFile('/home/u/.config/monitors.xml', '<monitors/>');
if (KNOB.savedMoves) {
    const realApply = lib.apply;
    lib.apply = (...a) => {
        GLib.setFile('/home/u/.config/monitors.xml', '<moved/>');
        return realApply(...a);
    };
}
if (KNOB.verifyAccepts) lib.verify = () => true;
if (KNOB.applyFails) lib.apply = () => false;
if (KNOB.applyThrows) lib.apply = () => { throw new Error('apply blew up'); };
if (KNOB.wrIgnored) lib.wr = () => null;   // the write does not land

// ---- the call -----------------------------------------------------------
const e = new Ext({uuid: 'w11-overlap@w11', path: '@MODULE@'});
const req = Object.assign({layout_mode: 1, expect: EXPECT}, ARG.req || {});
const want = ARG.want || [{connectors: ['Virtual-1'], x: 0, y: 0},
                          {connectors: ['Virtual-2'], x: 960, y: 0}];
const applyReq = () => JSON.stringify(Object.assign({}, req, {want}));
/** where the two logical monitors sit in the scripted heap right now */
const heap = () => [[lmc[LMC0].x, lmc[LMC0].y], [lmc[LMC1].x, lmc[LMC1].y]];

let out;
if (ARG.op === 'probe') {
    out = JSON.parse(e.Probe(JSON.stringify(req)));
} else if (ARG.op === 'apply') {
    out = JSON.parse(e.ApplyOverlap(applyReq()));
    out._heap = heap();
} else if (ARG.op === 'apply-twice') {
    JSON.parse(e.ApplyOverlap(applyReq()));
    // Mutter has applied it, so its public answer moved with the heap; the
    // caller's `expect` did not, which is the stale request.
    geo[1].x = 960;
    out = JSON.parse(e.ApplyOverlap(applyReq()));
    out._heap = heap();
} else if (ARG.op === 'calls') {
    out = {reply: JSON.parse(e.Probe(JSON.stringify(req))), calls: H.calls};
} else if (ARG.op === 'fields') {
    out = {ConfigN: Object.keys(lib.dup_cfg(CFG, 80)),
           NodeN: Object.keys(nodes[NODE0]),
           LMCN: Object.keys(lmc[LMC0]),
           MCN: Object.keys(mcs[MC0]),
           MSN: Object.keys(mss[MS0])};
} else {
    throw new Error(`no such op: ${ARG.op}`);
}
console.log(JSON.stringify(out));
"""


def run(op, knob=None, req=None, want=None):
    """One node process: the world, this case's knob, and one call."""
    return support.js_harness(support.OVERLAP_EXT, WORLD,
                              {"op": op, "knob": knob or {}, "req": req or {},
                               "want": want})


#: the keys `found` carries whenever a refusal happened somewhere that had it
FOUND_KEYS = ["instance_size", "known", "meta_typelib", "shell", "shell_major", "sonames"]

#: the table, as the extension reads it off disk -- named here because four
#: refusals print the path and nothing else may invent it
TABLE = os.path.join(support.OVERLAP_EXT, "generations.json")


@support.skip_without_node
class Guards(unittest.TestCase):
    """The guard chain, run: `version()`, `typelib()`, `sentinel()`,
    `noPendingDialog()`, `read()`, one case per way out.

    Every `reason` is pinned WHOLE rather than by a fragment, because the whole
    of it is what the user is shown -- hacks/display/gnome_overlap.py
    `refusal_text()` prints it verbatim with a check name in front.  That makes
    editing one of these sentences a two-file change, which is the point: the
    source of truth is the string literal in extension.js or rules.js, and the
    last line of this package's verify greps one distinctive fragment of each
    out of both files.
    """

    def refusal(self, check, reason, knob=None, req=None, forceable=False,
                found=({"shell": "50.1", "sonames": ["libmutter-18.so.0"],
                        "instance_size": 80})):
        """One probe that must refuse, with everything a refusal carries.

        `found` is None for the refusals raised before `this.found` is built
        (extension.js:438) -- the four table texts and `maps` -- and otherwise
        the subset of it this case cares about."""
        got = run("probe", knob, req)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["check"], check)
        self.assertEqual(got["reason"], reason)
        self.assertIs(got["forceable"], forceable)
        # extension.js:944-951 pushes g.checks into the answer only after the
        # whole chain returned, so a chain refusal cannot report what passed.
        self.assertEqual(got["checks"], [])
        if found is None:
            self.assertIsNone(got["found"])
        else:
            self.assertEqual(sorted(got["found"]), FOUND_KEYS)
            self.assertEqual({k: got["found"][k] for k in found}, found)
        return got

    # -- the run that refuses nothing ---------------------------------------

    def test_the_happy_probe_passes_all_six_checks(self):
        """The whole answer of a probe on the world as built: six passes in
        order, their details word for word, and the measurements the caller
        records an agreement against."""
        got = run("probe")
        self.assertIs(got["ok"], True)
        self.assertEqual(got["version"], 1)
        self.assertEqual(got["checks"], PASSES)
        self.assertEqual(got["shell"], "50.1")
        self.assertEqual(got["libmutter"], "18")
        self.assertEqual(got["libmutter_build"], BUILD)
        self.assertEqual(got["libmutter_path"], LIBMUTTER)
        self.assertEqual(got["instance_size"], 80)
        self.assertEqual(got["modal_count"], 0)
        self.assertIsNone(got["forced"])
        self.assertEqual(got["notes"], [])
        self.assertIs(got["wrote"], False)
        self.assertEqual(got["monitors"], [
            {"connectors": ["Virtual-1"], "x": 0, "y": 0, "w": 1920, "h": 1080,
             "scale": 1, "transform": 0, "primary": True},
            {"connectors": ["Virtual-2"], "x": 1920, "y": 0, "w": 1920, "h": 1080,
             "scale": 1, "transform": 0, "primary": False}])
        self.assertEqual(got["found"]["known"], gnome_overlap.describe_table())

    def test_the_build_id_is_read_out_of_the_mapped_library_itself(self):
        """Not out of anything the answer was handed: the inode of the path is
        compared with the inode the maps line carries, and only then is the
        file opened, read once and closed.  ELF_PREFIX is 1 << 16, which is the
        single read (extension.js:159, :168)."""
        got = run("calls")
        names = [c["what"] for c in got["calls"]]
        i = names.index("Gio.File.new_for_path")
        self.assertEqual(names[i:i + 7],
                         ["Gio.File.new_for_path", "file.query_info",
                          "info.get_attribute_uint64", "Gio.File.new_for_path",
                          "file.read", "stream.read_bytes", "stream.close"])
        self.assertEqual(got["calls"][i + 1]["args"], ["unix::inode"])
        self.assertEqual(got["calls"][i + 5]["args"], [1 << 16])
        self.assertEqual(got["reply"]["libmutter_build"], BUILD)

    # -- 1. version() -------------------------------------------------------

    def test_an_unmeasured_shell_is_the_one_refusal_forcing_can_get_past(self):
        self.refusal(
            "shell-version",
            "GNOME Shell 52.0: this extension knows the private layout of GNOME "
            "46 and 49 and 50 and 51 only",
            {"shell": "52.0"}, forceable=True,
            found={"shell": "52.0", "shell_major": 52, "instance_size": 80})

    def test_a_forced_probe_picks_the_description_by_size(self):
        """rules.js:138-168: the hits are sorted by shell_major and the last of
        them is picked, the rest named as the same shape.  Both details that
        follow name the generation that was PICKED and not the one this shell's
        soname would have chosen (extension.js:513-517 and :523)."""
        because = ("MetaMonitorsConfig is 80 bytes here, which is the size "
                   "W11Overlap51 describes (measured on GNOME 51, and the same "
                   "bytes as W11Overlap17, W11Overlap18)")
        got = run("probe", {"shell": "52.0"}, {"force": {"shell_major": 52}})
        self.assertIs(got["ok"], True)
        self.assertEqual(got["forced"], {"shell_major": 52, "using": "W11Overlap51",
                                         "because": because})
        self.assertEqual(got["checks"][0]["detail"],
                         "GNOME Shell 52.0, libmutter-18.so.0 (build 0f3a1b2c3d4e)"
                         " -- FORCED: " + because)
        self.assertEqual(got["checks"][1]["detail"],
                         "W11Overlap51, MetaMonitorsConfig 80 bytes as declared")

    def test_forcing_cannot_invent_a_description_for_a_size_nobody_shipped(self):
        """And it never becomes forceable: there is nothing to force with."""
        self.refusal(
            "struct-size",
            "this build's MetaMonitorsConfig is 99 bytes and no description "
            "shipped here describes a struct that size (W11Overlap14 72, "
            "W11Overlap17 80, W11Overlap18 80, W11Overlap51 80).  "
            "Forcing cannot invent a description: this needs a new one, from "
            "the release's own header",
            {"shell": "52.0", "size": 99}, {"force": {"shell_major": 52}},
            found={"shell": "52.0", "instance_size": 99})

    def test_two_libmutters_mapped_is_a_refusal(self):
        maps = ("55d0c0000000-55d0c1000000 rw-p 0 00:0 0 [heap]\n"
                "7f12-7f13 r-xp 0 08:02 1 /usr/lib/libmutter-18.so.0\n"
                "7f14-7f15 r-xp 0 08:02 2 /usr/lib/libmutter-14.so.0\n")
        self.refusal(
            "libmutter",
            "exactly one libmutter has to be mapped into gnome-shell; this "
            "process has [libmutter-18.so.0, libmutter-14.so.0]",
            {"maps": maps},
            found={"sonames": ["libmutter-18.so.0", "libmutter-14.so.0"]})

    def test_no_libmutter_mapped_is_the_same_refusal_with_an_empty_list(self):
        self.refusal(
            "libmutter",
            "exactly one libmutter has to be mapped into gnome-shell; this "
            "process has []",
            {"maps": "55d0c0000000-55d0c1000000 rw-p 0 00:0 0 [heap]\n"},
            found={"sonames": []})

    def test_a_libmutter_that_is_not_the_one_this_shell_carries(self):
        maps = ("55d0c0000000-55d0c1000000 rw-p 0 00:0 0 [heap]\n"
                "7f12-7f13 r-xp 0 08:02 1 /usr/lib/libmutter-14.so.0\n")
        self.refusal(
            "libmutter",
            "GNOME Shell 50.1 should carry libmutter-18.so.0, this process has "
            "[libmutter-14.so.0]",
            {"maps": maps}, found={"sonames": ["libmutter-14.so.0"]})

    def test_the_meta_typelib_and_the_mapped_library_have_to_agree(self):
        self.refusal("meta-typelib",
                     "the Meta typelib says 14, libmutter-18.so.0 says 18",
                     {"metaVersion": "14"}, found={"meta_typelib": "14"})

    def test_a_maps_that_cannot_be_read_refuses_before_anything_is_measured(self):
        """`found` is null here and only here among the non-table refusals:
        Maps is built at extension.js:428, ten lines before `this.found` is."""
        self.refusal("maps", "cannot read /proc/self/maps",
                     {"maps": None}, found=None)

    # -- the table ----------------------------------------------------------

    def test_the_table_has_to_be_there(self):
        """The doubled sentence is the file's own: `refuse()` throws from
        inside the try at extension.js:360-367, and the catch below refuses
        again with the first refusal's message appended."""
        self.refusal("table",
                     "cannot read %s: Error: cannot read %s" % (TABLE, TABLE),
                     {"noTable": 1}, found=None)

    def test_the_table_has_to_be_json(self):
        """node's own SyntaxError text is appended and moves between node
        versions, so the pin is the whole of what this file writes."""
        got = run("probe", {"badTable": "{"})
        self.assertEqual(got["check"], "table")
        self.assertTrue(got["reason"].startswith("%s is not JSON: SyntaxError:" % TABLE),
                        got["reason"])
        self.assertEqual(got["checks"], [])
        self.assertIsNone(got["found"])

    def test_the_table_has_to_name_a_generation(self):
        self.refusal("table", "%s names no generations at all" % TABLE,
                     {"badTable": '{"format":1,"generations":[]}'}, found=None)

    def test_every_table_record_has_to_carry_every_field(self):
        """TABLE_FIELDS is checked in its own order, so the first missing field
        of a record that has only two of the seven is `soname`."""
        self.refusal("table", "%s: the GNOME 50 record has no soname" % TABLE,
                     {"badTable": '{"format":1,"generations":'
                                  '[{"shell_major":50,"libmutter":"18"}]}'},
                     found=None)

    # -- 2. typelib() -------------------------------------------------------

    def test_the_typelib_has_to_be_installed(self):
        self.refusal("typelib",
                     "W11Overlap18-1.0.typelib is not installed in %s"
                     % os.path.join(support.OVERLAP_EXT, "typelib"),
                     {"noTypelib": 1})

    def test_a_description_naming_an_unmapped_library_refuses_before_any_call(self):
        """The one refusal in this file that exists to stop gjs aborting the
        session rather than to stop a wrong write: a description whose
        shared-library cannot be dlopened kills the process on its first call,
        so the name is read statically and compared with /proc/self/maps."""
        self.refusal(
            "shared-library",
            "W11Overlap18 names the shared library libfoo.so, which is not "
            "mapped into gnome-shell ([libmutter-18.so.0] are).  Calling through "
            "a description whose library cannot be opened aborts gjs, and on "
            "Wayland that is the session -- so this refuses instead.  A "
            "description generated by gnome/overlap-typelib/gen-gir.py names no "
            "library at all; reinstall with gnome/install-overlap.sh",
            {"sharedLib": ["libmutter-18.so.0", "libfoo.so"]})

    def test_a_symbol_that_is_gone_is_a_refusal(self):
        self.refusal("symbols", "W11Overlap18.wr is not callable",
                     {"dropSymbol": "wr"})

    def test_the_typelib_and_the_table_have_to_agree_on_the_size(self):
        self.refusal(
            "struct-size",
            "W11Overlap18 describes a 72-byte struct, generations.json records "
            "80 for GNOME 50: the table and the description beside it disagree, "
            "and neither can be trusted until they do not",
            {"declared": 72})

    def test_a_metamonitorsconfig_that_is_not_a_gtype(self):
        self.refusal("struct-size", "MetaMonitorsConfig is not a registered GType",
                     {"size": 0}, found={"instance_size": None})

    def test_the_registry_size_and_the_shipped_description_have_to_agree(self):
        self.refusal(
            "struct-size",
            "this build's MetaMonitorsConfig is 96 bytes, the "
            "description shipped for libmutter-18.so.0 is 80",
            {"size": 96}, found={"instance_size": 96})

    # -- 3. sentinel() and noPendingDialog() --------------------------------

    def test_the_sentinel_has_to_round_trip(self):
        """Through Mutter's own setter, on a throwaway config: a build whose
        setter does not stick is not one whose offsets mean anything."""
        self.refusal("sentinel", "Mutter did not read back its own switch_config",
                     {"sentinelDead": 1})

    def test_a_modal_grab_stops_everything(self):
        """rules.js modalVerdict(1) whole -- the guard that stands between this
        extension and the only lasting damage it can do."""
        self.refusal(
            "pending-dialog",
            "something holds a modal grab on the shell (Main.modalCount is 1).  "
            "If that is GNOME asking whether to keep a display change, "
            "confirming it while this had moved a monitor is the one way an "
            "overlapping layout could reach ~/.config/monitors.xml and stay "
            "there.  Nothing was read and nothing was written.  Answer it -- or "
            "close the overview, or the menu -- and run this again.  If there is "
            "nothing on screen to answer, this is a grab that has not been "
            "released yet, which was measured in the first seconds of a fresh "
            "session: wait a moment and run the same command again",
            {"modal": 1})

    # -- 4. read() ----------------------------------------------------------

    def test_a_wild_pointer_is_caught_by_the_range_check(self):
        """The whole reason nothing here is declared as a pointer: a GList node
        whose data is 0x10 is a number, and a number outside every readable
        mapping is a refusal rather than a SIGSEGV in the compositor."""
        self.refusal("bounded-read",
                     "logical[0]: 0x10+40 is not in a readable mapping",
                     {"wildPointer": 1})

    def test_the_layout_mode_has_to_be_the_one_displayconfig_saw(self):
        self.refusal("layout-mode",
                     "layout_mode reads 1 at the offset this description "
                     "believes; DisplayConfig says 2",
                     req={"layout_mode": 2})

    def test_the_private_read_has_to_agree_with_mutters_public_view(self):
        """rules.js compare(): the check that kills a wrong offset which got
        past the struct size and the sentinel, because garbage does not agree
        with Mutter on count, geometry, scale, primary and names at once."""
        self.refusal("public-view",
                     "monitor 1: connectors read Virtual-2, Mutter says Virtual-9",
                     req={"expect": [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                                     {"connectors": ["Virtual-9"], "x": 1920, "y": 0}]})

    def test_a_connector_mutter_does_not_know_refuses(self):
        """The other branch of the same world: `get_monitor_for_connector` is
        how a name relayed from DisplayConfig is resolved, and a -1 means the
        name never was Mutter's."""
        self.refusal("connectors", "Mutter does not know a connector named Virtual-9",
                     {"strictConnectors": 1},
                     {"expect": [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                                 {"connectors": ["Virtual-9"], "x": 1920, "y": 0}]})

    # -- what is a note and not a refusal -----------------------------------

    def test_a_replaced_library_is_a_note_and_no_build(self):
        """`apt upgrade` under a live session: the path resolves to a new inode
        while the old library stays mapped, so the ELF at that path is not read
        at all and the answer says so.  It decides nothing -- `ok` is true."""
        got = run("probe", {"inode": 1})
        self.assertIs(got["ok"], True)
        self.assertIsNone(got["libmutter_build"])
        self.assertEqual(got["notes"], [
            "libmutter has been replaced on disk since this session started (%s "
            "is no longer the file this gnome-shell has mapped).  The checks ran "
            "against the library this session is running, which is the one being "
            "written to, so this changes nothing now -- but the next login runs "
            "the new one, and this feature may refuse there" % LIBMUTTER])
        self.assertEqual(got["checks"][0]["detail"],
                         "GNOME Shell 50.1, libmutter-18.so.0")

    def test_an_unreadable_elf_leaves_the_build_null(self):
        """Not a note either: the inode agrees, so nothing was replaced; the
        file simply would not say its build id.  extension.js:155-157 -- every
        failure on this path is a null, never a refusal."""
        got = run("probe", {"noElf": 1})
        self.assertIs(got["ok"], True)
        self.assertIsNone(got["libmutter_build"])
        self.assertEqual(got["notes"], [])

    # -- the scripted world against the shipped description ------------------

    def test_the_scripted_records_carry_the_gir_field_names(self):
        """The one thing this file can say about the description: every field
        the scripted heap answers is a field W11Overlap18-1.0.gir declares, by
        that name.  It cannot say the OFFSETS are right -- that stays the rig's
        measurement and test_gnome_overlap.py's tail-slot check -- but a record
        answering a field the description does not have would mean the extension
        was reading something this world invented."""
        gir = os.path.join(ROOT, "gnome", "overlap-typelib", "W11Overlap18-1.0.gir")
        with open(gir, encoding="utf-8") as fh:
            text = fh.read()
        declared = {}
        for block in re.split(r'<record name="', text)[1:]:
            name = block[:block.index('"')]
            declared[name] = set(re.findall(r'<field name="(\w+)"', block))
        self.assertEqual(sorted(declared), ["ConfigN", "LMCN", "MCN", "MSN", "NodeN"])
        got = run("fields")
        for record, keys in got.items():
            self.assertTrue(set(keys) <= declared[record],
                            "%s: %s" % (record, set(keys) - declared[record]))
        # ...and the fields the walk actually needs are all there.
        self.assertIn("logical_monitor_configs", got["ConfigN"])
        self.assertEqual(set(got["LMCN"]) & {"x", "y"}, {"x", "y"})


@support.skip_without_node
class Apply(unittest.TestCase):
    """`_apply()`: the nine ways one ApplyOverlap ends, with the scripted heap
    read back after each.

    `_heap` is [[x, y], [x, y]] of the two MetaLogicalMonitorConfig records the
    world keeps, updated by the `wr` double the same way a real write updates
    the real struct.  It is the only way to tell "refused" from "refused and
    put it back": extension.js:1090-1098 rewrites the old words on every throw
    out of the write block, and a rollback that silently stopped working would
    leave a session with a layout Mutter had never validated."""

    LIVE = [[0, 0], [1920, 0]]

    def test_the_happy_apply_writes_two_words_and_says_what_mutter_said(self):
        got = run("apply")
        self.assertIs(got["ok"], True)
        self.assertNotIn("check", got)
        self.assertIs(got["wrote"], True)
        self.assertIs(got["applied"], True)
        self.assertEqual(got["wrote_words"], 2)
        # rules.js:191-207 tests adjacency before overlap, and two rectangles
        # that overlap share no edge either, so this is the sentence for both.
        self.assertEqual(got["fault"], "logical monitors not adjacent")
        self.assertEqual(got["verify"], "refused: Logical monitors not adjacent")
        self.assertEqual(got["saved_config"],
                         {"path": "/home/u/.config/monitors.xml", "before": "absent",
                          "after": "absent", "unchanged": True})
        self.assertEqual(got["monitors"][1]["x"], 960)
        self.assertEqual(got["_heap"], [[0, 0], [960, 0]])
        self.assertEqual(len(got["public"]), 2)
        self.assertEqual(got["checks"], PASSES)

    def refusal(self, check, reason, knob=None, want=None, op="apply"):
        got = run(op, knob, want=want)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["check"], check)
        self.assertEqual(got["reason"], reason)
        # The other shape: these are raised after extension.js:950 copied the
        # six passes in, so unlike a chain refusal they carry them.
        self.assertEqual(got["checks"], PASSES)
        return got

    def test_a_layout_mutter_would_accept_never_reaches_the_write(self):
        """The rule that keeps this extension off the ordinary path: there is
        no reason to write into a compositor's heap for a layout it would take
        through DisplayConfig, which validates and can be undone."""
        got = self.refusal(
            "not-an-overlap",
            "Mutter accepts this layout: apply it the ordinary way, without "
            "this extension",
            want=[{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                  {"connectors": ["Virtual-2"], "x": 1920, "y": 0}])
        self.assertEqual(got["_heap"], self.LIVE)

    def test_a_request_naming_a_monitor_this_session_does_not_have(self):
        got = self.refusal("request", "the requested layout does not name the same monitors",
                           want=[{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                                 {"connectors": ["DP-9"], "x": 960, "y": 0}])
        # Refused before the write, so this read-back is a weaker claim than
        # the rollback ones below (which the sweep at :798 repeats) -- but it
        # is the same claim, and every refusing case in this class makes it:
        # no path out of _apply(), early or late, moves the session's heap.
        self.assertEqual(got["_heap"], self.LIVE)

    def test_a_write_that_does_not_land_is_caught_by_the_read_back(self):
        """The bounded read is done again afterwards and every field compared:
        the two words must have moved and nothing else."""
        got = self.refusal("read-back", "monitor 1: x is 1920, expected 960",
                           {"wrIgnored": 1})
        self.assertEqual(got["_heap"], self.LIVE)

    def test_mutter_validating_the_mutated_configuration_is_a_refusal(self):
        """The positive control.  If Mutter's own validator accepts what was
        just built, the write did not land on the field the validator reads and
        nothing in the answer means what it says."""
        got = self.refusal(
            "positive-control",
            "Mutter validated the mutated configuration, so the write did not "
            "land where its validator reads: nothing was applied",
            {"verifyAccepts": 1})
        self.assertEqual(got["_heap"], self.LIVE)

    def test_an_apply_that_returns_false_rolls_the_words_back(self):
        got = self.refusal(
            "apply", "meta_monitor_manager_apply_monitors_config returned false",
            {"applyFails": 1})
        self.assertEqual(got["_heap"], self.LIVE)

    def test_an_apply_that_throws_rolls_the_words_back(self):
        got = self.refusal("apply", "apply blew up", {"applyThrows": 1})
        self.assertEqual(got["_heap"], self.LIVE)

    def test_a_saved_configuration_that_moved_clears_ok_and_names_no_check(self):
        """The one reply shape that says both things at once: the write went in,
        the apply succeeded, and ~/.config/monitors.xml is not the file it was.
        There is no `check` and no `reason` -- nothing refused; the digest taken
        before and the one taken after simply differ (extension.js:1101-1109)."""
        got = run("apply", {"savedMoves": 1})
        self.assertIs(got["ok"], False)
        self.assertNotIn("check", got)
        self.assertNotIn("reason", got)
        self.assertIs(got["applied"], True)
        self.assertEqual(got["saved_config"]["before"], "absent")
        self.assertEqual(got["saved_config"]["after"],
                         hashlib.sha256(b"<moved/>").hexdigest())
        self.assertIs(got["saved_config"]["unchanged"], False)
        self.assertEqual(got["_heap"], [[0, 0], [960, 0]])

    def test_a_saved_configuration_that_was_there_all_along_is_unchanged(self):
        """The digest is the real sha256 (the GLib stub hashes with node:crypto),
        so "unchanged" is a comparison and not a flag."""
        digest = hashlib.sha256(b"<monitors/>").hexdigest()
        got = run("apply", {"saved": "present"})
        self.assertIs(got["ok"], True)
        self.assertEqual(got["saved_config"],
                         {"path": "/home/u/.config/monitors.xml", "before": digest,
                          "after": digest, "unchanged": True})

    def test_a_request_built_before_the_last_apply_is_refused_by_position(self):
        """Two applies in one session: the second carries the `expect` the first
        was built from, which is now the layout before last.  It is refused by
        position and nothing is written."""
        got = self.refusal(
            "request",
            "Virtual-2 is at +960+0, the request was built when it was at "
            "+1920+0: re-read the layout and try again",
            op="apply-twice")
        self.assertEqual(got["_heap"], [[0, 0], [960, 0]])

    def test_every_refusal_after_the_write_leaves_the_heap_as_it_was(self):
        """The rollback, once more as a sweep: whatever goes wrong between the
        write and the apply, the two logical monitors are back where the
        session had them, and nothing was applied."""
        for knob in ({"wrIgnored": 1}, {"verifyAccepts": 1},
                     {"applyFails": 1}, {"applyThrows": 1}):
            with self.subTest(knob=sorted(knob)):
                got = run("apply", knob)
                self.assertIs(got["ok"], False)
                self.assertEqual(got["_heap"], self.LIVE)
                self.assertNotIn("applied", got)


@support.skip_without_node
class FakeOverlapParity(unittest.TestCase):
    """`FakeOverlap` (tests/test_gnome_overlap.py) against the replies the
    extension really emits, field for field.

    The double is what every other overlap test in the suite talks to -- the
    consent file, the force flag, the wxrandr command line, the GUI -- so what
    it says is what all of them are written against.  Until this class existed
    nothing held it to the file it stands in for, and it had drifted in seven
    places: the public-view detail, the tail of the struct-size refusal, the
    build id and the FORCED clause in the shell-version detail, the namespace
    the typelib detail names on a forced run, `libmutter` as a number where the
    extension answers a string, and the fault of a two-monitor overlap.  All
    seven were fixed IN THE DOUBLE, which is the only direction this class ever
    pushes: the extension is the thing being described.

    `extra` is what the real reply carries and the double does not; it is
    asserted rather than ignored so that a field the extension grows shows up
    here as a decision to make (teach the double, or say why not) instead of
    passing unnoticed."""

    PROBE_EXTRA = {"libmutter_path", "modal_count"}
    APPLY_EXTRA = PROBE_EXTRA | {"public"}
    WANT = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
            {"connectors": ["Virtual-2"], "x": 960, "y": 0}]

    def shape(self, value):
        """A digest compared as what it is rather than as which bytes: the
        double cannot know the sha256 of a file the extension hashed."""
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            return "sha256"
        return value

    def parity(self, real, fake, extra):
        real = dict(real)
        real.pop("_heap", None)          # the harness's own read-back, not a field
        for k in fake:
            if k == "saved_config":
                self.assertEqual(real[k]["path"], fake[k]["path"], "saved_config.path")
                self.assertEqual(real[k]["unchanged"], fake[k]["unchanged"],
                                 "saved_config.unchanged")
                for side in ("before", "after"):
                    self.assertEqual(self.shape(real[k][side]), self.shape(fake[k][side]),
                                     "saved_config." + side)
                continue
            self.assertEqual(real[k], fake[k], k)
        self.assertEqual(set(real) - set(fake), extra)

    def fake(self, member, req, shell="50.1", **attrs):
        double = FakeOverlap(shell=shell)
        for k, v in attrs.items():
            setattr(double, k, v)
        return double.answer(member, req)

    def test_a_probe_that_refuses_nothing(self):
        req = gnome_overlap._request(1, EXPECT)
        self.parity(run("probe"), self.fake("Probe", req), self.PROBE_EXTRA)

    def test_an_apply_that_writes(self):
        req = dict(gnome_overlap._request(1, EXPECT), want=self.WANT)
        self.parity(run("apply", want=self.WANT), self.fake("ApplyOverlap", req),
                    self.APPLY_EXTRA)

    def test_an_apply_whose_saved_configuration_moved(self):
        """The reply shape that is `ok: false` with no `check` and no `reason`.
        The two digests differ on both sides; which bytes they are cannot
        match, so they are compared as digests."""
        req = dict(gnome_overlap._request(1, EXPECT), want=self.WANT)
        real = run("apply", {"saved": "present", "savedMoves": 1}, want=self.WANT)
        self.parity(real, self.fake("ApplyOverlap", req, saved_config_moved=True),
                    self.APPLY_EXTRA)

    def test_a_probe_on_a_shell_nobody_measured(self):
        """A refusal carries no `libmutter_path` and no `modal_count`, so here
        the two answers are the same set of keys as well as the same values."""
        req = gnome_overlap._request(1, EXPECT)
        self.parity(run("probe", {"shell": "52.0"}),
                    self.fake("Probe", req, shell="52.0"), set())

    def test_a_probe_forced_onto_a_description_of_the_right_size(self):
        req = gnome_overlap._request(1, EXPECT, force={"shell_major": 52})
        self.parity(run("probe", {"shell": "52.0"}, {"force": {"shell_major": 52}}),
                    self.fake("Probe", req, shell="52.0"), self.PROBE_EXTRA)

    def test_a_probe_forced_where_no_description_is_that_size(self):
        req = gnome_overlap._request(1, EXPECT, force={"shell_major": 52})
        self.parity(run("probe", {"shell": "52.0", "size": 99},
                        {"force": {"shell_major": 52}}),
                    self.fake("Probe", req, shell="52.0", instance_size=99), set())


@support.skip_without_node
class CallerContract(unittest.TestCase):
    """hacks/display/gnome_overlap.py, fed replies the extension emitted.

    Everything the user reads about this feature is one of these functions
    applied to one of those replies, and the two files have never met: the
    module's tests drive it from dicts written beside them.  What is held still
    here is the text contract between the two -- including the one place it is
    literally a text contract, `facts()` reading the struct size back out of a
    check's prose when the reply is too old to carry the number."""

    @classmethod
    def setUpClass(cls):
        cls.probe = run("probe")
        cls.happy = run("apply")
        cls.moved = run("apply", {"savedMoves": 1})
        cls.forced = run("apply", {"shell": "52.0"}, {"force": {"shell_major": 52}})
        cls.unmeasured = run("probe", {"shell": "52.0"})
        cls.struct = run("probe", {"shell": "52.0", "size": 99},
                         {"force": {"shell_major": 52}})
        cls.replaced = run("probe", {"inode": 1})

    def test_a_refusal_is_one_line_with_the_check_in_front_of_it(self):
        self.assertEqual(
            gnome_overlap.refusal_text(self.unmeasured),
            "the overlap extension refused (shell-version): GNOME Shell 52.0: "
            "this extension knows the private layout of GNOME 46 and 49 and 50 "
            "and 51 only\n")

    def test_which_refusals_the_caller_offers_to_force_past(self):
        """The extension answers for itself in `forceable`; the caller falls
        back to its own copy of the list for an extension too old to say, and
        the two have to agree about both of these."""
        for reply, want in ((self.unmeasured, True), (self.struct, False)):
            with self.subTest(check=reply["check"]):
                self.assertIs(gnome_overlap.refusal_is_forceable(reply), want)
                older = {k: v for k, v in reply.items() if k != "forceable"}
                self.assertIs(gnome_overlap.refusal_is_forceable(older), want)

    def test_a_note_is_printed_and_a_reply_with_none_prints_nothing(self):
        self.assertEqual(
            gnome_overlap.notes_text(self.replaced),
            "note: libmutter has been replaced on disk since this session "
            "started (%s is no longer the file this gnome-shell has mapped).  "
            "The checks ran against the library this session is running, which "
            "is the one being written to, so this changes nothing now -- but "
            "the next login runs the new one, and this feature may refuse "
            "there\n" % LIBMUTTER)
        self.assertEqual(gnome_overlap.notes_text(self.probe), "")

    def test_what_is_printed_after_an_apply_that_worked(self):
        """Two lines, both of them reassurance, and `quiet` (an agreement
        covers this build) drops both -- gnome_overlap.py:694-775."""
        self.assertEqual(
            gnome_overlap.applied_text(self.happy),
            "mutter's own validator on the result: refused: Logical monitors "
            "not adjacent\n"
            "/home/u/.config/monitors.xml: unchanged (absent)\n")
        self.assertEqual(gnome_overlap.applied_text(self.happy, quiet=True), "")

    def test_a_saved_configuration_that_moved_is_printed_even_quiet(self):
        """The one line `quiet` never drops: it is news, not reassurance."""
        digest = hashlib.sha256(b"<moved/>").hexdigest()
        want = ("/home/u/.config/monitors.xml CHANGED across this call (absent "
                "-> %s); that should be impossible, please report it\n" % digest)
        self.assertIn(want, gnome_overlap.applied_text(self.moved))
        self.assertEqual(gnome_overlap.applied_text(self.moved, quiet=True), want)

    def test_a_forced_apply_says_so_on_every_invocation(self):
        """Named description and reason, in front of everything else, and never
        suppressed: a forced run never has an agreement to be quiet under."""
        out = gnome_overlap.applied_text(self.forced)
        self.assertTrue(out.startswith(
            "applied on an unmeasured GNOME through W11Overlap51: "
            "MetaMonitorsConfig is 80 bytes here, which is the size "
            "W11Overlap51 describes (measured on GNOME 51, and the same bytes "
            "as W11Overlap17, W11Overlap18).  Nothing was recorded; the next "
            "run asks in full again.\n"), out)

    def test_the_facts_an_agreement_is_recorded_against(self):
        got = gnome_overlap.facts(self.happy)
        self.assertEqual(got["shell"], "50.1")
        self.assertEqual(got["libmutter"], "18")
        self.assertEqual(got["struct_size"], 80)
        self.assertIs(got["forced"], False)
        self.assertEqual(got["libmutter_build"], BUILD)

    def test_the_struct_size_is_read_back_out_of_the_check_when_the_field_is_gone(self):
        """The text contract, exercised: an extension from before
        `instance_size` existed reported the same number in the typelib check's
        own detail, and `facts()` pulls it back out with a regex.  What that
        regex is run against is this file's pinned `typelib` detail."""
        older = {k: v for k, v in self.happy.items() if k != "instance_size"}
        self.assertEqual(gnome_overlap.facts(older)["struct_size"], 80)
        self.assertEqual([c for c in older["checks"] if c["name"] == "typelib"],
                         [{"name": "typelib", "ok": True,
                           "detail": "W11Overlap18, MetaMonitorsConfig 80 bytes "
                                     "as declared"}])


if __name__ == "__main__":
    unittest.main()
