#!/usr/bin/env python3
"""XTEST through the proxy: the captured frames, and the daemon calls they mean.

Every `FakeInput` in this file is a frame a real xdotool put on the wire --
`tests/fixtures/xw11/fakeinput-garbage.hex` and its siblings are the bytes of
recon/tools/caps/raw.jsonl and recon/wire/trace2.log, not a packing helper's
idea of them -- and the other end is `tests/support.py:FakeDaemon`, which is the
daemon's real line protocol on a real socket. So what is asserted is the
translation and nothing else: 36 bytes in, one JSON object out.

What the file is defending, in one sentence per group:

* the pads are Xlib's leftover output buffer and NONE of them is read: the
  three `key ctrl+a` presses and the release that carries `rootX=256` all mean
  the same thing;
* xdotool's seven FakeInputs for one `ctrl+a` are four daemon operations,
  because X drops a repeated press of a non-repeating key and the compositor
  refcounts them per seat;
* a keycode is resolved through the map the client just rewrote, so the spare
  keycode 8 that xdotool plants `€` into fires `€` and not evdev code 0;
* a keysym the compositor's layout cannot reach is TYPED instead, once, and its
  release is consumed;
* X button numbers reach the daemon as X button numbers, which is what its
  `button` op takes;
* a delay queues behind what the same client already sent, and no sleep runs in
  the loop;
* with no route to the daemon at all the frame goes to Xwayland untouched, and
  the log line carries the reason and the route.
"""

import os
import struct
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

import support                                                     # noqa: E402
from support import (FakeBackend, FakeDaemon, fake_view,           # noqa: E402
                     fake_window, stop_daemons_under)
from w11common.errors import CmdError                              # noqa: E402
from wdotool import daemon as daemon_mod                           # noqa: E402
from xw11 import policy, wire, xtest                               # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "xw11")

#: The keysym xdotool plants into the spare keycode for `€`: the Unicode
#: keysym space, `0x01000000 | 0x20ac` [recon/seams.md 5.3's xtrace].
EURO = 0x010020AC

#: What the `us` table on the fake server binds, so a test can name a key by
#: its keycode the way a FakeInput does. Level 1 only: xdotool sends the
#: modifiers as their own keycodes [recon/tools.md 4.5].
US_KEYSYMS = {
    36: 0xFF0D,      # Return
    37: 0xFFE3,      # Control_L
    38: 0x0061,      # a
    39: 0x0073,      # s
    31: 0x0069,      # i
    43: 0x0068,      # h
    50: 0xFFE1,      # Shift_L
}


