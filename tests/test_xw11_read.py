#!/usr/bin/env python3
"""The read side, packet by packet, against the layouts of recon/wire.md 4.1.

Every assertion here is on BYTES. A test that unpacked the reply with the same
`struct` format the code packed it with would pass for a reply with the fields
in the wrong order, and the four tools read these replies with libX11's own
offsets -- so the expected packet is written out at the offsets recon/wire.md
4.1 records and compared whole.

What the file is defending, in one sentence per group:

* `QueryTree` on the root keeps upstream's children verbatim, wlroots' four
  xwm internals included [recon/seams.md 3], and appends the shadows;
* `GetProperty` answers the way a real server answers -- measured against Xvfb
  21.1.22 rather than read off the spec (2026-09-10, scratchpad
  b3/getprop_probe.py): absent is a reply and never an error, a type mismatch
  is checked before the offset, and an offset past the end is `BadValue`
  carrying the long_offset itself;
* the shadow's property set is design section 4.4's, in its order, with the
  three names wxprop does not synthesize (`WM_STATE` everywhere,
  `_NET_FRAME_EXTENTS`, `WM_PROTOCOLS`) and without the five nothing reads;
* the root's set is synthesized whether or not upstream has the name -- sway
  DELETES `_NET_CLIENT_LIST` when no X client is mapped [recon/env.md 2.7];
* `_NET_SUPPORTED` is a union, which is what turns `xdotool get_desktop` and
  `wmctrl -d` from exit 1 into exit 0 [recon/tools.md 4.10];
* upstream's own root `PropertyNotify` for a synthesized name never reaches a
  client, because the compositor is the single source (design section 4.6).
"""

import dataclasses
import os
import struct
import sys
import time
import tracemalloc
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

import support                                                     # noqa: E402
from support import (FakeBackend, fake_view, fake_window,           # noqa: E402
                     stop_daemons_under)
from wdotool import daemon as daemon_mod                           # noqa: E402
from wdotool import x11_mini                                       # noqa: E402
from xw11 import policy, req_read, shadow as shadow_mod, wire      # noqa: E402

#: `_NET_SUPPORTED` on wlroots' Xwayland root, all 19 of it and in the order
#: `xprop -root` printed them [recon/env.md 2.1, headless sway on this box].
#: The union appends to this; every name the proxy adds that is already here
#: must not appear twice, which is the point of pinning the real list rather
#: than a short made-up one.
WLROOTS_SUPPORTED = (
    "_NET_WM_STATE", "_NET_ACTIVE_WINDOW", "_NET_CLOSE_WINDOW",
    "_NET_WM_MOVERESIZE", "_NET_WM_STATE_FOCUSED", "_NET_WM_STATE_MODAL",
    "_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_MAXIMIZED_VERT",
    "_NET_WM_STATE_MAXIMIZED_HORZ", "_NET_WM_STATE_HIDDEN",
    "_NET_WM_STATE_STICKY", "_NET_WM_STATE_SHADED",
    "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER",
    "_NET_WM_STATE_ABOVE", "_NET_WM_STATE_BELOW",
    "_NET_WM_STATE_DEMANDS_ATTENTION", "_NET_CLIENT_LIST",
    "_NET_CLIENT_LIST_STACKING",
)

#: The 25 names the proxy appends to those 19, written out rather than filtered
#: out of `policy.SUPPORTED` with the same rule the code uses: an expectation
#: computed by the code's own comprehension passes for a table that lost a name,
#: and the whole point of `_NET_SUPPORTED` here is that dropping one name turns
#: `xdotool get_desktop` back into exit 1 [recon/tools.md 4.10]. A deliberate
#: change to the table changes this list too.
OURS_APPENDED = (
    "_NET_NUMBER_OF_DESKTOPS", "_NET_CURRENT_DESKTOP", "_NET_DESKTOP_NAMES",
    "_NET_DESKTOP_GEOMETRY", "_NET_WM_NAME", "_NET_WM_PID", "_NET_WM_DESKTOP",
    "_NET_WM_WINDOW_TYPE", "_NET_FRAME_EXTENTS", "_NET_WM_WINDOW_TYPE_NORMAL",
    "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_DOCK",
    "_NET_WM_WINDOW_TYPE_DIALOG", "_NET_WM_WINDOW_TYPE_TOOLBAR",
    "_NET_WM_WINDOW_TYPE_MENU", "_NET_WM_WINDOW_TYPE_UTILITY",
    "_NET_WM_WINDOW_TYPE_SPLASH", "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
    "_NET_WM_WINDOW_TYPE_POPUP_MENU", "_NET_WM_WINDOW_TYPE_TOOLTIP",
    "_NET_WM_WINDOW_TYPE_NOTIFICATION", "_NET_WM_WINDOW_TYPE_COMBO",
    "_NET_WM_WINDOW_TYPE_DND", "_NET_MOVERESIZE_WINDOW", "WM_CHANGE_STATE",
)

#: The four wlroots xwm windows `QueryTree(root)` carries beside the managed
#: ones, and which `_NET_CLIENT_LIST` does not [recon/seams.md 3, one live
#: xterm]. Ids of the shape `(client << 21) | serial` with client 1.
XWM_INTERNALS = (0x00200001, 0x00200002, 0x00200003, 0x00200004)


class _Upstream(support.FakeUpstream):
    """`FakeUpstream` with `ListProperties`, which design section 9.1 names and
    `tests/support.py` (batch 1's file) does not have yet.

    A per-file subclass rather than a change there: the root's `ListProperties`
    is the one request in this batch whose answer is an EDIT of a reply only a
    server can produce, and it is 6 lines. The request is in xprop's set and in
    nothing else's [recon/tools.md 3, 6].
    """

    def intern(self, name: str) -> int:
        """Atoms 1..68 keep the ids the protocol gives them
        [recon/wire.md 7.2]. `FakeXServer` numbers every name from 100, so
        `STRING` came back 154 and the proxy's own table says 31 -- a
        disagreement no real server can have, and one that made the
        `_NET_SUPPORTED` union silently decline to edit a reply whose type it
        did not recognise."""
        fixed = policy.PREDEFINED_ATOMS.get(name)
        if fixed and name not in self._atoms:
            self._atoms[name] = fixed
            self._names[fixed] = name
        return super().intern(name)

    #: What `QueryPointer` answers when the proxy forwards one: the numbers
    #: real xdotool reads off Xwayland on the headless rig, at the middle of a
    #: 1280x720 screen [recon/env.md 2.2]. `FakeXServer` answers `BadRequest`
    #: for an opcode nobody wrote a branch for, and a real server answers this.
    POINTER = (0x99, 640, 360, 600, 300, 0x0004)

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        if opcode == wire.OP_QUERY_POINTER:
            (win,) = struct.unpack_from("<I", payload, 0)
            self.log.append(("QueryPointer", win))
            child, rx, ry, wx, wy, mask = self.POINTER
            conn.sendall(struct.pack("<BBHIIIhhhhH6x", 1, 1, seq, 0,
                                     self.ROOTS[0], child, rx, ry, wx, wy,
                                     mask))
            return
        if opcode == 21:                        # ListProperties
            (win,) = struct.unpack_from("<I", payload, 0)
            self.log.append(("ListProperties", win))
            atoms = [self.intern(n) for (w, n) in self.props if w == win]
            body = struct.pack("<%dI" % len(atoms), *atoms)
            conn.sendall(struct.pack("<BxHIH22x", 1, seq, len(body) // 4,
                                     len(atoms)) + body)
            return
        return super()._dispatch(conn, opcode, dbyte, payload, seq)


class _Rig(support.ProxyRig):
    """`ProxyRig` over the subclass above. `ProxyRig` names `FakeUpstream` by
    the module global, so swapping it for the length of the constructor is the
    whole of it."""

    def __init__(self, **kw):
        real = support.FakeUpstream
        support.FakeUpstream = _Upstream
        try:
            super().__init__(**kw)
        finally:
            support.FakeUpstream = real


