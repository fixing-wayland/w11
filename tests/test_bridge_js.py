#!/usr/bin/env python3
"""The GNOME bridge extension, executed.

gnome/w11-bridge@w11/extension.js is 1500 lines of gjs that
no test had ever run: it imports `gi://Meta` and
`resource:///org/gnome/shell/ui/main.js`, which nothing outside a GNOME
session resolves, so the suite could only grep it for strings while
`MockBridge` (tests/test_backend_gnome.py) stood in for its behaviour --
two implementations of one protocol, tied together by three assertions.

node 22 runs the shipped file, at its own path, through
tests/fixtures/gjs/loader.mjs (support.js_harness): every `gi://` namespace
and every shell resource resolves to a recording double under
tests/fixtures/gjs/stubs/. What is proved here is behaviour of the file the
.deb installs -- the maximize rule per Mutter API generation, the JSON every
method answers compared with MockBridge's answer for the same state, the
window picker's grab released on all ten ways out, and the display /
desktop / workspace / monitor methods.

Every case is one node process, so the extension's module state (the
selection cooldown) starts clean; a case that needs two selections says so.
"""

import json
import math
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support                                              # noqa: E402
from test_backend_gnome import (CALC, DESKTOP, EDITOR, XTERM, MockBridge,   # noqa: E402
                                fixture_windows)
from test_dbus_mini import MockBus                          # noqa: E402

#: Meta.MaximizeFlags, read out of Meta-14.typelib and unchanged in Meta-18.
HORIZONTAL, VERTICAL, BOTH = 1, 2, 3
#: Two more window ids, for the rows JsonParity adds to fixture_windows().
DIALOG, PLAYER = 4194308, 4194309
#: extension.js SELECT_MAX_MS / SELECT_RELEASE_MS
SELECT_MAX_MS, SELECT_RELEASE_MS = 30000, 300


def parity_windows():
    """fixture_windows() plus the two rows a parity check needs.

    fixture_windows() is a plausible desktop, and on a plausible desktop
    eleven of the thirty-four fields _windowInfo() builds hold the same value
    on every window: above, decorated, fullscreen, maximized_h, maximized_v,
    monitor, on_all_workspaces, role, sandboxed_app_id, transient_for and
    urgent. A field that never varies cannot be told apart from a constant --
    measured: replacing `decorated: !!safe(() => w.decorated, true)` with
    `decorated: true` in extension.js left every parity test green, while the
    same mutation of `minimized` (which does vary) failed one. So the parity
    world gets two more windows, which between them flip all eleven, and
    MockBridge is handed the same rows.

    They are real shapes, not a bag of flipped bits: a modal file chooser
    owned by the editor (transient, urgent, kept above, on the second head,
    with the GTK role a file chooser reports), and a Flatpak video player
    gone fullscreen while sticky and undecorated. The player carries both
    maximize flags because Mutter's make_fullscreen() does not clear them --
    a window maximized before F11 is still maximized underneath. Telling the
    two axes APART is SetStateMaximize's 108-cell table, not this.
    """
    rows = fixture_windows()
    rows.append(_row(rows[1], id=DIALOG, title="Save Document",
                     wm_class="org.gnome.TextEditor", gtk_app_id="", desktop_id="",
                     window_type="MODAL_DIALOG", role="GtkFileChooserDialog",
                     minimized=False, hidden=False, transient_for=EDITOR,
                     urgent=True, above=True, monitor=1,
                     x=700, y=400, width=480, height=320))
    rows.append(_row(rows[2], id=PLAYER, title="Big Buck Bunny",
                     wm_class="org.videolan.VLC", gtk_app_id="org.videolan.VLC",
                     desktop_id="", sandboxed_app_id="org.videolan.VLC", pid=1500,
                     workspace=-1, on_all_workspaces=True, on_active_workspace=True,
                     monitor=1, fullscreen=True, decorated=False,
                     maximized_h=True, maximized_v=True,
                     x=1920, y=0, width=1920, height=1080))
    return rows


def _row(base, **kw):
    """One more ListWindows row on the shape fixture_windows() builds: the
    buffer rect follows the frame (its windows have no shadow in the JSON)
    and stable_sequence is the low byte of the id."""
    d = dict(base)
    d.update(kw)
    d["buffer_rect"] = {"x": d["x"], "y": d["y"],
                        "width": d["width"], "height": d["height"]}
    d["stable_sequence"] = d["id"] & 0xff
    return d

# ---------------------------------------------------------------------------
# The world a case builds before it turns the extension on.
#
# gjs hands an extension `global` (Shell's Global object), the `Main` module
# and whatever Mutter exports; none of that has a default worth trusting, so
# every case builds the display, workspace manager and window actors it wants
# out of ListWindows rows -- the same dicts test_backend_gnome.fixture_windows()
# feeds MockBridge -- and the two answers are then compared.
#
# It is a Python string rather than a file under tests/fixtures/gjs/stubs/
# because the stubs are shared with the overlap extension's tests and carry
# nothing bridge-shaped; this is the bridge's own scaffolding.

WORLD = r"""
import Clutter from 'gi://Clutter';
import Gio, {makeInvocation} from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as H from '@STUBS@/harness.mjs';
import {installGlobals, makeEmitter} from '@STUBS@/gjs-globals.mjs';
import Bridge from '@MODULE@/extension.js';

const ARG = JSON.parse(process.argv[2] ?? 'null');

// The extension logs through console; the harness reads stdout as one JSON
// document, so the journal is collected and handed back in `logs` instead.
const LOGS = [];
console.log = (...a) => LOGS.push(a.join(' '));
console.debug = (...a) => LOGS.push('debug: ' + a.join(' '));
console.warn = console.log;
console.error = console.log;

function emit(o) {
    process.stdout.write(JSON.stringify(Object.assign({logs: LOGS}, o)));
}

/** An actor group as Clutter spells it: the Main stub keeps `children`. */
function withChildren(group) {
    group.get_children = () => group.children;
    return group;
}

/**
 * `rows` are ListWindows rows. opts:
 *   shape        Mutter API generation for the windows ('46'/'49'/'flags'/'hostile')
 *   nWorkspaces  how many, active   which one is active
 *   monitors     [{x,y,width,height,geometry_scale,connector}] for
 *                Main.layoutManager and the monitor manager
 *   primary      Main.layoutManager.primaryIndex
 *   manager      'none' (GNOME 46: no way to enumerate connectors),
 *                'monitors' (49+/50), 'logical' (the probed fallback)
 */
function makeWorld(rows, opts = {}) {
    const shape = opts.shape || '46';
    const wins = rows.map(r => Meta.makeWindow(r, {shape}));
    const actors = wins.map(w => Meta.makeActor(w));
    const byId = new Map(rows.map((r, i) => [r.id, wins[i]]));
    rows.forEach((r, i) => {
        if (r.desktop_id)
            Shell.setApp(wins[i], r.desktop_id);
        // get_transient_for() answers the parent WINDOW; the JSON row can
        // only carry its id, so the double is wired up here rather than in
        // the shared stub (which takes opts.transientFor, one window at a
        // time). A row naming a parent that is not in this world -- a dialog
        // whose owner is already gone -- gets null, as Mutter would give.
        if (r.transient_for) {
            wins[i].get_transient_for = () => H.record(
                `w${r.id}.get_transient_for`, [], byId.get(r.transient_for) ?? null);
        }
    });
    const area = opts.workArea || {x: 0, y: 32, width: 1920, height: 1048};
    const spaces = [];
    const makeSpace = i => {
        const ws = Meta.makeWorkspace(i);
        ws.get_work_area_all_monitors = () => H.record('ws.work_area', [i], area);
        ws.activate = () => H.record('ws.activate', [i]);
        ws.list_windows = () => H.record('ws.list_windows', [i],
            wins.filter((w, n) => (rows[n].workspace ?? 0) === i));
        return ws;
    };
    for (let i = 0; i < (opts.nWorkspaces ?? 3); i++)
        spaces.push(makeSpace(i));
    let active = opts.active ?? 0;

    const display = makeEmitter('display');
    display.get_size = () => H.record('display.get_size', [], [1920, 1080]);
    display.list_all_windows = () => H.record('display.list_all_windows', [], wins);
    display.set_cursor = c => H.record('display.set_cursor', [c]);
    display.focus_window = wins.find((w, n) => rows[n].focused) ?? null;

    const wm = makeEmitter('workspace_manager');
    wm.get_active_workspace = () => H.record('wm.active', [], spaces[active]);
    wm.get_active_workspace_index = () => H.record('wm.active_index', [], active);
    wm.get_n_workspaces = () => H.record('wm.n_workspaces', [], spaces.length);
    wm.get_workspace_by_index = i => H.record('wm.by_index', [i], spaces[i] ?? null);
    wm.append_new_workspace = () => {
        H.record('wm.append_new_workspace', []);
        spaces.push(makeSpace(spaces.length));
    };
    wm.remove_workspace = () => {
        H.record('wm.remove_workspace', []);
        spaces.pop();
    };

    const stage = makeEmitter('stage');
    const windowManager = H.tag({}, 'window_manager');
    if (opts.completeDisplayChange !== false) {
        windowManager.complete_display_change =
            k => H.record('window_manager.complete_display_change', [k]);
    }

    // Monitors: Main.layoutManager is what ListMonitors reads, the monitor
    // manager only supplies connector names (and cannot on 46).
    const mons = opts.monitors || [];
    Main.layoutManager.monitors = mons.map((m, i) => Object.assign(
        {index: i, x: 0, y: 0, width: 1920, height: 1080, geometry_scale: 1}, m));
    Main.layoutManager.primaryIndex = opts.primary ?? 0;
    withChildren(Main.layoutManager.modalDialogGroup);
    withChildren(Main.uiGroup);
    const mm = H.tag({}, 'MonitorManager');
    const byConnector = new Map();
    mons.forEach((m, i) => {
        if (m.connector)
            byConnector.set(m.connector, i);
    });
    if (opts.manager === 'monitors' || opts.manager === undefined) {
        mm.get_monitors = () => H.record('mm.get_monitors', [],
            [...byConnector.keys()].map(c => ({get_connector: () => c})));
        mm.get_monitor_for_connector =
            c => H.record('mm.get_monitor_for_connector', [c],
                          byConnector.has(c) ? byConnector.get(c) : -1);
    }
    if (opts.manager === 'logical') {
        mm.get_logical_monitors = () => H.record('mm.get_logical_monitors', [],
            [...byConnector.entries()].map(([c, i]) => ({
                get_number: () => i,
                get_monitors: () => [{get_connector: () => c}],
            })));
    }
    const backend = H.tag({}, 'backend');
    if (opts.manager !== 'none')
        backend.get_monitor_manager = () => H.record('backend.get_monitor_manager', [], mm);

    installGlobals({
        display, workspace_manager: wm, stage, backend,
        window_manager: windowManager,
        get_window_actors: () => H.record('global.get_window_actors', [], actors),
        get_pointer: () => H.record('global.get_pointer', [], opts.pointer || [640, 400, 0]),
        get_current_time: () => H.record('global.get_current_time', [], 12345),
    });
    GLib.environ = Object.assign(
        {DISPLAY: ':0', XAUTHORITY: '/run/user/1000/.mutter-Xwaylandauth.AB12CD'},
        opts.environ || {});
    return {wins, actors, spaces, display, wm, stage, rows,
            setActive: i => {
                active = i;
            }};
}

/**
 * The extension gnome-shell constructs, enabled, and the copy of it that is
 * actually running.  Since the route-3 split the object the shell holds is
 * the ROOT: it exports org.w11.BridgeReload1 and keeps `_live`, the instance
 * every org.w11.Bridge1 method is exported from (extension.js, `_swap`).  A
 * case drives that one; `root` is for the cases that are about the reload.
 */
function enabled(metadata = {uuid: 'w11-bridge@w11'}) {
    const root = new Bridge(metadata);
    root.enable();
    return {root, ext: root._live};
}

function bridgeOn(rows, opts = {}) {
    const world = makeWorld(rows, opts);
    const {root, ext} = enabled();
    return {ext, root, world};
}

/** Drive one D-Bus method the way GJS does and hand back the invocation. */
function invoke(ext, name, params = []) {
    const inv = makeInvocation();
    ext[`${name}Async`](params, inv);
    return inv;
}

/** ...and its out values, turning a D-Bus error into a JS one. */
function reply(ext, name, params = []) {
    const inv = invoke(ext, name, params);
    if (inv.error)
        throw new Error(`${name}: ${inv.error[0]}: ${inv.error[1]}`);
    return inv.reply.value;
}

function jsonReply(ext, name, params = []) {
    return JSON.parse(reply(ext, name, params)[0]);
}

/** The error of a call that must fail, as [name, message]. */
function failure(ext, name, params = []) {
    const inv = invoke(ext, name, params);
    if (!inv.error)
        throw new Error(`${name} was expected to fail, answered ${JSON.stringify(inv.reply)}`);
    return inv.error;
}
"""


