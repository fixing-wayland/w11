#!/usr/bin/env python3
"""The write side: the captured bytes of eight commands, and what they do.

Every request here is either a frame lifted out of `tests/fixtures/xw11/caps/`
-- the stream the real tool sent to a live Xwayland under headless sway
[recon/tools.md 11] -- or one packed by hand at the offsets of recon/wire.md
4.2 for a case no tool sends. Neither is packed with the code under test: a
frame built by the same helper the handler decodes with agrees with it about a
field in the wrong place, and libX11 would not.

What the file is defending, in one sentence per group:

* a `ConfigureWindow` value list is walked in BIT ORDER and every bit that has
  a backend verb becomes one call, with the axis a request leaves out taken
  from the window's current rect rather than zeroed;
* `DestroyWindow` is a close and `KillClient` is a kill -- `xdotool
  windowclose` sends the first [recon/tools.md 4.3] and nothing measured sends
  the second;
* the overlay honours Replace, Prepend and Append against the value a client
  would have read a moment earlier, and `xdotool set_window --name`'s
  `_NET_WM_NAME(STRING)` is stored as STRING, xdotool's bug and all;
* a `DeleteProperty` hides a synthesized name for the life of the entry, and a
  Replace after it brings the name back;
* an event mask is recorded per connection, on a shadow and on the root, and
  never on a real X window -- upstream's own bookkeeping is the truth there;
* a backend refusal is silence on the wire and one line in the log
  (design section 3.3), which is the issue's "what is lost: diagnostics" with
  the log as the substitute;
* a CONSUMEd write arms the settle poll of design section 5.2.
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
from support import FakeBackend, fake_view, fake_window            # noqa: E402
from w11common.errors import CmdError                              # noqa: E402
from wdotool.x11_mini import X11Error                              # noqa: E402
from xw11 import policy, req_write, server as server_mod           # noqa: E402
from xw11 import shadow as shadow_mod, wire                        # noqa: E402

CAPS = os.path.join(ROOT, "tests", "fixtures", "xw11", "caps")


class Capture:
    """One `tests/fixtures/xw11/caps/*.hex` fixture: the header's ids and the
    frames under it.

    The fixture is the request stream a real tool sent to a live Xwayland under
    headless sway, one hex frame per line, rebuilt from the decoded capture by
    scratchpad/b4/mkcaps_write.py; the counts match recon/tools.md 11 exactly.
    `# ROOT`, `# TARGET` and `# ATOM <id> <name>` are what a replay has to
    rewrite: the ids in the stream are the ones that server handed out.
    """

    #: Where a frame carries an ATOM, by opcode: offsets into the FRAME, so
    #: `SendEvent`'s `type` is the event's own 8 plus the 12 the event starts
    #: at [recon/wire.md 4.2, 4.4].
    ATOM_FIELDS = {
        wire.OP_CHANGE_PROPERTY: (8, 12),          # property, type
        wire.OP_DELETE_PROPERTY: (8,),             # property
        wire.OP_SEND_EVENT: (20,),                 # cm_type
    }

    #: Which `data32` slots of a `ClientMessage` hold ATOMS, by type name --
    #: and only these: `_NET_MOVERESIZE_WINDOW`'s slots hold `10` and `300`
    #: [recon/tools.md 4.4] and a blanket rewrite of every word that matches an
    #: atom id would turn a coordinate into one.
    ATOM_DATA = {
        "_NET_WM_STATE": (28, 32),                 # data32[1], data32[2]
        "WM_PROTOCOLS": (24,),                     # data32[0]
    }

    def __init__(self, name):
        self.name = name
        self.root = 0
        self.target = 0
        self.atoms = {}
        self.frames = []
        with open(os.path.join(CAPS, name + ".hex"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#"):
                    self._header(line[1:].strip())
                elif line:
                    self.frames.append(bytes.fromhex(line.split("#")[0].strip()))

    def _header(self, text):
        if text.startswith("ROOT 0x"):
            self.root = int(text[7:], 16)
        elif text.startswith("TARGET 0x"):
            self.target = int(text[9:], 16)
        elif text.startswith("ATOM "):
            _tag, atom, name = text.split()
            if int(atom):
                self.atoms[int(atom)] = name

    def of(self, opcode):
        got = [f for f in self.frames if f[0] == opcode]
        assert got, "no opcode %d in the %s capture" % (opcode, self.name)
        return got

    def rewrite(self, frame, root, window, resolve):
        """The frame with the capture's ids replaced by this rig's.

        Window ids are matched on 4-byte boundaries only -- an id is a CARD32
        field and a match halfway through one would be a coincidence -- and
        atoms are rewritten ONLY where the request carries an atom, through
        `resolve(name)`, which is the client interning the name the way the
        tool did. A word in a data32 slot that is not one of the capture's own
        atoms (an action, a coordinate) is left alone.
        """
        buf = bytearray(frame)
        for i in range(0, len(buf) - 3, 4):
            (word,) = struct.unpack_from("<I", buf, i)
            if word == self.root:
                struct.pack_into("<I", buf, i, root)
            elif self.target and word == self.target:
                struct.pack_into("<I", buf, i, window)
        fields = list(self.ATOM_FIELDS.get(frame[0], ()))
        if frame[0] == wire.OP_SEND_EVENT and len(buf) >= 24:
            (type_atom,) = struct.unpack_from("<I", buf, 20)
            fields += self.ATOM_DATA.get(self.atoms.get(type_atom), ())
        for at in fields:
            if at + 4 > len(buf):
                continue
            (word,) = struct.unpack_from("<I", buf, at)
            if word in self.atoms:
                struct.pack_into("<I", buf, at, resolve(self.atoms[word]))
        return bytes(buf)


def configure(window, **values):
    """A `ConfigureWindow` packed by hand at recon/wire.md 4.2's offsets: the
    mask, then one CARD32 per set bit in ASCENDING BIT ORDER."""
    bits = (("x", req_write.CW_X), ("y", req_write.CW_Y),
            ("width", req_write.CW_WIDTH), ("height", req_write.CW_HEIGHT),
            ("border_width", req_write.CW_BORDER_WIDTH),
            ("sibling", req_write.CW_SIBLING),
            ("stack_mode", req_write.CW_STACK_MODE))
    mask = 0
    words = []
    for name, bit in bits:
        if name in values:
            mask |= bit
            words.append(values[name] & 0xFFFFFFFF)
    body = struct.pack("<IH2x", window, mask)
    return body + struct.pack("<%dI" % len(words), *words)


class _Upstream(support.FakeUpstream):
    """`FakeUpstream` with an opcode log and predefined atom ids.

    The log is what says a CONSUMEd request cost upstream exactly one
    `NoOperation` -- a request the proxy eats still costs one sequence number
    upstream for ever [recon/wire.md 3.2a] -- and the atom ids are the
    protocol's own for 1..68, because `FakeXServer` numbers every name from 100
    and the proxy's table says `STRING` is 31 [recon/wire.md 7.2].
    """

    def __init__(self, *a, **kw):
        self.ops = []
        super().__init__(*a, **kw)

    def intern(self, name: str) -> int:
        fixed = policy.PREDEFINED_ATOMS.get(name)
        if fixed and name not in self._atoms:
            self._atoms[name] = fixed
            self._names[fixed] = name
        return super().intern(name)

    #: The void requests: a real X server answers NOTHING at all for these
    #: [recon/wire.md 4.2], and `FakeXServer` answers `BadRequest` for an
    #: opcode nobody wrote a branch for. Batch 1's file should take these up;
    #: until it does they are a per-file subclass (scratchpad
    #: requests-batch-4.md).
    VOID = (4, 8, 10, 12, 25, 42, 113)

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        self.ops.append(opcode)
        if opcode in self.VOID:
            return
        if opcode == 21:                        # ListProperties
            (win,) = struct.unpack_from("<I", payload, 0)
            atoms = [self.intern(n) for (w, n) in self.props if w == win]
            body = struct.pack("<%dI" % len(atoms), *atoms)
            conn.sendall(struct.pack("<BxHIH22x", 1, seq, len(body) // 4,
                                     len(atoms)) + body)
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
    """The proxy's log as a list of lines. `Server.say` writes to whatever
    `server._log` is (xw11/server.py), which is a file on a real display and
    /dev/null in `ProxyRig`."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def carrying(self, needle):
        return [line for line in self.lines if needle in line]