def frames_of(name):
    """Every hex line of a fixture, as bytes."""
    out = []
    with open(os.path.join(FIXTURES, name + ".hex"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(bytes.fromhex(line.split("#")[0].strip()))
    return out


class _Upstream(support.FakeUpstream):
    """`FakeUpstream` that knows the four requests this file sends past it.

    `FakeXServer` answers `BadRequest` for an opcode nobody wrote a branch for,
    and a real X server answers XTEST's `GetVersion` with 2.2 [recon/wire.md
    5.3], answers `QueryPointer` with a reply and answers `WarpPointer`,
    `FakeInput`, `GrabControl` and `ChangeKeyboardMapping` with nothing at all.
    A per-file subclass, because `tests/support.py`'s own doubles belong to the
    batch that owns them (scratchpad requests-batch-6.md).
    """

    #: What `QueryPointer` answers: (root, child, root_x, root_y, win_x,
    #: win_y, mask). The default is what real xdotool reads off Xwayland on the
    #: headless rig, at the middle of a 1280x720 screen [recon/env.md 2.2].
    POINTER = (0, 640, 360, 640, 360, 0)

    def __init__(self, *a, **kw):
        self.ops = []
        self.frames = []
        self.pointer = self.POINTER
        super().__init__(*a, **kw)

    def intern(self, name: str) -> int:
        fixed = policy.PREDEFINED_ATOMS.get(name)
        if fixed and name not in self._atoms:
            self._atoms[name] = fixed
            self._names[fixed] = name
        return super().intern(name)

    #: Void: a real server answers nothing at all for these [recon/wire.md 4.2,
    #: 5.2].
    VOID = (4, 8, 10, 12, 25, 41, 42, 100, 113)

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        self.ops.append(opcode)
        xtest_major = self.extensions.get("XTEST", (0, 0, 0))[0]
        if opcode == xtest_major:
            self.frames.append((dbyte, bytes(payload)))
            if dbyte == xtest.GET_VERSION:
                # major_version in byte 1, minor_version at 8 [wire 5.2].
                conn.sendall(struct.pack("<BBHIH22x", 1, 2, seq, 0, 2))
            return
        if opcode == wire.OP_QUERY_POINTER:
            (win,) = struct.unpack_from("<I", payload, 0)
            child, rx, ry, wx, wy, mask = self.pointer
            conn.sendall(struct.pack("<BBHIIIhhhhH6x", 1, 1, seq, 0,
                                     self.ROOTS[0], child, rx, ry, wx, wy,
                                     mask) if win else b"")
            return
        if opcode in self.VOID:
            return
        return super()._dispatch(conn, opcode, dbyte, payload, seq)


class _Rig(support.ProxyRig):
    def __init__(self, **kw):
        real = support.FakeUpstream
        support.FakeUpstream = _Upstream
        try:
            super().__init__(**kw)
        finally:
            support.FakeUpstream = real


class _Log:
    """The proxy's log as a list of lines."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def carrying(self, needle):
        return [line for line in self.lines if needle in line]


def foot_backend(pointer=None):
    win = fake_window(11, title="WXL-Foot", class_="foot", instance="foot",
                      pid=4242, x=10, y=20, w=300, h=200, visible=True,
                      focused=True, desktop=0)
    return FakeBackend(windows=[win], pointer=pointer,
                       views=[fake_view(win, xid=0, app_id="foot",
                                        instance="foot", cls="foot")])


class XtestCase(unittest.TestCase):
    """One proxy, one client, one fake daemon on a socket of its own."""

    num = 900
    upstream_num = 901
    #: the fake daemon binds nothing, so `connect_or_spawn` fails
    refuse = False

    def setUp(self):
        self.tmp = support.tempfile.mkdtemp(prefix="xw11-xtest-")
        # Registered before anything is spawned and before the directory goes:
        # cleanups run last-in-first-out, so a test that dies half way through
        # its own setup still takes any daemon with it.
        self.addCleanup(support.shutil.rmtree, self.tmp, True)
        self.addCleanup(stop_daemons_under, self.tmp)
        self.env = support.env(XDG_RUNTIME_DIR=self.tmp)
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        # Nothing in this suite may fork a real daemon: the fake IS the daemon,
        # and a `_spawn` that ran would double-fork a process no cleanup here
        # knows about.
        self.spawned = []
        real_spawn = daemon_mod.DaemonClient._spawn

        def no_spawn():
            self.spawned.append(time.monotonic())
        daemon_mod.DaemonClient._spawn = staticmethod(no_spawn)
        self.addCleanup(setattr, daemon_mod.DaemonClient, "_spawn",
                        staticmethod(real_spawn.__func__)
                        if hasattr(real_spawn, "__func__") else real_spawn)
        self.daemon = FakeDaemon(self.tmp, refuse=self.refuse)
        self.addCleanup(self.daemon.stop)
        self.backend = self.make_backend()
        self.rig = _Rig(num=self.num, upstream_num=self.upstream_num,
                        passthrough=False, backend=self.backend)
        self.addCleanup(self.rig.stop)
        self.log = _Log()
        self.rig.server._log = self.log
        self.conn = self.rig.conn()
        self.own = self.rig.wait_own()
        self.shadows = self.rig.server.shadows
        self.shadows.refresh()
        self.root = self.conn.root()
        native = [e for e in self.shadows.snapshot() if e.shadow]
        self.entry = native[0] if native else None
        self.engine = self.rig.server.xtest
        self.seed_keymap()
        del self.rig.upstream.ops[:]
        del self.rig.upstream.frames[:]
        del self.daemon.ops[:]

    def make_backend(self):
        return foot_backend()

    def seed_keymap(self):
        """The `us` rows the tests name, in the engine's table. The proxy reads
        its own table from `GetKeyboardMapping(8, 248)` at open and the fake
        answers `0x100 + keycode` for a row nobody set [tests/support.py], so
        the rows a test names are put in by hand rather than by making the fake
        pretend to be a whole keymap."""
        self.engine.load_keymap()
        for code, keysym in US_KEYSYMS.items():
            self.engine.keymap.rows[code] = (keysym,)

    # -- the wire -------------------------------------------------------------

    def xtest_major(self):
        return self.rig.upstream.extensions["XTEST"][0]

    def send_fake_input(self, frame):
        """One captured `FakeInput` frame, byte for byte, and a sync behind
        it."""
        return self.send(frame[0] if frame[0] >= 128 else self.xtest_major(),
                         frame[1], frame[4:], major=self.xtest_major())

    def send(self, op, byte1=0, body=b"", major=None):
        self.conn._send(major if major is not None else op, byte1, body)
        return self.sync()

    def sync(self):
        return self.conn._wait_reply(self.conn._send(wire.OP_GET_INPUT_FOCUS, 0))

    def ops(self, name):
        return self.daemon.of(name)

    def shadow(self):
        return self.entry.shadow


class PadsIgnored(XtestCase):
    """The garbage in a FakeInput's pads reaches nothing."""

    num, upstream_num = 902, 903

    def test_the_key_a_capture_decodes_to_seven_fields_and_no_more(self):
        """recon/wire.md 5.2's frame, field by field: type 2, detail 0x26,
        rootX 0xf708 and rootY 0x05f5, which are Xlib's leftover buffer."""
        press = frames_of("fakeinput-garbage")[0]
        got = xtest.decode_fake_input(press)
        self.assertEqual(got, (2, 0x26, 0, 0, -2296, 1525, 0))
        self.assertEqual(len(press), 36)

    def test_the_key_a_press_sends_one_key_down_and_reads_no_pad(self):
        press, release = frames_of("fakeinput-garbage")[:2]
        self.send_fake_input(press)
        self.assertEqual([(r["op"], r["spec"], r["direction"])
                          for r in self.ops("key")],
                         [("key", "a", "down")])
        self.send_fake_input(release)
        self.assertEqual([r["direction"] for r in self.ops("key")],
                         ["down", "up"])
        self.assertEqual(self.ops("mousemove_abs"), [],
                         "rootX/rootY on a key event are garbage and must not "
                         "move the pointer")

    def test_two_different_rootxy_pairs_produce_operations_with_no_coordinates(self):
        """The seven frames of `key ctrl+a` carry two different rootX/rootY
        pairs -- (3, 2) on six of them and (256, 0) on the release of keycode
        38 [M recon/tools/caps/raw.jsonl MARK 1]. Both pairs are Xlib's
        leftover output buffer, so neither may reach the daemon: the operations
        carry a spec and a direction and nothing else, and no motion is
        injected by a key event."""
        seven = frames_of("fakeinput-garbage")[2:]
        pairs = [struct.unpack_from("<hh", f, 24) for f in seven]
        self.assertEqual(set(pairs), {(3, 2), (256, 0)}, "the capture's pads")
        self.assertEqual(pairs[-1], (256, 0), "on the release of keycode 38")
        for frame in seven:
            self.send_fake_input(frame)
        ops = self.ops("key")
        self.assertEqual((ops[-1]["spec"], ops[-1]["direction"]), ("a", "up"),
                         "the frame with the odd pads is the release of 38")
        for op in ops:
            self.assertEqual(set(op) & {"x", "y", "root_x", "root_y"}, set(),
                             "a key operation carries no coordinates: %r" % op)
        self.assertEqual(self.ops("mousemove_abs"), [])
        self.assertEqual(self.ops("mousemove_rel"), [])

    def test_the_motion_capture_is_the_one_frame_whose_rootxy_is_real(self):
        """`mousemove 150 250` on the pinned 4.x [recon/wire.md 5.3]."""
        motion = frames_of("fakeinput-motion-4x")[0]
        self.assertEqual(xtest.decode_fake_input(motion),
                         (6, 0, 0, 0x22B, 150, 250, 0))
        self.send_fake_input(motion)
        self.assertEqual([(r["x"], r["y"]) for r in self.ops("mousemove_abs")],
                         [(150, 250)])

    def test_the_negative_twin_a_decoder_that_validates_a_pad_rejects_it(self):
        """The rule this file exists for, stated as its opposite: the strict
        decoder below -- `decode_fake_input` plus the one check nobody may
        write, that the pad at 16..23 is zero -- rejects every captured frame,
        while the real one reads all seven fields out of each."""
        def strict(buf):
            if len(buf) != 36 or buf[16:24] != b"\0" * 8:
                return None
            return xtest.decode_fake_input(buf)

        frames = frames_of("fakeinput-garbage")
        self.assertTrue(frames)
        for frame in frames:
            self.assertIsNone(strict(frame),
                              "a validating decoder would have dropped a real "
                              "xdotool keystroke")
            got = xtest.decode_fake_input(frame)
            self.assertEqual(len(got), 7, "and the real one reads it")
        # The twin really can say yes: the same decoder on a zero-padded frame.
        zeroed = frames[0][:16] + b"\0" * 8 + frames[0][24:]
        self.assertEqual(strict(zeroed), xtest.decode_fake_input(zeroed))

    def test_a_frame_shorter_than_36_bytes_decodes_to_nothing(self):
        self.assertIsNone(xtest.decode_fake_input(b"\0" * 35))


class TripleControl(XtestCase):
    """`xdotool key ctrl+a`'s seven FakeInputs are four operations."""

    num, upstream_num = 904, 905

    def test_seven_fake_inputs_become_four_daemon_operations(self):
        """`KP 37, KP 37, KP 37, KP 38, KR 37, KR 37, KR 38`
        [M recon/tools/caps/raw.jsonl MARK 1] -> `Control_L down, a down,
        Control_L up, a up`. The daemon dedupes nothing: `key Control_L down`
        sent twice writes `EV_KEY 29 value 1` twice to the uinput node
        [M 2026-09-10, a spawned daemon under WDOTOOL_FAKE_UINPUT=1], so the
        held set has to live in the proxy."""
        for frame in frames_of("fakeinput-garbage")[2:]:
            self.send_fake_input(frame)
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("Control_L", "down"), ("a", "down"),
                          ("Control_L", "up"), ("a", "up")])

    def test_a_release_of_a_key_nobody_holds_costs_no_round_trip(self):
        seven = frames_of("fakeinput-garbage")[2:]
        self.send_fake_input(seven[4])          # KR 37, with nothing held
        self.assertEqual(self.ops("key"), [])

    def test_every_one_of_the_seven_still_costs_upstream_one_request(self):
        """A CONSUMEd request costs one sequence number upstream for ever
        [recon/wire.md 3.2a]: seven FakeInputs are seven `NoOperation`s."""
        for frame in frames_of("fakeinput-garbage")[2:]:
            self.send_fake_input(frame)
        noops = [op for op in self.rig.upstream.ops
                 if op == wire.OP_NO_OPERATION]
        self.assertEqual(len(noops), 7)
        self.assertEqual(self.rig.upstream.frames, [],
                         "not one FakeInput reached the server")