def xcb_reply(seq, byte1, fixed24, body=b""):
    """A reply packed BY HAND at recon/wire.md 4.1's offsets: `1`, the
    reply-specific byte, the sequence, the additional length in 4-byte words,
    24 fixed bytes, then the variable part.

    Deliberately not `xw11.wire.reply`: an expectation built with the packer
    under test agrees with it about a header packed wrong in both places, which
    is the one thing libX11 would not agree with."""
    assert len(fixed24) == 24 and len(body) % 4 == 0
    return (bytes([1, byte1 & 0xFF]) + (seq & 0xFFFF).to_bytes(2, "little")
            + (len(body) // 4).to_bytes(4, "little") + fixed24 + body)


class _SwayShaped(FakeBackend):
    """A backend with no `views()` and a sway-shaped `_nodes()`.

    sway and i3 are the only two in the tree like this, and they are the two the
    registry pairs through `backend._nodes()` (`Shadows._read`). The node is
    where `fullscreen_mode` and `sticky` live -- `View` has the fields and a
    `list()`-only backend has neither -- so a fake that answers `views() ->
    None` and nothing else cannot show what a real sway shows.
    """

    def __init__(self, windows=None, nodes=None, **kw):
        super().__init__(windows=windows, views=None, **kw)
        #: `{window id: the tree node}`, in sway's own keys.
        self.nodes = dict(nodes or {})

    def views(self):
        self._enter("views")
        return None

    def _nodes(self):
        """`(node, Window, floating, workspace)` per window, which is
        `backend_sway._nodes`' own tuple (backend_sway.py:207). Copies, like
        every other read on this fake."""
        self._enter("_nodes")
        return [(dict(self.nodes.get(w.id) or {}), dataclasses.replace(w),
                 False, "1") for w in self.windows]


def sway_node(xid=0, fullscreen=0, sticky=False):
    """One sway tree node, with the three keys the registry reads off it."""
    return {"window": xid, "fullscreen_mode": fullscreen, "sticky": sticky}


def foot_window(wid=11, **kw):
    """One native toplevel, the `foot -T WXL-Foot` of design section 9.3."""
    got = dict(title="WXL-Foot", class_="foot", instance="foot", pid=4242,
               x=10, y=20, w=300, h=200, visible=True, focused=True, desktop=0)
    got.update(kw)
    return fake_window(wid, **got)


def foot_backend(win=None, **kw):
    win = foot_window() if win is None else win
    view = fake_view(win, xid=0, app_id="foot", instance="foot", cls="foot")
    return FakeBackend(windows=[win], views=[view], **kw)


class ReadCase(unittest.TestCase):
    """One rig, one native toplevel, one client. Every subclass sends real
    requests down a real socket and reads the bytes that come back."""

    num = 640
    upstream_num = 641

    def setUp(self):
        self.backend = self.make_backend()
        self.rig = _Rig(num=self.num, upstream_num=self.upstream_num,
                        passthrough=False, backend=self.backend)
        self.addCleanup(self.rig.stop)
        self.conn = self.rig.conn()
        self.own = self.rig.wait_own()
        self.shadows = self.rig.server.shadows
        self.shadows.refresh()
        self.root = self.conn.root()

    def make_backend(self):
        return foot_backend()

    # -- the wire -------------------------------------------------------------

    def send(self, op, byte1=0, body=b""):
        """One request, and the reply it draws, through the project's own wire
        client. `_wait_reply` raises `X11Error` for an error packet, which is
        what the BadValue tests catch."""
        return self.conn._wait_reply(self.conn._send(op, byte1, body))

    def shadow(self):
        ids = [e.shadow for e in self.shadows.snapshot() if e.shadow]
        self.assertTrue(ids, "the registry minted no shadow")
        return ids[0]

    def atom(self, name):
        got = self.own.atom_id(name)
        self.assertTrue(got, "the proxy never interned %s" % name)
        return got

    def get_property(self, win, name, want_type=0, offset=0,
                     length=0xFFFFFFFF, delete=0):
        return self.send(wire.OP_GET_PROPERTY, delete,
                         struct.pack("<IIIII", win, self.atom(name),
                                     want_type, offset, length))

    def props_named(self, win):
        """`{name: (type name, format, value)}` for one window, through
        `ListProperties` and `GetProperty` -- which is xprop's own walk."""
        pkt, body = self.send(wire.OP_LIST_PROPERTIES, 0, struct.pack("<I", win))
        (count,) = struct.unpack_from("<H", pkt, 8)
        atoms = struct.unpack_from("<%dI" % count, bytes(body), 0)
        out = {}
        for atom in atoms:
            name = self.own.atom_names[atom]
            pkt, body = self.send(wire.OP_GET_PROPERTY, 0,
                                  struct.pack("<IIIII", win, atom, 0, 0,
                                              0xFFFFFFFF))
            type_atom, _after, nitems = struct.unpack_from("<III", pkt, 8)
            width = max(pkt[1] // 8, 1)
            out[name] = (self.own.atom_names.get(type_atom, type_atom), pkt[1],
                         bytes(body)[:nitems * width])
        return out


class QueryTreeRootKeepsInternals(ReadCase):
    num, upstream_num = 642, 643

    def setUp(self):
        super().setUp()
        self.rig.upstream.children = list(XWM_INTERNALS)

    def test_upstreams_children_come_first_and_unchanged(self):
        pkt, body = self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", self.root))
        (count,) = struct.unpack_from("<H", pkt, 16)
        kids = struct.unpack_from("<%dI" % count, bytes(body), 0)
        self.assertEqual(kids[:4], XWM_INTERNALS)
        self.assertEqual(kids[4:], (self.shadow(),))

    def test_the_children_count_and_the_length_word_are_both_rewritten(self):
        """Two numbers say how long the list is and a reply that rewrote one of
        them is a reply libX11 walks off the end of."""
        pkt, body = self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", self.root))
        (words,) = struct.unpack_from("<I", pkt, 4)
        (count,) = struct.unpack_from("<H", pkt, 16)
        self.assertEqual(count, 5)
        self.assertEqual(words, 5)
        self.assertEqual(len(bytes(body)), 20)

    def test_the_shadows_follow_in_list_order_bottom_to_top(self):
        second = fake_window(12, title="second", class_="foot", visible=True)
        self.backend.windows.append(second)
        self.backend.views_.append(fake_view(second, xid=0, app_id="foot"))
        self.shadows.refresh()
        want = [e.shadow for e in self.shadows.snapshot()]
        pkt, body = self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", self.root))
        (count,) = struct.unpack_from("<H", pkt, 16)
        kids = struct.unpack_from("<%dI" % count, bytes(body), 0)
        self.assertEqual(list(kids[4:]), want)

    def test_a_paired_window_is_not_appended_twice(self):
        """An X toplevel is already in upstream's children; the registry knows
        it as a REAL entry and mints it no shadow [design section 4.2]."""
        xwin = fake_window(13, title="WXL-Xterm", class_="XTerm", visible=True)
        self.backend.windows.append(xwin)
        self.backend.views_.append(fake_view(xwin, xid=0x0040000C))
        self.rig.upstream.children = list(XWM_INTERNALS) + [0x0040000C]
        self.shadows.refresh()
        pkt, body = self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", self.root))
        (count,) = struct.unpack_from("<H", pkt, 16)
        kids = struct.unpack_from("<%dI" % count, bytes(body), 0)
        self.assertEqual(kids.count(0x0040000C), 1)


class QueryTreeShadow(ReadCase):
    num, upstream_num = 644, 645

    def test_a_shadow_is_a_leaf_of_the_root(self):
        want = xcb_reply(self.conn._seq + 1, 0,
                         struct.pack("<IIH14x", self.root, self.root, 0))
        pkt, body = self.send(wire.OP_QUERY_TREE, 0,
                              struct.pack("<I", self.shadow()))
        self.assertEqual(bytes(pkt), want)
        self.assertEqual(bytes(body), b"")

    def test_upstream_was_never_asked_about_the_shadow(self):
        """The substitute went up in its place, so upstream saw a
        `GetInputFocus` and no `QueryTree` naming an id it has never heard of
        (design section 4.1)."""
        self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", self.shadow()))
        asked = [row for row in self.rig.upstream.log
                 if row[0] == "QueryTree" and row[1] == self.shadow()]
        self.assertEqual(asked, [])


class GetPropertyExact(ReadCase):
    """Design section 4.7, field by field, against the Xvfb measurement."""

    num, upstream_num = 646, 647

    def test_a_2000_entry_client_list_truncates_the_way_wmctrl_reads_it(self):
        """wmctrl asks `_NET_CLIENT_LIST` with `length=1024` and never issues a
        second GetProperty with an offset, so it prints 1024 lines and stops
        [recon/tools.md 5, measured against a planted 2000-entry list]. The
        ceiling is inherited whole, truncation and all."""
        wins = [fake_window(100 + i, title="w%d" % i, visible=True)
                for i in range(2000)]
        self.backend.windows = wins
        self.backend.views_ = [fake_view(w, xid=0) for w in wins]
        self.shadows.refresh()
        pkt, body = self.get_property(self.root, "_NET_CLIENT_LIST",
                                      length=1024)
        type_atom, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual(pkt[1], 32)
        self.assertEqual(type_atom, self.atom("WINDOW"))
        self.assertEqual(nitems, 1024)
        self.assertEqual(after, 3904)
        self.assertEqual(len(bytes(body)), 4096)

    def test_a_type_mismatch_answers_the_actual_type_and_no_data(self):
        """wmctrl names `STRING` for `WM_CLASS` and reads a mismatch as absent
        [recon/tools.md 5]; Xvfb answers `bytes_after = len(value),
        value_len 0` with the actual type and format."""
        pkt, body = self.get_property(self.shadow(), "WM_CLASS",
                                      want_type=self.atom("CARDINAL"))
        type_atom, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual((pkt[1], type_atom, nitems), (8, self.atom("STRING"), 0))
        self.assertEqual(after, len(b"foot\0foot\0"))
        self.assertEqual(bytes(body), b"")

    def test_the_matching_type_reads_the_whole_value(self):
        pkt, body = self.get_property(self.shadow(), "WM_CLASS",
                                      want_type=self.atom("STRING"))
        _t, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual((after, nitems), (0, 10))
        self.assertEqual(bytes(body)[:nitems], b"foot\0foot\0")

    def test_an_absent_name_answers_type_zero_format_zero_nitems_zero(self):
        """recon/env.md 2.7's measured shape, which is what sway's root answers
        for a `_NET_CLIENT_LIST` it deleted -- and never an error."""
        want = wire.reply(self.conn._seq + 1, 0, struct.pack("<III12x", 0, 0, 0))
        pkt, body = self.send(wire.OP_GET_PROPERTY, 0,
                              struct.pack("<IIIII", self.shadow(),
                                          self.own.atom_id("_NET_WM_ICON"), 0,
                                          0, 0xFFFFFFFF))
        self.assertEqual(bytes(pkt), want)
        self.assertEqual(bytes(body), b"")

    def test_an_offset_at_the_end_answers_empty_and_one_past_it_is_badvalue(self):
        """Measured, not inferred: Xvfb 21.1.22 answers an empty reply for
        `long_offset` exactly at the end and `BadValue` with `bad = 5` for
        offset 5 on a 16-byte value (2026-09-10, scratchpad
        b3/getprop_probe.py). Both designs said "empty" for the second; the
        server says otherwise and X is the oracle."""
        pkt, body = self.get_property(self.shadow(), "_NET_FRAME_EXTENTS")
        self.assertEqual(len(bytes(body)), 16)                  # four units
        pkt, _body = self.get_property(self.shadow(), "_NET_FRAME_EXTENTS",
                                       offset=4)
        _t, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual((after, nitems), (0, 0))
        with self.assertRaises(x11_mini.X11Error) as caught:
            self.get_property(self.shadow(), "_NET_FRAME_EXTENTS", offset=5)
        self.assertEqual(caught.exception.code, wire.ERR_VALUE)

    def test_the_offset_check_counts_bytes_and_not_units(self):
        """A 10-byte `WM_CLASS` is two whole 32-bit units and a bit: offset 2
        reads the last two bytes, and offset 3 is already past the end. Xvfb
        compares `long_offset << 2` against the byte length, not against a
        count of units (2026-09-10, scratchpad b3/getprop_probe.py)."""
        pkt, body = self.get_property(self.shadow(), "WM_CLASS", offset=2)
        _t, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual((after, nitems), (0, 2))
        self.assertEqual(bytes(body)[:2], b"t\0")
        with self.assertRaises(x11_mini.X11Error):
            self.get_property(self.shadow(), "WM_CLASS", offset=3)

    def test_the_badvalue_names_the_long_offset_and_the_getproperty_opcode(self):
        """`bad` is the long_offset itself and NOT the byte offset, `major` is
        20, and the sequence is the request's own -- Xlib's default handler
        prints all three and the parity oracle reads those bytes
        [recon/tools.md 9]."""
        seq = (self.conn._seq + 1) & 0xFFFF
        with self.assertRaises(x11_mini.X11Error) as caught:
            self.get_property(self.shadow(), "WM_CLASS", offset=9)
        err = caught.exception
        self.assertEqual((err.code, err.major, err.minor, err.bad_value),
                         (wire.ERR_VALUE, wire.OP_GET_PROPERTY, 0, 9))
        self.assertEqual(err.sequence, seq)

    def test_the_four_hundred_megabyte_ceiling_allocates_nothing(self):
        """libX11's own prologue asks `length=100000000` (400 MB) on request 4
        of every connection [recon/tools.md 2], xprop asks 125000 units
        (500 kB) and xdotool asks 0xFFFFFFFF [recon/tools.md 4.2, 6].
        `long_length` is a CEILING, so the slice is taken with `min` against
        what is there -- and the handler is called directly here, with
        `tracemalloc` around it and nothing else, because a socket round trip's
        own buffers would drown the number being claimed."""
        pkt, body = self.get_property(self.shadow(), "WM_CLASS",
                                      length=0x05F5E100)
        self.assertEqual(len(bytes(body)), 12)
        _t, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual((after, nitems), (0, 10))
        conn = self.rig.server.conns[0]
        frame = (struct.pack("<BBH", wire.OP_GET_PROPERTY, 0, 6)
                 + struct.pack("<IIIII", self.shadow(), self.atom("WM_CLASS"),
                               0, 0, 0x05F5E100))
        req = conn.decode(frame, wire.OP_GET_PROPERTY, 0)
        tracemalloc.start()
        try:
            for _ in range(100):
                got = req_read.get_property(self.rig.server, conn, req)
                self.assertEqual(len(got), 44)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1 << 14, "a ceiling was taken for a size")

    def test_delete_tombstones_only_on_a_read_that_finished(self):
        """Xvfb deletes on `delete = 1` when `bytes_after == 0` and not before;
        a partial read with the bit set deletes nothing."""
        entry = self.shadows.by_shadow[self.shadow()]
        self.get_property(self.shadow(), "WM_CLASS", offset=0, length=1, delete=1)
        self.assertEqual(entry.overlay, {})
        self.get_property(self.shadow(), "WM_CLASS", delete=1)
        self.assertEqual(entry.overlay, {self.atom("WM_CLASS"): shadow_mod.TOMBSTONE})
        pkt, _body = self.get_property(self.shadow(), "WM_CLASS")
        self.assertEqual(struct.unpack_from("<III", pkt, 8), (0, 0, 0))

    def test_a_tombstoned_name_leaves_the_listing(self):
        self.get_property(self.shadow(), "WM_CLASS", delete=1)
        self.assertNotIn("WM_CLASS", self.props_named(self.shadow()))


class ShadowPropertySet(ReadCase):
    """Design section 4.4's table, name by name."""

    num, upstream_num = 648, 649

    def test_wm_class_is_string_whatever_the_app_id_is(self):
        """wmctrl names the type STRING explicitly and reads a mismatch as
        absent [recon/tools.md 5], so a UTF-8 app id does NOT promote the type
        the way `WM_NAME` is allowed to."""
        win = foot_window(title="plain", class_="é中", instance="中")
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, instance="中",
                                         cls="é中")]
        self.shadows.refresh()
        got = self.props_named(self.shadow())
        self.assertEqual(got["WM_CLASS"][0], "STRING")
        self.assertEqual(got["WM_CLASS"][1], 8)
        self.assertEqual(self.atom("STRING"), 31)

    def test_wm_name_is_utf8_only_when_latin1_will_not_hold_it(self):
        """wxprop's `_p_string` rule (wxprop/core.py:408): typing UTF-8 bytes
        as STRING makes xprop print mojibake, and a title that fits latin-1
        stays STRING, which is what the Xwayland twin's real WM_NAME carries."""
        got = self.props_named(self.shadow())
        self.assertEqual(got["WM_NAME"], ("STRING", 8, b"WXL-Foot"))
        self.assertEqual(got["_NET_WM_NAME"], ("UTF8_STRING", 8, b"WXL-Foot"))
        win = foot_window(title="中文")
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, app_id="foot")]
        self.shadows.refresh()
        got = self.props_named(self.shadow())
        self.assertEqual(got["WM_NAME"],
                         ("UTF8_STRING", 8, "中文".encode("utf-8")))

    def test_wm_state_is_there_on_a_backend_with_no_views(self):
        """The one row design section 4.4 changed from wxprop -- wxprop writes
        it on a `views()` backend only [recon/seams.md 4] and every shadow gets
        it here.

        The reason that row gives is measured false and the property stays
        anyway: `xdotool getwindowfocus` does read `WM_STATE` on the focus
        window [recon/tools.md 4.2] and does NOT climb when it is absent (the
        pinned 4.20260303.1 printed the same shadow with the property
        tombstoned out, `tests/test_xw11_live.py::NativeExists`). It stays
        because Mutter writes it on every X11 window it manages, so a native
        toplevel answers "is this minimized?" the way its X twin does."""
        self.backend.views_ = None
        self.shadows.refresh()
        got = self.props_named(self.shadow())
        self.assertEqual(got["WM_STATE"], ("WM_STATE", 32,
                                           struct.pack("<II", 1, 0)))

    def test_wm_state_is_iconic_for_a_window_the_compositor_calls_invisible(self):
        win = foot_window(visible=False)
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, app_id="foot")]
        self.shadows.refresh()
        got = self.props_named(self.shadow())
        self.assertEqual(got["WM_STATE"][2], struct.pack("<II", 3, 0))

    def test_frame_extents_is_four_zeroes(self):
        """The rect the backends report is the whole toplevel and GetGeometry
        answers the same rect, so zero is the true number: python-ewmh and
        devilspie2 SUBTRACT this."""
        got = self.props_named(self.shadow())
        self.assertEqual(got["_NET_FRAME_EXTENTS"],
                         ("CARDINAL", 32, struct.pack("<4I", 0, 0, 0, 0)))

    def test_wm_protocols_promises_wm_delete_window(self):
        """Without it a polite closer falls back to XKillClient, which is
        SIGKILL; with it the ClientMessage batch 4 routes to `close` arrives."""
        got = self.props_named(self.shadow())
        self.assertEqual(got["WM_PROTOCOLS"],
                         ("ATOM", 32,
                          struct.pack("<I", self.atom("WM_DELETE_WINDOW"))))

    def test_the_five_names_nothing_reads_are_not_synthesized(self):
        """`WM_HINTS`, `WM_NORMAL_HINTS`, `_NET_WM_ICON`, `_NET_WM_USER_TIME`
        and `_NET_WM_ALLOWED_ACTIONS`: nothing in recon/tools.md 3-7 reads any
        of them, a window without hints is legal X, and each is a row in the
        docs with its route rather than a lie in a reply."""
        got = self.props_named(self.shadow())
        for name in ("WM_HINTS", "WM_NORMAL_HINTS", "_NET_WM_ICON",
                     "_NET_WM_USER_TIME", "_NET_WM_ALLOWED_ACTIONS"):
            self.assertNotIn(name, got)

    def test_the_pid_is_left_out_when_the_compositor_does_not_know_it(self):
        win = foot_window(pid=0)
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, app_id="foot")]
        self.shadows.refresh()
        self.assertNotIn("_NET_WM_PID", self.props_named(self.shadow()))
        self.assertEqual(self.props_named(self.shadow())["WM_CLIENT_MACHINE"][2],
                         x11_mini.hostname().encode("latin-1"))

    def test_the_state_atoms_are_mutters_order_with_focused_last(self):
        """`_NET_WM_STATE` in Mutter's own order (window-x11.c
        `set_net_wm_state`) so a native window and an Xwayland one on GNOME
        print alike [recon/seams.md 4]."""
        win = foot_window(visible=False, focused=True)
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, app_id="foot",
                                         skip_pager=True, maximized_v=True,
                                         fullscreen=True, sticky=True)]
        self.shadows.refresh()
        got = self.props_named(self.shadow())["_NET_WM_STATE"]
        names = [self.own.atom_names[a] for a in
                 struct.unpack("<%dI" % (len(got[2]) // 4), got[2])]
        self.assertEqual(names, ["_NET_WM_STATE_SKIP_PAGER",
                                 "_NET_WM_STATE_MAXIMIZED_VERT",
                                 "_NET_WM_STATE_FULLSCREEN",
                                 "_NET_WM_STATE_HIDDEN",
                                 "_NET_WM_STATE_STICKY",
                                 "_NET_WM_STATE_FOCUSED"])


class ListPropertiesOrder(ReadCase):
    num, upstream_num = 650, 651

    #: Design section 4.4's table, in the order it is written. The live xterm
    #: on the rig answers thirteen in creation order reversed [M 2026-09-10,
    #: scratchpad b3/xterm_props.py]; a shadow has no creation order to
    #: reverse, so this one is fixed here and pinned there.
    ORDER = ("_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_PID",
             "WM_CLIENT_MACHINE", "_NET_WM_DESKTOP", "_NET_WM_STATE",
             "_NET_WM_WINDOW_TYPE", "WM_STATE", "_NET_FRAME_EXTENTS",
             "WM_PROTOCOLS")

    def names_of(self, win):
        pkt, body = self.send(wire.OP_LIST_PROPERTIES, 0, struct.pack("<I", win))
        (count,) = struct.unpack_from("<H", pkt, 8)
        atoms = struct.unpack_from("<%dI" % count, bytes(body), 0)
        return [self.own.atom_names[a] for a in atoms]

    def test_the_shadows_order_is_the_tables(self):
        self.assertEqual(self.names_of(self.shadow()), list(self.ORDER))

    def test_a_transient_parent_lands_between_the_type_and_wm_state(self):
        parent = foot_window(11)
        kid = fake_window(12, title="dialog", visible=True)
        self.backend.windows = [parent, kid]
        self.backend.views_ = [fake_view(parent, xid=0, app_id="foot"),
                               fake_view(kid, xid=0, transient_for=11)]
        self.shadows.refresh()
        ids = [e.shadow for e in self.shadows.snapshot()]
        names = self.names_of(ids[1])
        self.assertIn("WM_TRANSIENT_FOR", names)
        self.assertEqual(names.index("WM_TRANSIENT_FOR"),
                         names.index("_NET_WM_WINDOW_TYPE") + 1)

    def test_the_transient_parent_is_the_id_a_client_is_shown(self):
        """`View.transient_for` is a BACKEND HANDLE; a handle in that field is
        a lie in the one number a dialog-placing client trusts."""
        parent = foot_window(11)
        kid = fake_window(12, title="dialog", visible=True)
        self.backend.windows = [parent, kid]
        self.backend.views_ = [fake_view(parent, xid=0, app_id="foot"),
                               fake_view(kid, xid=0, transient_for=11)]
        self.shadows.refresh()
        ids = [e.shadow for e in self.shadows.snapshot()]
        got = self.props_named(ids[1])["WM_TRANSIENT_FOR"]
        self.assertEqual(got, ("WINDOW", 32, struct.pack("<I", ids[0])))

    def test_an_overlay_name_is_listed_after_the_table(self):
        """The overlay is what a client wrote (batch 4 writes it); a name only
        it has is appended in write order, which is the order a real server
        answers for a window whose properties arrived in that order."""
        entry = self.shadows.by_shadow[self.shadow()]
        entry.overlay[self.atom("_NET_WM_ICON")] = (self.atom("CARDINAL"), 32,
                                                    b"\0\0\0\0")
        self.assertEqual(self.names_of(self.shadow())[-1], "_NET_WM_ICON")

    def test_an_overlay_over_a_synthesized_name_keeps_the_tables_place(self):
        entry = self.shadows.by_shadow[self.shadow()]
        entry.overlay[self.atom("WM_NAME")] = (self.atom("STRING"), 8, b"rewritten")
        self.assertEqual(self.names_of(self.shadow()), list(self.ORDER))
        self.assertEqual(self.props_named(self.shadow())["WM_NAME"],
                         ("STRING", 8, b"rewritten"))


class ListPropertiesRootUnion(ReadCase):
    num, upstream_num = 652, 653

    def setUp(self):
        super().setUp()
        # What the live sway root really carries [recon/seams.md 3]: two of the
        # six overlap with what the proxy synthesizes and four do not.
        for name in ("_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING",
                     "_NET_SUPPORTING_WM_CHECK", "_NET_ACTIVE_WINDOW",
                     "_NET_SUPPORTED", "_XKB_RULES_NAMES"):
            self.rig.upstream.set_prop(self.rig.upstream.ROOTS[0], name,
                                       "STRING", 8, b"x")
    def names_of_root(self):
        """The fake's own table for the names, never `OwnConn.atom_name()`:
        that one sends a request on the proxy's connection and drains it
        synchronously, which is a thing only the LOOP thread may do -- a
        `flush()` from here races the loop's own `del self._out[:sent]` and
        raises `BufferError: Existing exports of data`."""
        pkt, body = self.send(wire.OP_LIST_PROPERTIES, 0,
                              struct.pack("<I", self.root))
        (count,) = struct.unpack_from("<H", pkt, 8)
        atoms = struct.unpack_from("<%dI" % count, bytes(body), 0)
        names = self.rig.upstream._names
        return [names.get(a, self.own.atom_names.get(a, a)) for a in atoms]

    def test_upstreams_atoms_come_first_and_the_synthesized_ones_follow(self):
        got = self.names_of_root()
        self.assertEqual(got[:6], ["_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING",
                                   "_NET_SUPPORTING_WM_CHECK",
                                   "_NET_ACTIVE_WINDOW", "_NET_SUPPORTED",
                                   "_XKB_RULES_NAMES"])
        self.assertEqual(got[6:], ["_NET_NUMBER_OF_DESKTOPS",
                                   "_NET_CURRENT_DESKTOP",
                                   "_NET_DESKTOP_GEOMETRY"])

    def test_no_name_is_listed_twice(self):
        got = self.names_of_root()
        self.assertEqual(len(got), len(set(got)))

    def test_the_length_word_matches_the_new_count(self):
        pkt, body = self.send(wire.OP_LIST_PROPERTIES, 0,
                              struct.pack("<I", self.root))
        (words,) = struct.unpack_from("<I", pkt, 4)
        (count,) = struct.unpack_from("<H", pkt, 8)
        self.assertEqual((words, len(bytes(body))), (count, 4 * count))


class RootSynthesizedWhenAbsent(ReadCase):
    """sway DELETES `_NET_CLIENT_LIST` when no X client is mapped rather than
    emptying it [recon/env.md 2.7]; the fake root has never had it."""

    num, upstream_num = 654, 655

    def test_the_client_list_is_answered_for_a_root_that_has_no_such_name(self):
        upstream = [row for row in self.rig.upstream.log
                    if row[0] == "GetProperty" and row[2] == "_NET_CLIENT_LIST"]
        pkt, body = self.get_property(self.root, "_NET_CLIENT_LIST")
        type_atom, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual((pkt[1], type_atom, after, nitems),
                         (32, self.atom("WINDOW"), 0, 1))
        self.assertEqual(struct.unpack("<I", bytes(body)[:4]), (self.shadow(),))
        after_log = [row for row in self.rig.upstream.log
                     if row[0] == "GetProperty" and row[2] == "_NET_CLIENT_LIST"]
        self.assertEqual(upstream, after_log,
                         "the request went upstream as well as being answered")

    def test_the_stacking_list_is_a_copy_of_the_client_list(self):
        """sway's tree is not a stacking order and no backend reports one, so
        the two lists ARE the same list (R13, design section 4.6). A backend
        that starts reporting stacking changes this test rather than sliding a
        wrong order past."""
        a = self.get_property(self.root, "_NET_CLIENT_LIST")[1]
        b = self.get_property(self.root, "_NET_CLIENT_LIST_STACKING")[1]
        self.assertEqual(bytes(a), bytes(b))

    def test_the_active_window_is_the_focused_entry(self):
        pkt, body = self.get_property(self.root, "_NET_ACTIVE_WINDOW")
        self.assertEqual(struct.unpack("<I", bytes(body)[:4]), (self.shadow(),))
        self.backend.windows[0].focused = False
        self.shadows.refresh()
        pkt, body = self.get_property(self.root, "_NET_ACTIVE_WINDOW")
        self.assertEqual(struct.unpack("<I", bytes(body)[:4]), (0,))

    def test_the_desktop_pair_is_the_answer_the_five_dead_commands_wanted(self):
        """`xdotool get_desktop`, `set_desktop`, `get_num_desktops` and
        `wmctrl -d` all exit 1 on sway today because these two names are not
        published [recon/tools.md 4.10]."""
        self.backend.desktop_ = 2
        self.backend.num_ = 2
        self.shadows.refresh()
        pkt, body = self.get_property(self.root, "_NET_CURRENT_DESKTOP")
        self.assertEqual((pkt[1], struct.unpack("<I", bytes(body)[:4])), (32, (2,)))
        pkt, body = self.get_property(self.root, "_NET_NUMBER_OF_DESKTOPS")
        self.assertEqual(struct.unpack("<I", bytes(body)[:4]), (3,),
                         "a compositor can be on a desktop it does not count")

    def test_a_root_name_the_proxy_does_not_own_still_goes_upstream(self):
        self.rig.upstream.set_prop(self.rig.upstream.ROOTS[0],
                                   "_NET_SUPPORTING_WM_CHECK", "WINDOW", 32,
                                   struct.pack("<I", 0x00200004))
        pkt, body = self.get_property(self.root, "_NET_SUPPORTING_WM_CHECK")
        self.assertEqual(struct.unpack("<I", bytes(body)[:4]), (0x00200004,))
        self.assertIn(("GetProperty", self.rig.upstream.ROOTS[0],
                       "_NET_SUPPORTING_WM_CHECK", 0), self.rig.upstream.log)


class DesktopGeometryAndNames(ReadCase):
    num, upstream_num = 656, 657

    def test_the_geometry_is_display_size(self):
        """`wwmctl -d` prints its `DG:` column from `display_size()`
        (wwmctl/core.py:780) and the parity target is the clone's bytes."""
        pkt, body = self.get_property(self.root, "_NET_DESKTOP_GEOMETRY")
        self.assertEqual(pkt[1], 32)
        self.assertEqual(struct.unpack("<2I", bytes(body)[:8]), (1280, 720))

    def test_the_names_are_absent_where_the_backend_has_no_workspaces(self):
        """A `workspaces()` that answers None is a backend with nothing to say,
        and `wmctrl -d` prints `N/A` for an absent column rather than a zero
        that reads as an answer."""
        pkt, _body = self.get_property(self.root, "_NET_DESKTOP_NAMES")
        self.assertEqual(struct.unpack_from("<III", pkt, 8), (0, 0, 0))

    def test_the_names_are_utf8_nul_terminated_where_it_has_them(self):
        from wdotool.backend import Workspace

        self.backend.workspaces = lambda: [Workspace(0, "one", True),
                                           Workspace(1, "", False)]
        self.shadows.refresh()
        pkt, body = self.get_property(self.root, "_NET_DESKTOP_NAMES")
        self.assertEqual((pkt[1], self.own.atom_names[
            struct.unpack_from("<I", pkt, 8)[0]]), (8, "UTF8_STRING"))
        (_t, _after, nitems) = struct.unpack_from("<III", pkt, 8)
        self.assertEqual(bytes(body)[:nitems], b"one\0001\0")


class SupportedUnion(ReadCase):
    num, upstream_num = 658, 659

    def setUp(self):
        super().setUp()
        for name in WLROOTS_SUPPORTED:
            self.own.atom_id(name) or self.rig.upstream.intern(name)
        ids = [self.own.atom_id(n) for n in WLROOTS_SUPPORTED]
        self.assertTrue(all(ids), "the proxy interned none of upstream's names")
        self.rig.upstream.set_prop(self.rig.upstream.ROOTS[0], "_NET_SUPPORTED",
                                   "ATOM", 32,
                                   struct.pack("<%dI" % len(ids), *ids))

    def read_supported(self):
        pkt, body = self.get_property(self.root, "_NET_SUPPORTED", length=0x3FFF)
        _t, _after, nitems = struct.unpack_from("<III", pkt, 8)
        return [self.own.atom_names[a]
                for a in struct.unpack("<%dI" % nitems, bytes(body)[:4 * nitems])]

    def test_upstreams_nineteen_come_first_in_upstreams_order(self):
        self.assertEqual(self.read_supported()[:19], list(WLROOTS_SUPPORTED))

    def test_ours_follow_in_the_supported_tables_order_and_none_twice(self):
        got = self.read_supported()
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(got[19:], list(OURS_APPENDED))
        self.assertEqual(len(got), 19 + 25)

    def test_the_five_names_the_dead_commands_gate_on_are_in_it(self):
        """`xdotool get_desktop` and `wmctrl -d` read `_NET_SUPPORTED` first
        and bail when the name is not in it [recon/env.md 2.1,
        recon/tools.md 4.10]."""
        got = self.read_supported()
        for name in ("_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS",
                     "_NET_CLIENT_LIST", "_NET_WM_DESKTOP",
                     "_NET_MOVERESIZE_WINDOW"):
            self.assertIn(name, got)

    def test_the_type_and_the_length_word_survive_the_edit(self):
        pkt, body = self.get_property(self.root, "_NET_SUPPORTED", length=0x3FFF)
        words, type_atom, after, nitems = struct.unpack_from("<IIII", pkt, 4)
        self.assertEqual(type_atom, self.atom("ATOM"))
        self.assertEqual((pkt[1], after), (32, 0))
        self.assertEqual((words, len(bytes(body))), (nitems, 4 * nitems))

    def test_a_chunked_read_is_left_alone(self):
        """An edit that appended atoms to a chunk read at an offset would put
        them in the middle of the value."""
        pkt, body = self.get_property(self.root, "_NET_SUPPORTED", offset=2,
                                      length=3)
        _t, after, nitems = struct.unpack_from("<III", pkt, 8)
        self.assertEqual(nitems, 3)
        self.assertEqual(after, 4 * (19 - 5))


class GeometryAttributesCoordinates(ReadCase):
    num, upstream_num = 660, 661

    def test_the_geometry_reply_is_the_compositors_rect_at_ratio_one(self):
        """R8, measured: with `output HEADLESS-1 scale 2` sway's tree rect and
        the xterm's xwininfo BOTH read 640x360, so one X device pixel is one
        compositor logical pixel [M 2026-09-10, headless sway on this box].
        `tests/test_xw11_live.py::HiDpi` keeps that measurement."""
        depth = self.rig.server.conns[0].setup.root_depth
        want = xcb_reply(self.conn._seq + 1, depth,
                         struct.pack("<IhhHHH10x", self.root, 10, 20, 300, 200, 0))
        pkt, body = self.send(wire.OP_GET_GEOMETRY, 0,
                              struct.pack("<I", self.shadow()))
        self.assertEqual(bytes(pkt), want)
        self.assertEqual(bytes(body), b"")

    def test_a_backend_that_reports_a_zero_rect_answers_four_zeroes(self):
        """Whatever the backend says and nothing else: zero is the wire's only
        honest unknown, and `hit_test` already ignores such a window. (The wlr
        floor is NOT this case -- it answers the whole output rect,
        backend_wlr.py:414, which `tests/test_xw11_live.py::WlrFloor` pins.)"""
        win = foot_window(x=0, y=0, w=0, h=0)
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, app_id="foot")]
        self.shadows.refresh()
        pkt, _body = self.send(wire.OP_GET_GEOMETRY, 0,
                               struct.pack("<I", self.shadow()))
        self.assertEqual(struct.unpack_from("<Ihh HHH", pkt, 8),
                         (self.root, 0, 0, 0, 0, 0))

    def test_map_state_follows_the_predicate_onlyvisible_uses(self):
        """`Window.visible` is what `wdotool search --onlyvisible` reads
        (wdotool/window_cmds.py:173), so the clone and the proxy agree about
        the same window."""
        pkt, body = self.send(wire.OP_GET_WINDOW_ATTRIBUTES, 0,
                              struct.pack("<I", self.shadow()))
        # The protocol's own numbers at recon/wire.md 4.1's offset 26, not the
        # module's constants: a `MAP_VIEWABLE` that said 1 (IsUnviewable) would
        # satisfy a test written against itself and no libX11 reader.
        self.assertEqual(pkt[26], 2, "IsViewable is 2")
        self.backend.windows[0].visible = False
        self.shadows.refresh()
        pkt, body = self.send(wire.OP_GET_WINDOW_ATTRIBUTES, 0,
                              struct.pack("<I", self.shadow()))
        self.assertEqual(pkt[26], 0, "IsUnmapped is 0")
        self.assertEqual((req_read.MAP_VIEWABLE, req_read.MAP_UNMAPPED), (2, 0))

    def test_the_attributes_reply_is_forty_four_bytes_at_the_measured_offsets(self):
        """44 bytes, `len = 3` [recon/wire.md 4.1]. The fixed values are the
        ones a real Xwayland answers for a managed window, all but two: the
        live xterm answered `backing_planes=0xffffffff backing_pixel=0
        save_under=0 map_is_installed=1 override_redirect=0` and
        `bit_gravity=1`, which is xterm's own NorthWest request -- a shadow has
        no client to have asked, so it keeps CreateWindow's Forget
        [M 2026-09-10, scratchpad b3/xterm_props.py]."""
        setup = self.rig.server.conns[0].setup
        want = xcb_reply(self.conn._seq + 1, 0,
                         struct.pack("<IHBBIIBBBBI", setup.root_visual, 1, 0, 1,
                                     0xFFFFFFFF, 0, 0, 1, 2, 0, setup.cmaps[0]),
                         struct.pack("<IIH2x", 0, 0, 0))
        pkt, body = self.send(wire.OP_GET_WINDOW_ATTRIBUTES, 0,
                              struct.pack("<I", self.shadow()))
        self.assertEqual(bytes(pkt) + bytes(body), want)
        self.assertEqual(len(want), 44)

    def test_translate_towards_the_root_adds_the_rect_origin(self):
        want = xcb_reply(self.conn._seq + 1, 1,
                         struct.pack("<Ihh16x", self.shadow(), 15, 27))
        pkt, body = self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                              struct.pack("<IIhh", self.shadow(), self.root, 5, 7))
        self.assertEqual(bytes(pkt), want)

    def test_translate_away_from_the_root_subtracts_it_and_names_no_child(self):
        pkt, _body = self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                               struct.pack("<IIhh", self.root, self.shadow(),
                                           15, 27))
        self.assertEqual(struct.unpack_from("<Ihh", pkt, 8), (0, 5, 7))

    def second_window(self):
        """A second native toplevel at a rect of its own, and the two shadow
        ids by handle."""
        first = self.backend.windows[0]
        other = foot_window(12, title="WXL-Other", x=500, y=400, w=100, h=50,
                            focused=False)
        self.backend.windows = [first, other]
        self.backend.views_ = [fake_view(first, xid=0, app_id="foot"),
                               fake_view(other, xid=0, app_id="foot")]
        self.shadows.refresh()
        return {e.handle: e.shadow for e in self.shadows.snapshot()}

    def test_the_child_is_the_window_under_the_destination_point(self):
        """`backend.hit_test` (backend.py:159), the one rule every backend and
        `getmouselocation` already share, mapped from a handle onto the id a
        client is shown -- so the answer is the SECOND window's shadow, which is
        the only id in the reply that could not have come from the request."""
        ids = self.second_window()
        pkt, _body = self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                               struct.pack("<IIhh", ids[11], self.root,
                                           510 - 10, 410 - 20))
        self.assertEqual(struct.unpack_from("<Ihh", pkt, 8),
                         (ids[12], 510, 410))

    def test_a_point_under_no_window_at_all_answers_child_zero(self):
        """`hit_test` over an empty result is 0 -- `None` in X's terms -- and
        the coordinates are still translated."""
        pkt, _body = self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                               struct.pack("<IIhh", self.shadow(), self.root,
                                           -100, -100))
        self.assertEqual(struct.unpack_from("<Ihh", pkt, 8), (0, -90, -80))

    def test_a_window_neither_the_root_nor_a_shadow_is_named_at_either_end(self):
        """The source half of the not-yet, which is the destination half
        mirrored: the proxy needs that window's own origin and only upstream
        has it, so the point is answered relative to the root and the log names
        the end it came from -- one line per direction, both naming the route
        (a `GetGeometry` on the proxy's own connection, AGENTS.md route 5).
        Nothing measured sends either shape [recon/tools.md 4.2, 5]."""
        lines = []
        self.rig.server.say = lines.append
        foreign = 0x00400010
        self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                  struct.pack("<IIhh", foreign, self.shadow(), 15, 27))
        self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                  struct.pack("<IIhh", self.shadow(), foreign, 5, 7))
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("from window 0x400010", lines[0])
        self.assertIn("to window 0x400010", lines[1])
        for line in lines:
            self.assertIn("not yet", line)
            self.assertIn("route 5", line)

    def test_a_rect_past_the_ends_of_the_wire_fields_is_truncated(self):
        """Every field of a `GetGeometry` reply is fixed width -- INT16 origin,
        CARD16 size -- and a compositor's numbers are not. X truncates such a
        rect; a handler that raised `struct.error` instead would take the
        exception out to the selector loop, which catches `CmdError` and
        nothing else."""
        win = foot_window(x=40000, y=-40000, w=70000, h=-3)
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0, app_id="foot")]
        self.shadows.refresh()
        pkt, _body = self.send(wire.OP_GET_GEOMETRY, 0,
                               struct.pack("<I", self.shadow()))
        self.assertEqual(struct.unpack_from("<IhhHHH", pkt, 8),
                         (self.root, 40000 - 0x10000, 0x10000 - 40000,
                          70000 - 0x10000, 0, 0))


