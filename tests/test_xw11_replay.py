#!/usr/bin/env python3
"""Eleven real command streams, replayed byte for byte through the proxy.

`tests/fixtures/xw11/caps/*.hex` is the request stream each of eleven commands
sent to a live Xwayland under headless sway, one request per line, rebuilt from
the decoded capture at recon/tools/caps/xwl.jsonl (and xvfb2.jsonl for
`getwindowfocus`, the one the 64-command harness ran on the Xvfb rig only). The
request COUNTS match recon/tools.md 11 exactly -- 93, 37, 38, 36, 58, 12, 20,
40, 13, 41 -- which is what says the rebuild is the capture and not a
paraphrase of it.

Two claims, and they are different claims:

* **nothing hangs and nothing misframes.** The whole stream goes down one raw
  socket, pipelined the way libX11 pipelines it, and every request draws
  exactly one packet back, in order, with its own sequence. A proxy that ate a
  request without substituting for it wedges libxcb for ever [recon/wire.md
  3.3's `swallow` row], and a proxy that mis-split one frame desynchronises the
  rest of the connection.
* **the substitution is where the policy says.** The upstream connection sees
  the same opcodes in the same places, with each locally answered request
  replaced by `GetInputFocus` (43) at that same position -- never one more,
  never one fewer, never moved.

Ids are tokenised: the capture's root and the capture's window are rewritten to
the rig's root and to the shadow of the one native toplevel the fake backend
holds, so a stream captured against a real xterm exercises the shadow path.
The capture's atom ids are seeded into the fake server, because atoms are
server-global and the ids in the stream have to be the ids the proxy interns
[recon/wire.md 7.2].
"""

import os
import re
import select
import shutil
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
from xw11 import policy, wire                                      # noqa: E402

CAPS = os.path.join(ROOT, "tests", "fixtures", "xw11", "caps")

#: The eleven streams, with the request count recon/tools.md 11 measured for
#: each. The count is asserted, so a fixture that lost a line fails here rather
#: than passing a shorter stream.
STREAMS = (
    ("xdotool-search-class", 93),
    ("xdotool-getactivewindow", 37),
    ("xdotool-getwindowgeometry", 38),
    ("xdotool-getwindowfocus", 36),
    ("xdotool-get_desktop", 36),
    ("wmctrl-l", 58),
    ("wmctrl-d", 12),
    ("wmctrl-m", 20),
    ("xprop-root", 40),
    ("xprop-root-one", 13),
    ("xprop-id", 41),
)