class Keycode8Remap(XtestCase):
    """The spare keycode xdotool steals for a character outside the layout."""

    num, upstream_num = 906, 907

    def change_map(self, keycode, keysym):
        return self.send(100, 1, struct.pack("<BB2xI", keycode, 1, keysym))

    def test_a_press_after_the_remap_sends_the_planted_keysym(self):
        """`ChangeKeyboardMapping(first=8, n=1, per=1, [0x010020ac])` and then
        `FakeInput KeyPress 8`, which is the pair recon/tools.md 4.6 measured
        and recon/seams.md 5.3 read the keysym off."""
        self.change_map(8, EURO)
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.KEY_PRESS, 8, 0, 0, 3, 2))
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("0x%08x" % EURO, "down")])

    def test_the_remap_request_still_reached_the_server(self):
        """The table is fed from the frame and the frame still goes: Xwayland's
        own map has to move too, and it sends `MappingNotify` to every client
        when it does, exactly as X does (design section 6.2)."""
        self.change_map(8, EURO)
        self.assertIn(100, self.rig.upstream.ops)

    def test_after_the_restore_keycode_8_is_a_logged_no_op(self):
        """xdotool restores keycode 8 to `NoSymbol` when it is done
        [recon/tools/caps/plain.jsonl, two ChangeKeyboardMappings after the
        release]. A `FakeInput` naming it then means nothing at all."""
        self.change_map(8, EURO)
        self.change_map(8, 0)
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.KEY_PRESS, 8, 0, 0, 3, 2))
        self.assertEqual(self.ops("key"), [])
        self.assertEqual(len(self.log.carrying("NoSymbol")), 1, self.log.lines)

    def test_the_capture_of_the_whole_remap_path_replays(self):
        """`remap-keycode8.hex` is the 53 requests of that command; the two
        `FakeInput`s in it are one `key down` and one `key up` of the planted
        keysym, and nothing else in the stream reaches the daemon.

        The order in the capture is what makes this more than a repeat of the
        press test: xdotool plants the keysym, presses, **restores keycode 8 to
        NoSymbol**, and only then releases it. A release that re-read the table
        would send `key 0x00000000 up` and leave the real key down, which is
        what this asserted before the engine remembered the spec its press
        used."""
        for frame in frames_of("remap-keycode8"):
            if frame[0] == 100:
                self.send(100, frame[1], frame[4:])
            elif frame[0] == 132 and frame[1] == xtest.FAKE_INPUT:
                self.send(self.xtest_major(), frame[1], frame[4:])
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("0x%08x" % EURO, "down"), ("0x%08x" % EURO, "up")])