class GetInputFocusShadow(ReadCase):
    num, upstream_num = 662, 663

    def test_the_focus_becomes_the_shadow_when_the_focused_toplevel_is_native(self):
        pkt, _body = self.send(wire.OP_GET_INPUT_FOCUS, 0, b"")
        self.assertEqual(struct.unpack_from("<I", pkt, 8), (self.shadow(),))

    def test_pointer_root_is_kept_when_nothing_native_has_the_focus(self):
        """The fake answers `focus = 1` (PointerRoot), which is exactly what a
        WM-less server answers -- and xdotool then reads `WM_STATE` on window
        `0x1`, draws `BadWindow` and exits 1 [recon/env.md 3]. That is
        xdotool's bug and AGENTS.md keeps bugs, so this must not be
        "improved"."""
        self.backend.windows[0].focused = False
        self.shadows.refresh()
        pkt, _body = self.send(wire.OP_GET_INPUT_FOCUS, 0, b"")
        self.assertEqual(struct.unpack_from("<I", pkt, 8), (1,))

    def test_a_paired_x_window_leaves_upstreams_answer_alone(self):
        """An Xwayland toplevel has a real X id and upstream already named it;
        the proxy has nothing to add and touching the field would be inventing
        an answer."""
        win = foot_window(title="WXL-Xterm")
        self.backend.windows = [win]
        self.backend.views_ = [fake_view(win, xid=0x0040000C)]
        self.shadows.refresh()
        pkt, _body = self.send(wire.OP_GET_INPUT_FOCUS, 0, b"")
        self.assertEqual(struct.unpack_from("<I", pkt, 8), (1,))

    def test_the_reply_keeps_its_revert_to_byte_and_its_length(self):
        pkt, body = self.send(wire.OP_GET_INPUT_FOCUS, 0, b"")
        self.assertEqual((len(bytes(pkt)), len(bytes(body))), (32, 0))
        self.assertEqual(struct.unpack_from("<I", pkt, 4), (0,))