class _Node(unittest.TestCase):
    """A case is a whole ES module; WORLD is prepended to every one of them."""

    MODULE = support.BRIDGE_EXT

    def run_js(self, body, arg=None):
        return support.js_harness(self.MODULE, WORLD + body, arg)


@support.skip_without_node
class SetStateMaximize(_Node):
    """T01/F0.4: `_setState` of the three maximize names, executed.

    The whole table -- 4 (h, v) starting states x 3 state names x 3 actions,
    on each of the three Mutter API generations -- because the rule is not
    one thing: which axes a call touches comes from the NAME, and which way
    a toggle goes comes from the horizontal flag, which is Mutter's own rule
    for the two atoms of one _NET_WM_STATE message (window-x11.c: `max =
    action == ADD || (action == TOGGLE && !...is_maximized_horizontally())`).
    Bridge v1/v2 asked instead for "both axes are already set", which
    disagreed with wmctrl on X for a window maximized on one axis only.

    The generations, measured on the rig: mutter 46-48 take the directions
    in maximize(flags)/unmaximize(flags) (noble-gnome, GNOME Shell 46.0);
    mutter 49+ moved them to set_maximize_flags()/set_unmaximize_flags() and
    its maximize() takes no argument at all -- the path GNOME 51.beta took on
    stonking-gnome, where the pair still returned the window to exactly
    100,100 800x600.
    """

    #: state name -> (touches horizontal, touches vertical, reads this flag)
    NAMES = {"MAXIMIZED": (True, True, "h"),
             "MAXIMIZED_HORZ": (True, False, "h"),
             "MAXIMIZED_VERT": (False, True, "v")}

    CASE = r"""
const ROW = {id: 1, title: 't', wm_class: 'c', window_type: 'NORMAL',
             x: 10, y: 20, width: 30, height: 40, client_type: 'wayland'};
const out = {};
for (const shape of ARG.shapes) {
  for (const h of [false, true]) {
    for (const v of [false, true]) {
      for (const name of ARG.names) {
        for (const action of ['add', 'remove', 'toggle']) {
          H.reset();
          const w = Meta.makeWindow({...ROW, maximized_h: h, maximized_v: v}, {shape});
          const ret = Bridge.prototype._setState.call({}, w, name, action);
          const key = [shape, h ? 1 : 0, v ? 1 : 0, name, action].join('|');
          out[key] = {
            ret,
            acts: H.calls.filter(c => /\.(set_)?(un)?maximize(_flags)?$/.test(c.what))
                         .map(c => [c.what.replace('w1.', ''), c.args]),
            reads: H.names().filter(n => /get:maximized|get_maximize_flags|is_maximized/.test(n))
                            .map(n => n.replace('w1.', '')),
            state: [w.state.h, w.state.v],
          };
        }
      }
    }
  }
}
emit({table: out});
"""

    @classmethod
    def setUpClass(cls):
        cls.got = None

    def table(self, shapes):
        if self.got is None:
            type(self).got = self.run_js(
                self.CASE, {"shapes": shapes, "names": sorted(self.NAMES)})["table"]
        return self.got

    def expected(self, shape, h, v, name, action):
        horz, vert, reads = self.NAMES[name]
        cur = h if reads == "h" else v
        on = {"add": True, "remove": False}.get(action, not cur)
        flags = (HORIZONTAL if horz else 0) | (VERTICAL if vert else 0)
        if shape == "49":
            call = "set_maximize_flags" if on else "set_unmaximize_flags"
        else:
            call = "maximize" if on else "unmaximize"
        state = [h, v]
        if horz:
            state[0] = on
        if vert:
            state[1] = on
        return {"call": [call, [flags]], "state": state, "on": on}

    def test_every_cell_of_the_table_on_all_three_api_generations(self):
        got = self.table(["46", "49", "flags"])
        self.assertEqual(len(got), 3 * 4 * 3 * 3)
        for key, cell in sorted(got.items()):
            shape, h, v, name, action = key.split("|")
            want = self.expected(shape, h == "1", v == "1", name, action)
            self.assertTrue(cell["ret"], key)
            self.assertEqual(cell["acts"], [want["call"]], key)
            self.assertEqual(cell["state"], want["state"], key)

    def test_a_toggle_of_the_pair_follows_the_horizontal_flag_alone(self):
        """The cell that made bridge v3: horizontally maximized only, toggle
        the pair. v2 asked `h && v` and maximized both; Mutter (and real
        wmctrl through it) unmaximizes both."""
        got = self.table(["46", "49", "flags"])
        for shape in ("46", "49", "flags"):
            down = "set_unmaximize_flags" if shape == "49" else "unmaximize"
            up = "set_maximize_flags" if shape == "49" else "maximize"
            self.assertEqual(got["%s|1|0|MAXIMIZED|toggle" % shape]["acts"],
                             [[down, [BOTH]]], shape)
            self.assertEqual(got["%s|1|0|MAXIMIZED|toggle" % shape]["state"],
                             [False, False], shape)
            # vertically maximized only: the horizontal flag is clear, so the
            # pair goes UP, not down -- the axes are never swapped
            self.assertEqual(got["%s|0|1|MAXIMIZED|toggle" % shape]["acts"],
                             [[up, [BOTH]]], shape)
            self.assertEqual(got["%s|0|1|MAXIMIZED|toggle" % shape]["state"],
                             [True, True], shape)
            # ...and a single-axis toggle reads its OWN axis
            self.assertEqual(got["%s|0|1|MAXIMIZED_VERT|toggle" % shape]["acts"],
                             [[down, [VERTICAL]]], shape)
            self.assertEqual(got["%s|0|1|MAXIMIZED_HORZ|toggle" % shape]["acts"],
                             [[up, [HORIZONTAL]]], shape)

    def test_add_and_remove_ignore_the_flags_the_window_already_has(self):
        got = self.table(["46", "49", "flags"])
        for h in (0, 1):
            for v in (0, 1):
                key = "46|%d|%d|MAXIMIZED|%%s" % (h, v)
                self.assertEqual(got[key % "add"]["acts"], [["maximize", [BOTH]]], key)
                self.assertEqual(got[key % "remove"]["acts"],
                                 [["unmaximize", [BOTH]]], key)

    def test_each_generation_is_read_through_its_own_api(self):
        """maximizedFlags() prefers the boolean GObject properties (46 and
        50 both define them), and falls through to get_maximize_flags() for a
        window that has neither -- which is the shape a typelib without the
        properties presents."""
        got = self.table(["46", "49", "flags"])
        self.assertEqual(got["46|0|0|MAXIMIZED|add"]["reads"],
                         ["get:maximized_horizontally", "get:maximized_vertically"])
        self.assertEqual(got["49|0|0|MAXIMIZED|add"]["reads"],
                         ["get:maximized_horizontally", "get:maximized_vertically"])
        self.assertEqual(got["flags|0|0|MAXIMIZED|add"]["reads"],
                         ["get_maximize_flags"])

    def test_mutter_49_maximize_takes_no_argument_and_the_bridge_knows(self):
        """gjs turns an extra argument into a TypeError, so a bridge that
        kept calling maximize(flags) on mutter 49+ would throw instead of
        maximizing. The 49-shaped double raises exactly that, and the pair
        still lands -- which is what the stonking-gnome measurement showed."""
        got = self.run_js(r"""
const ROW = {id: 1, title: 't', wm_class: 'c', window_type: 'NORMAL',
             x: 0, y: 0, width: 10, height: 10, client_type: 'wayland',
             maximized_h: false, maximized_v: false};
const w = Meta.makeWindow(ROW, {shape: '49'});
let threw = null;
try {
    w.maximize(3);
} catch (e) {
    threw = `${e}`;
}
H.reset();
Bridge.prototype._setState.call({}, w, 'MAXIMIZED', 'add');
Bridge.prototype._setState.call({}, w, 'MAXIMIZED', 'remove');
emit({threw, names: H.names(), state: [w.state.h, w.state.v]});
""")
        self.assertIn("expected 0 arguments", got["threw"])
        self.assertEqual([n for n in got["names"] if "maximize" in n and "get" not in n],
                         ["w1.set_maximize_flags", "w1.set_unmaximize_flags"])
        self.assertEqual(got["state"], [False, False])

    def test_a_hostile_window_defaults_to_not_maximized_and_still_asks(self):
        """Every getter throwing is the hostile-compositor axis: safe()
        answers [false, false], so a toggle asks to maximize -- and the call
        is made (and throws out of _setState, which _invoke turns into a
        D-Bus error rather than a shell crash)."""
        got = self.run_js(r"""
const ROW = {id: 1, title: 't', wm_class: 'c', window_type: 'NORMAL',
             x: 0, y: 0, width: 10, height: 10, client_type: 'wayland'};
const w = Meta.makeWindow(ROW, {shape: 'hostile'});
H.reset();
let threw = null;
try {
    Bridge.prototype._setState.call({}, w, 'MAXIMIZED', 'toggle');
} catch (e) {
    threw = `${e}`;
}
const ext = new Bridge({uuid: 'u'});
ext._find = () => w;
ext._installMethods();
const inv = makeInvocation();
ext.SetStateAsync([1, 'MAXIMIZED', 'toggle'], inv);
emit({threw, names: H.names(), error: inv.error});
""")
        self.assertIn("hostile", got["threw"])
        self.assertIn("w1.maximize", got["names"])
        self.assertEqual(got["error"][0], "org.w11.Bridge1.Failed")
        self.assertIn("hostile", got["error"][1])