class Capture:
    """One fixture: the header's tokens and the frames under it."""

    def __init__(self, name):
        self.name = name
        self.root = 0
        self.windows = []
        self.atoms = {}
        self.frames = []
        with open(os.path.join(CAPS, name + ".hex"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    self._header(line[1:].strip())
                    continue
                self.frames.append(bytes.fromhex(line.split("#")[0].strip()))

    def _header(self, text):
        got = re.match(r"^ROOT 0x([0-9a-f]+)$", text)
        if got:
            self.root = int(got.group(1), 16)
        got = re.match(r"^WINDOW 0x([0-9a-f]+)$", text)
        if got:
            self.windows.append(int(got.group(1), 16))
        got = re.match(r"^ATOM (\d+) (\S+)$", text)
        if got and int(got.group(1)):
            self.atoms[got.group(2)] = int(got.group(1))

    def opcodes(self):
        return [f[0] for f in self.frames]

    def tokenise(self, root, window):
        """The stream with the capture's root and window rewritten, on 4-byte
        boundaries only -- an id is a CARD32 field and a match halfway through
        one would be a coincidence, not a window."""
        out = []
        for frame in self.frames:
            buf = bytearray(frame)
            for i in range(0, len(buf) - 3, 4):
                (word,) = struct.unpack_from("<I", buf, i)
                if word == self.root:
                    struct.pack_into("<I", buf, i, root)
                elif self.windows and word == self.windows[0]:
                    struct.pack_into("<I", buf, i, window)
            out.append(bytes(buf))
        return out


class _Upstream(support.FakeUpstream):
    """`FakeUpstream` with the capture's atom ids and a log of every opcode.

    The atom seeding is what makes a replayed stream mean what it meant: the
    stream carries `GetProperty(..., property = 307)` and the proxy has to
    intern `_NET_CLIENT_LIST` to that same 307, which it does because atoms are
    server-global and both ask this one server [recon/wire.md 7.2]. New names
    are numbered above every seeded id so a fresh intern cannot land on one.
    """

    def __init__(self, *a, atoms=None, **kw):
        # `ops` before `super().__init__`, which is where `FakeXServer` starts
        # its accept thread; the atom tables after, because that constructor
        # assigns fresh ones over anything set here (support.py:1971). Nothing
        # can connect in between: the proxy that dials this server is not
        # constructed until `ProxyRig` has this object back.
        self.ops = []
        super().__init__(*a, **kw)
        for name, atom in (atoms or {}).items():
            self._atoms[name] = atom
            self._names[atom] = name

    def intern(self, name: str) -> int:
        got = self._atoms.get(name)
        if got:
            return got
        fixed = policy.PREDEFINED_ATOMS.get(name)
        atom = fixed or max([100] + list(self._names)) + 1
        self._atoms[name] = atom
        self._names[atom] = name
        return atom

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        self.ops.append(opcode)
        if opcode == 21:                        # ListProperties
            (win,) = struct.unpack_from("<I", payload, 0)
            atoms = [self.intern(n) for (w, n) in self.props if w == win]
            body = struct.pack("<%dI" % len(atoms), *atoms)
            conn.sendall(struct.pack("<BxHIH22x", 1, seq, len(body) // 4,
                                     len(atoms)) + body)
            return
        return super()._dispatch(conn, opcode, dbyte, payload, seq)


class _Rig(support.ProxyRig):
    def __init__(self, atoms=None, **kw):
        real = support.FakeUpstream

        def build(*a, **kws):
            return _Upstream(*a, atoms=atoms, **kws)

        support.FakeUpstream = build
        try:
            super().__init__(**kw)
        finally:
            support.FakeUpstream = real


def foot_backend():
    """One native toplevel with everything the eleven streams read: a title, a
    WM_CLASS pair, a pid, a rect and the focus."""
    win = fake_window(11, title="WXL-Foot", class_="foot", instance="foot",
                      pid=4242, x=10, y=20, w=300, h=200, visible=True,
                      focused=True, desktop=0)
    return FakeBackend(windows=[win],
                       views=[fake_view(win, xid=0, app_id="foot",
                                        instance="foot", cls="foot")])


class Replay(unittest.TestCase):
    """One rig per stream: a stream is 12 to 93 requests and the proxy's own
    connection is opened once per rig, so a shared rig would make the upstream
    opcode list a function of the order the tests ran in."""

    num = 680
    upstream_num = 681

    def replay(self, name):
        cap = Capture(name)
        backend = foot_backend()
        rig = _Rig(atoms=cap.atoms, num=self.num, upstream_num=self.upstream_num,
                   passthrough=False, backend=backend)
        self.addCleanup(rig.stop)
        Replay.num += 2
        Replay.upstream_num += 2
        warm = rig.conn()                       # forces the own connection open
        rig.wait_own()
        rig.server.shadows.refresh()
        shadow = [e.shadow for e in rig.server.shadows.snapshot() if e.shadow][0]
        root = warm.root()
        warm.close()
        rig.wait(lambda: rig.server.live == 0, what="the warm-up client to go")
        del rig.upstream.ops[:]

        sock, _setup = rig.raw(timeout=20.0)
        frames = cap.tokenise(root, shadow)
        sock.sendall(b"".join(frames))
        got = self.read_all(sock, len(frames))
        rig.wait(lambda: len(rig.upstream.ops) >= self.expected_ups(cap),
                 timeout=10.0, what="the upstream request log")
        return cap, frames, got, list(rig.upstream.ops), root, shadow

    @staticmethod
    def expected_ups(cap):
        """Every request costs one upstream request, always: the ones the proxy
        answers cost a `GetInputFocus` in their place (design section 3.1)."""
        return len(cap.frames)

    def read_all(self, sock, want, timeout=20.0):
        """Every packet the client is owed, framed the way a client frames
        them: 32 bytes, plus 4 * the length word for a reply or a
        GenericEvent [recon/wire.md 3.1]."""
        buf = b""
        out = []
        deadline = timeout
        while len(out) < want:
            while len(buf) >= 32:
                total = 32
                if buf[0] == 1 or (buf[0] & 0x7F) == 35:
                    total += 4 * struct.unpack_from("<I", buf, 4)[0]
                if len(buf) < total:
                    break
                out.append(buf[:total])
                buf = buf[total:]
            if len(out) >= want:
                break
            r, _w, _x = select.select([sock], [], [], deadline)
            if not r:
                break
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            deadline = 5.0
        return out


def _make_stream_test(name, count):
    def test(self):
        cap, frames, got, ups, root, shadow = self.replay(name)
        self.assertEqual(len(frames), count,
                         "recon/tools.md 11 counted %d requests for %s"
                         % (count, name))
        # One packet per request, in order, each carrying its own sequence.
        replies = [p for p in got if p[0] in (0, 1)]
        self.assertEqual(len(replies), len(got),
                         "an event turned up where only replies were owed")
        seqs = [struct.unpack_from("<H", p, 2)[0] for p in got]
        self.assertEqual(seqs, sorted(seqs), "the replies came back out of order")
        self.assertTrue(got, "the proxy answered nothing at all")
        self.assertEqual(seqs[-1], len(frames),
                         "the last reply's sequence is not the last request's")
        # The upstream stream is the same opcodes with the answered ones
        # substituted, at the same positions.
        want = cap.opcodes()
        self.assertEqual(len(ups), len(want))
        for i, (mine, theirs) in enumerate(zip(ups, want)):
            if mine == theirs:
                continue
            self.assertEqual(mine, wire.OP_GET_INPUT_FOCUS,
                             "request %d (op %d) went upstream as op %d, which "
                             "is neither itself nor the ANSWER substitute"
                             % (i + 1, theirs, mine))
    test.__name__ = "test_" + name.replace("-", "_")
    test.__doc__ = ("`%s` replayed: %d requests, %d packets back, and the "
                    "upstream stream differs only where the policy says ANSWER."
                    % (name, count, count))
    return test


for _name, _count in STREAMS:
    setattr(Replay, "test_" + _name.replace("-", "_"),
            _make_stream_test(_name, _count))


class WhatTheCommandsRead(Replay):
    """The replays above pin the framing; these pin the ANSWERS, by decoding
    what a client would have read out of the replies the stream drew."""

    num = 720
    upstream_num = 721

    def test_search_class_reads_the_shadows_wm_class(self):
        """`xdotool search --class` walks the tree and reads `WM_CLASS` per
        window [recon/tools.md 4.1]. Every `WM_CLASS` read in the stream comes
        back `STRING`, `foot\\0foot\\0` -- wmctrl and xdotool both read a type
        mismatch as absent [recon/tools.md 5]."""
        cap, frames, got, _ups, _root, _shadow = self.replay("xdotool-search-class")
        wanted = cap.atoms.get("WM_CLASS", 67)
        pairs = []
        for i, frame in enumerate(frames):
            if frame[0] != wire.OP_GET_PROPERTY:
                continue
            _w, atom = struct.unpack_from("<II", frame, 4)
            if atom == wanted:
                pairs.append(got[i])
        self.assertTrue(pairs, "the stream read no WM_CLASS at all")
        classes = [p[32:32 + struct.unpack_from("<I", p, 16)[0]] for p in pairs
                   if struct.unpack_from("<I", p, 16)[0]]
        self.assertIn(b"foot\0foot\0", classes)

    def test_get_desktop_is_given_the_supported_name_it_gave_up_over(self):
        """The capture ENDS where the command gave up: `xdotool get_desktop`
        reads `_NET_SUPPORTED`, does not find `_NET_CURRENT_DESKTOP` in it,
        prints "Your windowmanager claims not to support _NET_CURRENT_DESKTOP"
        and exits 1 after 36 requests -- it never sends the read it came for
        [recon/tools.md 4.10]. So the thing to pin is the reply it turned back
        on: through the proxy that `_NET_SUPPORTED` carries the atom, which is
        what turns those 36 requests into a command that goes on.
        """
        cap, frames, got, _ups, root, _shadow = self.replay("xdotool-get_desktop")
        atom = cap.atoms["_NET_SUPPORTED"]
        want = cap.atoms["_NET_CURRENT_DESKTOP"]
        seen = 0
        for i, frame in enumerate(frames):
            if frame[0] != wire.OP_GET_PROPERTY:
                continue
            win, prop = struct.unpack_from("<II", frame, 4)
            if win != root or prop != atom:
                continue
            pkt = got[i]
            _type, after, nitems = struct.unpack_from("<III", pkt, 8)
            self.assertEqual(after, 0)
            names = struct.unpack_from("<%dI" % nitems, pkt, 32)
            self.assertIn(want, names)
            seen += 1
        self.assertEqual(seen, 1, "the stream read _NET_SUPPORTED %d times" % seen)
        self.assertNotIn(wire.OP_GET_PROPERTY,
                         [f[0] for f in frames
                          if f[0] == wire.OP_GET_PROPERTY
                          and struct.unpack_from("<I", f, 8)[0] == want],
                         "the capture read _NET_CURRENT_DESKTOP after all")

    def test_wmctrl_l_reads_a_client_list_naming_the_shadow(self):
        cap, frames, got, _ups, root, shadow = self.replay("wmctrl-l")
        atom = cap.atoms["_NET_CLIENT_LIST"]
        for i, frame in enumerate(frames):
            if frame[0] != wire.OP_GET_PROPERTY:
                continue
            win, prop = struct.unpack_from("<II", frame, 4)
            if win == root and prop == atom:
                pkt = got[i]
                _type, after, nitems = struct.unpack_from("<III", pkt, 8)
                self.assertEqual((after, nitems), (0, 1))
                self.assertEqual(struct.unpack_from("<I", pkt, 32), (shadow,))
                return
        self.fail("wmctrl -l never read _NET_CLIENT_LIST")

    def test_xprop_id_on_the_shadow_lists_the_property_table(self):
        """The capture's `xprop -id` names a real xterm; tokenised onto the
        shadow it lists design section 4.4's set instead of xterm's thirteen."""
        cap, frames, got, _ups, _root, shadow = self.replay("xprop-id")
        for i, frame in enumerate(frames):
            if frame[0] != wire.OP_LIST_PROPERTIES:
                continue
            (win,) = struct.unpack_from("<I", frame, 4)
            if win != shadow:
                continue
            pkt = got[i]
            (count,) = struct.unpack_from("<H", pkt, 8)
            self.assertEqual(count, 11)
            self.assertEqual(len(pkt), 32 + 4 * count)
            return
        self.fail("xprop -id never sent ListProperties on the shadow")


# -- the xrandr streams --------------------------------------------------------
#
# `tests/fixtures/xw11/randr/caps/*.hex` is the request stream each of thirteen
# `xrandr` invocations sent, captured 2026-09-10 THROUGH the proxy on a live
# headless sway (scratchpad/b7/capp1.log and capp2.log). Through the proxy and
# not against Xwayland directly, because these are the streams a write path
# sends when nothing REFUSES it: against Xwayland alone, `--mode`, `--rotate`
# and `--off` all die inside Xlib's default error handler at the SetScreenSize,
# with the server still grabbed and their UngrabServer never sent
# [recon/tools.md 7, recon/env.md 2.4]. Every one of those runs exited 0
# through the proxy and sway's own outputs really moved -- which is
# recon/tools.md 11 rows 56-62's exit 1s becoming 0s, in wire form.
#
# The claim each replay makes is different from the eleven above: an xrandr
# stream does NOT draw one packet per request (a grab, a SetScreenSize and a
# SetOutputPrimary each draw none), so what is asserted here is that the client
# gets no error at all, that the upstream's request count is the client's to the
# request, and that the grab produced exactly the applies it should have --
# none for a read, one for a write.

XRANDR_CAPS = os.path.join(ROOT, "tests", "fixtures", "xw11", "randr", "caps")

#: Sequences 3, 6 and 9 of every one of these streams are `CreateGC`,
#: `XKEYBOARD.UseExtension` and `GenericEvent.QueryVersion` -- three requests of
#: libX11's identical nine-request prologue [recon/tools.md 2] that `FakeXServer`
#: does not implement and answers `BadRequest`. They are asserted rather than
#: worked around: the proxy forwards all three untouched, and an error that
#: turned up anywhere ELSE is a RandR request the proxy broke.
PROLOGUE_UNIMPLEMENTED = (3, 6, 9)

#: The root of the rig the streams were captured on. Rewritten to the fake
#: server's own root so the requests name a window this server really has, the
#: way `Capture.tokenise` does it for the eleven above.
CAPTURE_ROOTS = {"one": 0x234, "two": 0x236}

#: name -> (the rig's tables, how many applies the grab should commit, and the
#: target names the last apply should carry). The counts are the measured runs':
#: `-q` and `--listmonitors` write nothing, every `--output` path writes once,
#: and the two runs made while HEADLESS-1 was OFF write nothing because xrandr
#: cannot see an output the compositor has switched off -- which is a row in
#: docs/XW11.md with its route, not a silence.
XRANDR_STREAMS = (
    ("xrandr-q", "sway", "one", 0, ()),
    ("xrandr-listmonitors", "sway", "one", 0, ()),
    ("xrandr-mode", "sway", "one", 1, ("HEADLESS-1",)),
    ("xrandr-pos", "sway", "one", 1, ("HEADLESS-1",)),
    ("xrandr-primary", "sway", "one", 1, ("HEADLESS-1",)),
    ("xrandr-rotate-left", "sway", "one", 1, ("HEADLESS-1",)),
    ("xrandr-rotate-normal", "sway", "one", 1, ("HEADLESS-1",)),
    ("xrandr-off", "sway", "one", 1, ("HEADLESS-1",)),
    ("xrandr-auto-after-off", "sway", "one", 0, ()),
    ("xrandr-s-after-off", "sway", "one", 0, ()),
    ("xrandr-two-right-of", "sway2", "two", 1, ("HEADLESS-1", "HEADLESS-2")),
    ("xrandr-two-left-of", "sway2", "two", 1, ("HEADLESS-1", "HEADLESS-2")),
    ("xrandr-two-mode-and-right-of", "sway2", "two", 1,
     ("HEADLESS-1", "HEADLESS-2")),
)


def xrandr_stream(name, root):
    """One capture's frames, with the capture rig's root rewritten to `root`.

    The substitution is asserted rather than hoped for: a stream that carried no
    root at all would otherwise replay as a stream naming a window the fake
    server has never heard of, and pass.
    """
    frames = []
    with open(os.path.join(XRANDR_CAPS, name + ".hex"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                frames.append(bytes.fromhex(line))
    return frames


def retarget_root(frames, was, now):
    """The capture's root id, wherever it appears as a whole 4-byte field."""
    old = struct.pack("<I", was)
    new = struct.pack("<I", now)
    hits = 0
    out = []
    for f in frames:
        hits += f.count(old)
        out.append(f.replace(old, new))
    return out, hits


class XrandrReplay(unittest.TestCase):
    """Thirteen whole `xrandr` invocations, replayed against a fake upstream
    that answers the RandR reads with the same server's own measured bytes and a
    fake layout backend that records what it was asked to apply."""

    num = 720
    upstream_num = 721

    def replay(self, name, tables, rig_key):
        import tempfile

        backend = support.FakeBackend(windows=[support.fake_window(1, "w1")])
        rig = support.ProxyRig(num=XrandrReplay.num,
                               upstream_num=XrandrReplay.upstream_num,
                               passthrough=False, backend=backend)
        self.addCleanup(rig.stop)
        XrandrReplay.num += 2
        XrandrReplay.upstream_num += 2
        rig.upstream.randr = support.RandrTables(tables)
        # `--listmonitors` resolves the monitor's name atom -- 0x136 on the rig
        # tests/fixtures/xw11/randr/sway-atomname-336.hex came off and 0x140 on
        # the one these streams did, because an atom id is whatever that server
        # happened to hand out. Atoms are server-global [recon/wire.md 7.2], so
        # the fake has to BE the server that minted it or the stream asks about
        # an id nobody has ever heard of.
        for atom in (0x136, 0x140):
            rig.upstream._atoms.setdefault("HEADLESS-1", atom)
            rig.upstream._names[atom] = "HEADLESS-1"
        statedir = tempfile.mkdtemp(prefix="xw11-replay-state-")
        self.addCleanup(shutil.rmtree, statedir, True)
        outs = [support.randr_output("HEADLESS-1", modes=_REPLAY_MODES())]
        if tables == "sway2":
            outs = [support.randr_output("HEADLESS-2", x=1280,
                                         modes=_REPLAY_MODES()),
                    support.randr_output("HEADLESS-1", x=2560,
                                         modes=_REPLAY_MODES())]
        layout = support.install_fake_randr(
            rig.server, support.FakeRandrBackend(outputs=outs), statedir)
        before = len(rig.upstream.wires)
        sock, _setup = rig.raw(timeout=20.0)
        rig.wait_own()
        rig.wait(lambda: len(rig.upstream.wires) > before,
                 what="the client's own upstream connection")
        twin = rig.upstream.wires[-1]
        frames, hits = retarget_root(xrandr_stream(name, 0),
                                     CAPTURE_ROOTS[rig_key],
                                     support.FakeXServer.ROOTS[0])
        self.assertGreater(hits, 0, "%s names no root at all" % name)
        sock.sendall(b"".join(frames))
        rig.wait(lambda: twin.seq >= len(frames), timeout=20.0,
                 what="the upstream request count")
        time.sleep(0.3)                 # and let a wrong extra one show up
        return rig, layout, frames, sock, twin

    def read_packets(self, sock, timeout=2.0):
        """Everything the client is owed and nothing more: read until the socket
        goes quiet, because an xrandr stream does not draw one packet per
        request."""
        buf = b""
        out = []
        while True:
            r, _w, _x = select.select([sock], [], [], timeout)
            if not r:
                break
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            timeout = 0.5
            while len(buf) >= 32:
                total = 32
                if buf[0] == 1 or (buf[0] & 0x7F) == 35:
                    total += 4 * struct.unpack_from("<I", buf, 4)[0]
                if len(buf) < total:
                    break
                out.append(buf[:total])
                buf = buf[total:]
        return out


def _REPLAY_MODES():
    """The two modes every one of these rigs really had: 1280x720 and 800x600
    at 59.86 Hz, from the modelines of the measured mode table."""
    return [support.randr_mode(1280, 720, 59.86, preferred=True),
            support.randr_mode(800, 600, 59.86)]


def _make_xrandr_test(name, tables, rig_key, applies, names):
    def test(self):
        rig, layout, frames, sock, twin = self.replay(name, tables, rig_key)
        packets = self.read_packets(sock)
        errors = [p for p in packets if p[0] == 0]
        where = [struct.unpack_from("<H", p, 2)[0] for p in errors]
        self.assertEqual(where, list(PROLOGUE_UNIMPLEMENTED),
                         "%s drew errors at %r; the only ones this fake owes "
                         "are the three prologue requests it does not "
                         "implement" % (name, where))
        randr_major = support.FAKE_EXTENSIONS["RANDR"][0]
        self.assertEqual([p for p in errors if p[10] == randr_major], [],
                         "a RandR request drew an error: rows 56-62 of "
                         "recon/tools.md 11 are exit 1 again")
        seqs = [struct.unpack_from("<H", p, 2)[0] for p in packets]
        self.assertEqual(seqs, sorted(seqs), "the packets came back out of order")
        self.assertTrue(all(s <= len(frames) for s in seqs),
                        "a packet carried a sequence past the last request")
        self.assertEqual(twin.seq, len(frames),
                         "the upstream saw %d requests for the client's %d: the "
                         "sequence delta is not zero" % (twin.seq, len(frames)))
        self.assertEqual(len(layout.applies), applies)
        if applies:
            self.assertEqual(sorted(t.name for t in layout.applies[-1]),
                             sorted(names))
    test.__name__ = "test_" + name.replace("-", "_")
    test.__doc__ = ("`%s` replayed: no error reaches the client, the upstream's "
                    "request count is the client's, and the grab commits %d "
                    "apply/applies." % (name, applies))
    return test


for _n, _t, _k, _a, _names in XRANDR_STREAMS:
    setattr(XrandrReplay, "test_" + _n.replace("-", "_"),
            _make_xrandr_test(_n, _t, _k, _a, _names))


if __name__ == "__main__":
    unittest.main()