def foot_window(wid=11, **kw):
    """The `foot -T WXL-Foot` of design section 9.3, at (10, 20) 300x200 --
    the rect every "the axis it left out" assertion below reads."""
    got = dict(title="WXL-Foot", class_="foot", instance="foot", pid=4242,
               x=10, y=20, w=300, h=200, visible=True, focused=True, desktop=0)
    got.update(kw)
    return fake_window(wid, **got)


def foot_backend(xid=0, **kw):
    win = foot_window()
    view = fake_view(win, xid=xid, app_id="foot", instance="foot", cls="foot")
    return FakeBackend(windows=[win], views=[view], **kw)


class WriteCase(unittest.TestCase):
    """One rig, one native toplevel, one client, and a log we can read."""

    num = 760
    upstream_num = 761

    def setUp(self):
        self.backend = self.make_backend()
        self.rig = _Rig(num=self.num, upstream_num=self.upstream_num,
                        passthrough=False, backend=self.backend)
        self.addCleanup(self.rig.stop)
        self.log = _Log()
        self.rig.server._log = self.log
        self.prepare_upstream(self.rig.upstream)
        self.conn = self.rig.conn()
        self.own = self.rig.wait_own()
        self.shadows = self.rig.server.shadows
        self.shadows.refresh()
        self.root = self.conn.root()
        native = [e for e in self.shadows.snapshot() if e.shadow]
        self.entry = native[0] if native else None
        self.handle = self.entry.handle if self.entry is not None else 0
        del self.backend.calls[:]
        self.backend.counts.clear()
        del self.rig.upstream.ops[:]

    def make_backend(self):
        return foot_backend()

    def prepare_upstream(self, upstream):
        """Whatever has to be true of the X server BEFORE the proxy's own
        connection opens, which is at the first accept (design section 2.6):
        `OwnConn` reads the root's `_NET_SUPPORTED` once, at open."""

    # -- the wire -------------------------------------------------------------

    def send(self, op, byte1=0, body=b""):
        """One void request, and a sync behind it: `GetInputFocus` is what
        libX11's own `XSync` sends, and its reply is what says the proxy has
        finished with everything before it."""
        self.conn._send(op, byte1, body)
        return self.sync()

    def send_capture(self, name, opcode, which=None):
        """Every frame of one opcode out of a capture, rewritten onto this rig
        and sent. `which` picks one of them."""
        cap = Capture(name)
        frames = cap.of(opcode)
        if which is not None:
            frames = [frames[which]]
        for frame in frames:
            self.send_frame(cap.rewrite(frame, self.root, self.shadow(),
                                        self.conn.atom))
        return cap, frames

    def send_frame(self, frame: bytes):
        """A captured frame, byte for byte: the opcode and second byte as they
        were, the body as it was. `_send` recomputes the length word, which for
        a well-formed capture is the word that was in it."""
        return self.send(frame[0], frame[1], frame[4:])

    def sync(self):
        return self.conn._wait_reply(self.conn._send(wire.OP_GET_INPUT_FOCUS, 0))

    def shadow(self):
        return self.entry.shadow

    def atom(self, name):
        got = self.own.atom_id(name)
        self.assertTrue(got, "the proxy never interned %s" % name)
        return got

    def calls(self, op):
        return [args for (name, args) in self.backend.calls if name == op]

    def get_property(self, win, atom, want_type=0, length=0xFFFFFFFF):
        """`(type atom, format, value)` for one property, the way xprop reads
        it: `AnyPropertyType`, offset 0, a ceiling for a length."""
        pkt, body = self.conn._wait_reply(
            self.conn._send(wire.OP_GET_PROPERTY, 0,
                            struct.pack("<IIIII", win, atom, want_type, 0,
                                        length)))
        type_atom, _after, nitems = struct.unpack_from("<III", pkt, 8)
        width = max(pkt[1] // 8, 1)
        return type_atom, pkt[1], bytes(body)[:nitems * width]

    def list_properties(self, win):
        pkt, body = self.conn._wait_reply(
            self.conn._send(wire.OP_LIST_PROPERTIES, 0, struct.pack("<I", win)))
        (count,) = struct.unpack_from("<H", pkt, 8)
        return list(struct.unpack_from("<%dI" % count, bytes(body), 0))

    def change_property(self, win, atom, type_atom, fmt, data, mode=0):
        units = len(data) // (fmt // 8)
        body = (struct.pack("<IIIBxxxI", win, atom, type_atom, fmt, units)
                + data + b"\0" * (-len(data) % 4))
        return self.send(wire.OP_CHANGE_PROPERTY, mode, body)


class ConfigureBits(WriteCase):
    """`ConfigureWindow`, from the bytes `xdotool` and `wmctrl` really send."""

    num, upstream_num = 762, 763

    def test_the_windowmove_capture_moves_the_window_to_its_two_values(self):
        """`xdotool windowmove 6291468 100 100` is one `ConfigureWindow` with
        `{x: 100, y: 100}` [M recon/tools/caps/xwl.jsonl MARK 10]."""
        _cap, frames = self.send_capture("xdotool-windowmove",
                                         wire.OP_CONFIGURE_WINDOW)
        self.assertEqual(len(frames), 1, "the capture holds one of them")
        self.assertEqual(self.calls("move_window"), [(self.handle, 100, 100)])
        self.assertEqual(self.calls("resize"), [])

    def test_the_windowsize_capture_resizes_and_does_not_move(self):
        """`windowsize 400 300` sets `{width, height}` and nothing else."""
        self.send_capture("xdotool-windowsize", wire.OP_CONFIGURE_WINDOW)
        self.assertEqual(self.calls("resize"), [(self.handle, 400, 300)])
        self.assertEqual(self.calls("move_window"), [])

    def test_the_windowraise_capture_raises(self):
        """`windowraise` is `{stack_mode: Above}`, which is 0 [MARK 12]."""
        self.send_capture("xdotool-windowraise", wire.OP_CONFIGURE_WINDOW)
        self.assertEqual(self.calls("raise_"), [(self.handle,)])

    def test_wmctrls_fallback_configure_moves_and_resizes_in_that_order(self):
        """`wmctrl -r <n> -e 0,10,10,300,200` against a bare Xwayland sends ONE
        `ConfigureWindow` with all four values -- wlroots' xwm does not name
        `_NET_MOVERESIZE_WINDOW` in `_NET_SUPPORTED` and wmctrl falls back
        [M recon/tools/caps/xwl.jsonl MARK 39; recon/env.md 2.1 for the missing
        atom]. Move first, then resize, which is the order the bits are in."""
        self.send_capture("wmctrl-r-e-configure", wire.OP_CONFIGURE_WINDOW)
        self.assertEqual([name for (name, _a) in self.backend.calls
                          if name in ("move_window", "resize")],
                         ["move_window", "resize"])
        self.assertEqual(self.calls("move_window"), [(self.handle, 10, 10)])
        self.assertEqual(self.calls("resize"), [(self.handle, 300, 200)])

    def test_x_alone_keeps_the_windows_current_y(self):
        """`WindowBackend.move_window` takes both axes, so a request that sets
        one carries the other out of the snapshot -- the foot is at y = 20."""
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(self.shadow(), x=250))
        self.assertEqual(self.calls("move_window"), [(self.handle, 250, 20)])

    def test_height_alone_keeps_the_windows_current_width(self):
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(self.shadow(), height=99))
        self.assertEqual(self.calls("resize"), [(self.handle, 300, 99)])

    def test_a_negative_coordinate_arrives_as_an_int16_in_a_card32(self):
        """libX11 writes the INT16 field into a CARD32 word, so -1 is
        0xFFFFFFFF on the wire and -1 in the call."""
        self.send(wire.OP_CONFIGURE_WINDOW, 0,
                  configure(self.shadow(), x=-1, y=-30))
        self.assertEqual(self.calls("move_window"), [(self.handle, -1, -30)])

    def test_stack_mode_below_lowers(self):
        self.send(wire.OP_CONFIGURE_WINDOW, 0,
                  configure(self.shadow(), stack_mode=req_write.STACK_BELOW))
        self.assertEqual(self.calls("lower"), [(self.handle,)])

    def test_stack_mode_opposite_makes_no_call_and_says_not_yet(self):
        """`TopIf`, `BottomIf` and `Opposite` stack against a sibling and no
        backend in the tree has that verb: one line, and it carries the route
        rather than a refusal."""
        self.send(wire.OP_CONFIGURE_WINDOW, 0,
                  configure(self.shadow(), stack_mode=4))
        self.assertEqual(self.backend.calls, [])
        said = self.log.carrying("Opposite")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("not yet", said[0])
        self.assertIn("rung 2", said[0])

    def test_border_width_alone_makes_no_call_and_one_line(self):
        """A Wayland toplevel has no client-owned border. Nothing measured
        sends this [recon/tools.md 4.3, 5]."""
        self.send(wire.OP_CONFIGURE_WINDOW, 0,
                  configure(self.shadow(), border_width=4))
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(self.log.carrying("border_width")), 1,
                         self.log.lines)

    def test_a_configure_on_a_shadow_costs_upstream_one_noop(self):
        """A request the proxy eats still costs one sequence number upstream,
        for ever [recon/wire.md 3.2a, 3.3's `swallow` row]."""
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(self.shadow(), x=1, y=2))
        self.assertEqual(self.rig.upstream.ops[:2],
                         [wire.OP_NO_OPERATION, wire.OP_GET_INPUT_FOCUS])