class NamesForModifiers(XtestCase):
    """The spec is the keysym's NAME, which is what makes a chord work."""

    num, upstream_num = 908, 909

    def press(self, keycode):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.KEY_PRESS, keycode,
                              0, 0, 0, 0))

    def test_keycode_37_is_control_l_and_36_is_return_and_38_is_a(self):
        for code in (37, 36, 38):
            self.press(code)
        self.assertEqual([r["spec"] for r in self.ops("key")],
                         ["Control_L", "Return", "a"])

    def test_the_daemon_can_reach_every_one_of_those_names(self):
        """The claim underneath the choice, measured against the tree's own
        resolver rather than against the daemon: `resolve_token("Control_L")`
        is `(29, False)` and `resolve_token("0xffe3")` is "not reachable on the
        US layout" [M 2026-09-10, this box], so a hex spec would have made
        every ctrl+ chord unreachable (design section 6.2 step 4)."""
        from wdotool import keymap
        for name in ("Control_L", "Return", "a", "Shift_L"):
            self.assertIsInstance(keymap.resolve_token(name, None), tuple,
                                  "%s must resolve" % name)
        hexed = keymap.resolve_token("0x%08x" % 0xFFE3, None)
        self.assertIsInstance(hexed, str)
        self.assertIn(xtest.UNREACHABLE, hexed)

    def test_a_keysym_with_no_name_falls_back_to_hex(self):
        self.assertEqual(xtest.spec_for(EURO), "0x010020ac")
        self.assertEqual(xtest.spec_for(0xFFE3), "Control_L")


class TypedFallback(XtestCase):
    """A character the layout cannot reach is typed, once."""

    num, upstream_num = 910, 911

    def press(self, keycode):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.KEY_PRESS, keycode,
                              0, 0, 0, 0))

    def release(self, keycode):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.KEY_RELEASE, keycode,
                              0, 0, 0, 0))

    def test_an_unreachable_character_is_typed_and_the_release_consumed(self):
        self.daemon.warn_for("0x%08x" % EURO)
        self.engine.keymap.rows[8] = (EURO,)
        self.press(8)
        self.release(8)
        self.assertEqual([r["text"] for r in self.ops("type")], ["€"])
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("0x%08x" % EURO, "down")],
                         "the release must not reach the daemon: `type` is a "
                         "press AND a release already")

    def test_the_twin_with_no_warning_sends_no_type_at_all(self):
        """The same frames against a daemon that reached the key: the fallback
        is keyed on the daemon's own sentence and on nothing else."""
        self.engine.keymap.rows[8] = (EURO,)
        self.press(8)
        self.release(8)
        self.assertEqual(self.ops("type"), [])
        self.assertEqual([r["direction"] for r in self.ops("key")],
                         ["down", "up"])

    def test_an_unreachable_keysym_that_names_no_character_types_nothing(self):
        """`Control_L` unreachable is a broken layout, not a character: there
        is nothing to type instead and the line says so."""
        self.daemon.warn_for("Control_L")
        self.press(37)
        self.assertEqual(self.ops("type"), [])
        self.assertEqual(len(self.log.carrying("names no character")), 1,
                         self.log.lines)


class ButtonsAreXNumbers(XtestCase):
    """`daemon.button` takes X button numbers, and translates them itself."""

    num, upstream_num = 912, 913

    def button(self, detail, down=True):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x",
                              xtest.BUTTON_PRESS if down
                              else xtest.BUTTON_RELEASE, detail, 0, 0, 3, 2))

    def test_details_one_to_five_and_eight_reach_the_daemon_unchanged(self):
        """`_BTN = {1: BTN_LEFT, 2: BTN_MIDDLE, 3: BTN_RIGHT, 8..12}` and
        `_WHEEL = {4, 5, 6, 7}` are the daemon's own tables
        [wdotool/daemon.py:1219-1223], keyed by X number: the proxy must NOT
        translate."""
        for detail in (1, 2, 3, 4, 5, 8):
            self.button(detail)
        self.assertEqual([r["btn"] for r in self.ops("button")],
                         [1, 2, 3, 4, 5, 8])
        self.assertEqual([r["down"] for r in self.ops("button")], [True] * 6)

    def test_a_release_is_down_false(self):
        self.button(1, down=True)
        self.button(1, down=False)
        self.assertEqual([(r["btn"], r["down"]) for r in self.ops("button")],
                         [(1, True), (1, False)])

    def test_the_click_1_capture_is_a_press_and_a_release(self):
        """`xdotool click 1` is two FakeInputs and nothing else
        [recon/tools.md 4.5]."""
        frames = [f for f in frames_of("click-1")
                  if f[0] == 132 and f[1] == xtest.FAKE_INPUT]
        self.assertEqual(len(frames), 2)
        for frame in frames:
            self.send(self.xtest_major(), frame[1], frame[4:])
        self.assertEqual([(r["btn"], r["down"]) for r in self.ops("button")],
                         [(1, True), (1, False)])


class MotionAbsRel(XtestCase):
    """detail 0 is absolute and detail 1 is relative."""

    num, upstream_num = 914, 915

    def motion(self, detail, x, y):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.MOTION_NOTIFY, detail,
                              0, self.root, x, y))

    def test_detail_zero_is_an_absolute_move_and_moves_the_model(self):
        self.motion(0, 150, 250)
        self.assertEqual([(r["x"], r["y"]) for r in self.ops("mousemove_abs")],
                         [(150, 250)])
        self.assertEqual(self.rig.server.pointer_model, (150, 250))

    def test_detail_one_is_a_relative_move_and_leaves_the_model_alone(self):
        """A relative move needs no origin and establishes none: the daemon
        keeps its own model and the proxy does not guess at one."""
        self.motion(1, -5, 7)
        self.assertEqual([(r["dx"], r["dy"]) for r in self.ops("mousemove_rel")],
                         [(-5, 7)])
        self.assertIsNone(self.rig.server.pointer_model)

    def test_the_mousemove_capture_is_two_query_pointers_and_one_motion(self):
        """`xdotool mousemove 10 10` on the pinned 4.x [recon/tools.md 4.5]."""
        frames = frames_of("mousemove-4x")
        motions = [f for f in frames if f[0] == 132 and f[1] == xtest.FAKE_INPUT]
        pointers = [f for f in frames if f[0] == wire.OP_QUERY_POINTER]
        self.assertEqual((len(motions), len(pointers)), (1, 2))
        self.send(self.xtest_major(), motions[0][1], motions[0][4:])
        self.assertEqual([(r["x"], r["y"]) for r in self.ops("mousemove_abs")],
                         [(10, 10)])