class OneAnswerOnePlaceholder(ReadCase):
    """Every locally answered request costs upstream exactly one
    `GetInputFocus`, in stream order, carrying that request's own sequence
    (design section 3.1, R1). One more or one fewer and every reply after it is
    read against the wrong sequence."""

    num, upstream_num = 664, 665

    def test_each_answered_read_puts_one_get_input_focus_upstream(self):
        shadow = self.shadow()
        before = len([r for r in self.rig.upstream.log if r[0] == "GetInputFocus"])
        self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", shadow))
        self.send(wire.OP_GET_GEOMETRY, 0, struct.pack("<I", shadow))
        self.send(wire.OP_GET_WINDOW_ATTRIBUTES, 0, struct.pack("<I", shadow))
        self.send(wire.OP_LIST_PROPERTIES, 0, struct.pack("<I", shadow))
        self.get_property(shadow, "WM_CLASS")
        self.get_property(self.root, "_NET_CLIENT_LIST")
        self.send(wire.OP_TRANSLATE_COORDINATES, 0,
                  struct.pack("<IIhh", shadow, self.root, 0, 0))
        after = len([r for r in self.rig.upstream.log if r[0] == "GetInputFocus"])
        self.assertEqual(after - before, 7)

    def test_an_edited_read_puts_no_placeholder_upstream(self):
        """EDIT forwards the real request; only ANSWER substitutes."""
        before = len([r for r in self.rig.upstream.log if r[0] == "GetInputFocus"])
        self.send(wire.OP_QUERY_TREE, 0, struct.pack("<I", self.root))
        after = len([r for r in self.rig.upstream.log if r[0] == "GetInputFocus"])
        self.assertEqual(after - before, 0)
        self.assertIn(("QueryTree", self.rig.upstream.ROOTS[0]),
                      self.rig.upstream.log)

    def test_a_pipelined_burst_comes_back_in_order(self):
        """xdotool pipelines `GetWindowAttributes` and `GetGeometry` and blocks
        on the second reply [recon/tools.md 4.1]; a local reply written ahead
        of a forwarded one makes libxcb read NULL for the earlier request
        (R1)."""
        sock, _setup = self.rig.raw()
        shadow = self.shadow()
        sock.sendall(struct.pack("<BxHI", wire.OP_GET_WINDOW_ATTRIBUTES, 2, shadow)
                     + struct.pack("<BxHI", wire.OP_GET_GEOMETRY, 2, shadow)
                     + struct.pack("<BxH", wire.OP_GET_INPUT_FOCUS, 1))
        got = b""
        while len(got) < 44 + 32 + 32:
            got += sock.recv(4096)
        self.assertEqual(struct.unpack_from("<H", got, 2)[0], 1)
        self.assertEqual(struct.unpack_from("<H", got, 46)[0], 2)
        self.assertEqual(struct.unpack_from("<H", got, 78)[0], 3)