class MapUnmapFocus(WriteCase):
    """The three one-word requests, from their own captures."""

    num, upstream_num = 764, 765

    def test_the_windowmap_capture_maps(self):
        self.send_capture("xdotool-windowmap", wire.OP_MAP_WINDOW)
        self.assertEqual(self.calls("map"), [(self.handle,)])

    def test_the_windowunmap_capture_unmaps(self):
        self.send_capture("xdotool-windowunmap", wire.OP_UNMAP_WINDOW)
        self.assertEqual(self.calls("unmap"), [(self.handle,)])

    def test_the_windowfocus_capture_focuses_and_drops_revert_to_and_time(self):
        """`xdotool windowfocus` sends `revert_to = Parent, time = 0`
        [M MARK 9]. A compositor focuses a toplevel and has no parent to revert
        to, and no backend takes a timestamp: `focus` takes the window and
        nothing else."""
        _cap, frames = self.send_capture("xdotool-windowfocus",
                                         wire.OP_SET_INPUT_FOCUS)
        self.assertEqual(frames[0][1], 2, "revert_to Parent is byte 1")
        self.assertEqual(self.calls("focus"), [(self.handle,)])


class DestroyIsClose(WriteCase):
    """`xdotool windowclose` is `DestroyWindow` -- not `KillClient`, not
    `_NET_CLOSE_WINDOW` [recon/tools.md 4.3]."""

    num, upstream_num = 766, 767

    def test_the_windowclose_capture_closes_politely(self):
        self.send_capture("xdotool-windowclose", wire.OP_DESTROY_WINDOW)
        self.assertEqual(self.calls("close"), [(self.handle,)])
        self.assertEqual(self.calls("kill"), [])