class DelayDeferred(XtestCase):
    """`time` is a delay in milliseconds, and no sleep runs in the loop."""

    num, upstream_num = 916, 917

    def motion(self, x, y, delay=0):
        self.conn._send(self.xtest_major(), xtest.FAKE_INPUT,
                        struct.pack("<BB2xII8xhh8x", xtest.MOTION_NOTIFY, 0,
                                    delay, self.root, x, y))

    def test_a_delayed_operation_lands_no_earlier_than_its_delay(self):
        started = time.monotonic()
        self.motion(1, 1, delay=50)
        self.sync()
        self.assertEqual(self.ops("mousemove_abs"), [],
                         "the delay must not be paid inside the request")
        self.rig.wait(lambda: len(self.ops("mousemove_abs")) == 1, timeout=5.0,
                      what="the deferred motion")
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_an_undelayed_operation_from_the_same_client_queues_behind(self):
        """Order per client is the one thing a delay may never change."""
        self.motion(1, 1, delay=80)
        self.motion(2, 2, delay=0)
        self.sync()
        self.rig.wait(lambda: len(self.ops("mousemove_abs")) == 2, timeout=5.0,
                      what="both motions")
        self.assertEqual([(r["x"], r["y"]) for r in self.ops("mousemove_abs")],
                         [(1, 1), (2, 2)])

    def test_the_loops_timeout_shrinks_to_the_nearest_deadline(self):
        """What makes the delay cost nothing: the selector's own timeout is
        what wakes the loop, so an idle 15-second cadence becomes 80 ms and no
        thread is spent."""
        server = self.rig.server
        conn = server.conns[0]
        conn.deferred.append((time.monotonic() + 0.08, lambda: None))
        try:
            self.assertLessEqual(server.input_timeout(15.0), 0.081)
            self.assertGreater(server.input_timeout(15.0), 0.0)
        finally:
            conn.deferred.clear()


class NoRoutePasses(XtestCase):
    """With no daemon at all the frame goes to Xwayland (design section 6.7)."""

    num, upstream_num = 918, 919

    def setUp(self):
        super().setUp()
        self.engine.drop("the test wants a cold engine")
        self.refusals = []

        def refuse():
            self.refusals.append(time.monotonic())
            raise CmdError("cannot start wdotool daemon (see /tmp/wdotool.log)")
        self.real_connect = xtest.connect_daemon
        xtest.connect_daemon = lambda log=None: refuse()
        self.addCleanup(setattr, xtest, "connect_daemon", self.real_connect)

    def test_the_fake_input_reaches_the_server_untouched(self):
        press = frames_of("fakeinput-garbage")[0]
        self.send_fake_input(press)
        self.assertEqual([body for (minor, body) in self.rig.upstream.frames
                          if minor == xtest.FAKE_INPUT],
                         [press[4:]])
        self.assertEqual(self.daemon.ops, [])

    def test_the_warp_reaches_the_server_untouched(self):
        warp = [f for f in frames_of("warp-3x") if f[0] == wire.OP_WARP_POINTER]
        self.assertEqual(len(warp), 1)
        self.send(wire.OP_WARP_POINTER, 0, warp[0][4:])
        self.assertIn(wire.OP_WARP_POINTER, self.rig.upstream.ops)

    def test_one_line_carries_the_sentence_the_route_and_the_udev_rule(self):
        self.send_fake_input(frames_of("fakeinput-garbage")[0])
        said = self.log.carrying("the input daemon has no route")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("cannot start wdotool daemon", said[0])
        self.assertIn("udev", said[0])
        self.assertIn("not yet", said[0])
        self.assertIn("rung 4", said[0])

    def test_the_line_is_said_once_and_not_once_per_frame(self):
        for frame in frames_of("fakeinput-garbage")[2:]:
            self.send_fake_input(frame)
        self.assertEqual(len(self.log.carrying("the input daemon has no "
                                               "route")), 1)

    def test_after_the_retry_window_a_daemon_that_appears_is_used(self):
        self.send_fake_input(frames_of("fakeinput-garbage")[0])
        self.assertEqual(len(self.refusals), 1)
        # The retry is `DAEMON_RETRY` away; nothing is dialled before it.
        self.send_fake_input(frames_of("fakeinput-garbage")[0])
        self.assertEqual(len(self.refusals), 1, "no dial inside the window")
        self.engine.retry_at = 0.0
        xtest.connect_daemon = self.real_connect
        self.send_fake_input(frames_of("fakeinput-garbage")[0])
        self.assertEqual([(r["spec"], r["direction"])
                          for r in self.ops("key")], [("a", "down")])


class VersionPasses(XtestCase):
    """`GetVersion` and `CompareCursor` are upstream's (design section 3.2)."""

    num, upstream_num = 920, 921

    def test_the_captured_get_version_reaches_the_server_and_answers_2_2(self):
        """`8400020002000200` is what both generations send [recon/wire.md
        5.3]; the proxy answers nothing itself and upstream's 2.2 comes back."""
        frame = bytes.fromhex("8400020002000200")
        pkt = self.conn._wait_reply(
            self.conn._send(self.xtest_major(), frame[1], frame[4:]))[0]
        self.assertEqual(pkt[1], 2, "major_version")
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0], 2, "minor")
        self.assertIn((xtest.GET_VERSION, frame[4:]), self.rig.upstream.frames)

    def test_grab_control_is_eaten_and_costs_one_noop(self):
        self.send(self.xtest_major(), xtest.GRAB_CONTROL,
                  struct.pack("<B3x", 1))
        self.assertEqual([minor for (minor, _b) in self.rig.upstream.frames], [])
        self.assertIn(wire.OP_NO_OPERATION, self.rig.upstream.ops)