@support.skip_without_node
class JsonParity(_Node):
    """T02: the extension and MockBridge answer the same JSON.

    MockBridge is what every other GNOME test in this suite drives, and
    nothing tied the two together: a field the extension renamed, a default
    it changed or a type it stopped casting would have gone unnoticed for as
    long as both files agreed with themselves. Here the fixture rows are
    turned back into Meta.Window doubles, the shipped extension is enabled
    over them, and each method's answer is compared with MockBridge's answer
    for the same state.

    The rows are parity_windows(), not fixture_windows(): a comparison proves
    only the fields the fixture varies, and fixture_windows() holds eleven of
    them at the same value on every window.
    """

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()
        cls.bridge = MockBridge(cls.mock)
        # Two independent builds of the same six rows: the extension is run
        # over one and MockBridge answers out of the other, so nothing is
        # shared but the values themselves.
        cls.rows = parity_windows()
        cls.bridge.windows = parity_windows()
        cls.js = None

    @classmethod
    def tearDownClass(cls):
        cls.bridge.close()
        cls.mock.close()

    #: What Main.layoutManager reports on the QEMU rig, and what MockBridge
    #: answers for ListMonitors: one 1920x1080 head, scale 1, Virtual-1.
    MONITORS = [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080,
                 "geometry_scale": 1, "connector": "Virtual-1"}]

    def answers(self):
        if self.js is None:
            type(self).js = self.run_js(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors});
emit({
    ListWindows: jsonReply(ext, 'ListWindows'),
    GetWindow: jsonReply(ext, 'GetWindow', [ARG.xterm]),
    ListWorkspaces: jsonReply(ext, 'ListWorkspaces'),
    ListMonitors: jsonReply(ext, 'ListMonitors'),
    GetActiveWorkspace: reply(ext, 'GetActiveWorkspace'),
    GetNWorkspaces: reply(ext, 'GetNWorkspaces'),
    DisplaySize: reply(ext, 'DisplaySize'),
    GetPointer: reply(ext, 'GetPointer'),
    XInfo: reply(ext, 'XInfo'),
    GetVersion: reply(ext, 'GetVersion'),
});
""", {"rows": self.rows, "monitors": self.MONITORS, "xterm": XTERM})
        return self.js

    def mock_json(self, member, *args):
        sig, out = getattr(self.bridge, "m_" + member)(None, *args)
        return json.loads(out[0]) if sig == "s" else list(out)

    def test_list_windows_is_the_same_document_field_for_field(self):
        """Not "the same keys": the same values, for all six parity rows
        including the XWayland one whose xid the extension has to recover
        from Mutter's window description (`0x400005 (test@vm: ~)`, the shape
        measured on GNOME 50) because there is no X11 display to ask."""
        js = self.answers()["ListWindows"]
        self.assertEqual(js, self.mock_json("ListWindows"))
        self.assertEqual([w["id"] for w in js],
                         [DESKTOP, EDITOR, CALC, XTERM, DIALOG, PLAYER])
        self.assertEqual(js[3]["xid"], 0x400005)
        self.assertEqual(js[3]["client_type"], "x11")
        self.assertEqual(js[1]["desktop_id"], "org.gnome.TextEditor.desktop")

    def test_no_field_of_the_document_is_a_constant(self):
        """The parity comparison above is only worth what the fixture varies:
        a value the extension hard-coded would still match a row that happens
        to hold it. So every key of a ListWindows row is asserted to take at
        least two different values across the six -- which is what
        parity_windows()'s dialog and player are for -- and the eleven fields
        that were uniform before them are named here so the next person to
        add a row knows what is load-bearing.

        Measured while writing it: with only the four original rows,
        `decorated: true` in place of the safe() read passes this whole
        class; with the dialog and the player it does not."""
        js = self.answers()["ListWindows"]
        self.assertEqual(len(js), 6)
        flat = [{k: json.dumps(v, sort_keys=True) for k, v in row.items()}
                for row in js]
        constant = sorted(k for k in flat[0]
                          if len({row[k] for row in flat}) == 1)
        self.assertEqual(constant, [])
        # ...and specifically these, which fixture_windows() alone could not
        # tell from a literal
        for key, values in (("above", {False, True}),
                            ("decorated", {False, True}),
                            ("fullscreen", {False, True}),
                            ("maximized_h", {False, True}),
                            ("maximized_v", {False, True}),
                            ("monitor", {0, 1}),
                            ("on_all_workspaces", {False, True}),
                            ("role", {"", "GtkFileChooserDialog"}),
                            ("sandboxed_app_id", {"", "org.videolan.VLC"}),
                            ("transient_for", {0, EDITOR}),
                            ("urgent", {False, True})):
            self.assertEqual({row[key] for row in js}, values, key)

    def test_every_other_method_answers_what_mockbridge_answers(self):
        js = self.answers()
        for member, args in (("GetWindow", (XTERM,)), ("ListWorkspaces", ()),
                             ("ListMonitors", ()), ("GetActiveWorkspace", ()),
                             ("GetNWorkspaces", ()), ("DisplaySize", ()),
                             ("GetPointer", ()), ("XInfo", ()),
                             ("GetVersion", ())):
            self.assertEqual(js[member], self.mock_json(member, *args), member)

    def test_the_journal_line_names_the_version_and_the_shell(self):
        """The one line that says the bridge came up, and which version of
        it: `[w11-bridge] enabled (bridge v2, gnome-shell 46.0)` is
        what gnome/README.md:443 records for the 24.04 golden, and
        `[w11-bridge] enabled (bridge v3, gnome-shell 51.beta)` is
        what the orchestrator read out of journalctl on stonking-gnome after
        the metadata edit. Both halves have to be right -- a wrong VERSION
        here is how you diagnose an old extension.js still installed -- so
        the number in the line is MockBridge.VERSION and the shell string is
        Config.PACKAGE_VERSION, not a literal."""
        logs = self.answers()["logs"]
        self.assertIn("[w11-bridge] enabled (bridge v%d, gnome-shell 50.1)"
                      % MockBridge.VERSION, logs)

    def test_the_bus_name_and_object_path_are_the_ones_the_client_dials(self):
        got = self.run_js(r"""
const {ext} = bridgeOn(ARG);
const e = Gio.exported();
emit({path: e.path, exported: e.exported, names: Gio.names().map(n => n.name),
      version: ext.Version});
""", self.rows)
        self.assertEqual(got["path"], "/org/w11/Bridge")
        self.assertTrue(got["exported"])
        self.assertEqual(got["names"], ["org.w11.Bridge"])
        self.assertEqual(got["version"], MockBridge.VERSION)

    def test_a_window_whose_getters_all_throw_yields_the_safe_defaults(self):
        """The hostile-compositor axis of _windowInfo: every read is wrapped
        in safe(), so an unmanaged window being torn down under the call
        produces a row of defaults rather than an exception. get_id() is the
        one read that is not -- a window with no id is not a row at all --
        so it answers here and everything else throws."""
        got = self.run_js(r"""
const {ext} = bridgeOn(ARG.rows);
const w = Meta.makeWindow({id: 77, title: 'x', window_type: 'NORMAL',
                           x: 1, y: 2, width: 3, height: 4,
                           client_type: 'wayland'}, {shape: 'hostile'});
w.get_id = () => 77;
emit({info: ext._windowInfo(w)});
""", {"rows": self.rows})
        self.assertEqual(got["info"], {
            "id": 77, "xid": 0, "title": "", "wm_class": "",
            "wm_class_instance": "", "gtk_app_id": "", "sandboxed_app_id": "",
            "desktop_id": "", "role": "", "pid": 0, "client_type": "wayland",
            "window_type": "-1",
            "x": 0, "y": 0, "width": 0, "height": 0,
            "buffer_rect": {"x": 0, "y": 0, "width": 0, "height": 0},
            "focused": False, "minimized": False, "hidden": False,
            "on_all_workspaces": False, "workspace": -1,
            "on_active_workspace": False, "monitor": -1, "fullscreen": False,
            "maximized_h": False, "maximized_v": False, "above": False,
            "urgent": False, "skip_taskbar": False, "transient_for": 0,
            "decorated": True, "stable_sequence": 0})

    def test_a_window_that_cannot_be_read_at_all_is_skipped_not_fatal(self):
        """...and one whose get_id() throws too has no row: ListWindows drops
        it (with a debug line) and still answers for the other six, because
        one dying window may not take the whole listing with it."""
        got = self.run_js(r"""