class KillIsKill(WriteCase):
    """`KillClient` kills the client, which is `backend.kill` -- SIGKILL on the
    pid [recon/seams.md 2.1]. Nothing in the four tools sends it
    [recon/tools.md 3]; the issue names it."""

    num, upstream_num = 768, 769

    def test_kill_client_on_a_shadow_kills(self):
        self.send(wire.OP_KILL_CLIENT, 0, struct.pack("<I", self.shadow()))
        self.assertEqual(self.calls("kill"), [(self.handle,)])
        self.assertEqual(self.calls("close"), [])


class OverlayModes(WriteCase):
    """`ChangeProperty`'s three modes, read back through `GetProperty`."""

    num, upstream_num = 770, 771

    def test_replace_over_a_synthesized_wm_name_answers_the_new_bytes(self):
        """The overlay beats the synthesis for the same name (design section
        4.7): the shadow's `WM_NAME` is `WXL-Foot` until a client writes."""
        name = self.atom("WM_NAME")
        string = self.atom("STRING")
        self.assertEqual(self.get_property(self.shadow(), name)[2], b"WXL-Foot")
        self.change_property(self.shadow(), name, string, 8, b"written")
        self.assertEqual(self.get_property(self.shadow(), name),
                         (string, 8, b"written"))

    def test_prepend_and_append_run_against_the_current_value(self):
        """X appends to what is there, and what is there for a shadow is the
        synthesized title until somebody writes over it."""
        name = self.atom("WM_NAME")
        string = self.atom("STRING")
        self.change_property(self.shadow(), name, string, 8, b">>",
                             shadow_mod.PREPEND)
        self.assertEqual(self.get_property(self.shadow(), name)[2],
                         b">>WXL-Foot")
        self.change_property(self.shadow(), name, string, 8, b"<<",
                             shadow_mod.APPEND)
        self.assertEqual(self.get_property(self.shadow(), name)[2],
                         b">>WXL-Foot<<")

    def test_set_windows_two_writes_leave_net_wm_name_typed_string(self):
        """`xdotool set_window --name` writes `WM_NAME` and then
        `_NET_WM_NAME`, both `Replace`, both typed **STRING**
        [M recon/tools/caps/xwl.jsonl MARK 17]. The type is stored as given:
        the bug is xdotool's, and a proxy that corrected it would answer bytes
        no X server would have answered."""
        _cap, frames = self.send_capture("xdotool-set_window",
                                         wire.OP_CHANGE_PROPERTY)
        self.assertEqual(len(frames), 2, "WM_NAME and then _NET_WM_NAME")
        string = self.atom("STRING")
        self.assertEqual(self.get_property(self.shadow(), self.atom("WM_NAME")),
                         (string, 8, b"recon-renamed"))
        self.assertEqual(
            self.get_property(self.shadow(), self.atom("_NET_WM_NAME")),
            (string, 8, b"recon-renamed"))
        self.assertNotEqual(string, self.atom("UTF8_STRING"))

    def test_the_xprop_set_capture_writes_a_name_the_synthesis_never_had(self):
        """`xprop -id <w> -f RECON_TEST 8s -set RECON_TEST hello` is one
        `InternAtom` and one `ChangeProperty` [recon/tools.md 6, MARK 52]. A
        name only the overlay has is listed after the synthesized table, in
        write order (design section 4.7)."""
        self.send_capture("xprop-set", wire.OP_CHANGE_PROPERTY)
        atom = self.conn.atom("RECON_TEST")
        self.assertEqual(self.get_property(self.shadow(), atom),
                         (self.atom("STRING"), 8, b"hello"))
        self.assertEqual(self.list_properties(self.shadow())[-1], atom)

    def test_a_prepend_of_the_wrong_type_is_dropped_with_a_line(self):
        """A server answers `BadMatch` here. This proxy has no seam to answer
        an error on a request it consumes, so the write is dropped and the line
        says what would close the gap. Nothing measured sends one."""
        name = self.atom("WM_NAME")
        self.change_property(self.shadow(), name, self.atom("UTF8_STRING"), 8,
                             b">>", shadow_mod.PREPEND)
        self.assertEqual(self.get_property(self.shadow(), name)[2], b"WXL-Foot")
        said = self.log.carrying("BadMatch")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("not yet", said[0])


