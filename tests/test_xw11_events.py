#!/usr/bin/env python3
"""xw11/events.py: the 32-byte packets, who gets them, and the sequence they carry.

The three claims this file is built around, each measured:

* **the bytes are the protocol's own.** Every builder is compared with a layout
  rendered from `/usr/share/xcb/xproto.xml` and pinned in `LAYOUTS` below --
  the same table recon/wire.md 4.4 lists for the six it names, plus the five it
  does not (`EnterNotify`, `LeaveNotify`, `MotionNotify`, `FocusIn`,
  `FocusOut`) read off the XML here. `Layouts` re-derives the table from the
  XML when the file is on the box, so a hand-typed offset cannot rot.
* **an event carries the RECEIVING client's sequence.** It is a watermark of
  that connection's request stream, not a number of its own: seven
  `NoOperation`s and a `ChangeProperty` gave 61, 69 and 77 on three runs
  [M recon/wire.md 3.2b]. Two clients that have sent different numbers of
  requests get the same change with different bytes 2-3.
* **the diff is the truth, and the token is a hint.** sway emits `new`, `title`
  and `focus` for one `exec foot` inside 75 ms [recon/seams.md 2.3; +17.5 and
  +19.2 ms on this box, 2026-09-10] and NOTHING AT ALL for a floating
  `move position` [recon/seams.md 2.4; confirmed here, ten moves, zero tokens],
  so a `title` token whose re-list shows no title change produces nothing and a
  move with no token produces a `ConfigureNotify` (`DiffIsTruth`, `SettlePoll`).

`tools.md 11` rows 29 and 51 -- `xdotool behave <w> mouse-enter true` and
`xprop -spy -root _NET_ACTIVE_WINDOW` -- exit 124 under a timeout on both rigs,
having sent 32 and 14 requests and then blocked. Those two streams are replayed
here from `tests/fixtures/xw11/caps/{behave,spy-root,spy-id}.hex`;
`tests/test_xw11_live.py` runs the binaries themselves.
"""