class RootPropertyNotifyDropped(ReadCase):
    num, upstream_num = 666, 667

    def property_notify(self, atom, window=None):
        win = self.rig.upstream.ROOTS[0] if window is None else window
        return struct.pack("<BxHIIIB15x", wire.EV_PROPERTY_NOTIFY, 0, win,
                           atom, 0, 0)

    def read_events(self, timeout=1.0):
        import select

        out = []
        sock = self.conn._sock
        deadline = timeout
        while True:
            r, _w, _x = select.select([sock], [], [], deadline)
            if not r:
                return out
            got = sock.recv(4096)
            if not got:
                return out
            out += [got[i:i + 32] for i in range(0, len(got), 32)]
            deadline = 0.2

    def test_a_synthesized_name_never_reaches_the_client(self):
        """Otherwise `xprop -spy -root _NET_ACTIVE_WINDOW` prints every focus
        change twice: once for the X plane's and once for the compositor's
        (design section 4.6)."""
        self.rig.upstream.push_event(
            self.property_notify(self.atom("_NET_ACTIVE_WINDOW")), conn_index=1)
        self.rig.upstream.push_event(
            self.property_notify(self.atom("_NET_SUPPORTING_WM_CHECK")),
            conn_index=1)
        got = self.read_events()
        atoms = [struct.unpack_from("<I", pkt, 8)[0] for pkt in got
                 if pkt and pkt[0] & 0x7F == wire.EV_PROPERTY_NOTIFY]
        self.assertEqual(atoms, [self.atom("_NET_SUPPORTING_WM_CHECK")])

    def test_the_same_name_on_a_window_that_is_not_the_root_passes(self):
        """The compositor is the single source for these names ON THE ROOT. On
        a window the name means something else and belongs to whoever wrote
        it."""
        self.rig.upstream.push_event(
            self.property_notify(self.atom("_NET_CLIENT_LIST"),
                                 window=0x0040000C), conn_index=1)
        got = self.read_events()
        atoms = [struct.unpack_from("<I", pkt, 8)[0] for pkt in got
                 if pkt and pkt[0] & 0x7F == wire.EV_PROPERTY_NOTIFY]
        self.assertEqual(atoms, [self.atom("_NET_CLIENT_LIST")])