class DeleteHides(WriteCase):
    """`xprop -remove` on a shadow (design section 4.7's tombstone)."""

    num, upstream_num = 772, 773

    def delete_property(self, atom):
        return self.send(wire.OP_DELETE_PROPERTY, 0,
                         struct.pack("<II", self.shadow(), atom))

    def test_remove_makes_the_name_absent_and_unlisted(self):
        """A synthesized value cannot be deleted -- the next re-list builds it
        again -- so the overlay carries a tombstone for the life of the entry.
        An absent property is a REPLY and never an error: `type None(0),
        format 0, nitems 0` [recon/env.md 2.7's measured shape]."""
        name = self.atom("WM_NAME")
        self.assertIn(name, self.list_properties(self.shadow()))
        self.delete_property(name)
        self.assertEqual(self.get_property(self.shadow(), name), (0, 0, b""))
        self.assertNotIn(name, self.list_properties(self.shadow()))

    def test_a_replace_after_the_tombstone_brings_the_name_back(self):
        name = self.atom("WM_NAME")
        self.delete_property(name)
        self.change_property(self.shadow(), name, self.atom("STRING"), 8,
                             b"again")
        self.assertEqual(self.get_property(self.shadow(), name)[2], b"again")
        self.assertIn(name, self.list_properties(self.shadow()))

    def test_the_xprop_remove_capture_deletes_the_name_xprop_set_wrote(self):
        """The pair as the tool sends it [MARK 52, 53]: `-set` then `-remove`
        on a name the synthesis never had."""
        self.send_capture("xprop-set", wire.OP_CHANGE_PROPERTY)
        atom = self.conn.atom("RECON_TEST")
        self.send_capture("xprop-remove", wire.OP_DELETE_PROPERTY)
        self.assertEqual(self.get_property(self.shadow(), atom), (0, 0, b""))
        self.assertNotIn(atom, self.list_properties(self.shadow()))

    def test_deleting_a_name_the_window_never_had_writes_no_tombstone(self):
        """A real server sends no `PropertyNotify` for a `DeleteProperty` on a
        name the window does not have, and a tombstone would hide the synthesis
        on the strength of a request that did nothing."""
        self.delete_property(self.atom("_NET_WM_ICON_NAME"))
        self.assertEqual(self.entry.overlay, {})