class ReleaseOnDisconnect(XtestCase):
    """A client that goes away holding a key does not leave the seat holding
    it."""

    num, upstream_num = 922, 923

    def test_the_keys_a_client_held_are_released_when_it_goes(self):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.KEY_PRESS, 37, 0, 0, 0, 0))
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.BUTTON_PRESS, 1, 0, 0, 0, 0))
        self.assertEqual([r["direction"] for r in self.ops("key")], ["down"])
        self.conn.close()
        self.rig.wait(lambda: self.rig.server.live == 0,
                      what="the client to go")
        self.rig.wait(lambda: len(self.ops("key")) == 2, timeout=5.0,
                      what="the release")
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("Control_L", "down"), ("Control_L", "up")])
        self.assertEqual([(r["btn"], r["down"]) for r in self.ops("button")],
                         [(1, True), (1, False)])
        self.assertEqual(len(self.log.carrying("went away holding")), 1,
                         self.log.lines)

    def test_a_client_that_held_nothing_says_nothing(self):
        self.conn.close()
        self.rig.wait(lambda: self.rig.server.live == 0,
                      what="the client to go")
        self.assertEqual(self.log.carrying("went away holding"), [])


class TheRouteOutlivesAHeldKey(XtestCase):
    """`DAEMON_LINGER` may not drop the connection an `up` still has to go
    down.

    `xdotool keydown ctrl` is one operation and then silence, so the 60-second
    timer would have closed the connection while Control was down on the seat:
    the release at disconnect then had nowhere to go, and the daemon's own
    `self.down` holds the key until its 900-second idle exit
    [recon/seams.md 5.1 for the timers]. Nothing in the daemon undoes a
    client's keys when that client goes -- the release-on-disconnect in
    `wdotool/daemon.py` is the COMPOSITOR releasing the virtual devices.
    """

    num, upstream_num = 928, 929

    def press(self, keycode=37, kind=None):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", kind or xtest.KEY_PRESS,
                              keycode, 0, 0, 0, 0))

    def age_it(self):
        """Put the last operation `DAEMON_LINGER` + 1 s in the past."""
        self.engine.last_used = time.monotonic() - xtest.DAEMON_LINGER - 1.0

    def test_an_idle_connection_with_nothing_held_is_dropped(self):
        """The premise: the timer does work, and this class is about the one
        case it must not fire in."""
        self.press()
        self.press(kind=xtest.KEY_RELEASE)
        self.assertIsNotNone(self.engine.daemon)
        self.age_it()
        self.engine.tick()
        self.assertIsNone(self.engine.daemon)

    def test_a_connection_with_a_key_still_down_is_kept(self):
        self.press()
        self.age_it()
        self.engine.tick()
        self.assertIsNotNone(self.engine.daemon,
                             "the `up` for keycode 37 would have had no route")
        self.assertIsNone(self.engine.next_deadline(),
                          "and the loop has nothing to wake for until the "
                          "release, which is an operation of its own")

    def test_a_connection_with_a_button_still_down_is_kept(self):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.BUTTON_PRESS, 1, 0, 0, 0, 0))
        self.age_it()
        self.engine.tick()
        self.assertIsNotNone(self.engine.daemon)

    def test_a_dropped_route_is_dialled_again_to_release_what_is_held(self):
        """Belt and braces for the same key: a refusal earlier in this client's
        life can still have closed the connection, and `on_close` dials rather
        than giving up."""
        self.press()
        self.engine.drop("the test takes the route away under a held key")
        self.assertIsNone(self.engine.daemon)
        self.conn.close()
        self.rig.wait(lambda: self.rig.server.live == 0,
                      what="the client to go")
        self.rig.wait(lambda: len(self.ops("key")) == 2, timeout=5.0,
                      what="the release on a re-dialled connection")
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("Control_L", "down"), ("Control_L", "up")])


    def test_the_proxy_exiting_releases_before_it_drops_the_route(self):
        """`Server.shutdown` closes its clients first and lets the daemon go
        after. The other order dropped the connection the `up`s had to go down,
        so a proxy that exited while a client held Control left Control down on
        the seat -- and dialled the daemon a second time on the way out."""
        self.press()
        self.rig.server.stop("the test is exiting the proxy")
        self.rig.thread.join(timeout=5)
        self.assertFalse(self.rig.thread.is_alive(), "the loop never exited")
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("Control_L", "down"), ("Control_L", "up")])
        self.assertIsNone(self.engine.daemon,
                          "and the route is closed once, after the release")


class MappingNotifyRereads(XtestCase):
    """Feed three: a remap by a client that is not going through this proxy."""

    num, upstream_num = 924, 925

    def test_a_mapping_notify_on_the_own_connection_costs_one_read(self):
        before = len([row for row in self.rig.upstream.log
                      if row[0] == "GetKeyboardMapping"])
        pkt = struct.pack("<BxHBBB25x", xtest.EV_MAPPING_NOTIFY, 0,
                          xtest.MAPPING_KEYBOARD, 8, 1)
        self.rig.upstream.push_event(pkt, conn_index=self.own_index())
        self.rig.wait(lambda: len([row for row in self.rig.upstream.log
                                   if row[0] == "GetKeyboardMapping"])
                      == before + 1, timeout=5.0, what="the re-read")

    def test_a_pointer_mapping_notify_costs_nothing(self):
        """`request` 2 is the POINTER map; only the keyboard's feeds this
        table."""
        before = len([row for row in self.rig.upstream.log
                      if row[0] == "GetKeyboardMapping"])
        pkt = struct.pack("<BxHBBB25x", xtest.EV_MAPPING_NOTIFY, 0, 2, 8, 1)
        self.rig.upstream.push_event(pkt, conn_index=self.own_index())
        time.sleep(0.2)
        self.assertEqual(len([row for row in self.rig.upstream.log
                              if row[0] == "GetKeyboardMapping"]), before)

    def own_index(self):
        """Which of the fake's connections is the PROXY's own one.

        Identified by its request count and not by its position: the own
        connection is opened inside the first accept (design section 2.6) and
        landed at index 0 on this box, which is an ordering nothing promises.
        `OwnConn.seq` is the number of requests it has sent -- the extension
        probes, the ~57 atoms and the keymap read -- and the fake counts the
        same requests on its own side.
        """
        want = self.rig.server.own.seq
        hit = [i for i, w in enumerate(self.rig.upstream.wires) if w.seq == want]
        self.assertEqual(len(hit), 1,
                         "one connection has sent %d requests" % want)
        return hit[0]