class ViewLessStates(ReadCase):
    """`_NET_WM_STATE` on sway and i3, which have no `views()` at all.

    Design section 4.4 brackets the RICH states -- the ones only a `View`
    carries -- and leaves `_NET_WM_STATE_FULLSCREEN` and `_STICKY` unbracketed,
    which is to say on every backend. The source for those two on a view-less
    backend is the tree node the registry already holds for the xid pairing
    (`Shadows._read`), read the way `wxprop` reads it (wxprop/core.py:544,
    554). Without it the proxy printed `_NET_WM_STATE(ATOM) =` for a foot sway
    had fullscreened while `wxprop` printed `_NET_WM_STATE_FULLSCREEN` for the
    same window [M 2026-09-10, headless sway on this box].
    """

    num, upstream_num = 668, 669

    def make_backend(self):
        win = foot_window()
        return _SwayShaped(windows=[win],
                           nodes={win.id: sway_node(fullscreen=1, sticky=True)})

    def states(self):
        got = self.props_named(self.shadow())["_NET_WM_STATE"]
        return [self.own.atom_names[a]
                for a in struct.unpack("<%dI" % (len(got[2]) // 4), got[2])]

    def test_fullscreen_and_sticky_come_off_the_tree_node(self):
        """And the rich ones do not: this entry's window says `focused=True`
        and there is no `View` to read FOCUSED off, so the list is the two
        sway really knows."""
        self.assertEqual(self.states(), ["_NET_WM_STATE_FULLSCREEN",
                                         "_NET_WM_STATE_STICKY"])

    def test_fullscreen_mode_two_is_fullscreen_too(self):
        """sway's `fullscreen_mode` is an INT -- 0 none, 1 output, 2 global --
        and `wxprop` tests it for truth, not for 1."""
        self.backend.nodes[11]["fullscreen_mode"] = 2
        self.shadows.refresh()
        self.assertIn("_NET_WM_STATE_FULLSCREEN", self.states())

    def test_leaving_fullscreen_re_publishes_without_moving_the_rect(self):
        """The cache's half of the same claim. A window already the size of the
        output changes no rect when it leaves fullscreen, so nothing else about
        the entry moves and a `props` cache kept across it would answer the old
        state list for as long as the entry lives."""
        self.assertIn("_NET_WM_STATE_FULLSCREEN", self.states())
        self.backend.nodes[11]["fullscreen_mode"] = 0
        self.shadows.refresh()
        self.assertEqual(self.states(), ["_NET_WM_STATE_STICKY"])

    def test_the_pairing_still_reads_the_nodes_x_id(self):
        """The node is read for two things now, and the first one must not have
        moved: a node with a `window` is an Xwayland toplevel and gets no
        shadow at all (design section 4.2)."""
        self.backend.nodes[11] = sway_node(xid=0x0040000C, fullscreen=1)
        self.shadows.refresh()
        entries = self.shadows.snapshot()
        self.assertEqual([(e.xid, e.shadow) for e in entries],
                         [(0x0040000C, 0)])


class TheRegistryIsReadThroughTheTtl(ReadCase):
    """Design section 4.8's freshness rule, on the requests that never touch the
    root.

    `xdotool getwindowname`, `getwindowpid`, `getwindowgeometry`,
    `getwindowfocus` and `xprop -id <shadow>` send no root request at all, so a
    handler that read `Shadows.by_shadow` straight answered whatever the last
    re-list saw -- for as long as nobody sent a root request, which in a script
    polling one window is for ever. Both halves are pinned here: the pump's
    `invalidate()` (what `Server.drain_pump` calls when upstream says something
    moved) and the 20 ms TTL on its own.
    """

    num, upstream_num = 670, 671

    def make_backend(self):
        first = foot_window(11, title="A", focused=True)
        second = foot_window(12, title="B", focused=False, x=500, y=400)
        return FakeBackend(windows=[first, second],
                           views=[fake_view(first, xid=0, app_id="foot"),
                                  fake_view(second, xid=0, app_id="foot")])

    def shadow_of(self, handle):
        return self.shadows.entries[handle].shadow

    def name_of(self, shadow):
        pkt, body = self.get_property(shadow, "_NET_WM_NAME")
        (nitems,) = struct.unpack_from("<I", pkt, 16)
        return bytes(body)[:nitems]

    def assert_no_root_request(self, since):
        """The claim's other half: nothing in this test asked the root
        anything, so nothing but the handlers themselves can have re-listed."""
        sent = self.rig.upstream.log[since:]
        asked = [row for row in sent
                 if row[0] in ("GetProperty", "ListProperties", "QueryTree")]
        self.assertEqual(asked, [], "a root request re-listed the registry")

    def test_the_focus_follows_an_invalidate_with_no_root_request(self):
        pkt, _body = self.send(wire.OP_GET_INPUT_FOCUS, 0, b"")
        self.assertEqual(struct.unpack_from("<I", pkt, 8), (self.shadow_of(11),))
        mark = len(self.rig.upstream.log)
        self.backend.windows[0].focused = False
        self.backend.windows[1].focused = True
        self.shadows.invalidate()               # what `Server.drain_pump` does
        pkt, _body = self.send(wire.OP_GET_INPUT_FOCUS, 0, b"")
        self.assertEqual(struct.unpack_from("<I", pkt, 8), (self.shadow_of(12),))
        self.assert_no_root_request(mark)

    def test_a_retitle_is_seen_once_the_ttl_is_up_with_no_root_request(self):
        shadow = self.shadow_of(11)
        self.assertEqual(self.name_of(shadow), b"A")
        mark = len(self.rig.upstream.log)
        self.backend.windows[0].title = "A2"
        time.sleep(self.shadows.ttl * 3)
        self.assertEqual(self.name_of(shadow), b"A2")
        self.assert_no_root_request(mark)

    def test_the_geometry_and_the_attributes_are_fresh_too(self):
        """The same rule on the other two shadow-targeted reads: `xdotool
        getwindowgeometry` in a loop is `GetGeometry` and nothing else."""
        shadow = self.shadow_of(12)
        self.backend.windows[1].x = 640
        self.backend.windows[1].visible = False
        self.shadows.invalidate()
        pkt, _body = self.send(wire.OP_GET_GEOMETRY, 0,
                               struct.pack("<I", shadow))
        self.assertEqual(struct.unpack_from("<hh", pkt, 12), (640, 400))
        pkt, _body = self.send(wire.OP_GET_WINDOW_ATTRIBUTES, 0,
                               struct.pack("<I", shadow))
        self.assertEqual(pkt[26], 0, "IsUnmapped is 0")

    def test_a_fresh_list_is_not_re_read_for_every_request_in_a_burst(self):
        """`snapshot()` is the TTL and not a re-list per request: `xdotool
        search` sends `GetWindowAttributes` and `GetGeometry` for every window
        in one pipelined burst [recon/tools.md 4.1], and one `views()` answers
        the lot."""
        self.shadows.ttl = 5.0                  # not a stopwatch race
        self.shadows.refresh()
        before = self.backend.counts["views"]
        for _ in range(6):
            self.send(wire.OP_GET_GEOMETRY, 0,
                      struct.pack("<I", self.shadow_of(11)))
        self.assertEqual(self.backend.counts["views"] - before, 0)



class QueryPointerSources(ReadCase):
    """Where `root_x/root_y` come from, in design section 6.6's order.

    `getmouselocation` is one `QueryPointer(root)` [recon/tools.md 4.2] and
    that is the only request behind it, so this handler is the whole of the
    read half of the pointer -- for the 3.x generation, which warps, and for
    the 4.x, which fakes a motion [recon/wire.md 5.3], alike.
    """

    num, upstream_num = 690, 691

    def query(self, win):
        """`(child, root_x, root_y, win_x, win_y, mask)` of one reply."""
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", win))
        return struct.unpack_from("<IhhhhH", pkt, 12)

    def test_upstreams_answer_stands_when_nothing_else_knows(self):
        """sway's IPC carries no cursor and the daemon refuses to guess
        [recon/seams.md 2.3], so with nothing routed yet the reply is
        Xwayland's own -- which is `x:640 y:360` on the headless rig today
        [recon/env.md 2.2] -- and the request really went upstream."""
        self.assertIsNone(self.rig.server.pointer_model)
        got = self.query(self.root)
        self.assertEqual(got[1:3], (640, 360))
        self.assertEqual(got[5], 0x0004, "mask is upstream's, untouched")
        self.assertIn(("QueryPointer", self.root), self.rig.upstream.log)

    def test_the_backends_own_pointer_wins(self):
        """GNOME's Meta pointer and Wayfire's `stipc/get-cursor` are the two
        that answer [recon/seams.md 2.3]."""
        self.backend.pointer_ = (12, 34)
        got = self.query(self.root)
        self.assertEqual(got[1:3], (12, 34))

    def test_the_routed_position_is_used_when_the_backend_has_none(self):
        """The proxy's own last routed position: what makes `getmouselocation`
        after `mousemove` answer the number the move asked for, on a
        compositor whose IPC has no cursor at all."""
        self.assertIsNone(self.backend.pointer_)
        self.rig.server.pointer_model = (7, 9)
        self.assertEqual(self.query(self.root)[1:3], (7, 9))

    def test_win_x_and_win_y_move_by_the_same_delta(self):
        """Upstream answered `root 640,360 / win 600,300`, so the window's
        origin is 40,60 whatever window it was: the recomputed pair keeps that
        origin rather than inventing one."""
        self.rig.server.pointer_model = (100, 200)
        got = self.query(self.root)
        self.assertEqual((got[1], got[2], got[3], got[4]), (100, 200, 60, 140))

    def test_the_child_becomes_the_shadow_under_the_position(self):
        """The foot is at (10, 20) 300x200, so (100, 100) is inside it."""
        self.rig.server.pointer_model = (100, 100)
        self.assertEqual(self.query(self.root)[0], self.shadow())

    def test_upstreams_child_stands_where_no_native_window_is(self):
        """`hit_test` naming nothing leaves the X window upstream named: an X
        client under the pointer is upstream's own truth."""
        self.rig.server.pointer_model = (1000, 700)
        self.assertEqual(self.query(self.root)[0], 0x99)


class QueryPointerOnShadowAnswers(ReadCase):
    """A shadow id may never be forwarded (design section 3.2)."""

    num, upstream_num = 692, 693

    def make_backend(self):
        """Two native toplevels. The second one is what `child` is measured
        against: a sibling under the pointer is not a child of the window the
        request asked about."""
        first = foot_window(11)
        second = foot_window(12, title="WXL-Foot-2", focused=False,
                             x=500, y=400, w=300, h=200)
        return FakeBackend(windows=[first, second],
                           views=[fake_view(first, xid=0, app_id="foot",
                                            instance="foot", cls="foot"),
                                  fake_view(second, xid=0, app_id="foot",
                                            instance="foot", cls="foot")])

    def shadow(self, handle=11):
        return self.shadows.entries[handle].shadow

    def test_the_shadow_id_never_reaches_the_server(self):
        self.rig.server.pointer_model = (100, 100)
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                               struct.pack("<I", self.shadow()))
        self.assertEqual(pkt[1], 1, "same_screen")
        self.assertNotIn(("QueryPointer", self.shadow()),
                         self.rig.upstream.log)
        self.assertIn(("GetInputFocus",), [(row[0],) for row
                                           in self.rig.upstream.log],
                      "the ANSWER substitute went in its place")

    def test_win_x_and_win_y_are_relative_to_the_shadows_own_rect(self):
        """The foot is at (10, 20); the pointer at (100, 100) is 90, 80 into
        it."""
        self.rig.server.pointer_model = (100, 100)
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                               struct.pack("<I", self.shadow()))
        child, rx, ry, wx, wy = struct.unpack_from("<Ihhhh", pkt, 12)
        self.assertEqual((rx, ry, wx, wy), (100, 100, 90, 80))
        self.assertEqual(child, 0, "the window asked about is not its own child")

    def test_the_mask_carries_what_this_proxy_holds(self):
        """Physical modifiers the user is holding are NOT in it: they reach
        Xwayland's XKB state and there is no upstream answer to take them from
        on a shadow. Not yet, and the route is evdev -- /dev/input/event*,
        which the daemon already opens on the uinput path (AGENTS.md rung 4)."""
        engine = self.rig.server.xtest
        engine.keymap.rows[37] = (0xFFE3,)          # Control_L
        conn = self.rig.server.conns[0]
        conn.held.add(37)
        conn.buttons.add(1)
        self.addCleanup(conn.held.clear)
        self.addCleanup(conn.buttons.clear)
        self.rig.server.pointer_model = (100, 100)
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                               struct.pack("<I", self.shadow()))
        (mask,) = struct.unpack_from("<H", pkt, 24)
        self.assertEqual(mask, 0x04 | 0x0100, "ControlMask | Button1Mask")

    def test_a_sibling_under_the_pointer_is_not_this_windows_child(self):
        """X's `child` is the child of the window ASKED ABOUT that contains the
        pointer. A shadow has no children at all -- `QueryTree` on one answers
        none (design section 4.1) -- so the answer is 0 whatever toplevel the
        pointer is over, and the second foot is there to prove the 0 is a
        decision rather than an empty hit test: the same position on the ROOT,
        where `child` IS a child of the window asked about, names it."""
        self.rig.server.pointer_model = (600, 500)      # inside the second foot
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                               struct.pack("<I", self.shadow(11)))
        child, rx, ry = struct.unpack_from("<Ihh", pkt, 12)
        self.assertEqual((rx, ry), (600, 500))
        self.assertEqual(child, 0, "a sibling toplevel is not a child")
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                               struct.pack("<I", self.root))
        (on_root,) = struct.unpack_from("<I", pkt, 12)
        self.assertEqual(on_root, self.shadow(12),
                         "the hit test does find the second foot there")

    def test_upstreams_own_answer_is_the_third_source_on_a_shadow(self):
        """Design section 6.6's third source, on the one path that cannot
        forward: the proxy asks its OWN connection `QueryPointer(root)` rather
        than inventing a number. Upstream answers (640, 360) here, which is
        what real xdotool reads off Xwayland on the headless rig
        [recon/env.md 2.2]."""
        self.assertIsNone(self.rig.server.pointer_model)
        self.assertIsNone(self.backend.pointer_)
        mark = len(self.rig.upstream.log)
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                               struct.pack("<I", self.shadow(11)))
        child, rx, ry, wx, wy = struct.unpack_from("<Ihhhh", pkt, 12)
        self.assertEqual((rx, ry), (640, 360))
        self.assertEqual((wx, wy), (630, 340), "relative to the foot at 10,20")
        self.assertEqual(child, 0)
        asked = [win for (name, win) in
                 [(row[0], row[-1]) for row in self.rig.upstream.log[mark:]]
                 if name == "QueryPointer"]
        self.assertEqual(asked, [self.rig.upstream.ROOTS[0]],
                         "one question, about the ROOT, and never about the "
                         "shadow")

    def test_with_no_source_at_all_the_origin_is_answered_and_said_once(self):
        """Upstream gone too: the answer is the window's own origin, and the
        line that says the number is not a measurement is said ONCE -- a poller
        of a shadow would otherwise be the only thing in the log."""
        # Put it back for the teardown: the rig closes the connection it owns,
        # and a test that dropped the reference leaked the socket.
        self.addCleanup(setattr, self.rig.server, "own", self.rig.server.own)
        self.rig.server.own = None
        log = []
        self.rig.server.say = log.append
        for _ in range(3):
            pkt, _body = self.send(wire.OP_QUERY_POINTER, 0,
                                   struct.pack("<I", self.shadow(11)))
        child, rx, ry, wx, wy = struct.unpack_from("<Ihhhh", pkt, 12)
        self.assertEqual((rx, ry, wx, wy), (10, 20, 0, 0))
        self.assertEqual(child, 0)
        self.assertEqual(len(log), 1, log)
        self.assertIn("not yet", log[0])
        self.assertIn("rung", log[0])

    def test_a_dead_shadow_id_passes_and_upstream_answers(self):
        """An id the registry does not know is not the proxy's business
        (design section 3.1)."""
        self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", 0x6DEAD0))
        self.assertIn(("QueryPointer", 0x6DEAD0), self.rig.upstream.log)