class RootPropertyWrites(WriteCase):
    """`ChangeProperty` on the ROOT is PASS (design section 3.2) and is still
    handed to a handler, so that a client writing a name the proxy answers from
    the compositor leaves a line."""

    num, upstream_num = 782, 783

    def test_a_write_of_an_override_name_is_forwarded_and_logged(self):
        """No `written_root` state: the write reaches upstream and the READ
        still answers the compositor's own number, because the compositor is
        the single source for that name (design section 4.6). The line is what
        makes that visible to whoever is debugging a pager."""
        self.change_property(self.root, self.atom("_NET_CLIENT_LIST"),
                             self.atom("WINDOW"), 32, struct.pack("<I", 42))
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_CHANGE_PROPERTY)
        said = self.log.carrying("_NET_CLIENT_LIST")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("(root,", said[0])

    def test_the_compositors_answer_is_still_what_a_reader_gets(self):
        """The proxy answers `_NET_CLIENT_LIST` from the registry whether or
        not upstream has it -- sway DELETES the property outright when no X
        client is mapped [recon/env.md 2.7] -- so a client's write to the root
        changes what upstream holds and not what a reader reads."""
        self.change_property(self.root, self.atom("_NET_CLIENT_LIST"),
                             self.atom("WINDOW"), 32, struct.pack("<I", 42))
        self.assertEqual(
            self.get_property(self.root, self.atom("_NET_CLIENT_LIST"))[2],
            struct.pack("<I", self.shadow()))

    def test_a_write_of_any_other_name_is_forwarded_in_silence(self):
        """A client's own root property is its own business, and resolving
        every atom a client writes would be a `GetAtomName` round trip on the
        loop thread per write."""
        self.change_property(self.root, self.conn.atom("XW11_SOMETHING"),
                             self.atom("STRING"), 8, b"hello")
        self.assertEqual(self.rig.upstream.ops[-2:],
                         [wire.OP_CHANGE_PROPERTY, wire.OP_GET_INPUT_FOCUS])
        self.assertEqual(self.log.carrying("(root,"), [])