const rows = ARG.rows;
const world = makeWorld(rows);
const bad = Meta.makeWindow({id: 99, window_type: 'NORMAL', x: 0, y: 0,
                             width: 1, height: 1, client_type: 'wayland'},
                            {shape: 'hostile'});
const actors = world.actors.concat([Meta.makeActor(bad)]);
installGlobals({get_window_actors: () => actors});
const {ext} = enabled({uuid: 'u'});
emit({ids: jsonReply(ext, 'ListWindows').map(d => d.id)});
""", {"rows": self.rows})
        self.assertEqual(got["ids"],
                         [DESKTOP, EDITOR, CALC, XTERM, DIALOG, PLAYER])
        self.assertNotIn(99, got["ids"])


#: A "Keep these display settings?" dialog, as gnome-shell's windowManager.js
#: registers it: `GObject.registerClass(class DisplayChangeDialog extends
#: ModalDialog.ModalDialog)`, whose _init ends in
#: `Main.layoutManager.modalDialogGroup.add_child(this)`. Registration leaves
#: constructor.name alone on gjs 1.80.2 and 1.88.0 and adds
#: constructor.$gtype.name = 'Gjs_DisplayChangeDialog', so either half of the
#: pair identifies it -- both are built here.
DIALOG_JS = r"""
function makeDialog(opts = {}) {
    let cls;
    if (opts.gtypeOnly) {
        cls = class SomethingElse {};
        cls.$gtype = {name: 'Gjs_DisplayChangeDialog'};
    } else {
        cls = class DisplayChangeDialog {};
    }
    const d = new cls();
    H.tag(d, opts.tag || 'dialog');
    d._onSuccess = () => H.record(`${opts.tag || 'dialog'}._onSuccess`, []);
    if (!opts.noFailure)
        d._onFailure = () => H.record(`${opts.tag || 'dialog'}._onFailure`, []);
    return d;
}
"""


@support.skip_without_node
class ConfirmDisplayChange(_Node):
    """T29: `ConfirmDisplayChange`, executed against a dialog double.

    Measured live on GNOME 46.0, 50.1 and 51.beta (gnome/README.md "Verified
    live"): `wxrandr --persistent` raises the dialog, ConfirmDisplayChange
    (true) returns (true,) and Mutter writes ~/.config/monitors.xml
    immediately; with no dialog on screen it returns (false,) and nothing
    changes. Both halves are here, plus the shapes of dialog the lookup has
    to accept and refuse.
    """

    def run_case(self, body, arg=None):
        return self.run_js(DIALOG_JS + body, arg)

    def test_keep_presses_keep_on_the_dialog_and_leaves_mutter_alone(self):
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors});
Main.layoutManager.modalDialogGroup.add_child(makeDialog());
H.reset();
emit({keep: reply(ext, 'ConfirmDisplayChange', [true]), names: H.names()});
""", {"rows": fixture_windows(), "monitors": self.MONITORS})
        self.assertEqual(got["keep"], [True])
        self.assertEqual([n for n in got["names"] if n.startswith("dialog.")],
                         ["dialog._onSuccess"])
        # the dialog's own handler is what tells Mutter; calling it AND
        # complete_display_change would answer the same question twice
        self.assertNotIn("window_manager.complete_display_change", got["names"])

    def test_revert_presses_the_other_button(self):
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors});
Main.layoutManager.modalDialogGroup.add_child(makeDialog());
H.reset();
emit({revert: reply(ext, 'ConfirmDisplayChange', [false]), names: H.names()});
""", {"rows": fixture_windows(), "monitors": self.MONITORS})
        self.assertEqual(got["revert"], [True])
        self.assertEqual([n for n in got["names"] if n.startswith("dialog.")],
                         ["dialog._onFailure"])

    def test_the_gtype_spelling_is_accepted_and_a_half_dialog_is_not(self):
        """Three children in one group: something that is only a
        DisplayChangeDialog by its GType name (which is how gjs 1.88 spells a
        registered class), something that looks like one but has no Revert
        action -- half a dialog is not one, and pressing Keep on it would
        leave the session with no way back -- and the real one."""
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors});
const g = Main.layoutManager.modalDialogGroup;
g.add_child(makeDialog({noFailure: true, tag: 'half'}));
g.add_child(makeDialog({gtypeOnly: true, tag: 'gtype'}));
H.reset();
emit({keep: reply(ext, 'ConfirmDisplayChange', [true]), names: H.names()});
""", {"rows": fixture_windows(), "monitors": self.MONITORS})
        self.assertEqual(got["keep"], [True])
        self.assertEqual([n for n in got["names"] if "_onSuccess" in n],
                         ["gtype._onSuccess"])
        self.assertNotIn("half._onSuccess", got["names"])

    def test_a_dialog_that_moved_to_the_ui_group_is_still_found(self):
        """Insurance for a release that stops parenting it to
        modalDialogGroup: uiGroup is searched second, never first."""
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors});
Main.uiGroup.add_child(makeDialog({tag: 'ui'}));
H.reset();
emit({keep: reply(ext, 'ConfirmDisplayChange', [true]), names: H.names()});
""", {"rows": fixture_windows(), "monitors": self.MONITORS})
        self.assertEqual(got["keep"], [True])
        self.assertIn("ui._onSuccess", got["names"])

    def test_no_dialog_forwards_the_verdict_to_mutter_and_answers_false(self):
        """Live on 46, 50 and 51: (false,) with nothing on screen, and seven
        calls with no dialog left the layout and monitors.xml untouched. The
        caller needs the distinction -- "pressed it" is not "there was
        nothing to press"."""
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors});
H.reset();
const first = reply(ext, 'ConfirmDisplayChange', [true]);
const second = reply(ext, 'ConfirmDisplayChange', [false]);
emit({first, second, calls: H.callsTo('window_manager.complete_display_change')});
""", {"rows": fixture_windows(), "monitors": self.MONITORS})
        self.assertEqual(got["first"], [False])
        self.assertEqual(got["second"], [False])
        self.assertEqual([c["args"] for c in got["calls"]], [[True], [False]])

    def test_a_shell_without_complete_display_change_answers_false(self):
        """Probed, never assumed: the method is in the Shell-14 and Shell-18
        typelibs, and a shell without it must mean "not handled", not a
        traceback in the journal."""
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors,
                                  completeDisplayChange: false});
H.reset();
emit({answer: reply(ext, 'ConfirmDisplayChange', [true]), names: H.names()});
""", {"rows": fixture_windows(), "monitors": self.MONITORS})
        self.assertEqual(got["answer"], [False])
        self.assertEqual([n for n in got["names"] if "complete_display" in n], [])

    def test_keeping_a_layout_with_no_monitor_left_is_refused(self):
        """The one outcome the dialog exists to prevent: nobody can press
        Revert on a screen that is not there, and the 20-second self-revert
        is the only way back. An UNREADABLE monitor list is not a refusal --
        the count is -1 then, not 0 -- and Revert is never refused."""
        got = self.run_case(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: []});
const refused = failure(ext, 'ConfirmDisplayChange', [true]);
const revert = reply(ext, 'ConfirmDisplayChange', [false]);
Main.layoutManager.monitors = null;      // unreadable, not empty
const unreadable = reply(ext, 'ConfirmDisplayChange', [true]);
emit({refused, revert, unreadable});
""", {"rows": fixture_windows()})
        self.assertEqual(got["refused"][0], "org.w11.Bridge1.Unsupported")
        self.assertIn("no enabled monitor", got["refused"][1])
        self.assertEqual(got["revert"], [False])
        self.assertEqual(got["unreadable"], [False])

    MONITORS = [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080,
                 "geometry_scale": 1, "connector": "Virtual-1"}]


@support.skip_without_node
class MonitorsWorkspacesAndDesktop(_Node):
    """T29: ListMonitors, ShowDesktop and SetNWorkspaces, executed."""

    ROWS = None

    @classmethod
    def setUpClass(cls):
        cls.ROWS = fixture_windows()

    def test_list_monitors_types_the_scale_and_names_the_primary(self):
        """Fractional scale is the point: `geometry_scale` is 1.5 on a
        125%-scaled head and the row has to carry 1.5, not 1 -- wxrandr's
        callers divide by it. primaryIndex 1 means the second head is
        primary, which is not the same as index 1 being second in the list."""
        got = self.run_js(r"""
const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors, primary: 1});
emit({mons: jsonReply(ext, 'ListMonitors')});
""", {"rows": self.ROWS,
      "monitors": [{"index": 0, "x": 0, "y": 0, "width": 3840, "height": 2160,
                    "geometry_scale": 2, "connector": "DP-1"},
                   {"index": 1, "x": 1920, "y": 0, "width": 2560, "height": 1440,
                    "geometry_scale": 1.5, "connector": "HDMI-1"}]})
        self.assertEqual(got["mons"], [
            {"index": 0, "x": 0, "y": 0, "width": 3840, "height": 2160,
             "scale": 2, "primary": False, "connector": "DP-1"},
            {"index": 1, "x": 1920, "y": 0, "width": 2560, "height": 1440,
             "scale": 1.5, "primary": True, "connector": "HDMI-1"}])

    def test_the_connector_route_per_gnome_release(self):
        """GNOME 46 exports no way to enumerate connectors from JS, so the
        name is "" there and a client matches DisplayConfig's logical
        monitors on x/y instead (verified live on noble-gnome); 49+/50 answer
        through MonitorManager.get_monitors() (verified on resolute-gnome,
        where ListMonitors reports Virtual-1); and get_logical_monitors() is
        the probed fallback the 49 porting notes mention."""
        mons = [{"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080,
                 "geometry_scale": 1, "connector": "Virtual-1"}]
        got = self.run_js(r"""