class QueryPointerChildWithNoSource(ReadCase):
    """`child` is edited even when no proxy-side source knows the position.

    The third source of design section 6.6 is upstream's own answer, and the
    shadow under THAT is what `getmouselocation` has to print: without this the
    `window:` field named an X window (or 0) with a `foot` sitting under
    Xwayland's own (640, 360) [recon/env.md 2.2] until something moved the
    pointer.
    """

    num, upstream_num = 696, 697

    def make_backend(self):
        return foot_backend(foot_window(11, x=600, y=300, w=200, h=200))

    def query(self, win):
        pkt, _body = self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", win))
        return struct.unpack_from("<IhhhhH", pkt, 12)

    def test_the_child_is_the_shadow_under_upstreams_own_position(self):
        self.assertIsNone(self.rig.server.pointer_model)
        self.assertIsNone(self.backend.pointer_)
        got = self.query(self.root)
        self.assertEqual(got[0], self.shadow())
        self.assertEqual(got[1:3], (640, 360), "upstream's own number stands")
        self.assertEqual(got[3:5], (600, 300), "win_x/win_y untouched too")
        self.assertEqual(got[5], 0x0004, "and the mask")

    def test_upstreams_child_still_stands_where_the_foot_is_not(self):
        """The negative twin: the edit replaces `child` only where the hit test
        names a native toplevel."""
        self.backend.windows[0].x = 5
        self.backend.windows[0].y = 5
        self.shadows.invalidate()
        self.assertEqual(self.query(self.root)[0], 0x99)