class MaskRecorded(WriteCase):
    """`ChangeWindowAttributes`, the one request that says who wants events."""

    num, upstream_num = 774, 775

    #: `xprop -spy` selects `StructureNotify|PropertyChange` on its target and
    #: blocks [M recon/tools.md 6, `caps/events2.jsonl`].
    SPY_MASK = 0x420000

    def make_backend(self):
        """A second toplevel that IS an Xwayland window, so the third case has
        a real id to aim at. The pairing is `View.xid` and the proxy repeats no
        matching (design section 4.2)."""
        foot = foot_window(11)
        xterm = fake_window(12, title="WXL-Xterm", class_="XTerm",
                            instance="xterm", pid=99, x=0, y=0, w=800, h=600)
        return FakeBackend(
            windows=[foot, xterm],
            views=[fake_view(foot, xid=0, app_id="foot", instance="foot",
                             cls="foot"),
                   fake_view(xterm, xid=0x40000C, instance="xterm",
                             cls="XTerm")])

    def select(self, win, mask):
        return self.send(wire.OP_CHANGE_WINDOW_ATTRIBUTES, 0,
                         struct.pack("<III", win, wire.CW_EVENT_MASK, mask))

    def server_conn(self):
        self.rig.wait(lambda: bool(self.rig.server.conns), what="the client")
        return self.rig.server.conns[0]

    def test_a_mask_on_a_shadow_is_recorded_and_the_request_is_eaten(self):
        self.select(self.shadow(), self.SPY_MASK)
        self.assertEqual(self.server_conn().masks.get(self.shadow()),
                         self.SPY_MASK)
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_NO_OPERATION)

    def test_a_mask_on_the_root_is_forwarded_and_recorded(self):
        """Upstream keeps delivering its own root events for the X plane and
        the proxy still has to know who wants the ones it synthesizes
        (design section 3.2)."""
        self.select(self.root, self.SPY_MASK)
        self.assertEqual(self.server_conn().masks.get(self.root), self.SPY_MASK)
        self.assertEqual(self.rig.upstream.ops[0],
                         wire.OP_CHANGE_WINDOW_ATTRIBUTES)

    def test_a_mask_on_a_real_x_window_is_forwarded_and_not_recorded(self):
        """That row is PASS: upstream's own bookkeeping is the truth for a
        window upstream owns."""
        self.select(0x40000C, self.SPY_MASK)
        self.assertEqual(self.server_conn().masks, {})
        self.assertEqual(self.rig.upstream.ops[0],
                         wire.OP_CHANGE_WINDOW_ATTRIBUTES)

    def test_the_event_mask_is_read_from_its_own_slot_in_bit_order(self):
        """`CWEventMask` is bit 11 and the value list carries one CARD32 per
        set bit BELOW it [recon/wire.md 4.2]: a handler that read slot 0 would
        record the backing-store value here."""
        mask = 0x0800 | 0x0040 | 0x0002        # EventMask, BackingStore, BorderPixel
        body = struct.pack("<IIIII", self.shadow(), mask, 0x11111111,
                           0x22222222, self.SPY_MASK)
        self.send(wire.OP_CHANGE_WINDOW_ATTRIBUTES, 0, body)
        self.assertEqual(self.server_conn().masks.get(self.shadow()),
                         self.SPY_MASK)


class HeldNothing(WriteCase):
    """Every write on a REAL X id passes untouched -- the baseline the parity
    oracle measures [design section 9.3's "the X twin is untouched"]."""

    num, upstream_num = 776, 777

    def make_backend(self):
        win = foot_window(12, title="WXL-Xterm", class_="XTerm",
                          instance="xterm")
        return FakeBackend(windows=[win],
                           views=[fake_view(win, xid=0x40000C,
                                            instance="xterm", cls="XTerm")])

    def setUp(self):
        super().setUp()
        self.entry = self.shadows.by_xid[0x40000C]

    def test_set_input_focus_on_a_real_id_reaches_upstream_itself(self):
        self.send(wire.OP_SET_INPUT_FOCUS, 2, struct.pack("<II", 0x40000C, 0))
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SET_INPUT_FOCUS)
        self.assertEqual(self.backend.calls, [])

    def test_configure_window_on_a_real_id_reaches_upstream_itself(self):
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(0x40000C, x=5, y=6))
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_CONFIGURE_WINDOW)
        self.assertEqual(self.backend.calls, [])