const out = {};
for (const manager of ['none', 'monitors', 'logical']) {
    H.reset();
    Main.reset();
    const {ext} = bridgeOn(ARG.rows, {monitors: ARG.monitors, manager});
    out[manager] = jsonReply(ext, 'ListMonitors');
}
emit({out});
""", {"rows": self.ROWS, "monitors": mons})
        self.assertEqual(got["out"]["none"][0]["connector"], "")
        self.assertEqual(got["out"]["monitors"][0]["connector"], "Virtual-1")
        self.assertEqual(got["out"]["logical"][0]["connector"], "Virtual-1")
        for route in ("none", "monitors", "logical"):
            self.assertEqual(got["out"][route][0]["width"], 1920, route)

    def test_show_desktop_minimizes_what_it_should_and_latches(self):
        """`wmctrl -k on` twice then `-k off` restores the desktop on every
        real window manager, so ShowDesktop(true) is a LATCH: a second one
        must not rescan, because the scan skips already-minimized windows and
        would store an empty restore set. DESKTOP and DOCK layers, windows
        that say they cannot be minimized, override-redirect surfaces and
        windows on another workspace are all left alone."""
        rows = fixture_windows()
        rows.append(dict(rows[0], id=5001, title="Dock", window_type="DOCK",
                         minimized=False, hidden=False))
        rows.append(dict(rows[0], id=5002, title="Locked", window_type="NORMAL",
                         minimized=False, hidden=False))
        rows.append(dict(rows[0], id=5003, title="Popup", window_type="NORMAL",
                         minimized=False, hidden=False))
        got = self.run_js(r"""
const {ext, world} = bridgeOn(ARG.rows);
const win = id => world.wins.find(w => w.__fw === `Window(${id})`);
win(5002).can_minimize = () => false;
win(5003).is_override_redirect = () => true;
const min = () => world.wins.filter(w => w.state.minimized).map(w => w.__fw);
reply(ext, 'ShowDesktop', [true]);
const after = min();
reply(ext, 'ShowDesktop', [true]);          // the latch: no rescan
const latched = min();
reply(ext, 'ShowDesktop', [false]);
emit({after, latched, restored: min(), dock: win(5001).state.minimized});
""", {"rows": rows})
        # only the xterm is minimized by the sweep: the desktop window is
        # DESKTOP, the dock DOCK, the editor already minimized, the
        # calculator on workspace 1, 5002 says it cannot be minimized and
        # 5003 is an override-redirect surface
        self.assertEqual(got["after"], ["Window(4194306)", "Window(4194305)"])
        # a second `on` must not rescan: the scan skips already-minimized
        # windows, so a rescan here would store an EMPTY restore set...
        self.assertEqual(got["latched"], ["Window(4194306)", "Window(4194305)"])
        # ...and `off` would then restore nothing, forever. It restores the
        # xterm, and leaves the editor -- which this latch did not minimize.
        self.assertEqual(got["restored"], ["Window(4194306)"])
        self.assertFalse(got["dock"])

    def test_set_n_workspaces_refuses_what_it_cannot_do_and_does_the_rest(self):
        """1..36 is the range (wmctrl -n takes a count, and a shell asked for
        0 has nowhere to put its windows); dynamic workspaces are the shell's
        own business and saying so is better than fighting it; and a static
        session gets the count AND the gsettings key, so Mutter's own handler
        agrees with what just happened."""
        got = self.run_js(r"""
Gio.setSchemas('org.gnome.mutter', 'org.gnome.desktop.wm.preferences');
// The Settings double is read-only; SetNWorkspaces writes one key, and the
// write is inside safe(), so without this the call would silently pass.
Gio.Settings.prototype.set_int = function (k, v) {
    return H.record('settings.set_int', [k, v]);
};
Gio.settingsValues = {'org.gnome.mutter': {'dynamic-workspaces': true}};
const {ext, world} = bridgeOn(ARG.rows, {nWorkspaces: 3});
const dynamic = failure(ext, 'SetNWorkspaces', [5]);
Gio.settingsValues = {'org.gnome.mutter': {'dynamic-workspaces': false}};
const zero = failure(ext, 'SetNWorkspaces', [0]);
const many = failure(ext, 'SetNWorkspaces', [37]);
H.reset();
reply(ext, 'SetNWorkspaces', [5]);
const up = [world.spaces.length, H.names().filter(n => /append|remove/.test(n)).length];
const wroteUp = H.callsTo('settings.set_int').map(c => c.args);
H.reset();
reply(ext, 'SetNWorkspaces', [2]);
const down = [world.spaces.length, H.names().filter(n => /append|remove/.test(n)).length];
const wroteDown = H.callsTo('settings.set_int').map(c => c.args);
emit({dynamic, zero, many, up, down, wroteUp, wroteDown});
""", {"rows": self.ROWS})
        self.assertEqual(got["dynamic"][0], "org.w11.Bridge1.Unsupported")
        self.assertIn("dynamic-workspaces", got["dynamic"][1])
        for bad in ("zero", "many"):
            self.assertEqual(got[bad][0], "org.w11.Bridge1.InvalidArgs", bad)
            self.assertIn("must be 1..36", got[bad][1], bad)
        self.assertEqual(got["up"], [5, 2])       # 3 -> 5 is two appends
        self.assertEqual(got["down"], [2, 3])     # 5 -> 2 is three removes
        # both directions write the key, and write the count that was asked
        # for -- H.reset() between the two calls is what makes these two
        # separate lists rather than one that only shows the last write
        self.assertEqual(got["wroteUp"], [["num-workspaces", 5]])
        self.assertEqual(got["wroteDown"], [["num-workspaces", 2]])


#: Clutter events as the picker reads them, and the one stub override these
#: cases need: gi-Gio.mjs records bus_watch_name_on_connection but keeps no
#: callbacks, and "the caller pressed Ctrl-C" IS the vanished callback.
SELECT_JS = r"""
const T = Clutter.EventType;

function press(x, y) {
    return {type: () => T.BUTTON_PRESS, get_coords: () => [x, y]};
}

function release() {
    return {type: () => T.BUTTON_RELEASE};
}

function key(sym) {
    return {type: () => T.KEY_PRESS, get_key_symbol: () => sym};
}

let vanished = null;
Gio.bus_watch_name_on_connection = (conn, name, flags, appeared, gone) => {
    vanished = gone;
    return H.record('Gio.bus_watch_name_on_connection', [name], 4242);
};

function start(ext, ms = 0) {
    const inv = makeInvocation();
    ext.SelectWindowAsync([ms], inv);
    return inv;
}

function answer(inv) {
    return {reply: inv.reply ? inv.reply.value : null, error: inv.error};
}

function pending() {
    return {timers: GLib.timeouts.size,
            handlers: globalThis.stage ? globalThis.stage.handlers.size : -1};
}
"""


@support.skip_without_node
class SelectWindow(_Node):
    """T28: the window picker, executed -- and the grab released on every
    way out.

    A grab that outlives the call leaves a session that cannot click
    anything, which is far worse than a slow picker, so the extension routes
    every exit through one teardown. There are ten of them: the press, the
    release that follows it, Escape, the armed timeout, the caller's bus
    name vanishing, a second picker, the cooldown, a shell that is already
    modal, a grab that took nothing, and disable(). ShippedFilesTests used to
    grep thirteen strings out of this region; these run it.

    Each case is its own node process: the cooldown is module state in
    extension.js (`let selectCooldownUntil`), so two selections in one
    process are exactly one case -- the one that is about the cooldown.
    """

    ROWS = None

    @classmethod
    def setUpClass(cls):
        cls.ROWS = fixture_windows()

    def sel(self, body, arg=None):
        return self.run_js(SELECT_JS + body, arg if arg is not None else
                           {"rows": self.ROWS})

    def test_a_press_answers_the_window_under_it_and_lets_everything_go(self):
        """The press decides; the grab is held for the matching release so
        the application under the pointer does not get half a click. (150,
        100) is inside the xterm (100,80 640x480) and inside the DESKTOP
        window under it, which the hit-test looks through."""
        got = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
H.reset();
const inv = start(ext);
const armed = H.callsTo('GLib.timeout_add').map(c => c.args[1]);
world.stage.emit('captured-event', press(150, 100));
const afterPress = {picked: pending(),
                    armed: H.callsTo('GLib.timeout_add').map(c => c.args[1]),
                    answered: inv.reply !== undefined};
world.stage.emit('captured-event', release());
emit({armed, afterPress, ...answer(inv), left: pending(),
      names: H.names().filter(n => !n.startsWith('w') && !n.startsWith('ws.')),
      modes: H.callsTo('Main.pushModal').map(c => c.args)});
""")
        self.assertEqual(got["reply"], [XTERM])
        self.assertIsNone(got["error"])
        # armed before the grab was taken, at the cap
        self.assertEqual(got["armed"], [SELECT_MAX_MS])
        # the press replaces the deadline rather than adding one, and does
        # not answer yet
        self.assertEqual(got["afterPress"]["armed"], [SELECT_MAX_MS, SELECT_RELEASE_MS])
        self.assertEqual(got["afterPress"]["picked"]["timers"], 1)
        self.assertFalse(got["afterPress"]["answered"])
        # nothing is left holding anything
        self.assertEqual(got["left"], {"timers": 0, "handlers": 0})
        for once in ("Main.pushModal", "Main.popModal", "stage.disconnect",
                     "Gio.bus_unwatch_name"):
            self.assertEqual(got["names"].count(once), 1, (once, got["names"]))
        # the modal action mode is POPUP (Shell.ActionMode.POPUP == 128 in
        # the Shell-14 and Shell-18 typelibs), so no keybinding or hot corner
        # fires while the picker is up
        self.assertEqual(got["modes"], [[128]])

    def test_a_press_on_the_desktop_layer_only_answers_zero(self):
        """DESKTOP and DOCK are looked through, as the client-side hit-test
        does, and on Wayland there is no root window to name instead."""
        got = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