class QueryPointerSeedsDaemon(ReadCase):
    """Every position a source other than the daemon knows is seeded into it,
    so a relative move starts from truth (design section 6.6)."""

    num, upstream_num = 694, 695

    def setUp(self):
        self.tmp = support.tempfile.mkdtemp(prefix="xw11-seed-")
        self.addCleanup(support.shutil.rmtree, self.tmp, True)
        self.addCleanup(stop_daemons_under, self.tmp)
        self.env = support.env(XDG_RUNTIME_DIR=self.tmp)
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        real_spawn = daemon_mod.DaemonClient._spawn
        daemon_mod.DaemonClient._spawn = staticmethod(lambda: None)
        self.addCleanup(setattr, daemon_mod.DaemonClient, "_spawn", real_spawn)
        self.daemon = support.FakeDaemon(self.tmp)
        self.addCleanup(self.daemon.stop)
        super().setUp()
        self.rig.server.xtest.route()
        del self.daemon.ops[:]

    def test_the_backends_position_is_sent_to_the_daemon_once(self):
        self.backend.pointer_ = (12, 34)
        self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", self.root))
        self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", self.root))
        self.assertEqual([(r["x"], r["y"]) for r in self.daemon.of("seed_pointer")],
                         [(12, 34)],
                         "xdotool mousemove sends two QueryPointers "
                         "[recon/tools.md 4.5]; the daemon's model does not "
                         "move between them")

    def test_a_position_only_this_proxy_routed_costs_no_round_trip(self):
        """The daemon put that number there itself."""
        self.rig.server.xtest.moved(50, 60)
        self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", self.root))
        self.assertEqual(self.daemon.of("seed_pointer"), [])

    def test_nothing_is_seeded_when_nothing_knows(self):
        self.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", self.root))
        self.assertEqual(self.daemon.of("seed_pointer"), [])


class RootEventsInvalidateTheRegistry(unittest.TestCase):
    """`OwnConn.interesting` -- the drain's filter. Every packet on the proxy's
    own connection used to drop the registry's cache, which defeats the 20 ms
    TTL on a busy X plane: one `xprop -set` anywhere under the root cost the
    next read a whole `views()` (design section 2.4 step 1)."""

    def setUp(self):
        from xw11 import upstream as upstream_mod

        self.own = upstream_mod.OwnConn(":0")
        self.own.atoms = {"_NET_CLIENT_LIST": 307, "RESOURCE_MANAGER": 23,
                          "_NET_ACTIVE_WINDOW": 247}
        self.own.atom_names = {v: k for k, v in self.own.atoms.items()}

    def event(self, code, atom=0):
        return struct.pack("<BxHIIIB15x", code, 0, 0x234, atom, 0, 0)

    def test_structure_events_are_worth_a_relist(self):
        for code in (wire.EV_CREATE_NOTIFY, wire.EV_DESTROY_NOTIFY,
                     wire.EV_MAP_NOTIFY, wire.EV_UNMAP_NOTIFY,
                     wire.EV_CONFIGURE_NOTIFY):
            self.assertTrue(self.own.interesting(self.event(code)), code)

    def test_a_property_the_proxy_answers_for_is_worth_a_relist(self):
        for name in ("_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW"):
            pkt = self.event(wire.EV_PROPERTY_NOTIFY, self.own.atoms[name])
            self.assertTrue(self.own.interesting(pkt), name)

    def test_any_other_property_write_under_the_root_is_not(self):
        pkt = self.event(wire.EV_PROPERTY_NOTIFY, self.own.atoms["RESOURCE_MANAGER"])
        self.assertFalse(self.own.interesting(pkt))
        self.assertFalse(self.own.interesting(self.event(wire.EV_PROPERTY_NOTIFY, 0)))

    def test_a_focus_event_is_not_and_a_mapping_notify_is(self):
        self.assertFalse(self.own.interesting(self.event(wire.EV_FOCUS_IN)))
        self.assertTrue(self.own.interesting(self.event(34)))


class SupportedUnionUnit(unittest.TestCase):
    """`shadow.supported_union` on its own, with a table instead of a server."""

    class Atoms:
        def __init__(self):
            self.by_name = {}

        def atom_id(self, name, default=0):
            return self.by_name.setdefault(name, 100 + len(self.by_name))

    def test_upstreams_ids_are_kept_in_upstreams_order_and_ours_appended(self):
        atoms = self.Atoms()
        upstream = [atoms.atom_id(n) for n in WLROOTS_SUPPORTED]
        got = shadow_mod.supported_union(upstream, atoms)
        self.assertEqual(got[:19], upstream)
        self.assertEqual(got[19:], [atoms.atom_id(n) for n in OURS_APPENDED])
        self.assertEqual(len(got), 44)

    def test_a_name_the_proxy_cannot_intern_is_left_out(self):
        """Atom 0 is `None` on the wire; a `_NET_SUPPORTED` carrying it makes
        xprop print a name nobody can resolve."""
        class Broken(self.Atoms):
            def atom_id(self, name, default=0):
                return 0

        self.assertEqual(shadow_mod.supported_union([5, 6], Broken()), [5, 6])


if __name__ == "__main__":
    unittest.main()