class RefusalIsSilent(WriteCase):
    """Design section 3.3: a backend refusal on a CONSUMEd request is silence
    on the wire and one line in the log."""

    num, upstream_num = 778, 779

    def test_a_refused_move_answers_no_packet_and_logs_the_sentence(self):
        """X gives a client no error when a redirecting window manager ignores
        its `ConfigureWindow` -- the request is void and the window manager did
        what it wanted. The clone's own sentence is what the line carries."""
        self.backend.raise_on("move_window", CmdError("sway said no to that"))
        self.send(wire.OP_CONFIGURE_WINDOW, 0,
                  configure(self.shadow(), x=1, y=2))
        said = self.log.carrying("sway said no to that")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("silence on the wire", said[0])

    def test_an_unsupported_operation_says_not_yet_and_names_the_rung(self):
        """`WindowBackend._unsupported` raises a `CmdError` with
        `.unsupported` (wdotool/backend.py:280). The gap is ours, so the line
        carries the route and never a policy."""
        err = CmdError("windowstate is not supported by this compositor")
        err.unsupported = True
        self.backend.raise_on("minimize", err)
        self.send_client_message("WM_CHANGE_STATE", self.shadow(), [3, 0, 0, 0, 0])
        said = self.log.carrying("cannot minimize")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("not yet", said[0])
        self.assertIn("rung", said[0])

    def test_the_refusal_still_costs_upstream_exactly_one_noop(self):
        self.backend.raise_on("move_window", CmdError("no"))
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(self.shadow(), x=1))
        self.assertEqual(self.rig.upstream.ops[:2],
                         [wire.OP_NO_OPERATION, wire.OP_GET_INPUT_FOCUS])

    def test_the_negative_twin_an_error_packet_fails_the_clients_next_sync(self):
        """The proof that "no packet" is a claim and not a tautology: with a
        handler that answers an error for the same request, the client's next
        sync raises instead of returning. `_wait_reply` raises `X11Error` for
        any error packet, which is Xlib's own default handler's input
        [recon/tools.md 9]."""
        def answer_an_error(server, conn, req):
            conn.out_down += wire.error(wire.ERR_WINDOW, req.seq, req.xid,
                                        req.opcode)
            server.pump(conn)
            return None

        self.rig.server.handlers[wire.OP_CONFIGURE_WINDOW] = answer_an_error
        self.conn._send(wire.OP_CONFIGURE_WINDOW, 0, configure(self.shadow(), x=1))
        with self.assertRaises(X11Error):
            self.sync()

    def send_client_message(self, name, window, data):
        atom = self.atom(name)
        ev = (struct.pack("<BBHII", wire.EV_CLIENT_MESSAGE, 32, 0, window, atom)
              + struct.pack("<5I", *data))
        return self.send(wire.OP_SEND_EVENT, 0,
                         struct.pack("<II", self.root, 0x180000) + ev)


class SettleArmed(WriteCase):
    """Design section 5.2's settle poll: a CONSUMEd write re-lists at +50 ms
    and +250 ms, because sway emits NOTHING for a floating `move position`
    [recon/seams.md 2.4] and the `ConfigureNotify` after `xdotool windowmove`
    has to come from the diff of two listings."""

    num, upstream_num = 780, 781

    def make_backend(self):
        """The foot plus a paired xterm, so "a write on a REAL id" is a write
        on an id the registry knows is upstream's."""
        foot = foot_window(11)
        xterm = fake_window(12, title="WXL-Xterm", class_="XTerm",
                            instance="xterm", pid=99, x=0, y=0, w=800, h=600)
        return FakeBackend(
            windows=[foot, xterm],
            views=[fake_view(foot, xid=0, app_id="foot", instance="foot",
                             cls="foot"),
                   fake_view(xterm, xid=0x40000C, instance="xterm",
                             cls="XTerm")])

    def setUp(self):
        super().setUp()
        # The two real numbers are 50 ms and 250 ms; the claim is the ARMING,
        # so the test patches them short and asserts the call count rather than
        # the clock.
        self.rig.server.settle_delays = (0.01, 0.03)

    def test_a_consumed_move_re_lists_twice_after_the_write(self):
        before = self.backend.counts["views"]
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(self.shadow(), x=1, y=2))
        self.rig.wait(lambda: self.backend.counts["views"] >= before + 2,
                      timeout=2.0, what="the two settle polls")
        self.assertFalse(self.rig.server.settles, "a poll was left armed")

    def test_a_write_on_a_real_id_arms_nothing(self):
        """It is upstream's window and upstream's `ConfigureNotify`; the proxy
        re-lists for its own plane only."""
        self.send(wire.OP_CONFIGURE_WINDOW, 0, configure(0x40000C, x=1))
        self.assertEqual(self.rig.server.settles, [])

    def test_four_due_polls_cost_one_re_list_and_leave_the_queue_empty(self):
        """A command that sent four writes armed eight polls and they all mean
        the same thing: read the compositor again. The loop's own call is
        stubbed out for the length of this test so that what runs the queue is
        this thread and the count is not a race with it."""
        server = self.rig.server
        server.run_settles = lambda: 0
        self.addCleanup(lambda: server.__dict__.pop("run_settles", None))
        server.settle(1)
        server.settle(2)
        self.assertEqual([xid for _when, xid in server.settles], [1, 2, 1, 2])
        deadlines = [when for when, _xid in server.settles]
        self.assertEqual(deadlines, sorted(deadlines))
        before = self.backend.counts["views"]
        time.sleep(0.05)
        self.assertEqual(server_mod.Server.run_settles(server), 4)
        self.assertEqual(self.backend.counts["views"], before + 1)
        self.assertEqual(server.settles, [])


if __name__ == "__main__":
    unittest.main()