const inv = start(ext);
world.stage.emit('captured-event', press(1800, 1000));
world.stage.emit('captured-event', release());
emit(answer(inv));
""")
        self.assertEqual(got["reply"], [0])

    def test_escape_cancels_before_the_press_and_is_swallowed_after_it(self):
        """Once the click has happened the answer is settled, so Escape is
        then just another key the grab has to eat rather than pass to the
        application under it."""
        got = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
const inv = start(ext);
world.stage.emit('captured-event', key(Clutter.KEY_Escape));
emit({...answer(inv), left: pending()});
""")
        self.assertIsNone(got["reply"])
        self.assertEqual(got["error"][0], "org.w11.Bridge1.Cancelled")
        self.assertEqual(got["error"][1], "cancelled with Escape")
        self.assertEqual(got["left"], {"timers": 0, "handlers": 0})

        after = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
const inv = start(ext);
world.stage.emit('captured-event', press(150, 100));
world.stage.emit('captured-event', key(Clutter.KEY_Escape));
world.stage.emit('captured-event', release());
emit(answer(inv));
""")
        self.assertEqual(after["reply"], [XTERM])

    def test_the_cap_bounds_the_wait_however_long_the_caller_asked(self):
        """timeout_ms 0 means "wait for the user" and is still bounded by
        SELECT_MAX_MS; a caller asking for more gets the cap; a caller asking
        for less gets what it asked for. A pointer grab is the one thing here
        that can make a desktop feel broken, so it is never held forever."""
        for asked, ms in ((0, SELECT_MAX_MS), (60000, SELECT_MAX_MS),
                          (500, 500)):
            got = self.sel(r"""
const {ext} = bridgeOn(ARG.rows);
H.reset();
const inv = start(ext, ARG.asked);
GLib.runTimeouts();
emit({...answer(inv), left: pending(),
      armed: H.callsTo('GLib.timeout_add').map(c => c.args[1]),
      released: H.callsTo('Main.popModal').length});
""", {"rows": self.ROWS, "asked": asked})
            self.assertEqual(got["armed"], [ms], asked)
            self.assertEqual(got["error"][0], "org.w11.Bridge1.Cancelled", asked)
            self.assertEqual(got["error"][1], "no window picked within %d ms" % ms, asked)
            self.assertEqual(got["released"], 1, asked)
            self.assertEqual(got["left"], {"timers": 0, "handlers": 0}, asked)

    def test_the_caller_going_away_ends_the_selection(self):
        """Ctrl-C on `wdotool selectwindow`: the pending method call would
        otherwise never be answered and the grab never released, so the
        sender's unique bus name is watched for the whole selection."""
        got = self.sel(r"""
const {ext} = bridgeOn(ARG.rows);
H.reset();
const inv = start(ext);
const watched = H.callsTo('Gio.bus_watch_name_on_connection').map(c => c.args);
vanished();
emit({...answer(inv), watched, left: pending(),
      released: H.callsTo('Main.popModal').length,
      unwatched: H.callsTo('Gio.bus_unwatch_name').length});
""")
        self.assertEqual(got["watched"], [[":1.42"]])
        self.assertEqual(got["error"],
                         ["org.w11.Bridge1.Cancelled", "the caller went away"])
        self.assertEqual((got["released"], got["unwatched"]), (1, 1))
        self.assertEqual(got["left"], {"timers": 0, "handlers": 0})

    def test_a_second_picker_is_refused_before_anything_is_taken(self):
        """Two stage grabs coexist happily, but only the first captured-event
        handler sees each event, so the second picker would sit there
        grabbing -- and then eat the user's next click, for up to thirty
        seconds. It is refused before takeGrab, and the first one is
        untouched."""
        got = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
H.reset();
const first = start(ext);
const second = start(ext);
const duringSecond = {grabs: H.callsTo('Main.pushModal').length,
                      timers: GLib.timeouts.size};
world.stage.emit('captured-event', press(150, 100));
world.stage.emit('captured-event', release());
emit({second: answer(second), first: answer(first), duringSecond});
""")
        self.assertEqual(got["second"]["error"][0],
                         "org.w11.Bridge1.Unsupported")
        self.assertIn("already in progress", got["second"]["error"][1])
        self.assertEqual(got["duringSecond"], {"grabs": 1, "timers": 1})
        self.assertEqual(got["first"]["reply"], [XTERM])

    def test_a_selection_may_not_hold_the_grab_back_to_back(self):
        """The per-call cap bounds one call, not the caller: measured, a loop
        could re-grab every 5 us and hold the session's input for as long as
        it liked. Each selection is now followed by a quiet period as long as
        the grab it held, so a loop gets at most half the time -- and the
        refusal says how long is left, in seconds.

        The first selection is held for a measurable moment on purpose: a
        selection that begins and ends inside one millisecond earns a quiet
        period of zero, which is the honest answer for it."""
        got = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
H.reset();
const t0 = Date.now();
const first = start(ext);
while (Date.now() - t0 < 40)
    ;                                     // hold the grab 40 ms
world.stage.emit('captured-event', press(150, 100));
world.stage.emit('captured-event', release());
const held = Date.now() - t0;
const second = start(ext);
emit({first: answer(first), second: answer(second), held,
      grabs: H.callsTo('Main.pushModal').length});
""")
        self.assertEqual(got["first"]["reply"], [XTERM])
        self.assertEqual(got["second"]["error"][0],
                         "org.w11.Bridge1.Unsupported")
        self.assertIn("just held the input grab", got["second"]["error"][1])
        # The quiet period is the length of the grab, rounded UP to whole
        # seconds, so the number in the message is a function of how long
        # this node process was actually scheduled: 40 ms of spinning reads
        # "1 s" on an idle host, but a node preempted under load (measured
        # on this host: test_warandr_gui times out above load average 8)
        # would honestly say 2. The bound is what is invariant -- the
        # remaining quiet is never longer than the hold, and never zero or
        # the call would not have been refused.
        m = re.search(r"for another (\d+) s", got["second"]["error"][1])
        self.assertIsNotNone(m, got["second"]["error"][1])
        self.assertGreaterEqual(got["held"], 40)
        self.assertGreaterEqual(int(m.group(1)), 1)
        self.assertLessEqual(int(m.group(1)), math.ceil(got["held"] / 1000))
        self.assertEqual(got["grabs"], 1)

    def test_a_shell_that_is_already_modal_is_refused_not_grabbed_over(self):
        """With the overview up a click on a visible thumbnail hit-tests
        against the windows' real frame rects, which are not what is on
        screen: measured, that answered with a window nowhere near the
        pointer on GNOME 50 and with none at all on 46. Asking pushModal is
        not enough -- on some releases it nests happily on top of the
        overview -- so modalCount is read first, and nothing is taken."""
        got = self.sel(r"""
const out = {};
const {ext} = bridgeOn(ARG.rows);
for (const [name, set] of [['modalCount', () => Main.setModalCount(1)],
                           ['actionMode', () => Main.setActionMode(2)],
                           ['overview', () => {
                               Main.overview.visible = true;
                           }]]) {
    Main.setModalCount(0);
    Main.setActionMode(1);
    Main.overview.visible = false;
    set();
    H.reset();
    out[name] = {...answer(start(ext)),
                 grabs: H.callsTo('Main.pushModal').length,
                 timers: H.callsTo('GLib.timeout_add').length};
}
emit(out);
""")
        for axis in ("modalCount", "actionMode", "overview"):
            self.assertEqual(got[axis]["error"][0],
                             "org.w11.Bridge1.Unsupported", axis)
            self.assertIn("the shell is already modal", got[axis]["error"][1], axis)
            self.assertEqual(got[axis]["grabs"], 0, axis)
            self.assertEqual(got[axis]["timers"], 0, axis)

    def test_a_grab_that_took_nothing_is_dismissed_and_the_call_refused(self):
        """A Clutter.Grab whose seat state is NONE answers no events and
        still has to be dismissed, which is worse than no grab at all. The
        timeout is armed BEFORE the grab is asked for, so even this failure
        leaves nothing armed behind it."""
        got = self.sel(r"""
const {ext} = bridgeOn(ARG.rows);
Main.setModalGrab(Clutter.makeGrab(Clutter.GrabState.NONE));
H.reset();
const inv = start(ext);
emit({...answer(inv), left: pending(),
      names: H.names().filter(n => /pushModal|popModal|stage.connect|timeout/.test(n))});
""")
        self.assertEqual(got["error"][0], "org.w11.Bridge1.Unsupported")
        self.assertIn("would not grant an input grab", got["error"][1])
        self.assertEqual(got["names"],
                         ["GLib.timeout_add", "Main.pushModal", "Main.popModal"])
        self.assertEqual(got["left"], {"timers": 0, "handlers": 0})

    def test_disable_cancels_a_pending_selection_and_releases_the_grab(self):
        """An extension being disabled (a shell restart, `gnome-extensions
        disable`, an upgrade) must not leave the session grabbed, and the
        caller must not wait for a reply that is never coming."""
        got = self.sel(r"""
const {ext} = bridgeOn(ARG.rows);
H.reset();
const inv = start(ext);
ext.disable();
emit({...answer(inv), left: pending(),
      released: H.callsTo('Main.popModal').length,
      unexported: H.callsTo('dbus.unexport').length,
      unowned: H.callsTo('Gio.bus_unown_name').length});
""")
        self.assertEqual(got["error"],
                         ["org.w11.Bridge1.Cancelled",
                          "the bridge extension was disabled"])
        self.assertEqual(got["released"], 1)
        self.assertEqual((got["unexported"], got["unowned"]), (1, 1))
        self.assertEqual(got["left"], {"timers": 0, "handlers": 0})

    def test_the_picker_swallows_what_an_application_would_have_acted_on(self):
        """Motion, enter and leave are deliberately let through -- hover
        feedback while aiming changes nothing about the answer -- and every
        discrete event is stopped, so nothing aimed at the picker lands in
        the window under it. EVENT_STOP is `true` and EVENT_PROPAGATE
        `false` in Clutter."""
        got = self.sel(r"""
const {ext, world} = bridgeOn(ARG.rows);
const inv = start(ext);
const handler = [...world.stage.handlers.values()][0].cb;
const fire = t => handler(world.stage, {type: () => t,
                                        get_key_symbol: () => 0,
                                        get_coords: () => [150, 100]});
const out = {};
for (const name of ['MOTION', 'ENTER', 'LEAVE', 'KEY_RELEASE', 'SCROLL',
                    'TOUCH_UPDATE', 'TOUCH_CANCEL', 'PAD_BUTTON_PRESS',
                    'PAD_BUTTON_RELEASE'])
    out[name] = fire(T[name]);
emit({out, answered: inv.reply !== undefined});
""")
        self.assertEqual([k for k, v in got["out"].items() if v is False],
                         ["MOTION", "ENTER", "LEAVE"])
        self.assertEqual([k for k, v in got["out"].items() if v is True],
                         ["KEY_RELEASE", "SCROLL", "TOUCH_UPDATE", "TOUCH_CANCEL",
                          "PAD_BUTTON_PRESS", "PAD_BUTTON_RELEASE"])
        self.assertFalse(got["answered"])