import os
import re
import struct
import sys
import time
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`, and
# it is set BEFORE the imports because a tool module reads it at import time.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import FakeBackend, fake_view, fake_window, _recvn     # noqa: E402
from xw11 import events as events_mod                               # noqa: E402
from xw11 import policy, pump as pump_mod, wire                     # noqa: E402


CAPS = os.path.join(ROOT, "tests", "fixtures", "xw11", "caps")
XPROTO = "/usr/share/xcb/xproto.xml"

#: Every event this proxy builds, at the offsets `/usr/share/xcb/xproto.xml`
#: gives them. Byte 0 is the code and bytes 2-3 are the sequence in all eleven
#: [recon/wire.md 4.4]; a field at offset 1 is the event's `detail` byte, and a
#: layout without one has a pad there.
LAYOUTS = {
    6: ("MotionNotify",
        (("detail", "B", 1), ("time", "I", 4), ("root", "I", 8),
         ("event", "I", 12), ("child", "I", 16), ("root_x", "h", 20),
         ("root_y", "h", 22), ("event_x", "h", 24), ("event_y", "h", 26),
         ("state", "H", 28), ("same_screen", "B", 30))),
    7: ("EnterNotify",
        (("detail", "B", 1), ("time", "I", 4), ("root", "I", 8),
         ("event", "I", 12), ("child", "I", 16), ("root_x", "h", 20),
         ("root_y", "h", 22), ("event_x", "h", 24), ("event_y", "h", 26),
         ("state", "H", 28), ("mode", "B", 30), ("same_screen_focus", "B", 31))),
    8: ("LeaveNotify",
        (("detail", "B", 1), ("time", "I", 4), ("root", "I", 8),
         ("event", "I", 12), ("child", "I", 16), ("root_x", "h", 20),
         ("root_y", "h", 22), ("event_x", "h", 24), ("event_y", "h", 26),
         ("state", "H", 28), ("mode", "B", 30), ("same_screen_focus", "B", 31))),
    9: ("FocusIn", (("detail", "B", 1), ("event", "I", 4), ("mode", "B", 8))),
    10: ("FocusOut", (("detail", "B", 1), ("event", "I", 4), ("mode", "B", 8))),
    16: ("CreateNotify",
         (("parent", "I", 4), ("window", "I", 8), ("x", "h", 12),
          ("y", "h", 14), ("width", "H", 16), ("height", "H", 18),
          ("border_width", "H", 20), ("override_redirect", "B", 22))),
    17: ("DestroyNotify", (("event", "I", 4), ("window", "I", 8))),
    18: ("UnmapNotify",
         (("event", "I", 4), ("window", "I", 8), ("from_configure", "B", 12))),
    19: ("MapNotify",
         (("event", "I", 4), ("window", "I", 8),
          ("override_redirect", "B", 12))),
    22: ("ConfigureNotify",
         (("event", "I", 4), ("window", "I", 8), ("above_sibling", "I", 12),
          ("x", "h", 16), ("y", "h", 18), ("width", "H", 20),
          ("height", "H", 22), ("border_width", "H", 24),
          ("override_redirect", "B", 26))),
    28: ("PropertyNotify",
         (("window", "I", 4), ("atom", "I", 8), ("time", "I", 12),
          ("state", "B", 16))),
}


#: code by name, out of the pinned table: a test that asserted `events.X` was
#: the code it got would be asking the module under test what it meant.
CODES = {name: code for code, (name, _fields) in LAYOUTS.items()}


def decode(pkt):
    """One event packet as `(name, {field: value})`, read at `LAYOUTS`' offsets
    rather than at the builder's, so a builder that moved a field fails here."""
    code = pkt[0] & 0x7F
    name, fields = LAYOUTS[code]
    got = {"seq": struct.unpack_from("<H", pkt, 2)[0]}
    for field, kind, at in fields:
        (got[field],) = struct.unpack_from("<" + kind, pkt, at)
    return name, got


def kinds(evs):
    """`[(code name, target window, mask bit)]` for a list of `Event`s -- what
    a test asserts on when the order and the routing are the claim."""
    return [(decode(e.packet)[0], e.target, e.bit) for e in evs]


def foot(wid=11, **kw):
    """The `foot -T WXL-Foot` of design section 9.3, at (10, 20) 300x200."""
    got = dict(title="WXL-Foot", class_="foot", instance="foot", pid=4242,
               x=10, y=20, w=300, h=200, visible=True, focused=True, desktop=0)
    got.update(kw)
    return fake_window(wid, **got)


def backend_of(windows, xids=None):
    """A `FakeBackend` whose `views()` pairs each window with the X id in
    `xids` -- 0 (a native toplevel, which gets a shadow) unless named. The
    pairing is `View.xid` and the proxy repeats no matching
    [recon/seams.md 3]."""
    xids = xids or {}
    views = [fake_view(w, xid=xids.get(w.id, 0), app_id=w.class_,
                       instance=w.instance, cls=w.class_) for w in windows]
    return FakeBackend(windows=list(windows), views=views)


class _Upstream(support.FakeUpstream):
    """`FakeUpstream` with the protocol's own ids for the predefined atoms, so
    `WM_NAME` is 39 and not 100 [recon/wire.md 7.2]."""

    def intern(self, name: str) -> int:
        fixed = policy.PREDEFINED_ATOMS.get(name)
        if fixed and name not in self._atoms:
            self._atoms[name] = fixed
            self._names[fixed] = name
        return super().intern(name)


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


class Watcher:
    """One client on the wire: the setup done by `ProxyRig.raw`, its own
    requests counted, and every event packet the proxy wrote to it kept.

    A raw socket and not `x11_mini.X11Conn` because what is under test is the
    BYTES -- `next_event()` parses three of the eleven codes and throws the
    rest away -- and because two watchers with different request counts are how
    `EventSeqIsClientSeq` is written.
    """

    def __init__(self, rig, timeout=5.0):
        self.sock, self.setup = rig.raw(timeout=timeout)
        self.seq = 0
        self.events = []
        #: every packet's code in arrival ORDER, replies included: what says a
        #: property event was written before the reply of the request that
        #: caused it, which is "at once, as the server does" (design section
        #: 5.3's last row).
        self.order = []

    def send(self, op, byte1=0, body=b""):
        body = body + b"\0" * (-len(body) % 4)
        self.sock.sendall(struct.pack("<BBH", op, byte1, 1 + len(body) // 4)
                          + body)
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq

    def select(self, win, mask):
        """`ChangeWindowAttributes(CWEventMask)`: the request `xprop -spy` and
        `xdotool behave` end their prologue with [M recon/tools.md 6, 4.7]."""
        return self.send(wire.OP_CHANGE_WINDOW_ATTRIBUTES, 0,
                         struct.pack("<III", win, wire.CW_EVENT_MASK, mask))

    def noop(self, n=1):
        """`NoOperation` consumes a sequence number like anything else
        [M recon/wire.md 3.2a], which is how two clients here come to be at
        different counts."""
        for _i in range(n):
            self.send(wire.OP_NO_OPERATION)

    def sync(self):
        """`GetInputFocus` and read until its reply: everything the proxy had
        written for us is in front of it, so a test that syncs and then looks
        at `events` is not racing the loop."""
        want = self.send(wire.OP_GET_INPUT_FOCUS)
        while True:
            pkt = self.packet()
            if pkt[0] == 1 and struct.unpack_from("<H", pkt, 2)[0] == want:
                return

    def packet(self):
        """One packet off the socket; an event is kept as well as returned."""
        head = _recvn(self.sock, 32)
        if head[0] in (1, 35):                  # a reply, or a GenericEvent
            (extra,) = struct.unpack_from("<I", head, 4)
            if extra:
                head += _recvn(self.sock, extra * 4)
        elif head[0] != 0:
            self.events.append(bytes(head))
        self.order.append(head[0] & 0x7F)
        return bytes(head)

    def wait_for(self, code, timeout=5.0, extra=None):
        """The first event with that code, waited for rather than assumed: the
        proxy is a loop on another thread and the compositor's change reaches
        it after the request that caused it was answered."""
        deadline = time.monotonic() + timeout
        while True:
            for pkt in self.events:
                if pkt[0] & 0x7F != code:
                    continue
                if extra is not None and not extra(decode(pkt)[1]):
                    continue
                return pkt
            if time.monotonic() > deadline:
                raise AssertionError(
                    "no event %d in %gs; got %r"
                    % (code, timeout, [p[0] & 0x7F for p in self.events]))
            self.sock.settimeout(0.05)
            try:
                self.packet()
            except (TimeoutError, OSError):
                pass
            finally:
                self.sock.settimeout(5.0)

    def drain(self, seconds=1.0):
        """Whatever arrives within `seconds` and is not a reply. Used only
        where the claim is that NOTHING arrives."""
        deadline = time.monotonic() + seconds
        self.sock.settimeout(0.05)
        try:
            while time.monotonic() < deadline:
                try:
                    self.packet()
                except (TimeoutError, OSError):
                    pass
        finally:
            self.sock.settimeout(5.0)
        return self.events

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def capture(name, opcode):
    """`(root, target, [frame])` for one opcode out of a `caps/*.hex` fixture.

    The fixtures are the request streams the real tools sent to a live Xwayland
    under headless sway, rebuilt from the decoded capture by
    scratchpad/b5/mkcaps_events.py; the counts are recon/tools.md 11's rows 29
    (behave, 32 requests) and 51 (spy -root, 14), and recon/tools.md 6 for the
    `-spy -id` stream.
    """
    root = target = 0
    frames = []
    with open(os.path.join(CAPS, name + ".hex"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("# ROOT 0x"):
                root = int(line[9:], 16)
            elif line.startswith("# TARGET 0x"):
                target = int(line[11:], 16)
            elif line and not line.startswith("#"):
                frame = bytes.fromhex(line.split("#")[0].strip())
                if frame[0] == opcode:
                    frames.append(frame)
    assert frames, "no opcode %d in the %s capture" % (opcode, name)
    return root, target, frames


# -- the builders --------------------------------------------------------------


class EventBytes(unittest.TestCase):
    """Each builder against its pinned layout, field by field."""

    def one(self, packet, code, **want):
        name, got = decode(packet)
        self.assertEqual(packet[0], code, "%s: wrong code" % name)
        self.assertEqual(len(packet), 32)
        self.assertEqual(got["seq"], 0, "the builder must not invent a "
                                        "sequence: delivery patches it")
        for field, value in want.items():
            self.assertEqual(got[field], value, "%s.%s" % (name, field))

    def test_create_notify(self):
        self.one(events_mod.create_notify(0x2A, 0x600001, 10, 20, 300, 200),
                 16, parent=0x2A, window=0x600001, x=10, y=20, width=300,
                 height=200, border_width=0, override_redirect=0)

    def test_create_notify_carries_a_negative_x_as_int16(self):
        """`x`/`y` are INT16 and a window off the left of the screen is
        ordinary on a multi-head layout [recon/wire.md 4.4]."""
        self.one(events_mod.create_notify(1, 2, -20, -1, 4, 5), 16, x=-20, y=-1)

    def test_a_rect_past_the_ends_of_the_wire_fields_is_truncated(self):
        """A compositor's numbers are not bounded by the protocol's fields, and
        a builder that raised `struct.error` would take the exception out of
        the selector loop. The same rule the read side already truncates by
        (`req_read._clamp16`, pinned by
        `test_xw11_read.py::GeometryAttributesCoordinates`), because the
        `ConfigureNotify` and the `GetGeometry` for one window must agree."""
        self.one(events_mod.configure_notify(1, 2, 40000, -40000, 70000, -3),
                 22, x=40000 - 0x10000, y=0x10000 - 40000,
                 width=70000 - 0x10000, height=0)
        self.one(events_mod.create_notify(1, 2, 40000, -40000, 70000, -3),
                 16, x=40000 - 0x10000, y=0x10000 - 40000,
                 width=70000 - 0x10000, height=0)

    def test_destroy_notify(self):
        self.one(events_mod.destroy_notify(0x2A, 0x600001), 17,
                 event=0x2A, window=0x600001)

    def test_unmap_notify(self):
        self.one(events_mod.unmap_notify(0x2A, 0x600001), 18,
                 event=0x2A, window=0x600001, from_configure=0)

    def test_map_notify(self):
        self.one(events_mod.map_notify(0x2A, 0x600001), 19,
                 event=0x2A, window=0x600001, override_redirect=0)

    def test_configure_notify(self):
        self.one(events_mod.configure_notify(0x2A, 0x600001, 10, 20, 300, 200),
                 22, event=0x2A, window=0x600001, above_sibling=0, x=10, y=20,
                 width=300, height=200, border_width=0, override_redirect=0)

    def test_property_notify(self):
        self.one(events_mod.property_notify(0x600001, 39), 28,
                 window=0x600001, atom=39, time=0, state=0)

    def test_property_notify_deleted(self):
        self.one(events_mod.property_notify(0x600001, 39, events_mod.DELETED),
                 28, state=1)

    def test_focus_in(self):
        self.one(events_mod.focus_in(0x600001), 9, detail=3, event=0x600001,
                 mode=0)

    def test_focus_out(self):
        """xproto.xml declares `FocusOut` an `<eventcopy>` of `FocusIn`: the
        same layout, a different code [recon/wire.md 4.4]."""
        self.one(events_mod.focus_out(0x600001), 10, detail=3,
                 event=0x600001, mode=0)
        self.assertEqual(events_mod.focus_in(7)[1:],
                         events_mod.focus_out(7)[1:])

    def test_enter_notify(self):
        """Byte 31 is a bitmask and its default here is `same-screen` alone:
        bit 1 (`ELFlagSameScreen`) set because this proxy has one screen, bit 0
        (`ELFlagFocus`) clear unless the window named is the focused one. A `1`
        there -- the plain value design section 5.6 was written with -- tells an
        Xlib client the crossing happened on ANOTHER screen."""
        self.one(events_mod.enter_notify(0x600001, 0x2A, root_x=120,
                                         root_y=140, event_x=110, event_y=120),
                 7, detail=3, root=0x2A, event=0x600001, child=0, root_x=120,
                 root_y=140, event_x=110, event_y=120, state=0, mode=0,
                 same_screen_focus=2)
        self.one(events_mod.enter_notify(0x600001, 0x2A, same_screen_focus=3),
                 7, same_screen_focus=3)

    def test_leave_notify(self):
        self.one(events_mod.leave_notify(0x600001, 0x2A, root_x=1, root_y=2),
                 8, detail=3, root=0x2A, event=0x600001, root_x=1, root_y=2,
                 mode=0, same_screen_focus=2)

    def test_motion_notify(self):
        self.one(events_mod.motion_notify(0x600001, 0x2A, root_x=5, root_y=6,
                                          event_x=1, event_y=2),
                 6, detail=0, root=0x2A, event=0x600001, root_x=5, root_y=6,
                 event_x=1, event_y=2, same_screen=1)


class ThirtyTwoBytesNeverEleven(unittest.TestCase):
    ALL = (
        lambda: events_mod.create_notify(1, 2, 3, 4, 5, 6),
        lambda: events_mod.destroy_notify(1, 2),
        lambda: events_mod.unmap_notify(1, 2),
        lambda: events_mod.map_notify(1, 2),
        lambda: events_mod.configure_notify(1, 2, 3, 4, 5, 6),
        lambda: events_mod.property_notify(1, 2),
        lambda: events_mod.focus_in(1),
        lambda: events_mod.focus_out(1),
        lambda: events_mod.enter_notify(1, 2),
        lambda: events_mod.leave_notify(1, 2),
        lambda: events_mod.motion_notify(1, 2),
    )

    def test_every_builder_is_thirty_two_bytes(self):
        self.assertEqual(len(self.ALL), 11)
        for build in self.ALL:
            self.assertEqual(len(build()), 32)

    def test_no_builder_writes_code_eleven(self):
        """`KeymapNotify` (11) is the one core event with no sequence field --
        bytes 1..31 are the keymap [recon/wire.md 3.1] -- so a proxy that built
        one and then patched bytes 2-3 would corrupt two keycodes' worth of
        it."""
        for build in self.ALL:
            self.assertNotEqual(build()[0] & 0x7F, events_mod.KEYMAP_NOTIFY)

    def test_no_builder_sets_the_send_event_bit(self):
        """`0x80` in byte 0 means a client sent it with `SendEvent`
        [recon/wire.md 4.4]; these come from the server the proxy stands in
        for."""
        for build in self.ALL:
            self.assertFalse(build()[0] & 0x80)


class Layouts(unittest.TestCase):
    """`LAYOUTS` against `/usr/share/xcb/xproto.xml` itself."""

    SIZES = {"BYTE": 1, "CARD8": 1, "BOOL": 1, "CARD16": 2, "INT16": 2,
             "CARD32": 4, "INT32": 4, "WINDOW": 4, "ATOM": 4, "TIMESTAMP": 4}

    def parse(self, text, name):
        """The offsets one `<event>` declares. Byte 0 is the code and bytes 2-3
        the sequence, so the cursor starts at 1 and jumps to 4 -- which is what
        makes a one-byte first field the `detail` byte and everything else a
        pad."""
        body = re.search(r'<event name="%s" number="(\d+)"(.*?)</event>'
                         % name, text, re.S)
        code = int(body.group(1))
        inner = re.sub(r"<doc>.*?</doc>", "", body.group(2), flags=re.S)
        at, got = 1, []
        for kind, field, pad in re.findall(
                r'<field type="(\w+)" name="(\w+)"|<pad bytes="(\d+)"', inner):
            if pad:
                at += int(pad)
            else:
                got.append((field, at))
                at += self.SIZES[kind]
            if at == 2:
                at = 4
        return code, got

    def test_every_pinned_layout_is_the_xml_file_s(self):
        if not os.path.exists(XPROTO):
            self.skipTest("no %s on this box" % XPROTO)
        with open(XPROTO, encoding="utf-8") as fh:
            text = fh.read()
        seen = 0
        for code, (name, fields) in sorted(LAYOUTS.items()):
            if name in ("LeaveNotify", "FocusOut"):
                continue                # <eventcopy>, no <event> of their own
            got_code, got = self.parse(text, name)
            self.assertEqual(got_code, code, name)
            self.assertEqual([(f, at) for f, _k, at in fields], got, name)
            seen += 1
        self.assertEqual(seen, 9)

    def test_the_two_eventcopies_are_declared_as_copies(self):
        if not os.path.exists(XPROTO):
            self.skipTest("no %s on this box" % XPROTO)
        with open(XPROTO, encoding="utf-8") as fh:
            text = fh.read()
        for name, ref, code in (("LeaveNotify", "EnterNotify", 8),
                                ("FocusOut", "FocusIn", 10)):
            got = re.search(r'<eventcopy name="%s" number="(\d+)" '
                            r'ref="%s"' % (name, ref), text)
            self.assertIsNotNone(got, name)
            self.assertEqual(int(got.group(1)), code)


# -- what a change becomes (design section 5.3) --------------------------------


class EventsCase(unittest.TestCase):
    """One rig, one native toplevel, and the registry the diff comes out of.

    The pump is NOT armed in these: nothing has selected a mask, so the loop
    thread re-lists for nobody and the diff a test drives from here is the diff
    that test caused. `Delivery` below is where the loop and the sockets are.
    """

    num, upstream_num = 780, 781

    def make_windows(self):
        return [foot()]

    def xids(self):
        return {}

    def setUp(self):
        self.backend = backend_of(self.make_windows(), self.xids())
        self.rig = _Rig(num=self.num, upstream_num=self.upstream_num,
                        passthrough=False, backend=self.backend)
        self.addCleanup(self.rig.stop)
        self.log = _Log()
        self.rig.server._log = self.log
        self.conn = self.rig.conn()
        self.own = self.rig.wait_own()
        self.server = self.rig.server
        self.shadows = self.server.shadows
        self.shadows.refresh()
        self.root = self.conn.root()
        native = [e for e in self.shadows.snapshot() if e.shadow]
        self.entry = native[0] if native else None

    # -- the compositor's next answer -----------------------------------------

    def set_windows(self, windows, xids=None):
        """What `list()`/`views()` answer from now on. A whole new list, never a
        mutation of the old one: `FakeBackend` hands out copies precisely so
        that a previous listing cannot change under the registry's feet."""
        got = backend_of(windows, xids)
        self.backend.windows = got.windows
        self.backend.views_ = got.views_

    def changes(self):
        """One re-list and its diff -- what `Server.drain_pump` and
        `Server.run_settles` both do."""
        self.shadows.invalidate()
        return self.shadows.refresh()

    def evs(self):
        return events_mod.synthesize(self.server, self.changes())

    def atom(self, name):
        got = self.own.atom_id(name)
        self.assertTrue(got, "the proxy never interned %s" % name)
        return got

    def shadow(self):
        return self.entry.shadow

    def props(self, evs, window=None):
        """`[atom name]` for the `PropertyNotify`s aimed at one window, in
        order."""
        out = []
        for ev in evs:
            name, got = decode(ev.packet)
            if name != "PropertyNotify":
                continue
            if window is not None and ev.target != window:
                continue
            out.append(self.own.atom_names.get(got["atom"], got["atom"]))
        return out


class NewWindow(EventsCase):
    num, upstream_num = 782, 783

    def test_a_new_toplevel_is_create_then_map_to_the_roots_selectors(self):
        """Nobody has selected on the shadow yet -- a shadow has no
        `CreateWindow` to have carried a mask and the first one arrives on the
        first `ChangeWindowAttributes` (design section 5.4) -- so both go to
        the root's `SubstructureNotify`."""
        self.set_windows([foot(), foot(12, title="WXL-Second", x=1, y=2,
                                       w=30, h=40, focused=False)])
        evs = self.evs()
        new = [e for e in self.shadows.snapshot() if e.handle == 12][0]
        self.assertEqual(kinds(evs)[:2],
                         [("CreateNotify", self.root,
                           events_mod.SUBSTRUCTURE_NOTIFY),
                          ("MapNotify", self.root,
                           events_mod.SUBSTRUCTURE_NOTIFY)])
        _name, got = decode(evs[0].packet)
        self.assertEqual((got["parent"], got["window"]),
                         (self.root, new.shadow))
        self.assertEqual((got["x"], got["y"], got["width"], got["height"]),
                         (1, 2, 30, 40))
        self.assertEqual(decode(evs[1].packet)[1]["event"], self.root)

    def test_and_then_the_roots_two_client_lists(self):
        self.set_windows([foot(), foot(12, title="WXL-Second")])
        self.assertEqual(self.props(self.evs(), self.root),
                         ["_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"])

    def test_the_root_properties_come_last(self):
        """Design section 5.3: per changed entry in list order, root properties
        last. A watcher that re-reads `_NET_CLIENT_LIST` on the notify must
        find the window already announced."""
        self.set_windows([foot(), foot(12, title="WXL-Second")])
        names = [k[0] for k in kinds(self.evs())]
        self.assertEqual(names.index("PropertyNotify"), 2)
        self.assertEqual(names[2:], ["PropertyNotify", "PropertyNotify"])


class Gone(EventsCase):
    num, upstream_num = 784, 785

    def test_unmap_then_destroy_each_twice_with_two_event_fields(self):
        """X's own shape: `event = window` to the `StructureNotify` selectors on
        it, `event = root` to the `SubstructureNotify` selectors on the root
        (design section 5.3)."""
        shadow = self.shadow()
        self.set_windows([])
        evs = self.evs()
        self.assertEqual(kinds(evs)[:4],
                         [("UnmapNotify", shadow, events_mod.STRUCTURE_NOTIFY),
                          ("UnmapNotify", self.root,
                           events_mod.SUBSTRUCTURE_NOTIFY),
                          ("DestroyNotify", shadow,
                           events_mod.STRUCTURE_NOTIFY),
                          ("DestroyNotify", self.root,
                           events_mod.SUBSTRUCTURE_NOTIFY)])
        for ev in evs[:4]:
            _name, got = decode(ev.packet)
            self.assertEqual(got["window"], shadow)
            self.assertEqual(got["event"], ev.target)

    def test_the_root_learns_the_list_and_the_focus_it_lost(self):
        """The foot of design section 9.3 is the focused window, so its
        departure moves `_NET_ACTIVE_WINDOW` as well as the two lists."""
        self.set_windows([])
        self.assertEqual(self.props(self.evs(), self.root),
                         ["_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING",
                          "_NET_ACTIVE_WINDOW"])

    def test_a_window_that_was_not_focused_does_not_move_the_active_window(self):
        self.set_windows([foot(), foot(12, title="WXL-Second", focused=False)])
        self.changes()
        self.set_windows([foot()])
        self.assertEqual(self.props(self.evs(), self.root),
                         ["_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"])


class VisibleFlip(EventsCase):
    num, upstream_num = 786, 787

    def test_hiding_is_unmap_both_ways_then_the_two_state_properties(self):
        shadow = self.shadow()
        self.set_windows([foot(visible=False)])
        evs = self.evs()
        self.assertEqual(kinds(evs),
                         [("UnmapNotify", shadow, events_mod.STRUCTURE_NOTIFY),
                          ("UnmapNotify", self.root,
                           events_mod.SUBSTRUCTURE_NOTIFY),
                          ("PropertyNotify", shadow,
                           events_mod.PROPERTY_CHANGE),
                          ("PropertyNotify", shadow,
                           events_mod.PROPERTY_CHANGE)])
        self.assertEqual(self.props(evs, shadow),
                         ["WM_STATE", "_NET_WM_STATE"])

    def test_showing_again_is_map(self):
        self.set_windows([foot(visible=False)])
        self.changes()
        self.set_windows([foot(visible=True)])
        evs = self.evs()
        self.assertEqual([k[0] for k in kinds(evs)][:2],
                         ["MapNotify", "MapNotify"])


class Move(EventsCase):
    num, upstream_num = 788, 789

    def test_a_moved_window_is_one_configure_notify_each_way(self):
        shadow = self.shadow()
        self.set_windows([foot(x=200, y=200)])
        evs = self.evs()
        self.assertEqual(kinds(evs),
                         [("ConfigureNotify", shadow,
                           events_mod.STRUCTURE_NOTIFY),
                          ("ConfigureNotify", self.root,
                           events_mod.SUBSTRUCTURE_NOTIFY)])
        for ev in evs:
            _name, got = decode(ev.packet)
            self.assertEqual((got["x"], got["y"], got["width"],
                              got["height"]), (200, 200, 300, 200))
            self.assertEqual(got["window"], shadow)
            self.assertEqual(got["event"], ev.target)
            self.assertEqual(got["above_sibling"], 0)

    def test_a_relist_with_the_same_rect_says_nothing(self):
        """The claim `SettlePoll` depends on: the settle re-lists twice per
        write and the second one must not repeat the first one's event."""
        self.set_windows([foot(x=200, y=200)])
        self.assertTrue(self.evs())
        self.assertEqual(self.evs(), [])


class TitleOrder(EventsCase):
    num, upstream_num = 790, 791

    def test_wm_name_before_net_wm_name(self):
        """The order xterm publishes a rename in, and the order `wxprop`
        already emits [recon/seams.md 4's `_NATIVE_EVENT_PROPS`]."""
        self.set_windows([foot(title="WXL-Renamed")])
        evs = self.evs()
        self.assertEqual(self.props(evs, self.shadow()),
                         ["WM_NAME", "_NET_WM_NAME"])
        for ev in evs:
            self.assertEqual(ev.bit, events_mod.PROPERTY_CHANGE)
            self.assertEqual(decode(ev.packet)[1]["state"], 0)


class PropsToAtoms(EventsCase):
    num, upstream_num = 792, 793

    def test_a_state_field_publishes_net_wm_state_once(self):
        """`Change.names` carries BACKEND FIELD names
        [requests-batch-2.md item 7]; two of them that mean the same property
        publish it once."""
        self.set_windows([foot(desktop=3)])
        self.assertEqual(self.props(self.evs(), self.shadow()),
                         ["_NET_WM_DESKTOP", "_NET_WM_STATE"])

    def test_the_pid_and_the_class_have_their_own_names(self):
        """In `Shadows._WINDOW_PROPS`' own order -- `class_`, `instance`,
        `pid`, ... -- because that is the order the diff walks the fields in
        and X has no order of its own for two unrelated properties."""
        self.set_windows([foot(pid=77, class_="Foot2", instance="foot2")])
        self.assertEqual(self.props(self.evs(), self.shadow()),
                         ["WM_CLASS", "_NET_WM_PID"])


class FocusPair(EventsCase):
    num, upstream_num = 794, 795

    def make_windows(self):
        return [foot(), foot(12, title="WXL-Second", focused=False)]

    def test_focus_out_on_the_old_then_focus_in_on_the_new(self):
        """The diff reports one change per entry in list order and cannot order
        the pair itself; X's order is the loss first."""
        old = self.shadow()
        self.set_windows([foot(focused=False),
                          foot(12, title="WXL-Second", focused=True)])
        evs = self.evs()
        new = [e for e in self.shadows.snapshot() if e.handle == 12][0]
        self.assertEqual(kinds(evs)[:2],
                         [("FocusOut", old, events_mod.FOCUS_CHANGE),
                          ("FocusIn", new.shadow, events_mod.FOCUS_CHANGE)])
        for ev in evs[:2]:
            _name, got = decode(ev.packet)
            self.assertEqual(got["detail"], 3)      # NotifyNonlinear
            self.assertEqual(got["mode"], 0)        # NotifyNormal
            self.assertEqual(got["event"], ev.target)

    def test_and_then_the_root_learns_the_active_window(self):
        self.set_windows([foot(focused=False),
                          foot(12, title="WXL-Second", focused=True)])
        evs = self.evs()
        self.assertEqual(self.props(evs, self.root), ["_NET_ACTIVE_WINDOW"])
        self.assertEqual([k[0] for k in kinds(evs)][-1], "PropertyNotify")


class RootListChange(EventsCase):
    num, upstream_num = 796, 797

    def make_windows(self):
        return [foot(), foot(12, title="WXL-Second", focused=False)]

    def test_a_reorder_with_the_same_membership_is_the_two_lists(self):
        """`Shadows` reports `order` only when the id SEQUENCE moved and the id
        SET did not [requests-batch-2.md item 7]; membership has its own rows."""
        self.set_windows([foot(12, title="WXL-Second", focused=False), foot()])
        evs = self.evs()
        self.assertEqual(self.props(evs, self.root),
                         ["_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"])
        self.assertEqual([k[0] for k in kinds(evs)],
                         ["PropertyNotify", "PropertyNotify"])


class Desktop(EventsCase):
    num, upstream_num = 798, 799

    def test_current_and_number_of_desktops(self):
        self.backend.desktop_ = 2
        self.backend.num_ = 4
        evs = self.evs()
        self.assertEqual(self.props(evs, self.root),
                         ["_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS"])

    def test_a_backend_that_names_no_workspaces_publishes_no_names(self):
        """sway numbers its workspaces and `wxprop` publishes no
        `_NET_DESKTOP_NAMES` there [recon/seams.md 4's
        `_SWAY_ROOT_EVENT_PROPS`]; `FakeBackend` has no `workspaces()` either.
        """
        self.backend.desktop_ = 1
        self.assertNotIn("_NET_DESKTOP_NAMES", self.props(self.evs(), self.root))

    def test_and_one_that_does_publishes_it(self):
        from wdotool.backend import Workspace

        self.backend.workspaces = lambda: [Workspace(index=0, name="web"),
                                           Workspace(index=1, name="code")]
        self.backend.desktop_ = 1
        self.assertEqual(self.props(self.evs(), self.root),
                         ["_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS",
                          "_NET_DESKTOP_NAMES"])


class RealIdNoPerWindowEvents(EventsCase):
    num, upstream_num = 800, 801

    def make_windows(self):
        return [foot(), fake_window(12, title="WXL-Xterm", class_="XTerm",
                                    instance="xterm", pid=99, x=0, y=0,
                                    w=800, h=600, visible=True, desktop=0)]

    def xids(self):
        return {12: 0x40000C}

    def test_a_real_x_windows_title_change_produces_no_per_window_event(self):
        """Xwayland sends those itself and they pass untouched; a second copy
        from the proxy is `xprop -spy` printing every rename twice (design
        section 5.3)."""
        self.set_windows([foot(), fake_window(12, title="WXL-Renamed",
                                              class_="XTerm", instance="xterm",
                                              pid=99, x=0, y=0, w=800, h=600,
                                              visible=True, desktop=0)],
                         {12: 0x40000C})
        self.assertEqual(self.evs(), [])

    def test_but_its_arrival_and_departure_move_the_roots_own_lists(self):
        """Upstream's `_NET_CLIENT_LIST` describes the X plane alone and sway
        deletes it outright when no X window is mapped [recon/env.md 2.7], so
        the root's list is the proxy's to publish for a real id too."""
        self.set_windows([foot()])
        evs = self.evs()
        self.assertEqual(self.props(evs, self.root),
                         ["_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"])
        self.assertEqual([k[0] for k in kinds(evs)],
                         ["PropertyNotify", "PropertyNotify"])

    def test_no_upstream_event_can_ever_name_a_shadow(self):
        """The other half of the root filter (design section 4.6): upstream's
        per-window events for shadow ids need no drop because no such window
        exists there. Every shadow is minted out of the PROXY's own
        resource-id range -- `rid_base | n` [recon/wire.md 1.3, 7.1] -- and a
        server hands each connection a range of its own, so an id Xwayland
        names to a client is never one of these.
        """
        shadow = self.shadow()
        mask = self.own.rid_mask
        self.assertTrue(shadow)
        self.assertEqual(shadow & ~mask, self.own.rid_base)
        # The client's own range is a different base, so its ids and the
        # shadows cannot collide either.
        self.assertNotEqual(self.conn._rid_base & ~mask, self.own.rid_base)
        for entry in self.shadows.snapshot():
            self.assertFalse(entry.xid and self.server.is_shadow(entry.xid))


class DiffIsTruth(EventsCase):
    num, upstream_num = 802, 803

    def test_a_relist_that_shows_nothing_produces_nothing(self):
        """The `title` token sway sends for a window whose title did not change
        -- a `move` arrives as a bare word with no geometry in it and `new`,
        `title` and `focus` all arrive for one `exec foot`
        [recon/seams.md 2.3, 2.4] -- costs a client nothing."""
        self.assertEqual(self.evs(), [])

    def test_a_change_with_no_token_behind_it_still_produces_its_event(self):
        """The other direction, and the one that matters on sway: ten floating
        `move position`s produced ZERO window events on this box (2026-09-10,
        headless sway 1.11, a subscribed `backend_sway.events()`), and the
        `ConfigureNotify` exists only because two listings differ."""
        self.set_windows([foot(x=400, y=400)])
        self.assertEqual([k[0] for k in kinds(self.evs())],
                         ["ConfigureNotify", "ConfigureNotify"])


# -- delivery, over real sockets (design section 5.4) --------------------------

#: `xprop -spy`'s mask, measured: `StructureNotify|PropertyChange`
#: [M recon/tools.md 6, caps/events2.jsonl].
SPY_MASK = 0x420000
#: `xdotool behave <w> mouse-enter`'s, measured: `EnterWindow`
#: [M recon/tools.md 4.7, caps/events.jsonl].
BEHAVE_MASK = 0x000010


class DeliveryCase(EventsCase):
    """The rig of `EventsCase` with clients on it, and changes driven through
    the REAL loop: a token on the backend's stream wakes the pump, the pump
    wakes the loop, the loop re-lists and the packets come out of a socket."""

    num, upstream_num = 810, 811

    def watcher(self):
        """One client, and the `ClientConn` the server made for it.

        Matched by ARRIVAL and not by file descriptor: the server's `down` is
        the accepted end of the pair and never shares a number with the
        client's own socket."""
        before = list(self.rig.server.conns)
        got = Watcher(self.rig)
        self.addCleanup(got.close)
        self.rig.wait(lambda: len(self.rig.server.conns) > len(before),
                      what="the client connection")
        got.conn = [c for c in self.rig.server.conns if c not in before][0]
        return got

    def conn_of(self, watcher):
        return watcher.conn

    def change(self, windows, token=(11, "title"), xids=None):
        """What the compositor answers next, and the token that says so. The
        token is a hint; what the clients are told is the diff."""
        self.set_windows(windows, xids)
        self.backend.feed(token)


class MaskGate(DeliveryCase):
    num, upstream_num = 812, 813

    def expected(self):
        return 2

    def test_only_the_client_that_selected_the_bit_is_written_to(self):
        watching, deaf = self.watcher(), self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        deaf.sync()
        self.change([foot(title="WXL-Renamed")])
        watching.wait_for(CODES["PropertyNotify"])
        deaf.drain(0.3)
        self.assertEqual(deaf.events, [])

    def test_the_mask_arms_the_pump_and_the_last_one_to_go_stops_it(self):
        """Design section 5.2: a proxy nobody watches through costs the
        compositor no subscription at all."""
        watching = self.watcher()
        watching.sync()
        self.assertIsNone(self.rig.server.events_pump)
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        self.rig.wait(lambda: self.rig.server.events_pump is not None
                      and self.rig.server.events_pump.started,
                      what="the pump")
        watching.select(self.shadow(), 0)
        watching.sync()
        # The reference goes with the thread: a stopped pump is a pump that is
        # not there, and the next mask builds a new one.
        self.rig.wait(lambda: self.rig.server.events_pump is None,
                      what="the pump stopping")


class EventSeqIsClientSeq(DeliveryCase):
    num, upstream_num = 814, 815

    def expected(self):
        return 2

    def test_two_clients_at_two_counts_get_two_sequences(self):
        """An event's sequence is a WATERMARK of the connection it goes to, not
        a number of its own [M recon/wire.md 3.2b: 61/69/77]."""
        early, late = self.watcher(), self.watcher()
        early.select(self.shadow(), SPY_MASK)
        late.noop(20)
        late.select(self.shadow(), SPY_MASK)
        early.sync()
        late.sync()
        self.change([foot(title="WXL-Renamed")])
        first = early.wait_for(CODES["PropertyNotify"])
        second = late.wait_for(CODES["PropertyNotify"])
        self.assertEqual(decode(first)[1]["seq"], early.seq)
        self.assertEqual(decode(second)[1]["seq"], late.seq)
        self.assertNotEqual(early.seq, late.seq)

    def test_the_watermark_counts_a_consumed_request_too(self):
        """`ChangeWindowAttributes` on a shadow is CONSUMEd -- upstream gets a
        `NoOperation` in its place -- and still costs one sequence number for
        ever [recon/wire.md 3.2a]. The event after it carries the client's own
        count, which is the proof that the delta is zero."""
        watching = self.watcher()
        watching.noop(3)
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        counted = watching.seq
        self.change([foot(title="WXL-Renamed")])
        got = watching.wait_for(CODES["PropertyNotify"])
        self.assertEqual(decode(got)[1]["seq"], counted)


class EventBehindPlaceholder(DeliveryCase):
    """An event written while the client is still owed a reply.

    The proxy ANSWERs `GetProperty` on a shadow itself, which costs a
    `GetInputFocus` round trip upstream (`client.answer`), so between the
    request and its reply the connection has a sequence it has not finished.
    An event stamped with `conn.seq` in that window is a packet whose number is
    ABOVE a reply that has not gone out -- libxcb marks the request completed
    with no reply on ANY packet but `KeymapNotify`, and widens the reply that
    then arrives lower by 65536: the teardown `client.write_after_replies`
    carries the measurement for (2026-09-10). Measured here first as a bug --
    `PropertyNotify` seq 4, seq 4, then the reply for seq 3 -- and pinned as
    `events.watermark`.
    """

    num, upstream_num = 820, 821

    def test_an_event_never_carries_a_sequence_above_a_reply_still_owed(self):
        watching = self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        owed = (watching.seq + 1) & 0xFFFF
        # Hold the substitute's reply: the proxy has the answer and cannot
        # write it, which is exactly the 1-44 ms an apply takes on sway.
        self.rig.upstream.hold_reply(owed)
        self.addCleanup(self.rig.upstream.release)
        got = watching.send(wire.OP_GET_PROPERTY, 0,
                            struct.pack("<IIIII", self.shadow(),
                                        self.atom("WM_NAME"), 0, 0, 125000))
        self.assertEqual(got, owed)
        watching.noop(1)                    # owed + 1: the pipelined request
        self.rig.wait(lambda: owed in self.conn_of(watching).placeholders,
                      what="the placeholder upstream")
        self.change([foot(title="WXL-Renamed")])
        first = watching.wait_for(CODES["PropertyNotify"])
        self.assertEqual(decode(first)[1]["seq"], (owed - 1) & 0xFFFF)
        self.assertNotEqual(decode(first)[1]["seq"], watching.seq)
        # The reply comes after the events and above them, which is the whole
        # point: libxcb reads 28, 28, then a reply whose sequence went UP.
        self.rig.upstream.release()
        watching.sock.settimeout(5.0)
        while True:
            pkt = watching.packet()
            if pkt[0] == 1:
                break
        self.assertEqual(struct.unpack_from("<H", pkt, 2)[0], owed)
        self.assertEqual([c for c in watching.order if c in (1, 28)],
                         [1, 28, 28, 1])

    def test_and_carries_the_client_count_again_once_nothing_is_owed(self):
        """The hold-back is only for as long as the reply is: a connection with
        an empty `placeholders` is stamped with its own count, which is what
        `EventSeqIsClientSeq` pins for every other client in the tree."""
        watching = self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.send(wire.OP_GET_PROPERTY, 0,
                      struct.pack("<IIIII", self.shadow(),
                                  self.atom("WM_NAME"), 0, 0, 125000))
        watching.sync()
        self.rig.wait(lambda: not self.conn_of(watching).placeholders,
                      what="the placeholder answered")
        self.change([foot(title="WXL-Renamed")])
        got = watching.wait_for(CODES["PropertyNotify"])
        self.assertEqual(decode(got)[1]["seq"], watching.seq)


class Watermark(unittest.TestCase):
    """`events.watermark` on its own, at the edges the socket tests cannot
    reach: the 16-bit wrap, and a book that holds a sequence AHEAD of the
    count (a stale entry `client._forget_stale` has not swept yet)."""

    class Conn:
        def __init__(self, seq, placeholders=(), holding=()):
            self.seq = seq
            self.placeholders = dict.fromkeys(placeholders)
            self.holding = dict.fromkeys(holding)

    def test_nothing_owed_is_the_clients_own_count(self):
        self.assertEqual(events_mod.watermark(self.Conn(77)), 77)

    def test_one_below_the_oldest_of_the_two_books(self):
        self.assertEqual(
            events_mod.watermark(self.Conn(40, placeholders=[38],
                                           holding=[31])), 30)

    def test_across_the_wrap_the_oldest_is_the_furthest_behind(self):
        """Sequences are 16-bit and `client._SEQ_WINDOW` compares them over a
        half circle: 0xFFF0 is behind 5, not 65515 ahead of it."""
        self.assertEqual(
            events_mod.watermark(self.Conn(5, placeholders=[0xFFF0])), 0xFFEF)

    def test_a_sequence_ahead_of_the_count_is_not_owed_yet(self):
        self.assertEqual(
            events_mod.watermark(self.Conn(10, holding=[0x8000])), 10)


class MasksDie(DeliveryCase):
    num, upstream_num = 816, 817

    def test_the_masks_of_a_dead_shadow_are_forgotten(self):
        """The id can be REUSED after an Xwayland restart re-bases the registry
        (design section 4.3), so a mask left behind would deliver one window's
        events to a client that asked about another."""
        watching = self.watcher()
        shadow = self.shadow()
        watching.select(shadow, SPY_MASK)
        watching.sync()
        conn = self.conn_of(watching)
        self.assertEqual(conn.masks.get(shadow), SPY_MASK)
        self.change([], token=(11, "close"))
        watching.wait_for(CODES["DestroyNotify"])
        self.rig.wait(lambda: shadow not in conn.masks,
                      what="the mask dropped")
        # ... and with the last mask gone, so is the compositor's stream.
        self.rig.wait(lambda: self.rig.server.events_pump is None,
                      what="the pump stopping")

    def test_and_the_masks_of_a_client_that_left_go_with_it(self):
        watching = self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        conn = self.conn_of(watching)
        watching.close()
        self.rig.wait(lambda: conn not in self.rig.server.conns,
                      what="the client leaving")
        # The mask dict went with the connection -- it is that object's own --
        # so what is left to check is the consequence, and the consequence is
        # what the claim is about: nobody is watching anything any more, and
        # the compositor's stream is stopped.
        self.rig.wait(lambda: not self.rig.server.events_wanted(),
                      what="no mask left anywhere")
        self.rig.wait(lambda: self.rig.server.events_pump is None,
                      what="the pump stopping")


class OverlayNotify(DeliveryCase):
    num, upstream_num = 818, 819

    def change_property(self, win, atom, data, type_atom=31, fmt=8, mode=0):
        body = (struct.pack("<IIIBxxxI", win, atom, type_atom, fmt, len(data))
                + data + b"\0" * (-len(data) % 4))
        return self.send_raw(wire.OP_CHANGE_PROPERTY, mode, body)

    def send_raw(self, op, byte1, body):
        return self.watch.send(op, byte1, body)

    def test_a_clients_own_write_publishes_its_property_notify_at_once(self):
        """Design section 5.3's last row: not at the next re-list, which would
        never come -- nothing about the COMPOSITOR changed."""
        self.watch = self.watcher()
        self.watch.select(self.shadow(), SPY_MASK)
        self.watch.sync()
        del self.watch.order[:]
        self.change_property(self.shadow(), 39, b"renamed")
        self.watch.sync()
        got = [p for p in self.watch.events
               if p[0] & 0x7F == CODES["PropertyNotify"]]
        self.assertEqual(len(got), 1)
        _name, fields = decode(got[0])
        self.assertEqual((fields["window"], fields["atom"], fields["state"]),
                         (self.shadow(), 39, events_mod.NEW_VALUE))
        # Written INSIDE the handler, so it is in front of the sync's reply.
        self.assertEqual(self.watch.order[0], CODES["PropertyNotify"])

    def test_a_delete_publishes_state_one(self):
        self.watch = self.watcher()
        self.watch.select(self.shadow(), SPY_MASK)
        self.watch.sync()
        self.send_raw(wire.OP_DELETE_PROPERTY, 0,
                      struct.pack("<II", self.shadow(), 39))
        self.watch.sync()
        got = [p for p in self.watch.events
               if p[0] & 0x7F == CODES["PropertyNotify"]]
        self.assertEqual(len(got), 1)
        self.assertEqual(decode(got[0])[1]["state"], events_mod.DELETED)

    def test_a_delete_of_a_name_the_window_never_had_says_nothing(self):
        """A real server sends no `PropertyNotify` for a `DeleteProperty` on a
        name a window does not have (`Shadows.delete` answers False)."""
        self.watch = self.watcher()
        self.watch.select(self.shadow(), SPY_MASK)
        self.watch.sync()
        self.send_raw(wire.OP_DELETE_PROPERTY, 0,
                      struct.pack("<II", self.shadow(),
                                  self.atom("_NET_WM_ICON")))
        self.watch.sync()
        self.watch.drain(0.2)
        self.assertEqual(self.watch.events, [])


class UpstreamRootFiltered(DeliveryCase):
    num, upstream_num = 820, 821

    def property_notify(self, atom, window=None):
        return events_mod.property_notify(
            self.root if window is None else window, atom)

    def test_upstreams_own_copy_of_a_synthesized_name_never_arrives(self):
        """Verified here, written by batch 3 in `client._drop_root_property`:
        the compositor is the single source for the seven names of
        `policy.OVERRIDES`, and letting upstream's copy through as well is
        `xprop -spy -root _NET_ACTIVE_WINDOW` printing every focus change
        twice (design section 4.6)."""
        watching = self.watcher()
        watching.select(self.root, SPY_MASK)
        watching.sync()
        index = self.rig.upstream.wires.index(
            self.rig.upstream.wires[-1])
        self.rig.upstream.push_event(
            self.property_notify(self.atom("_NET_ACTIVE_WINDOW")),
            conn_index=index)
        self.rig.upstream.push_event(
            self.property_notify(self.atom("_NET_SUPPORTING_WM_CHECK")),
            conn_index=index)
        got = watching.wait_for(CODES["PropertyNotify"])
        self.assertEqual(decode(got)[1]["atom"],
                         self.atom("_NET_SUPPORTING_WM_CHECK"))
        watching.drain(0.2)
        atoms = [decode(p)[1]["atom"] for p in watching.events]
        self.assertNotIn(self.atom("_NET_ACTIVE_WINDOW"), atoms)


class SettlePoll(DeliveryCase):
    num, upstream_num = 822, 823

    def test_a_move_with_no_token_behind_it_still_configures(self):
        """R5. sway sends NOTHING for a floating `move position`
        [recon/seams.md 2.4; ten moves, zero tokens, headless sway 1.11 on this
        box 2026-09-10], so the `ConfigureNotify` after `xdotool windowmove`
        comes from the settle poll's diff. `FakeBackend` emits no token either,
        which is exactly that case; the delays are patched short so what is
        asserted is the ARMING and not the wall clock."""
        self.rig.server.settle_delays = (0.01, 0.05)
        watching = self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        watching.send(wire.OP_CONFIGURE_WINDOW, 0,
                      struct.pack("<IH2xII", self.shadow(), 0x03, 200, 210))
        watching.sync()
        got = watching.wait_for(CODES["ConfigureNotify"], timeout=3.0)
        _name, fields = decode(got)
        self.assertEqual((fields["x"], fields["y"]), (200, 210))
        self.assertEqual(fields["window"], self.shadow())
        self.assertEqual(self.backend.counts["move_window"], 1)
        # The second poll re-lists and finds the same rect: one event, not two.
        watching.drain(0.3)
        self.assertEqual(len([p for p in watching.events
                              if p[0] & 0x7F == CODES["ConfigureNotify"]]), 1)

    def test_the_two_delays_are_the_measured_ones(self):
        """R5, measured 2026-09-10 on headless sway 1.11 on this box: ten
        `swaymsg '[title=WXL-Foot] move position X Y'` runs with `get_tree`
        polled every 5 ms saw the new rect on the FIRST poll every time --
        0.9 to 3.6 ms after the IPC returned, 1.6 to 6.1 ms end to end. 50 ms
        is 14x that worst case and 250 ms is the backstop for a compositor an
        order of magnitude slower (KWin loads a JS engine per operation
        [recon/seams.md 2.3], unmeasured -- R9)."""
        from xw11 import server as server_mod
        self.assertEqual(server_mod.SETTLE_DELAYS, (0.050, 0.250))
        self.assertGreater(server_mod.SETTLE_DELAYS[0], 0.0036 * 10)


class HitTest(unittest.TestCase):
    """`events.hit_test` on its own: which shadow a point is in when two of
    them overlap, which no socket test in this file can set up (the rig lists
    one window)."""

    class Entry:
        dead = False

        def __init__(self, shadow, rect):
            self.shadow = shadow
            self.window = fake_window(shadow, x=rect[0], y=rect[1],
                                      w=rect[2], h=rect[3])
            self.view = None
            self.xid = 0

    def entries(self, *rects):
        return [self.Entry(0x600001 + i, r) for i, r in enumerate(rects)]

    def test_the_topmost_of_two_overlapping_windows_wins(self):
        """`WindowBackend.list()` answers bottom to top and `Shadows.order`
        keeps that, so the LAST match is the one the pointer is over -- the
        window a click would reach. A first-match rule sends `behave
        mouse-enter` to the window UNDER the one the user is pointing at."""
        got = self.entries((0, 0, 300, 300), (100, 100, 300, 300))
        self.assertEqual(events_mod.hit_test(got, 150, 150).shadow,
                         got[1].shadow)
        self.assertEqual(events_mod.hit_test(got, 50, 50).shadow,
                         got[0].shadow)

    def test_and_the_other_way_round_when_the_stack_is_the_other_way(self):
        """The same two rects with the stacking reversed: the hit follows the
        ORDER and not the geometry."""
        got = self.entries((100, 100, 300, 300), (0, 0, 300, 300))
        self.assertEqual(events_mod.hit_test(got, 150, 150).shadow,
                         got[1].shadow)

    def test_a_point_in_neither_is_nothing(self):
        got = self.entries((0, 0, 300, 300), (100, 100, 300, 300))
        self.assertIsNone(events_mod.hit_test(got, 900, 900))

    def test_the_right_and_bottom_edges_are_outside(self):
        """X's rule: a window at (10, 20) 300x200 covers x in [10, 310) and y
        in [20, 220)."""
        got = self.entries((10, 20, 300, 200))
        self.assertIsNotNone(events_mod.hit_test(got, 309, 219))
        self.assertIsNone(events_mod.hit_test(got, 310, 219))
        self.assertIsNone(events_mod.hit_test(got, 309, 220))


class EnterLeave(DeliveryCase):
    num, upstream_num = 824, 825

    def setUp(self):
        super().setUp()
        self.rig.server.pointer_poll = 0.01

    def test_a_source_crossing_the_rect_is_leave_then_enter_then_motion(self):
        """Design section 5.6. The foot of design section 9.3 is at (10, 20)
        300x200, so (100, 100) is inside it and (1000, 1000) is not."""
        watching = self.watcher()
        watching.select(self.shadow(), events_mod.POINTER_MASK)
        watching.sync()
        self.rig.server.pointer_model = (100, 100)
        got = watching.wait_for(CODES["EnterNotify"])
        _name, fields = decode(got)
        self.assertEqual((fields["event"], fields["root"]),
                         (self.shadow(), self.root))
        self.assertEqual((fields["root_x"], fields["root_y"]), (100, 100))
        # event_x/y are relative to the window's origin.
        self.assertEqual((fields["event_x"], fields["event_y"]), (90, 80))
        # Byte 31 is `same-screen | focus` (2 | 1): the foot of design section
        # 9.3 is the focused window, and there is one screen.
        self.assertEqual((fields["detail"], fields["mode"],
                          fields["same_screen_focus"]), (3, 0, 3))
        self.rig.server.pointer_model = (120, 100)
        moved = watching.wait_for(CODES["MotionNotify"])
        self.assertEqual(decode(moved)[1]["event_x"], 110)
        self.rig.server.pointer_model = (1000, 1000)
        left = watching.wait_for(CODES["LeaveNotify"])
        self.assertEqual(decode(left)[1]["event"], self.shadow())

    def test_the_focus_bit_of_byte_31_is_clear_on_an_unfocused_window(self):
        """`ELFlagSameScreen` stays, `ELFlagFocus` goes: the same crossing into
        the same rect, with the compositor reporting the window unfocused."""
        self.set_windows([foot(focused=False)])
        watching = self.watcher()
        watching.select(self.shadow(), events_mod.POINTER_MASK)
        watching.sync()
        # The registry answers from a 20 ms cache, so the sampler must be given
        # the listing that says "unfocused" before the pointer is placed;
        # `snapshot()` is what re-lists when that cache is stale.
        self.rig.wait(
            lambda: not any(getattr(e.window, "focused", False)
                            for e in self.rig.server.shadows.snapshot()),
            what="the unfocused listing")
        self.rig.server.pointer_model = (100, 100)
        got = watching.wait_for(CODES["EnterNotify"])
        self.assertEqual(decode(got)[1]["same_screen_focus"], 2)

    def test_a_client_without_the_crossing_bits_gets_none_of_it(self):
        watching = self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.sync()
        self.rig.server.pointer_model = (100, 100)
        watching.drain(0.3)
        self.assertEqual(watching.events, [])

    def test_the_compositors_own_pointer_wins_where_it_has_one(self):
        """GNOME (Meta's own pointer) and Wayfire (`stipc/get-cursor`) are the
        two backends that answer `pointer()` [recon/seams.md 2.3]; the routed
        position is the fallback for the other six."""
        watching = self.watcher()
        watching.select(self.shadow(), events_mod.POINTER_MASK)
        watching.sync()
        self.backend.pointer_ = (50, 60)
        self.rig.server.pointer_model = (1000, 1000)
        got = watching.wait_for(CODES["EnterNotify"])
        self.assertEqual(decode(got)[1]["root_x"], 50)


class NoSourceNothing(DeliveryCase):
    num, upstream_num = 826, 827

    def test_with_no_pointer_at_all_nothing_is_sent_and_the_log_names_route_4(self):
        """sway's IPC carries no cursor and the daemon refuses to guess
        [recon/seams.md 2.3, 5.2]. A physical mouse is not seen yet: the route
        is AGENTS.md route 4 (evdev), at the cost of read access to
        /dev/input."""
        self.rig.server.pointer_poll = 0.01
        watching = self.watcher()
        watching.select(self.shadow(), BEHAVE_MASK)
        watching.sync()
        watching.drain(0.4)
        self.assertEqual(watching.events, [])
        said = self.log.carrying("route 4")
        self.assertEqual(len(said), 1, "said once, not twenty times a second")
        self.assertIn("not yet", said[0])
        self.assertIn("at the cost of", said[0])


class TokenIsAHint(DeliveryCase):
    num, upstream_num = 832, 833

    def test_a_token_with_no_change_behind_it_reaches_no_client(self):
        """sway sends `new`, `title` AND `focus` for one `exec foot`
        [recon/seams.md 2.3, +17.5 ms on this box 2026-09-10] and a `title` for
        a window whose title is what it was. The wake is real; the event is
        whatever two listings differ by, which here is nothing."""
        watching = self.watcher()
        watching.select(self.shadow(), SPY_MASK)
        watching.select(self.root, SPY_MASK)
        watching.sync()
        reads = self.backend.counts["views"]
        self.backend.feed((11, "title"))
        self.rig.wait(lambda: self.backend.counts["views"] > reads,
                      what="the re-list")
        watching.drain(0.3)
        self.assertEqual(watching.events, [])


class OneDrainOneRelist(EventsCase):
    num, upstream_num = 828, 829

    def test_one_drain_is_one_relist(self):
        """The deterministic half of `test_xw11_pump.py`'s coalescing pair: the
        pump here writes to a pipe the loop is not watching, so the drain is
        this thread's and the count is exact. `new`, `title` and `focus` for one
        `exec foot` arrive together [recon/seams.md 2.3] and one re-list answers
        all three."""
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        pump = pump_mod.Pump(self.backend, w, log=self.rig.server.say)
        self.rig.server.events_pump = pump
        self.addCleanup(setattr, self.rig.server, "events_pump", None)
        for token in ((11, "new"), (11, "title"), (11, "focus")):
            pump.put(token)
        # `views()` and not `list()`: six of the eight backends have one and
        # the registry asks for it first [recon/seams.md 4].
        listed = self.backend.counts["views"]
        self.assertEqual(self.rig.server.drain_pump(), 3)
        self.assertEqual(self.backend.counts["views"] - listed, 1)

    def test_a_drain_with_nothing_in_it_lists_nothing(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        pump = pump_mod.Pump(self.backend, w, log=self.rig.server.say)
        self.rig.server.events_pump = pump
        self.addCleanup(setattr, self.rig.server, "events_pump", None)
        listed = self.backend.counts["views"]
        self.assertEqual(self.rig.server.drain_pump(), 0)
        self.assertEqual(self.backend.counts["views"], listed)


# -- the streams the real tools sent (recon/tools.md 11 rows 29, 51) -----------


class SpyStreams(DeliveryCase):
    """The `ChangeWindowAttributes` each of the three blocking commands ends
    its prologue with, byte for byte out of the capture."""

    num, upstream_num = 830, 831

    def replay(self, name, window):
        root, target, frames = capture(name, wire.OP_CHANGE_WINDOW_ATTRIBUTES)
        self.assertEqual(len(frames), 1)
        buf = bytearray(frames[0])
        for at in range(0, len(buf) - 3, 4):
            (word,) = struct.unpack_from("<I", buf, at)
            if word == root:
                struct.pack_into("<I", buf, at, self.root)
            elif target and word == target:
                struct.pack_into("<I", buf, at, window)
        watching = self.watcher()
        watching.send(buf[0], buf[1], bytes(buf[4:]))
        watching.sync()
        return self.conn_of(watching)

    def test_xprop_spy_root_selects_structure_and_property_on_the_root(self):
        """Row 51: 14 requests, RC 124 under a 6 s timeout on both rigs
        [recon/tools.md 11]. The last of the 14 is this one."""
        conn = self.replay("spy-root", self.root)
        self.assertEqual(conn.masks.get(self.root), SPY_MASK)
        self.rig.wait(lambda: self.rig.server.events_pump is not None
                      and self.rig.server.events_pump.started,
                      what="the pump")

    def test_xprop_spy_id_selects_the_same_on_its_target(self):
        conn = self.replay("spy-id", self.shadow())
        self.assertEqual(conn.masks.get(self.shadow()), SPY_MASK)

    def test_behave_selects_enter_window_and_arms_the_pointer(self):
        """Row 29: 32 requests, RC 124 [recon/tools.md 11], the last of them
        `ChangeWindowAttributes(EventMask = EnterWindow)`
        [M recon/tools.md 4.7]."""
        conn = self.replay("behave", self.shadow())
        self.assertEqual(conn.masks.get(self.shadow()), BEHAVE_MASK)
        self.assertTrue(self.rig.server.pointer_wanted())


if __name__ == "__main__":
    unittest.main()