class NoXtestIsNotAnOption(XtestCase):
    """Why `QueryExtension("XTEST")` keeps upstream's answer (design 6.1)."""

    num, upstream_num = 928, 929

    def test_the_query_extension_reply_is_upstreams_and_says_present(self):
        pkt = self.conn._wait_reply(self.conn._send(
            wire.OP_QUERY_EXTENSION, 0,
            struct.pack("<H2x", 5) + b"XTEST\0\0\0"))[0]
        self.assertEqual(pkt[8], 1, "present")
        self.assertEqual(pkt[9], self.xtest_major())
        self.assertIn(("QueryExtension", "XTEST"), self.rig.upstream.log)

    def test_the_denied_prologue_is_three_requests_shorter(self):
        """`tests/fixtures/xw11/noxtest-prologue.hex` is what xdotool sends
        when the answer is `present=0` [recon/tools/caps/noxtest.jsonl]: no
        `QueryExtension("Generic Event Extension")`, no `GE.QueryVersion` and
        no `XTEST.GetVersion` -- 28 requests against the 31 of
        `prologue-xdotool.hex`. After that it sends no input request at all,
        no `SendEvent` fallback either, and exits 0 for `key`/`type` and 1 for
        `click` [recon/tools.md 4.9]. There is nothing for a proxy to fall back
        onto, which is the argument for owning `FakeInput`.

        (recon/tools.md 4.9 has this slightly differently -- it says the
        prologue loses `XTEST.GetVersion` and asks for 11 atom names instead of
        14. Measured off the capture itself: three requests go and the 14 atom
        names stay.)
        """
        with_xtest = frames_of("prologue-xdotool")
        without = frames_of("noxtest-prologue")
        self.assertEqual((len(with_xtest), len(without)), (31, 28))
        atom_names = [f for f in without if f[0] == wire.OP_GET_ATOM_NAME]
        self.assertEqual(len(atom_names), 14)
        major = 132                     # XTEST's major on the Xvfb both were
        self.assertTrue(any(f[0] == major and f[1] == xtest.GET_VERSION
                            for f in with_xtest))
        self.assertFalse(any(f[0] == major for f in without))



def two_plane_backend():
    """A native `foot` and an X `xterm`, the two planes design section 4.2
    pairs. The xterm's `View.xid` is non-zero, which is the compositor saying
    "this toplevel IS an Xwayland window" [recon/seams.md 3] -- what the
    registry trusts and never re-derives."""
    foot = fake_window(11, title="WXL-Foot", class_="foot", instance="foot",
                       pid=4242, x=0, y=0, w=640, h=720, visible=True,
                       focused=True, desktop=0)
    xterm = fake_window(12, title="WXL-Xterm", class_="XTerm", instance="xterm",
                        pid=4243, x=640, y=0, w=640, h=720, visible=True,
                        focused=False, desktop=0)
    return FakeBackend(
        windows=[foot, xterm],
        views=[fake_view(foot, xid=0, app_id="foot", instance="foot", cls="foot"),
               fake_view(xterm, xid=0x40000C, app_id="", instance="xterm",
                         cls="XTerm")])