@support.skip_without_node
class ReloadTheRunningShellReads(_Node):
    """Route 3: gnome-shell re-reads extension.js with nobody logged out.

    GNOME's own answer is gone -- `org.gnome.Shell.Extensions.ReloadExtension
    w11-bridge@w11` answers `GDBus.Error:org.freedesktop.DBus.Error
    .NotSupported: ReloadExtension is deprecated and does not work`, measured
    on noble-gnome-x11 (GNOME Shell 46.0, gjs 1.80.2, 2026-09-11) -- and the
    route below it costs the session (`gnome-shell --replace` walks
    org.gnome.Shell@x11.service into its start limit, whose OnFailure disables
    user extensions for that user in every later session). So the extension
    reloads itself: the object the shell constructs is the ROOT, which keeps
    org.w11.BridgeReload1 and a pointer to the copy that runs, and a reload
    imports extension.js under a path nothing has imported yet -- the one
    cache-busting that works on both gjs releases, see the route-3 block in
    extension.js for what a `?v=<mtime>` query does on 1.80.2 -- and hands the
    session to a fresh instance of what the file says now.

    The heap cost is real and one-way (gjs unloads no module), so what these
    cases are mostly about is the thing that would turn one copy per reload
    into a session-wide fault: a listener the old copy left connected. Every
    reload goes through the same disable() the shell calls, and
    test_a_reload_leaves_no_listener... is the assertion that it is enough.

    The copy is written into $XDG_RUNTIME_DIR and imported from there, so
    these cases -- and only these -- give the GLib double a real directory and
    real file calls: what node imports has to be a file that exists. PRE's
    `file_set_contents` and `unlink` are per-file overrides of the shared
    doubles in tests/fixtures/gjs/stubs/gi-GLib.mjs (which keep their files in
    a Map and have nowhere to write), asked for there in
    goal2/requests-batch-11.md; the next file that needs a GLib writing to a
    real directory should take them from the stub rather than copy these. Under
    node the extension file itself has no mtime unless a case gives it one
    (`Gio.setFileMtime`), so SELF_MTIME is 0 here and a case that registers
    one takes the reload path from the first enable; in a session the shell
    has just read the file, so that first enable is synchronous, which is what
    the first case below pins.
    """

    #: Prepended to every case in this class, after WORLD.
    PRE = r"""
import fs from 'node:fs';
import os from 'node:os';
import nodePath from 'node:path';

const SELF = '@MODULE@/extension.js';
const RUNTIME = fs.mkdtempSync(nodePath.join(os.tmpdir(), 'w11-reload-'));
process.on('exit', () => fs.rmSync(RUNTIME, {recursive: true, force: true}));
GLib.userRuntimeDir = RUNTIME;
GLib.setFile(SELF, fs.readFileSync(SELF));
GLib.file_set_contents = (p, bytes) => {
    fs.writeFileSync(p, Buffer.from(bytes));
    return true;
};
GLib.unlink = p => {
    fs.unlinkSync(p);
    return 0;
};

/** What the reload left behind in the runtime dir: nothing, when it works. */
const copies = () => fs.readdirSync(RUNTIME);

/** org.w11.BridgeReload1.Reload, the way gdbus would call it. */
function reload(root) {
    const inv = makeInvocation();
    root.ReloadAsync([], inv);
    return inv;
}

/** Every D-Bus object this run ever exported, oldest first. */
function exportedObjects() {
    const all = [];
    for (let i = 0; ; i++) {
        const e = Gio.exported(i);
        if (!e)
            break;
        all.push(e);
    }
    return all;
}
"""

    @classmethod
    def setUpClass(cls):
        cls.rows = fixture_windows()

    def run_js(self, body, arg=None):
        return support.js_harness(self.MODULE, WORLD + self.PRE + body, arg)

    def test_the_shell_reads_the_file_once_and_the_copy_it_read_is_the_one_running(self):
        """The login path: no import, no second copy of the module, nothing
        written into the runtime dir, and the bus surface split the way the
        route-3 block describes -- the root holds org.w11.BridgeReload1 and no
        method table at all, the copy it made holds org.w11.Bridge1 and the
        well-known name."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
// Read BEFORE any await: a session must have its bridge up when enable()
// returns, not one main-loop iteration later.
const ext = root._live;
emit({
    up: !!ext,
    sameModule: ext.constructor === Bridge,
    rootHasMethods: typeof root.ListWindowsAsync === 'function',
    liveHasMethods: typeof ext.ListWindowsAsync === 'function',
    rootXml: Gio.exported(0).xml.includes('org.w11.BridgeReload1'),
    liveXml: Gio.exported(1).xml.includes('org.w11.Bridge1'),
    reloadXmlIsNotBridge1: Gio.exported(0).xml.includes('org.w11.Bridge1'),
    paths: H.callsTo('dbus.export').map(c => c.args[0]),
    names: Gio.names().map(n => n.name),
    copies: copies(),
    ids: jsonReply(ext, 'ListWindows').map(d => d.id),
});
""", {"rows": self.rows})
        self.assertTrue(got["up"])
        self.assertTrue(got["sameModule"])
        self.assertFalse(got["rootHasMethods"])
        self.assertTrue(got["liveHasMethods"])
        self.assertTrue(got["rootXml"])
        self.assertTrue(got["liveXml"])
        self.assertFalse(got["reloadXmlIsNotBridge1"])
        self.assertEqual(got["paths"], ["/org/w11/Bridge", "/org/w11/Bridge"])
        self.assertEqual(got["names"], ["org.w11.Bridge"])
        self.assertEqual(got["copies"], [])
        self.assertEqual(got["ids"], [DESKTOP, EDITOR, CALC, XTERM])

    def test_a_changed_file_is_read_again_and_its_code_is_what_answers(self):
        """The reload itself. A second evaluation of the module is a second
        class object, which is what `differentClass` is: the instance now
        serving org.w11.Bridge1 was built from a class this case never
        imported, out of a file node read off the disk again. The reply says
        which file, which mtime and that it was re-read, so a caller can tell
        a reload from a no-op; and the copy it imported is gone afterwards."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
const first = root._live;
Gio.setFileMtime(SELF, 2000);
const inv = reload(root);
await root._pending;
emit({error: inv.error, answer: JSON.parse(inv.reply.value[0]),
      differentClass: root._live.constructor !== first.constructor,
      neitherIsThisModule: root._live.constructor !== Bridge &&
                           first.constructor !== Bridge,
      unexported: H.callsTo('dbus.unexport').length,
      copies: copies(),
      version: reply(root._live, 'GetVersion')[0],
      ids: jsonReply(root._live, 'ListWindows').map(d => d.id)});
""", {"rows": self.rows})
        self.assertIsNone(got["error"])
        self.assertEqual(got["answer"]["mtime"], 2000)
        self.assertTrue(got["answer"]["reread"])
        self.assertEqual(got["answer"]["version"], 3)
        self.assertTrue(got["answer"]["path"].endswith(
            "gnome/w11-bridge@w11/extension.js"))
        self.assertTrue(got["differentClass"])
        self.assertTrue(got["neitherIsThisModule"])
        self.assertEqual(got["unexported"], 1)
        self.assertEqual(got["copies"], [])
        self.assertEqual(got["version"], 3)
        self.assertEqual(got["ids"], [DESKTOP, EDITOR, CALC, XTERM])

    def test_a_file_that_has_not_changed_is_not_read_again(self):
        """A Reload that follows no edit -- neither timestamp nor bytes --
        costs no second copy of the module in a heap that never gives one
        back: the class is the same object, and only the instance is new."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
const first = root._live;
const inv = reload(root);
await root._pending;
emit({answer: JSON.parse(inv.reply.value[0]),
      sameClass: root._live.constructor === first.constructor,
      newInstance: root._live !== first,
      stillUp: reply(root._live, 'GetVersion')[0]});
""", {"rows": self.rows})
        self.assertFalse(got["answer"]["reread"])
        self.assertEqual(got["answer"]["mtime"], 1000)
        self.assertTrue(got["sameClass"])
        self.assertTrue(got["newInstance"])
        self.assertEqual(got["stillUp"], 3)

    def test_a_reload_leaves_no_listener_no_timeout_and_no_export_behind(self):
        """The one that decides whether route 3 may ship at all.

        The old module's closures are in the heap for good, so a handler the
        old copy left connected fires for the rest of the session: after
        three reloads a window-created would emit four WindowEvents, the
        tools would see every event four times, and the session would get
        slower with every reload. Counted here on the doubles the extension
        connected to (`makeEmitter` keeps its handler map), and counted the
        same way on the live shell: one WindowEvent per window event after
        three reloads on noble-gnome-x11, not two and not four."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
const tracked = world.wins[1];
const before = {display: world.display.handlers.size, wm: world.wm.handlers.size,
                win: tracked.handlers.size};
for (let i = 2; i <= 4; i++) {
    Gio.setFileMtime(SELF, 1000 * i);
    const inv = reload(root);
    await root._pending;
    if (inv.error)
        throw new Error(`reload ${i}: ${inv.error[0]}: ${inv.error[1]}`);
}
// A leaked copy would still be connected and would emit on its own (dead)
// object as well as on the live one.
const objects = exportedObjects();
const marks = objects.map(o => o.signals.length);
const fresh = Meta.makeWindow(ARG.fresh, {shape: '46'});
world.display.emit('window-created', fresh);
const events = objects.map((o, i) => o.signals.length - marks[i]);
emit({before, after: {display: world.display.handlers.size,
                      wm: world.wm.handlers.size, win: tracked.handlers.size},
      timeouts: GLib.timeouts.size,
      objects: objects.length,
      stillExported: objects.filter(o => o.exported).length,
      unexported: H.callsTo('dbus.unexport').length,
      unowned: H.callsTo('Gio.bus_unown_name').length,
      copies: copies(),
      events});
""", {"rows": self.rows, "fresh": _row(self.rows[1], id=DIALOG, title="fresh")})
        # what one live copy connects: window-created and notify::focus-window
        # on the display, three on the workspace manager, ten per window
        self.assertEqual(got["before"], {"display": 2, "wm": 3, "win": 10})
        self.assertEqual(got["after"], got["before"])
        self.assertEqual(got["timeouts"], 0)
        # the root's reload object, plus one org.w11.Bridge1 object per copy
        self.assertEqual(got["objects"], 1 + 4)
        self.assertEqual(got["stillExported"], 2)
        self.assertEqual(got["unexported"], 3)
        self.assertEqual(got["unowned"], 3)
        self.assertEqual(got["copies"], [])
        self.assertEqual(got["events"], [0, 0, 0, 0, 1])

    def test_an_enable_that_cannot_read_the_file_runs_what_the_shell_loaded(self):
        """The upgrade case, when the upgrade is half done: extension.js has
        a newer mtime than the copy the shell read, and reading it fails (it
        is being replaced under us, the runtime dir is full, the file is
        gone). A bridge running the code already in the heap is worth far
        more than no bridge, so enable() falls back to it and says so in the
        journal instead of leaving the session without one."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);   // the disk is ahead of the copy the shell read
GLib.setFile(SELF, null);       // ...and cannot be read at all
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
emit({up: root._live !== null,
      sameModule: root._live && root._live.constructor === Bridge,
      names: Gio.names().map(n => n.name),
      copies: copies(),
      ids: jsonReply(root._live, 'ListWindows').map(d => d.id)});
""", {"rows": self.rows})
        self.assertTrue(got["up"])
        self.assertTrue(got["sameModule"])
        self.assertEqual(got["names"], ["org.w11.Bridge"])
        self.assertEqual(got["copies"], [])
        self.assertEqual(got["ids"], [DESKTOP, EDITOR, CALC, XTERM])
        self.assertTrue([ln for ln in got["logs"]
                         if "could not be read again" in ln], got["logs"])

    def test_a_copy_that_will_not_enable_leaves_the_one_that_did_running(self):
        """An edit that evaluates and then throws on enable() must not take
        the session's bridge down with it: the swap puts the previous class
        back, the tools keep answering, and the error names both mtimes.
        Driven through _swap() because the module this case can import is the
        shipped one, which enables."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
const good = root._live.constructor;
class Broken {
    enable() {
        throw new Error('window-created is not a signal');
    }
}
let threw = null;
try {
    root._swap(Broken, 9999, 'sha256-of-the-copy-that-threw', root._gen);
} catch (e) {
    threw = `${e}`;
}
emit({threw, backToGood: root._live.constructor === good,
      version: reply(root._live, 'GetVersion')[0],
      ids: jsonReply(root._live, 'ListWindows').map(d => d.id)});
""", {"rows": self.rows})
        self.assertIn("9999", got["threw"])
        self.assertIn("1000", got["threw"])
        self.assertIn("window-created is not a signal", got["threw"])
        self.assertTrue(got["backToGood"])
        self.assertEqual(got["version"], 3)
        self.assertEqual(got["ids"], [DESKTOP, EDITOR, CALC, XTERM])

    def test_disabling_the_extension_beats_a_reload_that_is_still_importing(self):
        """`gnome-extensions disable` while an import is in flight: the copy
        that arrives afterwards must not put the bridge back on the bus in a
        session that turned it off. The generation counter is what says so --
        disable() bumps it, and a swap whose generation is stale puts nothing
        back on the bus and answers its caller with an error instead of with
        a version read off an object that is no longer exported."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
Gio.setFileMtime(SELF, 2000);
const inv = reload(root);
root.disable();
const during = {live: root._live === null,
                unexported: H.callsTo('dbus.unexport').length};
await root._pending;
emit({during, error: inv.error,
      after: {live: root._live === null,
              exports: H.callsTo('dbus.export').length,
              unexported: H.callsTo('dbus.unexport').length,
              unowned: H.callsTo('Gio.bus_unown_name').length,
              copies: copies()}});
""", {"rows": self.rows})
        # disable() took the copy and the reload interface off the bus...
        self.assertTrue(got["during"]["live"])
        self.assertEqual(got["during"]["unexported"], 2)
        # ...and the import that landed afterwards changed nothing, and said
        # so to the caller rather than answering with a version read off an
        # object that is no longer on the bus
        self.assertIsNotNone(got["error"])
        self.assertIn("overtaken", got["error"][1])
        self.assertTrue(got["after"]["live"])
        self.assertEqual(got["after"]["exports"], 2)
        self.assertEqual(got["after"]["unexported"], 2)
        self.assertEqual(got["after"]["unowned"], 1)
        self.assertEqual(got["after"]["copies"], [])

    def test_a_copy_that_throws_part_way_through_enable_is_disabled_not_abandoned(self):
        """The dangerous half of the fallback above, and the reason it is not
        just `new this._klass()`.

        _enableHere() exports /org/w11/Bridge, owns org.w11.Bridge and
        connects the display and workspace handlers BEFORE its last act --
        tracking every window on the display -- can throw, so an edit that
        breaks anything in that tail leaves an instance holding all of it.
        Abandoned, its handlers fire for the life of the session (two
        WindowEvents per event, then three), its object stays exported, and
        under real GDBus rather than these doubles the fallback's own
        `export(Gio.DBus.session, OBJECT_PATH)` then throws because an
        interface is already exported at that path -- so the session would
        end with NO bridge, which is the opposite of what the fallback is
        for. The swap disables the copy that threw first."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
const good = root._live.constructor;
const before = {display: world.display.handlers.size, wm: world.wm.handlers.size};
// An edit that evaluates, exports, owns the name and connects -- and throws
// where _enableHere() ends: `for (const w of this._allWindows()) this._track(w)`.
class Broken extends good {
    _track(w) {
        throw new Error('window-created is not a signal');
    }
}
let threw = null;
try {
    root._swap(Broken, 9999, 'sha256-of-the-copy-that-threw', root._gen);
} catch (e) {
    threw = `${e}`;
}
const objects = exportedObjects();
emit({threw, before,
      after: {display: world.display.handlers.size, wm: world.wm.handlers.size},
      objects: objects.length,
      stillExported: objects.filter(o => o.exported).length,
      owns: Gio.names().length,
      unowned: H.callsTo('Gio.bus_unown_name').length,
      backToGood: root._live.constructor === good,
      version: reply(root._live, 'GetVersion')[0],
      ids: jsonReply(root._live, 'ListWindows').map(d => d.id)});
""", {"rows": self.rows})
        self.assertIn("window-created is not a signal", got["threw"])
        self.assertIn("9999", got["threw"])
        # nothing the copy that threw connected is still connected
        self.assertEqual(got["before"], {"display": 2, "wm": 3})
        self.assertEqual(got["after"], got["before"])
        # the root's reload object, the copy the login made, the copy that
        # threw and the fallback -- two of the four still exported, and the
        # two that are not are the login's copy and the one that threw
        self.assertEqual(got["objects"], 4)
        self.assertEqual(got["stillExported"], 2)
        # it owned the name on its way up, so it has to have unowned it: one
        # unown for the copy the swap replaced, one for the copy that threw
        self.assertEqual(got["owns"], 3)
        self.assertEqual(got["unowned"], 2)
        self.assertTrue(got["backToGood"])
        self.assertEqual(got["version"], 3)
        self.assertEqual(got["ids"], [DESKTOP, EDITOR, CALC, XTERM])

    def test_two_saves_that_share_an_mtime_are_still_two_reloads(self):
        """The clock is not the reload key on its own.

        Gio reports `time::modified` in whole seconds, and even the
        microseconds under it come from the kernel's coarse file-timestamp
        clock: six GLib.file_set_contents in a tight loop on this build host
        (gjs 1.88.0, glib 2.88, overlayfs) produced five distinct
        `time::modified[-usec]` stamps for six different contents. So an
        editor's save-save, or install-bridge.sh's cp landing in the second a
        Reload read the file, is one timestamp and two sets of bytes -- and
        keying on the timestamp alone would answer `"reread":false` and go on
        running the bytes from the first save with nothing said. The SHA256
        of what was read is the other half of the key: same second, same
        mtime in the reply, and the code that answers is the second save's."""
        got = self.run_js(r"""
const world = makeWorld(ARG.rows);
Gio.setFileMtime(SELF, 1000);
const src = new TextDecoder().decode(GLib.file_get_contents(SELF)[1]);
const root = new Bridge({uuid: 'w11-bridge@w11'});
root.enable();
await root._pending;
const first = root._live;
// The second save, in the same second: the file the shell will read again
// differs from the one it read, and its mtime does not.
GLib.setFile(SELF, src.replace(/const VERSION = \d+;/, 'const VERSION = 7;'));
const inv = reload(root);
await root._pending;
emit({error: inv.error, answer: JSON.parse(inv.reply.value[0]),
      differentClass: root._live.constructor !== first.constructor,
      version: reply(root._live, 'GetVersion')[0],
      copies: copies()});
""", {"rows": self.rows})
        self.assertIsNone(got["error"])
        self.assertEqual(got["answer"]["mtime"], 1000)
        self.assertTrue(got["answer"]["reread"])
        self.assertEqual(got["answer"]["version"], 7)
        self.assertTrue(got["differentClass"])
        self.assertEqual(got["version"], 7)
        self.assertEqual(got["copies"], [])


if __name__ == "__main__":
    unittest.main()