class KeysFollowTheFocus(XtestCase):
    """A keystroke goes where the thing that can receive it is.

    Measured on 2026-09-11 through this proxy on a headless sway with no
    /dev/uinput: routed to the input daemon, `xdotool type 'ünï €ur ß λ 日'`
    into an `xterm -u8` wrote `n ur   ` -- the daemon said "Can't type
    character 'ü' (not on the US layout). Skipping." for every non-ASCII one --
    while real xdotool straight at Xwayland writes all of them, CJK included
    [recon/env.md 2.3], because the keysym it wants goes into a spare keycode
    of Xwayland's OWN map. AGENTS.md decides it: X did it, so a key for an X
    window goes to Xwayland. The native half is what the daemon is for:
    Xwayland's XTEST puts nothing at all into a focused `foot`
    [recon/env.md 2.7].
    """

    num, upstream_num = 926, 927

    def make_backend(self):
        return two_plane_backend()

    def focus(self, wid):
        for win in self.backend.windows:
            win.focused = (win.id == wid)
        self.shadows.invalidate()
        self.shadows.snapshot()

    def key(self, keycode, kind=None):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", kind or xtest.KEY_PRESS,
                              keycode, 0, 0, 3, 2))

    def button(self, detail=1):
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.BUTTON_PRESS, detail,
                              0, 0, 3, 2))

    def test_a_key_with_the_native_toplevel_focused_goes_to_the_daemon(self):
        self.focus(11)
        self.key(38)
        self.assertEqual([r["spec"] for r in self.ops("key")], ["a"])
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames], [])

    def test_a_key_with_the_x_toplevel_focused_goes_to_xwayland(self):
        self.focus(12)
        self.key(38)
        self.assertEqual(self.ops("key"), [],
                         "the daemon must not see it: the compositor's layout "
                         "would decide the character and Xwayland's own remap "
                         "is what xdotool set up")
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames],
                         [xtest.FAKE_INPUT])

    def test_a_button_still_goes_to_the_daemon_whatever_has_the_focus(self):
        """A click has to move the SEAT's pointer: that is what raises and
        focuses a window, and Xwayland's own warp moves only Xwayland's pointer
        (measured 2026-09-11: a WarpPointer straight at Xwayland moved its
        QueryPointer to 321,123 and the compositor's cursor not at all)."""
        self.focus(12)
        self.button()
        self.assertEqual([r["btn"] for r in self.ops("button")], [1])
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames], [])

    def test_a_motion_still_goes_to_the_daemon_too(self):
        self.focus(12)
        self.send(self.xtest_major(), xtest.FAKE_INPUT,
                  struct.pack("<BB2xII8xhh8x", xtest.MOTION_NOTIFY, 0, 0,
                              self.root, 7, 8))
        self.assertEqual([(r["x"], r["y"]) for r in self.ops("mousemove_abs")],
                         [(7, 8)])

    def test_with_nothing_focused_the_keystroke_goes_to_xwayland(self):
        """"Nobody knows" answers the way that works today: Xwayland's XTEST
        reaches every X window on the display [recon/env.md 2.3]."""
        for win in self.backend.windows:
            win.focused = False
        self.shadows.invalidate()
        self.shadows.snapshot()
        self.key(38)
        self.assertEqual(self.ops("key"), [])
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames],
                         [xtest.FAKE_INPUT])

    def test_the_forwarded_key_still_costs_exactly_one_request(self):
        """Forwarded, not consumed: the sequence upstream is the request
        itself and not a `NoOperation` in its place."""
        self.focus(12)
        self.key(38)
        self.assertNotIn(wire.OP_NO_OPERATION, self.rig.upstream.ops)

    def test_a_release_goes_where_its_press_went_and_not_where_the_focus_is(self):
        """`xdotool keydown a` with the foot focused, a click that moves the
        compositor's focus to the xterm, `xdotool keyup a`.

        With the focus alone deciding, the release went to Xwayland -- which
        had never seen the press -- and the daemon held `a` down on the seat
        until the client disconnected. The press's half owns the release."""
        self.focus(11)
        self.key(38)
        self.assertEqual([r["spec"] for r in self.ops("key")], ["a"])
        self.focus(12)                            # the click landed elsewhere
        del self.rig.upstream.frames[:]
        self.key(38, kind=xtest.KEY_RELEASE)
        self.assertEqual([(r["spec"], r["direction"]) for r in self.ops("key")],
                         [("a", "down"), ("a", "up")])
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames], [],
                         "Xwayland never saw the press and must not see the "
                         "release")
        self.assertEqual(self.rig.server.conns[0].held, set(),
                         "and the proxy is holding nothing afterwards")

    def test_a_release_of_a_key_this_proxy_never_held_still_follows_the_focus(self):
        """The other half of the same rule: a keycode neither `held` nor
        `typed` carries is Xwayland's -- its press was forwarded there."""
        self.focus(12)
        self.key(38)
        self.key(38, kind=xtest.KEY_RELEASE)
        self.assertEqual(self.ops("key"), [])
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames],
                         [xtest.FAKE_INPUT, xtest.FAKE_INPUT])

    def test_the_release_of_a_typed_character_is_consumed_whatever_the_focus(self):
        """A press the daemon answered "not reachable" became one `type`, which
        is a press AND a release [design section 6.2]. Its KeyRelease is
        consumed here too, and a focus change may not turn it into a stray
        KeyRelease on Xwayland's seat."""
        self.engine.keymap.rows[8] = (EURO,)
        self.daemon.warn_for("0x010020ac")
        self.focus(11)
        self.key(8)
        self.assertEqual([r["text"] for r in self.ops("type")], ["\u20ac"])
        self.focus(12)
        del self.rig.upstream.frames[:]
        self.key(8, kind=xtest.KEY_RELEASE)
        self.assertEqual([r["direction"] for r in self.ops("key")], ["down"],
                         "the `down` the daemon warned about, and no `up`")
        self.assertEqual([m for (m, _b) in self.rig.upstream.frames], [])


class TableFeeds(unittest.TestCase):
    """`KeyMap`, without a server in the way."""

    def test_a_reply_fills_the_range_it_carries(self):
        per, first = 2, 8
        rows = [(0x61, 0x41), (0x62, 0x42)]
        body = b"".join(struct.pack("<II", *r) for r in rows)
        pkt = struct.pack("<BBHI24x", 1, per, 0, len(body) // 4) + body
        table = xtest.KeyMap()
        self.assertEqual(table.feed_reply(pkt, first), 2)
        self.assertEqual(table.keysym(8), 0x61)
        self.assertEqual(table.keysym(9), 0x62)
        self.assertEqual(table.keysym(10), xtest.NO_SYMBOL)

    def test_a_change_is_read_from_byte_one_four_five_and_eight(self):
        """`keycode_count` at 1, `first_keycode` at 4, `keysyms_per_keycode` at
        5, keysyms from 8 -- the layout the capture confirms, where byte 1 is 1
        and the request is twelve bytes [recon/tools/caps/plain.jsonl seq
        33]."""
        frame = struct.pack("<BBHBB2xI", 100, 1, 3, 8, 1, EURO)
        table = xtest.KeyMap()
        self.assertEqual(table.feed_change(frame), 1)
        self.assertEqual(table.keysym(8), EURO)

    def test_a_change_claiming_more_keysyms_than_it_carries_is_bounded(self):
        frame = struct.pack("<BBHBB2xI", 100, 40, 3, 8, 1, EURO)
        table = xtest.KeyMap()
        self.assertEqual(table.feed_change(frame), 1)
        self.assertEqual(table.keysym(9), xtest.NO_SYMBOL)

    def test_every_name_in_the_inverse_table_resolves_for_the_daemon(self):
        """The claim `spec_for` rests on: a name this table hands out has to be
        one `keymap.resolve_token` accepts as a name at all -- it may still be
        unreachable on a layout, which is what the typed fallback is for, but
        it must never be "(symbol) No such key name"."""
        from wdotool import keymap
        reachable, unreachable = 0, 0
        for keysym, name in list(xtest.KEYSYM_NAMES.items())[:400]:
            got = keymap.resolve_token(name, None)
            if isinstance(got, str):
                # The other answer shape: a warning. "not reachable on the
                # <layout>" is the typed fallback's cue and legitimate; "No
                # such key name" means this table handed out a name the daemon
                # has never heard of.
                self.assertNotIn("No such key name", got, name)
                self.assertIn(xtest.UNREACHABLE, got, name)
                unreachable += 1
            else:
                self.assertIsInstance(got, tuple, name)
                self.assertEqual(len(got), 2, name)
                reachable += 1
        self.assertEqual(reachable + unreachable, 400)
        self.assertGreater(reachable, 0, "not one name resolved to a keycode")


if __name__ == "__main__":
    unittest.main()
