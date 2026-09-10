#!/usr/bin/env python3
"""xw11/randr.py: the grab, the batch, and the one apply.

Every write path `xrandr` has ends in a transaction, not a layout: `GrabServer`,
a `SetCrtcConfig` per crtc that has to change, a `SetScreenSize` when the
framebuffer moves, `SetOutputPrimary` when `--primary` was asked for, and
`UngrabServer` [recon/wire.md 6, `xrandr.c:1680 apply()`]. What this file pins is
that the proxy reads that transaction as one thing:

* **one apply per grab**, whatever was in it -- and **none** for an empty one,
  which is what `--dryrun` sends;
* **the reply to `SetCrtcConfig` goes out before the grab closes**, because
  `XRRSetCrtcConfig` blocks on it and a deferred reply would deadlock the grab
  the commit is waiting for;
* **the last write for a crtc wins**, so the measured disable-then-enable pair
  is one output turned on and not one turned off;
* **a failed apply is an X error with the bytes Xwayland answers with**, so a
  script that checks `$?` still sees 1 and Xlib's default handler still prints
  its five lines.

The requests come out of `tests/fixtures/xw11/xrandr-*.hex`, captured on this
box on 2026-09-10; the replies the proxy reads before it applies come out of
`tests/fixtures/xw11/randr/`, captured the same day off a real Xwayland under
sway, a real Xvfb, and (for the VNC-0 rig) recon's own trace. The layout backend
is `support.FakeRandrBackend`, which is `wxrandr`'s six-method contract
[recon/seams.md 6.2] with a log.
"""

import binascii
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import (FakeBackend, FakeRandrBackend, ProxyRig,       # noqa: E402
                     fake_window, install_fake_randr, randr_mode,
                     randr_output)
from wxrandr import core as wcore                                   # noqa: E402
from xw11 import client as client_mod                               # noqa: E402
from xw11 import policy, randr, upstream, wire                      # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "xw11")

#: The RANDR major of each fixture's own rig. Never a constant in the product:
#: 139 on Xwayland, 140 on Xvfb and on the box's :355 [recon/env.md 2.5,
#: recon/tools.md 7], and `BatchFromCapture` below runs the VNC-0 capture
#: against a fake server that says 140 for exactly that reason.
SWAY_MAJOR = support.FAKE_EXTENSIONS["RANDR"][0]
VNC_MAJOR = 140
VNC_EXTENSIONS = dict(support.FAKE_EXTENSIONS, RANDR=(VNC_MAJOR, 89, 147))

#: FakeXServer's first screen: the root every fixture reply names, and the id
#: the BadMatch of design section 7.5 carries in `bad`.
FAKE_ROOT = support.FakeXServer.ROOTS[0]

GRAB = b"\x24\x00\x01\x00"
UNGRAB = b"\x25\x00\x01\x00"
GET_INPUT_FOCUS = wire.GET_INPUT_FOCUS


def frames(name):
    """The request lines of one `.hex` fixture, `#` comments dropped."""
    out = []
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(binascii.unhexlify(line))
    return out


def recv_packet(sock):
    """One whole server packet: 32 bytes, plus `4 * u32le[4]` for a reply or a
    GenericEvent (recon/wire.md 3.1)."""
    head = _recvn(sock, 32)
    total = 32
    if head[0] == 1 or (head[0] & 0x7F) == 35:
        (extra,) = struct.unpack_from("<I", head, 4)
        total += extra * 4
    return head + _recvn(sock, total - 32)


def _recvn(sock, n):
    got = b""
    while len(got) < n:
        chunk = sock.recv(n - len(got))
        if not chunk:
            raise AssertionError("the proxy closed with %d of %d bytes owed"
                                 % (len(got), n))
        got += chunk
    return got


class RandrCase(unittest.TestCase):
    """A proxy with the shadow machinery on, a fake upstream that answers the
    RandR reads with measured bytes, and a fake layout backend under it."""

    def rig(self, tables="sway", extensions=None, backend=None, **kw):
        kw.setdefault("backend", FakeBackend(windows=[fake_window(1, "w1")]))
        kw.setdefault("passthrough", False)
        kw.setdefault("num", 46)
        kw.setdefault("upstream_num", 47)
        if extensions is not None:
            kw["extensions"] = extensions
        got = ProxyRig(**kw)
        self.addCleanup(got.stop)
        got.upstream.randr = support.RandrTables(tables)
        statedir = tempfile.mkdtemp(prefix="xw11-randr-state-")
        self.addCleanup(_rmtree, statedir)
        self.layout = install_fake_randr(
            got.server, backend or FakeRandrBackend(), statedir)
        return got

    def vnc(self, **kw):
        """The rig recon/wire.md 6's own capture came off: crtc 0x3a, output
        0x3b named VNC-0, and a server that calls RANDR 140. The five requests
        of `xrandr-mode-write.hex` replay against it byte for byte, major
        included -- which is also how this file proves nothing in `xw11/`
        writes a major down."""
        kw.setdefault("backend", FakeRandrBackend(outputs=[randr_output(
            "VNC-0", w=1280, h=1024,
            modes=[randr_mode(1280, 1024, 60.0, preferred=True),
                   randr_mode(1680, 1050, 60.0)])]))
        return self.rig("vnc", extensions=VNC_EXTENSIONS, **kw)

    def client(self, rig):
        """A raw socket with the setup done, and the proxy's own connection
        open -- which is what makes the RandR snapshot readable at all."""
        sock, _body = rig.raw()
        sock.settimeout(5.0)
        rig.wait_own()
        return sock

    def conn(self, rig):
        rig.wait(lambda: rig.server.conns, what="a client connection")
        return rig.server.conns[0]

    def send(self, sock, *frames_):
        sock.sendall(b"".join(frames_))

    def sync(self, sock):
        """`GetInputFocus`, the way every X tool ends a command: it makes the
        proxy answer everything queued before it, and the reply is the proof
        the connection is still good."""
        sock.sendall(GET_INPUT_FOCUS)
        return recv_packet(sock)

    def wait_applies(self, rig, n=1, what="an apply"):
        rig.wait(lambda: len(self.layout.applies) >= n, what=what)
        # and give a second one a chance to be wrong about it
        time.sleep(0.05)
        return self.layout.applies


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class Snapshot(RandrCase):
    """The tables the proxy reads on its own connection at `GrabServer`."""

    def test_the_measured_replies_become_a_resources_object(self):
        """crtc 0x3a, output 0x3b named VNC-0, mode 0x3f = 1680x1050 and the
        config timestamp 0x304a204b -- the ids of recon/wire.md 6's own capture,
        read back through `GetScreenResourcesCurrent`, `GetOutputInfo`,
        `GetCrtcInfo`, `GetOutputPrimary` and `GetScreenInfo`."""
        rig = self.rig("vnc", extensions=VNC_EXTENSIONS)
        sock = self.client(rig)
        self.send(sock, GRAB)
        conn = self.conn(rig)
        rig.wait(lambda: conn.batch is not None, what="a batch")
        res = conn.batch.resources
        self.assertIsNotNone(res)
        self.assertEqual(sorted(res.crtcs), [0x3A])
        self.assertEqual(sorted(res.outputs), [0x3B])
        self.assertEqual(res.output_name(0x3B), "VNC-0")
        self.assertEqual(res.crtcs[0x3A].outputs, (0x3B,))
        self.assertEqual(res.config_timestamp, 0x304A204B)
        self.assertEqual(res.modes[0x3F].size_name, "1680x1050")
        self.assertEqual(res.name_for(0x3A, ()), "VNC-0")

    def test_the_sway_rigs_sixteen_modes_carry_the_refresh_xrandr_prints(self):
        """`mode_refresh_hz` off the modeline, not a rounded number: all sixteen
        modes of this box's HEADLESS-1 come back to the second decimal
        `xrandr -q` printed for them on 2026-09-10 (scratchpad/b7/cap1.log
        connection 1). If this drifts, every stanza's `rate` misses
        `_find_mode_for`'s nearest-refresh match."""
        res = randr.parse_screen_resources(
            support.randr_fixture("sway-screenresources-current"))
        got = {res.modes[m].size_name: round(res.modes[m].refresh_hz(), 2)
               for m in res.modes}
        self.assertEqual(got["1280x720"], 59.86)
        self.assertEqual(got["800x600"], 59.86)
        self.assertEqual(got["640x480"], 59.38)
        self.assertEqual(got["320x200"], 58.14)
        self.assertEqual(len(res.modes), 16)

    def test_a_missing_output_name_drops_that_crtcs_half_and_says_so(self):
        """A crtc the snapshot cannot name is the one case the commit refuses to
        guess about: a stanza needs an output NAME and there is no second place
        to get one from."""
        res = randr.Resources()
        said = []
        batch = randr.RandrBatch(res, grabbed=True)
        batch.crtcs[0x99] = (0, 0, 1, 1, ())
        self.assertEqual(randr.stanzas_for(batch, log=said.append), ([], {}))
        self.assertTrue(any("0x99" in line for line in said), said)


class BatchFromCapture(RandrCase):
    """recon/wire.md 6's own five requests, byte for byte, against a fake server
    that calls RANDR 140 -- the major that capture's server used."""

    def test_the_five_requests_commit_one_apply_with_one_target(self):
        rig = self.vnc()
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-mode-write.hex"))
        self.wait_applies(rig)
        targets = self.layout.targets
        self.assertEqual([t.name for t in targets], ["VNC-0"])
        t = targets[0]
        self.assertTrue(t.enabled, "the disable was not coalesced away")
        self.assertEqual((t.mode.w, t.mode.h), (1680, 1050))
        self.assertEqual(t.stanza.pos, (0, 0))
        self.assertEqual(self.layout.verified and
                         [x.name for x in self.layout.verified[0]], ["VNC-0"])

    def test_the_screen_size_never_reaches_the_upstream(self):
        """`SetScreenSize` is CONSUMEd: a `NoOperation` goes up in its place, so
        the sequence delta stays zero and the client never sees the BadMatch the
        real server answers it with [recon/env.md 2.4]."""
        rig = self.vnc()
        sock = self.client(rig)
        before = rig.upstream.noops
        self.send(sock, *frames("xrandr-mode-write.hex"))
        self.wait_applies(rig)
        self.assertEqual([w for w in rig.upstream.randr.writes
                          if w[0] == "SetScreenSize"], [])
        rig.wait(lambda: rig.upstream.noops > before, what="a NoOperation")
        pkt = self.sync(sock)
        self.assertEqual(pkt[0], 1, "the sync after the batch was not answered")


class ReplyBeforeCommit(RandrCase):
    """`XRRSetCrtcConfig` blocks on its reply. A proxy that held it back would
    never be sent the `UngrabServer` its commit is waiting for."""

    def test_every_set_crtc_config_is_answered_before_the_ungrab_arrives(self):
        rig = self.vnc()
        sock = self.client(rig)
        grab, disable, screen, enable, ungrab = frames("xrandr-mode-write.hex")
        self.send(sock, grab, disable, screen, enable)
        # a client that behaves like Xlib: read both replies, THEN ungrab
        first = recv_packet(sock)
        second = recv_packet(sock)
        for pkt in (first, second):
            self.assertEqual(pkt[0], 1, "not a reply: %r" % pkt[:8])
            self.assertEqual(pkt[1], 0, "status was not Success")
        self.assertEqual(self.layout.applies, [],
                         "the layout was applied before the grab closed")
        self.send(sock, ungrab)
        self.wait_applies(rig)

    def test_the_deferred_twin_deadlocks_that_client(self):
        """The negative twin, and the reason the row is ANSWER rather than
        CONSUME: with the reply held back, a client that waits for it before
        sending `UngrabServer` waits for ever and nothing is ever applied."""
        rig = self.vnc()
        rig.server.handlers[("RANDR", randr.SET_CRTC_CONFIG)] = _record_only
        sock = self.client(rig)
        grab, disable, _screen, enable, _ungrab = frames("xrandr-mode-write.hex")
        self.send(sock, grab, disable, enable)
        sock.settimeout(1.0)
        with self.assertRaises(socket.timeout):
            recv_packet(sock)
        self.assertEqual(self.layout.applies, [])


def _record_only(server, conn, req):
    """A `SetCrtcConfig` handler that records and answers nothing -- the
    deadlock design section 7.3 exists to avoid."""
    decoded = randr.decode_set_crtc_config(req.frame)
    if decoded is not None:
        randr._batch_for(server, conn).record_crtc(decoded, req.seq)
    return None


class Coalescing(RandrCase):
    """Which of the two writes for one crtc the compositor is told about."""

    def test_disable_then_enable_is_one_output_turned_on(self):
        rig = self.vnc()
        sock = self.client(rig)
        grab, disable, screen, enable, ungrab = frames("xrandr-mode-write.hex")
        self.send(sock, grab, disable, screen, enable)
        recv_packet(sock)
        recv_packet(sock)
        self.send(sock, ungrab)
        self.wait_applies(rig)
        self.assertEqual(len(self.layout.targets), 1)
        self.assertTrue(self.layout.targets[0].enabled)
        self.assertFalse(self.layout.targets[0].stanza.off)

    def test_a_disable_with_no_enable_after_it_is_an_off(self):
        """`--off`'s measured bytes: a `SetCrtcConfig` with mode None and an
        EMPTY output list, plus the 16x16 `SetScreenSize` that follows it
        (tests/fixtures/xw11/xrandr-off.hex). The output the disable is about
        comes from the SNAPSHOT, since the request itself names none."""
        rig = self.rig()
        sock = self.client(rig)
        grab, disable, screen = frames("xrandr-off.hex")
        self.send(sock, grab, disable, screen, UNGRAB)
        self.wait_applies(rig)
        t = self.layout.target("HEADLESS-1")
        self.assertTrue(t.stanza.off)
        self.assertFalse(t.enabled)


class EmptyGrabAppliesNothing(RandrCase):
    def test_dryruns_empty_grab_commits_nothing_and_logs_nothing(self):
        """`--dryrun` still grabs -- `grab_server` is its own flag, cleared only
        by `--nograb` [xrandr.c:55, 2723] -- so an empty grab is a real batch
        that must do nothing at all. A proxy that read it as "apply what is
        there" would make `--dryrun` the one flag that changes the screen."""
        rig = self.rig()
        lines = []
        rig.server.say = lines.append
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-dryrun.hex"))
        self.sync(sock)
        self.assertEqual(self.layout.applies, [])
        self.assertEqual([ln for ln in lines if "RandR batch" in ln], [])
        # `snapshots == 0` used to be asserted here too; it is true of
        # `FakeRandrBackend` by construction (only `Applier.apply` ever calls
        # `snapshot`) and so proved nothing about the commit.


class NoSetScreenSizeStillApplies(RandrCase):
    def test_a_pos_only_change_carries_no_screen_size_and_applies_anyway(self):
        """`screen_apply()` skips `SetScreenSize` entirely when the framebuffer
        does not move [xrandr.c], so `--pos` and `--auto` are grab, one enable,
        ungrab -- measured as connections 4 and 7 of scratchpad/b7/cap1.log.
        Requiring a `SetScreenSize` would have made those two do nothing."""
        rig = self.rig()
        sock = self.client(rig)
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        self.send(sock, grab, enable, ungrab)
        self.wait_applies(rig)
        t = self.layout.target("HEADLESS-1")
        self.assertTrue(t.enabled)
        self.assertEqual(t.stanza.pos, (0, 0))


class NoGrabIsBatchOfOne(RandrCase):
    def test_a_set_crtc_config_outside_a_grab_commits_where_it_stands(self):
        """`--nograb`, and any client that never grabs. The same code, a batch
        of one: nothing is waiting to close it, so it closes itself."""
        rig = self.rig()
        sock = self.client(rig)
        _grab, enable, _primary, _ungrab = frames("xrandr-primary.hex")
        self.send(sock, enable)
        self.wait_applies(rig)
        self.assertEqual(len(self.layout.targets), 1)
        self.assertIsNone(self.conn(rig).batch,
                          "the batch of one was left open")

    def test_a_refused_batch_of_one_replaces_its_reply_and_never_follows_it(self):
        """The `status 0` for a `SetCrtcConfig` outside a grab is held until the
        apply returns, so a refusal takes its place. Writing the error AFTER a
        reply the client already had for the same sequence would be two answers
        to one request -- and libxcb reads the second as a reply "already
        completed" and hangs up."""
        rig = self.rig(backend=FakeRandrBackend(
            fail=wcore.Fatal("the compositor said no\n")))
        sock = self.client(rig)
        _grab, enable, _primary, _ungrab = frames("xrandr-primary.hex")
        self.send(sock, enable, GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0, "the status 0 went out anyway: %r" % pkt[:8])
        self.assertEqual(struct.unpack_from("<H", pkt, 2)[0], 1)
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0],
                         randr.SET_CRTC_CONFIG)
        second = recv_packet(sock)
        self.assertEqual(second[0], 1, "not the sync's reply: %r" % second[:8])
        self.assertEqual(struct.unpack_from("<H", second, 2)[0], 2,
                         "a second answer for the SetCrtcConfig came after it")


class PrimaryPassesAndRecords(RandrCase):
    def test_set_output_primary_reaches_the_upstream_and_the_target(self):
        """Both halves, because both matter: Xwayland accepts the request and
        `xrandr -q` reads the flag back out of it [recon/env.md 2.4], while
        `wxrandr` keeps its own primary in `State`. Recording it here is what
        makes the two agree."""
        rig = self.rig()
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-primary.hex"))
        self.wait_applies(rig)
        rig.wait(lambda: rig.upstream.randr.primary == 0x21,
                 what="SetOutputPrimary upstream")
        t = self.layout.target("HEADLESS-1")
        self.assertTrue(t.stanza.primary)
        self.assertEqual(rig.server.randr.state.primary, "HEADLESS-1")


class Transforms(RandrCase):
    def test_a_pure_scale_matrix_becomes_the_stanzas_scale(self):
        """`--scale 2x2` sends [[2,0,0],[0,2,0],[0,0,1]] in 16.16 fixed point
        and the filter name `bilinear` (tests/fixtures/xw11/xrandr-scale.hex,
        measured 2026-09-10). Today that whole request answers BadValue on
        Xwayland [recon/env.md 2.4]; here it is a scale the compositor can
        take -- the RECIPROCAL of the one in the matrix, because the two planes
        mean opposite things by the word.

        `xrandr --scale 2x2` on X makes the crtc read a 2560x1440 rectangle of
        the framebuffer through a 1280x720 mode: the screen grows and everything
        on it gets smaller, which is why the same invocation sends
        `SetScreenSize 2560x1440` with it. A Wayland output's scale runs the
        other way -- measured 2026-09-10 on this box's headless sway, `swaymsg
        output HEADLESS-1 scale 2` leaves the output rect 640x360 and `scale
        0.5` leaves it 2560x1440. Handing the compositor 2.0 would have given
        the client a QUARTER of the desktop X gives it; handing it 0.5 gives the
        same 2560x1440, and X wins where the two disagree (AGENTS.md)."""
        rig = self.rig()
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-scale.hex"), UNGRAB)
        self.wait_applies(rig)
        t = self.layout.target("HEADLESS-1")
        self.assertEqual(t.stanza.scale, (0.5, 0.5))
        self.assertEqual(t.scale, 0.5)
        self.assertEqual(randr.decode_transform_scale(
            frames("xrandr-scale.hex")[3]), (2.0, 2.0),
            "the matrix on the wire is not the number the compositor is given")

    def test_a_shear_is_bad_value_and_is_not_recorded(self):
        """Anything that is not a pure scale is a transform no
        output-management protocol carries -- not yet, and the route is a
        patched compositor (AGENTS.md route 6). It answers the same `BadValue`
        Xwayland answers, so the bytes a client sees do not change."""
        rig = self.rig()
        sock = self.client(rig)
        shear = _transform_request(SWAY_MAJOR, 0x20,
                                   (2, 1, 0, 0, 2, 0, 0, 0, 1))
        self.send(sock, GRAB, shear, UNGRAB)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0, "not an error: %r" % pkt[:8])
        self.assertEqual(pkt[1], wire.ERR_VALUE)
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0],
                         randr.SET_CRTC_TRANSFORM)
        self.assertEqual(pkt[10], SWAY_MAJOR)
        self.assertEqual(self.layout.applies, [],
                         "the shear was recorded and applied anyway")

    def test_the_decoder_takes_the_measured_scale_and_refuses_a_translation(self):
        raw = frames("xrandr-scale.hex")[3]
        self.assertEqual(randr.decode_transform_scale(raw), (2.0, 2.0))
        moved = _transform_request(SWAY_MAJOR, 0x20, (1, 0, 5, 0, 1, 0, 0, 0, 1))
        self.assertIsNone(randr.decode_transform_scale(moved))


def _transform_request(major, crtc, matrix):
    """A `SetCrtcTransform` with `matrix` as nine 16.16 fixed-point words and
    the `bilinear` filter, in the shape the measured one has."""
    body = struct.pack("<I", crtc)
    body += struct.pack("<9i", *[int(v * randr.FIXED_ONE) for v in matrix])
    body += struct.pack("<H2x", len(b"bilinear")) + b"bilinear"
    return struct.pack("<BBH", major, randr.SET_CRTC_TRANSFORM,
                       1 + len(body) // 4) + body


class CustomModeResolves(RandrCase):
    """A mode a client made with `RRCreateMode` and attached with
    `RRAddOutputMode` -- both of which succeed on today's Xwayland
    [recon/env.md 2.4] -- named by a later `SetCrtcConfig`."""

    def test_a_created_mode_resolves_onto_the_real_one_of_its_size_and_rate(self):
        """The `--newmode b7cap 60.00 800 840 900 1000 600 610 620 640` of
        scratchpad/b7/cap2.log is really in that rig's mode table: id 0x23e,
        800x600, name `b7cap`, and 93.75 Hz off its own modeline -- the number
        `xrandr -q` prints for it [recon/env.md 2.4].

        A compositor cannot be handed a modeline, so the rule is
        `resolve_real_mode`'s: a custom mode is applicable when a real mode of
        the same size and rate exists. Here two 800x600 modes exist, at 59.86
        and at 93.75, and the one the batch resolves to is the second."""
        rig = self.rig("sway2", backend=FakeRandrBackend(outputs=[randr_output(
            "HEADLESS-1", modes=[randr_mode(1280, 720, 59.86, preferred=True),
                                 randr_mode(800, 600, 59.86),
                                 randr_mode(800, 600, 93.75)])]))
        sock = self.client(rig)
        self.send(sock, GRAB, _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0x23E,
                                           (0x23,)), UNGRAB)
        self.wait_applies(rig)
        t = self.layout.target("HEADLESS-1")
        self.assertEqual((t.mode.w, t.mode.h), (800, 600))
        self.assertEqual(t.mode.refresh_mhz, 93750)
        self.assertEqual(t.stanza.mode, "800x600")
        self.assertAlmostEqual(t.stanza.rate, 93.75, places=2)


def _caps(name):
    """One whole-command stream out of `tests/fixtures/xw11/randr/caps/`,
    captured through the proxy on a live sway."""
    out = []
    with open(os.path.join(FIXTURES, "randr", "caps", name + ".hex"),
              encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(binascii.unhexlify(line))
    return out


def _crtc_config(major, crtc, x, y, mode, outputs, rotation=1,
                 config_ts=0x3163DFD0):
    """A `SetCrtcConfig`, in the layout of recon/wire.md 6 -- the same bytes the
    captures carry, with the ids a test needs."""
    body = struct.pack("<IIIhhIH2x", crtc, 0, config_ts, x, y, mode, rotation)
    body += struct.pack("<%dI" % len(outputs), *outputs) if outputs else b""
    return struct.pack("<BBH", major, randr.SET_CRTC_CONFIG,
                       1 + len(body) // 4) + body


class TwoCrtcBatch(RandrCase):
    """R2. One `SetCrtcConfig` per crtc plus one `SetScreenSize` in one grab.

    The shape was inferred from `apply()`'s source until 2026-09-10, when it was
    measured on a two-head headless sway (`WLR_HEADLESS_OUTPUTS=2`,
    scratchpad/b7/cap2.log connection 14): `xrandr --output HEADLESS-2 --right-of
    HEADLESS-1` sent GrabServer, `SetCrtcConfig(0x22, mode None, outputs [])`,
    `SetScreenSize(2560x720)`, `SetCrtcConfig(0x20, x=1280, mode 0x43, outputs
    [0x21])` -- and then died, because Xwayland answered that SetScreenSize
    BadMatch before the second enable could go. The requests below are that
    capture with the enable the error cut off put back. It was measured
    again that day THROUGH the proxy, where nothing refuses the SetScreenSize
    and the whole grab goes out:
    `tests/fixtures/xw11/randr/caps/xrandr-two-right-of.hex` and its two
    neighbours are those runs, and the shape design section 7.7 inferred is
    exactly the shape they have. `--right-of` itself adds nothing to the wire:
    it is client-side arithmetic that ends in the x field [recon/wire.md 6].
    """

    #: the sway2 rig's two modes, 0x43 and 0x44 in its own table
    MODES = (randr_mode(1280, 720, 59.86, preferred=True),
             randr_mode(800, 600, 59.86))

    def two_head(self, **kw):
        kw.setdefault("backend", FakeRandrBackend(outputs=[
            randr_output("HEADLESS-2", x=1280, modes=list(self.MODES)),
            randr_output("HEADLESS-1", x=2560, modes=list(self.MODES))]))
        return self.rig("sway2", **kw)

    def test_two_crtcs_and_one_screen_size_are_one_apply_of_two_targets(self):
        rig = self.two_head()
        sock = self.client(rig)
        disable = _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0, ())
        screen = struct.pack("<BBHIHHII", SWAY_MAJOR, randr.SET_SCREEN_SIZE, 5,
                             FAKE_ROOT, 2560, 720, 675, 190)
        enable2 = _crtc_config(SWAY_MAJOR, 0x20, 1280, 0, 0x43, (0x21,))
        enable1 = _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0x43, (0x23,))
        self.send(sock, GRAB, disable, screen, enable2, enable1, UNGRAB)
        self.wait_applies(rig)
        targets = self.layout.targets
        self.assertEqual(sorted(t.name for t in targets),
                         ["HEADLESS-1", "HEADLESS-2"])
        self.assertEqual(self.layout.target("HEADLESS-2").stanza.pos, (1280, 0))
        self.assertEqual(self.layout.target("HEADLESS-1").stanza.pos, (0, 0))
        self.assertTrue(all(t.enabled for t in targets))
        self.assertEqual(len(self.layout.applies), 1)

    def test_the_measured_right_of_capture_is_that_same_batch(self):
        """The synthetic requests above and the real ones agree, which is what
        closes R2: two enables, one disable coalesced away, one SetScreenSize
        recorded and never applied, one apply."""
        rig = self.two_head()
        sock = self.client(rig)
        self.send(sock, *_caps("xrandr-two-right-of")[24:])   # the grab onward
        self.wait_applies(rig)
        self.assertEqual(sorted(t.name for t in self.layout.targets),
                         ["HEADLESS-1", "HEADLESS-2"])
        self.assertEqual(self.layout.target("HEADLESS-2").stanza.pos, (1280, 0))
        self.assertEqual(self.layout.target("HEADLESS-1").stanza.pos, (0, 0))
        self.assertTrue(all(t.enabled for t in self.layout.targets))

    def test_a_left_of_carries_neither_a_disable_nor_a_screen_size(self):
        """`--left-of` does not move the framebuffer, so `screen_apply()` skips
        `SetScreenSize` and the disable loop finds nothing to disable: two
        enables in one grab and nothing else (measured,
        `xrandr-two-left-of.hex`). A proxy that needed either would do nothing
        here."""
        rig = self.two_head()
        sock = self.client(rig)
        self.send(sock, *_caps("xrandr-two-left-of")[24:])
        self.wait_applies(rig)
        self.assertEqual(self.layout.target("HEADLESS-2").stanza.pos, (0, 0))
        self.assertEqual(self.layout.target("HEADLESS-1").stanza.pos, (1280, 0))

    def test_a_mode_and_a_relation_in_one_invocation_carry_two_modes(self):
        """One `xrandr` call with two `--output` blocks -- a mode change on one
        head, a relation on the other -- is one grab and one apply, with two
        different modes in it (measured,
        `xrandr-two-mode-and-right-of.hex`)."""
        rig = self.two_head()
        sock = self.client(rig)
        self.send(sock, *_caps("xrandr-two-mode-and-right-of")[24:])
        self.wait_applies(rig)
        self.assertEqual(self.layout.target("HEADLESS-1").stanza.mode, "800x600")
        self.assertEqual(self.layout.target("HEADLESS-2").stanza.mode, "1280x720")
        self.assertEqual(self.layout.target("HEADLESS-2").stanza.pos, (800, 0))
        self.assertEqual(len(self.layout.applies), 1)

    def test_the_measured_capture_alone_leaves_the_second_head_disabled(self):
        """What the real, refused run collected: the disable for 0x22 with no
        enable after it. It is why a client that goes away mid-grab has its
        batch DROPPED rather than applied -- half a layout is a head turned off
        with nothing left to turn it back on."""
        rig = self.two_head()
        sock = self.client(rig)
        disable = _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0, ())
        enable2 = _crtc_config(SWAY_MAJOR, 0x20, 1280, 0, 0x43, (0x21,))
        self.send(sock, GRAB, disable, enable2, UNGRAB)
        self.wait_applies(rig)
        self.assertFalse(self.layout.target("HEADLESS-1").enabled)


class Failure(RandrCase):
    """A `Fatal` from the resolver or the apply, and the bytes the client gets."""

    def test_a_failed_apply_is_a_bad_match_naming_rr_set_screen_size(self):
        """`BadMatch`, minor 7, `bad` = the root -- the bytes today's Xwayland
        answers a `RRSetScreenSize` with, measured twice
        (tests/fixtures/xw11/randr-badmatch.hex and recon/env.md 2.4's five
        printed lines). Logging alone would have left `xrandr` reporting success
        for a layout that never happened."""
        rig = self.vnc(backend=FakeRandrBackend(
            outputs=[randr_output("VNC-0")],
            fail=wcore.Fatal("the compositor said no\n")))
        sock = self.client(rig)
        grab, disable, screen, enable, ungrab = frames("xrandr-mode-write.hex")
        self.send(sock, grab, disable, screen, enable)
        recv_packet(sock)                      # the disable's reply
        recv_packet(sock)                      # the enable's reply
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0, "not an error: %r" % pkt[:12])
        self.assertEqual(pkt[1], wire.ERR_MATCH)
        self.assertEqual(struct.unpack_from("<H", pkt, 2)[0], 5,
                         "not the UngrabServer's sequence")
        self.assertEqual(struct.unpack_from("<I", pkt, 4)[0], FAKE_ROOT)
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0],
                         randr.SET_SCREEN_SIZE)
        self.assertEqual(pkt[10], VNC_MAJOR,
                         "the error did not carry THIS server's major")

    def test_the_error_lands_before_the_sync_reply_and_the_client_lives(self):
        """The order is the whole point of pausing the client's reads: Xlib
        prints and exits on the error, and a proxy that let the sync's reply
        overtake it would have `xrandr` announce success first. And the
        connection is not poisoned -- a later request still answers.

        The failure is a SLOW one (0.3 s) for the same reason `ApplyOffLoop`'s
        is: with an instant `Fatal` the worker's hand-back wins the race on its
        own and the test passes with the pause taken out, which would make it a
        test of nothing. The sync reaches the proxy while the apply is still
        running -- it is in `in_down` already, xrandr writes requests 23 and 24
        back to back [recon/wire.md 6] -- and only the paused framer keeps it
        from being answered."""
        rig = self.vnc(backend=FakeRandrBackend(
            outputs=[randr_output("VNC-0")], slow=0.3,
            fail=wcore.Fatal("the compositor said no\n")))
        sock = self.client(rig)
        grab, disable, screen, enable, ungrab = frames("xrandr-mode-write.hex")
        self.send(sock, grab, disable, screen, enable)
        recv_packet(sock)
        recv_packet(sock)
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        first = recv_packet(sock)
        self.assertEqual(first[0], 0, "the sync's reply overtook the error")
        second = recv_packet(sock)
        self.assertEqual(second[0], 1, "the sync was never answered")
        again = self.sync(sock)
        self.assertEqual(again[0], 1, "the connection was poisoned")

    def test_the_error_never_carries_a_sequence_that_has_already_passed(self):
        """The measurement that corrects design section 7.5, taken 2026-09-10
        on a live sway: an error whose 16-bit sequence goes BACKWARDS makes
        libxcb widen it by 65536 (`xcb_in.c: read_packet`), every later reply
        then reads as "already completed", and the client's connection is torn
        down -- `xrandr` printed `X connection to :83 broken` and Xlib's five
        lines never appeared. So the sequence is the one of the request that
        CLOSED the batch, which is by construction the highest the client has
        seen. The major, the minor and `bad` still name the `SetScreenSize`."""
        rig = self.vnc(backend=FakeRandrBackend(
            outputs=[randr_output("VNC-0")],
            fail=wcore.Fatal("the compositor said no\n")))
        sock = self.client(rig)
        grab, disable, screen, enable, ungrab = frames("xrandr-mode-write.hex")
        self.send(sock, grab, disable, screen, enable)
        delivered = [struct.unpack_from("<H", recv_packet(sock), 2)[0]
                     for _ in range(2)]
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0)
        self.assertGreater(struct.unpack_from("<H", pkt, 2)[0], max(delivered),
                           "the error went backwards past a delivered reply")

    def test_without_a_screen_size_it_is_bad_value_at_the_last_set_crtc(self):
        rig = self.rig(backend=FakeRandrBackend(
            fail=wcore.Fatal("the compositor said no\n")))
        sock = self.client(rig)
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        self.send(sock, grab, enable)
        recv_packet(sock)
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0)
        self.assertEqual(pkt[1], wire.ERR_VALUE)
        self.assertEqual(struct.unpack_from("<H", pkt, 2)[0], 3,
                         "not the UngrabServer's sequence")
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0],
                         randr.SET_CRTC_CONFIG)

    def test_a_verify_that_refuses_never_reaches_apply(self):
        """`verify` is the backend's `--dryrun` check -- Mutter's overlap
        refusal lives there. A layout it refuses is not half-applied."""
        rig = self.rig(backend=FakeRandrBackend(
            verify_fail=wcore.Fatal("outputs overlap\n")))
        sock = self.client(rig)
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        self.send(sock, grab, enable)
        recv_packet(sock)
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0)
        self.assertEqual(self.layout.applies, [])


class DisconnectDiscards(RandrCase):
    def test_a_client_that_leaves_mid_grab_applies_nothing_and_logs_once(self):
        """xrandr dying inside Xlib's error handler with the server still
        grabbed is the measured case [recon/tools.md 7; and again here as
        connections 3, 6, 8 and 10 of scratchpad/b7/cap1.log, none of which
        reached its UngrabServer]. What it collected is a disable with no
        enable."""
        rig = self.rig()
        lines = []
        rig.server.say = lines.append
        sock = self.client(rig)
        grab, disable, screen = frames("xrandr-off.hex")
        self.send(sock, grab, disable, screen)
        recv_packet(sock)                       # the disable's reply
        conn = self.conn(rig)
        rig.wait(lambda: conn.batch is not None and conn.batch.crtcs,
                 what="the half-batch")
        sock.close()
        rig.wait(lambda: conn not in rig.server.conns, what="the close")
        self.assertEqual(self.layout.applies, [])
        dropped = [ln for ln in lines if "batch open" in ln]
        self.assertEqual(len(dropped), 1, lines)
        self.assertIn("dropped, not applied", dropped[0])


class ScreenConfig(RandrCase):
    """`xrandr -s`: RandR 1.1, no grab at all."""

    def test_the_four_requests_commit_one_stanza_and_answer_with_the_fields(self):
        """The measured reply is `status 0`, a new timestamp, the config
        timestamp, the root and subpixel 0 (scratchpad/b7/cap1.log connection 11
        -- the one write path that already succeeds on Xwayland today, though
        only for its own screen and not for the compositor's output)."""
        rig = self.rig(backend=FakeRandrBackend(outputs=[randr_output(
            "HEADLESS-1", modes=[randr_mode(1280, 720, 59.86, preferred=True),
                                 randr_mode(800, 600, 59.86)])]))
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-s.hex"))
        # the GetScreenInfo reply first, then the SetScreenConfig's
        info = recv_packet(sock)
        self.assertEqual(info[0], 1)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 1, "not a reply: %r" % pkt[:8])
        self.assertEqual(pkt[1], 0, "status was not Success")
        # 8-11 new_timestamp, 12-15 config_timestamp, 16-19 root,
        # 20-21 subpixel_order [recon/wire.md 6], which is the layout the
        # measured reply really has.
        self.assertEqual(struct.unpack_from("<I", pkt, 12)[0], 0x3163DFD0,
                         "the config timestamp is not the snapshot's")
        self.assertEqual(struct.unpack_from("<I", pkt, 16)[0], FAKE_ROOT)
        self.assertEqual(struct.unpack_from("<H", pkt, 20)[0], 0)
        self.wait_applies(rig)
        t = self.layout.target("HEADLESS-1")
        self.assertEqual(t.stanza.mode, "800x600")
        self.assertEqual((t.mode.w, t.mode.h), (800, 600))

    def test_the_reply_waits_for_the_apply_the_way_x_answers_it(self):
        """X answers `SetScreenConfig` after the screen has changed, and a
        script whose next line reads the geometry is owed the new one. The reply
        is written by the worker's hand-back and not at dispatch: the
        placeholder keeps the stream order, the hold keeps the timing.

        Measured through a live sway with XW11_DEBUG on 2026-09-10: `xrandr -s
        800x600` sends `SetScreenConfig`, takes its reply and sends NO sync
        afterwards, so the reply is the only thing standing between the client
        and its exit. Sway's own apply is 1-44 ms; Mutter waits up to five
        seconds for `MonitorsChanged` [recon/seams.md 6.2], which is what this
        0.3 s stands in for."""
        rig = self.rig(backend=FakeRandrBackend(slow=0.3, outputs=[randr_output(
            "HEADLESS-1", modes=[randr_mode(1280, 720, 59.86, preferred=True),
                                 randr_mode(800, 600, 59.86)])]))
        sock = self.client(rig)
        started = time.monotonic()
        self.send(sock, *frames("xrandr-s.hex"))
        self.assertEqual(recv_packet(sock)[0], 1)       # GetScreenInfo, passed
        pkt = recv_packet(sock)
        took = time.monotonic() - started
        self.assertEqual(pkt[0], 1, "not a reply: %r" % pkt[:8])
        self.assertEqual(pkt[1], 0, "status was not Success")
        self.assertEqual(len(self.layout.applies), 1,
                         "the reply landed before the layout moved")
        self.assertGreaterEqual(took, 0.25, "answered in %gs" % took)

    def test_a_refused_screen_config_is_a_status_and_never_an_error(self):
        """RandR 1.1 has no error on this path: `SetScreenConfig` refuses in the
        reply's own status byte (randr.xml `SetConfig`, 3 = `Failed`), and
        xrandr turns that byte into `Failed to change the screen
        configuration!` -- the string is in this box's /usr/bin/xrandr -- and
        exits 1. An error packet here would be bytes no X server writes."""
        rig = self.rig(backend=FakeRandrBackend(
            fail=wcore.Fatal("the compositor said no\n"),
            outputs=[randr_output("HEADLESS-1", modes=[
                randr_mode(1280, 720, 59.86, preferred=True),
                randr_mode(800, 600, 59.86)])]))
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-s.hex"))
        self.assertEqual(recv_packet(sock)[0], 1)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 1, "an error packet, not a status: %r" % pkt[:8])
        self.assertEqual(pkt[1], randr.SET_CONFIG_FAILED)
        self.assertEqual(self.layout.applies, [])
        self.assertEqual(self.sync(sock)[0], 1, "the connection was poisoned")

    def test_a_size_id_off_the_end_of_the_table_applies_nothing(self):
        rig = self.rig()
        sock = self.client(rig)
        _info, _cwa, _select, setcfg = frames("xrandr-s.hex")
        bad = setcfg[:16] + struct.pack("<HHH2x", 99, 1, 0)
        self.send(sock, bad, GET_INPUT_FOCUS)
        recv_packet(sock)
        self.assertEqual(self.layout.applies, [])


class ReadsPass(RandrCase):
    def test_every_read_minor_is_forwarded_byte_for_byte(self):
        """The read side is the compositor's view already, through Xwayland
        [recon/env.md 2.4]. Nothing is synthesized on it, so `xrandr -q` through
        the proxy is `xrandr -q` without one."""
        rig = self.rig()
        sock = self.client(rig)
        reads = [(8, "sway-screenresources"), (25, "sway-screenresources-current"),
                 (5, "sway-screeninfo"), (6, "sway-screensizerange"),
                 (31, "sway-outputprimary")]
        for minor, name in reads:
            req = struct.pack("<BBHI", SWAY_MAJOR, minor, 2, FAKE_ROOT)
            sock.sendall(req)
            got = recv_packet(sock)
            want = support.randr_fixture(name)
            self.assertEqual(got[4:], want[4:],
                             "minor %d came back changed" % minor)
            self.assertEqual(got[0], want[0])
        crtc = struct.pack("<BBHII", SWAY_MAJOR, 20, 3, 0x20, 0x3163DFD0)
        sock.sendall(crtc)
        self.assertEqual(recv_packet(sock)[4:],
                         support.randr_fixture("sway-crtcinfo")[4:])
        self.assertEqual(self.layout.applies, [])


class ApplyOffLoop(RandrCase):
    """A slow apply must not be a slow proxy."""

    def test_another_client_is_served_while_the_layout_is_applying(self):
        """Mutter waits up to five seconds for `MonitorsChanged`
        [recon/seams.md 6.2]. Half a second of it here: a second client's
        pass-through request is answered DURING the apply, and the applying
        client's own next reply arrives after it."""
        rig = self.rig(backend=FakeRandrBackend(slow=0.5))
        sock = self.client(rig)
        other, _body = rig.raw()
        other.settimeout(5.0)
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        self.send(sock, grab, enable)
        recv_packet(sock)
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        self.assertTrue(self.layout.started.wait(5.0), "the apply never began")
        started = time.monotonic()
        other.sendall(GET_INPUT_FOCUS)
        pkt = recv_packet(other)
        during = time.monotonic() - started
        self.assertEqual(pkt[0], 1)
        self.assertLess(during, 0.4,
                        "the second client waited %gs for the apply" % during)
        mine = recv_packet(sock)
        self.assertEqual(mine[0], 1)
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_the_applying_clients_sync_is_answered_after_the_apply(self):
        """Which is what X does, and what makes `xrandr` exit only once the
        screen has moved."""
        seen = []
        rig = self.rig(backend=FakeRandrBackend(slow=0.4))
        sock = self.client(rig)
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        self.send(sock, grab, enable)
        recv_packet(sock)
        self.send(sock, ungrab, GET_INPUT_FOCUS)

        def read():
            seen.append(recv_packet(sock))
        t = threading.Thread(target=read, daemon=True)
        t.start()
        self.assertTrue(self.layout.started.wait(5.0))
        time.sleep(0.15)
        self.assertEqual(seen, [], "the sync was answered mid-apply")
        t.join(timeout=5)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self.layout.applies), 1)


class PersistentNever(RandrCase):
    def test_apply_is_called_with_persistent_false_even_with_the_variable_set(self):
        """A long-lived proxy would otherwise inherit one wrapper's
        `WXRANDR_PERSIST` and hand it to every client for the rest of the
        session. `wxrandr --persistent` stays the front end for it (design
        section 7.4, decided (A))."""
        rig = self.rig()
        old = os.environ.get("WXRANDR_PERSIST")
        os.environ["WXRANDR_PERSIST"] = "1"
        self.addCleanup(_restore_env, "WXRANDR_PERSIST", old)
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-primary.hex"))
        self.wait_applies(rig)
        self.assertEqual(self.layout.persistent, [False])


def _restore_env(name, old):
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


def _screen_size(major, width, height, mm_w=0, mm_h=0, root=FAKE_ROOT):
    """A `SetScreenSize`, in the layout of recon/wire.md 6 -- the layout
    `xrandr-mode-write.hex`'s own request 21 has, with the size a test needs."""
    body = struct.pack("<IHHII", root, width, height, mm_w, mm_h)
    return struct.pack("<BBH", major, randr.SET_SCREEN_SIZE,
                       1 + len(body) // 4) + body


class CustomModeRefused(RandrCase):
    """The other half of `resolve_real_mode`'s rule: a client-made mode that no
    real one matches is refused, and refused the way the CLI refuses it."""

    def test_a_custom_mode_no_real_mode_is_near_is_a_bad_match_and_no_apply(self):
        """`--newmode b7cap 108.00 800 840 968 1056 600 601 605 628` is a
        93.75 Hz modeline; against a compositor whose only 800x600 mode is
        59.86 there is nothing to apply it as. `wxrandr --output X --mode
        <that name>` says `cannot find mode` and exits 1 (core.py:1103,
        `tolerance=1.0`), and `_find_mode_for` alone would not: it takes the
        nearest refresh with no threshold (core.py:1072), so 59.86 would come
        back and the client would be told its 93.75 Hz mode was set.

        33.89 Hz apart is the gap here; the tolerance is 1.0."""
        rig = self.rig("sway2", backend=FakeRandrBackend(outputs=[randr_output(
            "HEADLESS-1", modes=[randr_mode(1280, 720, 59.86, preferred=True),
                                 randr_mode(800, 600, 59.86)])]))
        said = []
        rig.server.say = said.append
        sock = self.client(rig)
        self.send(sock, GRAB, _screen_size(SWAY_MAJOR, 800, 600),
                  _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0x23E, (0x23,)),
                  UNGRAB, GET_INPUT_FOCUS)
        recv_packet(sock)                      # the SetCrtcConfig's status 0
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0, "not an error: %r" % pkt[:12])
        self.assertEqual(pkt[1], wire.ERR_MATCH)
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0],
                         randr.SET_SCREEN_SIZE)
        self.assertEqual(self.layout.applies, [],
                         "the layout was applied at a refresh nobody asked for")
        self.assertTrue(any("cannot find mode" in ln for ln in said), said)

    def test_the_mode_the_compositor_lists_is_the_one_the_target_ends_on(self):
        """The same batch against a list that HAS 93.75: the target's mode is
        the compositor's own object, not the modeline's -- a compositor is
        handed a mode and never a set of timings."""
        real = randr_mode(800, 600, 93.75)
        rig = self.rig("sway2", backend=FakeRandrBackend(outputs=[randr_output(
            "HEADLESS-1", modes=[randr_mode(1280, 720, 59.86, preferred=True),
                                 randr_mode(800, 600, 59.86), real])]))
        sock = self.client(rig)
        self.send(sock, GRAB, _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0x23E,
                                           (0x23,)), UNGRAB)
        self.wait_applies(rig)
        self.assertIs(self.layout.target("HEADLESS-1").mode, real)


    def test_a_virtual_output_takes_a_custom_mode_no_real_mode_matches(self):
        """The carve-out `_find_mode_for` already makes (core.py:1065), and the
        reason the live `--newmode`+`--addmode`+`--mode` works: a headless head
        has no mode list at all (measured 2026-09-10 -- `swaymsg -t get_outputs`
        reports `modes: []` for `HEADLESS-1`) and the compositor drives whatever
        size it is handed. Refusing there would have broken a path that is
        measured working against a real sway."""
        rig = self.rig("sway2", backend=FakeRandrBackend(outputs=[randr_output(
            "HEADLESS-1", modes=[], current=None, virtual_modes=True)]))
        sock = self.client(rig)
        self.send(sock, GRAB, _crtc_config(SWAY_MAJOR, 0x22, 0, 0, 0x23E,
                                           (0x23,)), UNGRAB)
        self.wait_applies(rig)
        t = self.layout.target("HEADLESS-1")
        self.assertEqual((t.mode.w, t.mode.h), (800, 600))
        self.assertTrue(t.mode.custom, "a virtual output was given a real mode")


class SnapshotFailure(RandrCase):
    """A batch that cannot be resolved is a failure the client is TOLD about.

    Every `SetCrtcConfig` in it has already been answered `status 0` by the time
    the commit finds out (it must be -- `XRRSetCrtcConfig` blocks on that
    reply), so a commit that only logged would leave `xrandr` exiting 0 for a
    layout that never happened, which is the outcome design section 7.5 rules
    out."""

    def test_an_upstream_that_cannot_be_read_is_an_error_and_not_a_silent_zero(self):
        """`randr_snapshot` raising is a real path: the own connection's 1 s
        deadline running out, or Xwayland restarting mid-session."""
        rig = self.rig()
        said = []
        rig.server.say = said.append
        sock = self.client(rig)

        def gone(timeout=None):
            raise upstream.UpstreamGone("the upstream went away")
        rig.server.own.randr_snapshot = gone
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        self.send(sock, grab, enable)
        recv_packet(sock)                      # the enable's status 0
        self.send(sock, ungrab, GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0, "the client exited 0 on an unread upstream")
        self.assertEqual(pkt[1], wire.ERR_VALUE)
        self.assertEqual(struct.unpack_from("<H", pkt, 8)[0],
                         randr.SET_CRTC_CONFIG)
        self.assertEqual(self.layout.applies, [])
        self.assertTrue(any("was not applied" in ln for ln in said), said)
        self.assertEqual(recv_packet(sock)[0], 1, "the sync was never answered")

    def test_a_batch_whose_crtcs_name_no_output_is_an_error_too(self):
        """The other way a batch resolves to nothing: a crtc the tables do not
        have. The stanza needs an output NAME and there is no second place to
        get one from."""
        rig = self.rig()
        said = []
        rig.server.say = said.append
        sock = self.client(rig)
        self.send(sock, GRAB, _crtc_config(SWAY_MAJOR, 0x99, 0, 0, 0x41,
                                           (0x98,)), UNGRAB, GET_INPUT_FOCUS)
        recv_packet(sock)                      # the SetCrtcConfig's status 0
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 0)
        self.assertEqual(self.layout.applies, [])
        self.assertTrue(any("0x99" in ln for ln in said), said)

    def test_a_bare_fb_still_exits_zero_and_the_log_names_the_route(self):
        """`xrandr --fb 2560x1440` sends a grab, a `SetScreenSize` and nothing
        else. On X the screen really grows past its outputs. Here nothing moves
        -- but the exit code stays the 0 X gave it, because an error X never
        wrote is a worse answer than a screen that did not grow, and the log
        carries the route (AGENTS.md route 1: a scale below 1 on the head)."""
        rig = self.rig()
        said = []
        rig.server.say = said.append
        sock = self.client(rig)
        self.send(sock, GRAB, _screen_size(SWAY_MAJOR, 2560, 1440), UNGRAB,
                  GET_INPUT_FOCUS)
        pkt = recv_packet(sock)
        self.assertEqual(pkt[0], 1, "--fb answered an error: %r" % pkt[:12])
        self.assertEqual(self.layout.applies, [])
        line = [ln for ln in said if "2560x1440" in ln]
        self.assertTrue(line, said)
        self.assertIn("not yet", line[0])
        self.assertIn("route 1", line[0])

    def test_a_framebuffer_the_layout_does_not_fill_is_logged(self):
        """`--fb` next to a real output change: the layout is applied and the
        size the client thinks the screen is, is the thing the log has to have
        said when someone comes asking. Nothing is refused -- xrandr sizes the
        framebuffer from the same arithmetic and a compositor may round it
        differently."""
        rig = self.rig()
        said = []
        # `Applier` was handed `server.say` when the proxy was built, so the
        # line this test is about goes through the applier's own log and not
        # through a `server.say` replaced afterwards.
        rig.server.randr._log = said.append
        sock = self.client(rig)
        _grab, enable, _primary, _ungrab = frames("xrandr-primary.hex")
        self.send(sock, GRAB, _screen_size(SWAY_MAJOR, 2560, 1440), enable,
                  UNGRAB)
        self.wait_applies(rig)
        line = [ln for ln in said if "framebuffer" in ln]
        self.assertTrue(line, said)
        self.assertIn("2560x1440", line[0])
        self.assertIn("1280x720", line[0])


class PrimaryOutsideAGrab(RandrCase):
    """`xrandr --nograb --output X --primary`, and any client that sets the
    primary output without grabbing."""

    def test_a_lone_set_output_primary_commits_and_leaves_no_batch_open(self):
        """It is a layout change of exactly one field. A batch left open here
        would sit on the connection until the client hung up and then be logged
        as a half-layout that was dropped -- while `State.primary`, which is
        only written at commit, never moved, and the two planes disagreed about
        which output is primary for the rest of the session."""
        rig = self.rig()
        said = []
        rig.server.say = said.append
        sock = self.client(rig)
        _grab, _enable, primary, _ungrab = frames("xrandr-primary.hex")
        self.send(sock, primary, GET_INPUT_FOCUS)
        recv_packet(sock)
        self.wait_applies(rig)
        self.assertTrue(self.layout.target("HEADLESS-1").stanza.primary)
        self.assertEqual(rig.server.randr.state.primary, "HEADLESS-1")
        conn = self.conn(rig)
        self.assertIsNone(conn.batch, "the batch of one was left open")
        rig.wait(lambda: rig.upstream.randr.primary == 0x21,
                 what="SetOutputPrimary upstream")
        sock.close()
        rig.wait(lambda: not rig.server.conns, what="the client to go")
        self.assertEqual([ln for ln in said if "batch open" in ln], [],
                         "a committed batch was logged as dropped")


class RefusalOrder(unittest.TestCase):
    """A refusal written straight into the client's stream can overtake a reply
    still in flight, and the whole of design section 7.5's sequence rule is that
    nothing this proxy writes may go backwards past a packet the client already
    read: libxcb widens a 16-bit sequence that does (`xcb_in.c: read_packet`)
    and the connection is torn down (measured 2026-09-10 on a live sway --
    `xrandr` printed `X connection to :83 broken` and Xlib's five lines never
    appeared).

    The seam is `ClientConn.write_after_replies`, and it lives here rather than
    in `tests/test_xw11_client.py` because `randr._fail` is its only caller."""

    def test_a_refusal_waits_for_the_reply_that_is_still_in_flight(self):
        """A client that pipelined its whole grab in one write -- which xrandr
        cannot do (`XRRSetCrtcConfig` blocks on each reply) but a client X
        served can -- has the `SetCrtcConfig`'s substitute reply still on the
        wire when the apply fails."""
        pairs = [socket.socketpair(), socket.socketpair()]
        for a, b in pairs:
            self.addCleanup(a.close)
            self.addCleanup(b.close)
        conn = client_mod.ClientConn(pairs[0][0], pairs[1][0], None)
        conn.state = client_mod.ESTABLISHED
        conn.seq = 2
        reply = wire.reply(1, 0, b"\0" * 24)
        conn.placeholders[1] = reply
        err = wire.error(wire.ERR_VALUE, 2, 0, SWAY_MAJOR,
                         randr.SET_CRTC_CONFIG)
        conn.write_after_replies(err)
        self.assertEqual(bytes(conn.out_down), b"",
                         "the error overtook a reply in flight")
        # the substitute's own 32-byte reply, which releases the placeholder
        conn.feed_server(struct.pack("<BBHI24x", 1, 0, 1, 0))
        self.assertEqual(bytes(conn.out_down), reply + err)


class ProbeUnderTheLock(RandrCase):
    def test_the_backend_probe_happens_inside_the_apply_lock(self):
        """`ensure()` sets `tried` before the probe finishes. Outside the lock,
        two first applies from two clients -- each on its own worker thread --
        have the second read `tried` true and `backend` still None and refuse a
        session that has a backend. The lock already serialises the apply; the
        probe is the first thing that has to be inside it."""
        rig = self.rig()
        applier = rig.server.randr
        seen = []
        real = applier.ensure

        def watched():
            seen.append(applier.lock.locked())
            return real()
        applier.ensure = watched
        sock = self.client(rig)
        self.send(sock, *frames("xrandr-primary.hex"))
        self.wait_applies(rig)
        self.assertEqual(seen, [True], "the probe ran outside the apply lock")


class GrabHeldElsewhere(RandrCase):
    """A second client's `GrabServer` while the first one's is open upstream.

    The grab is forwarded, so the X server processes nobody else's requests
    while it is held -- the proxy's own connection included. Reading the RandR
    tables there would block the loop until the deadline and stop the proxy
    forwarding for every client on the display, the first one's `UngrabServer`
    with it."""

    def test_the_second_batch_borrows_the_grabbers_tables_and_reads_nothing(self):
        rig = self.rig()
        first = self.client(rig)
        self.send(first, GRAB)
        rig.wait(lambda: rig.server.conns and rig.server.conns[0].batch,
                 what="the first grab")
        theirs = rig.server.conns[0].batch.resources
        self.assertIsNotNone(theirs)
        before = list(rig.upstream.randr.reads_done)
        second, _body = rig.raw()
        second.settimeout(5.0)
        grab, enable, _primary, ungrab = frames("xrandr-primary.hex")
        second.sendall(grab)
        rig.wait(lambda: len(rig.server.conns) > 1 and rig.server.conns[1].batch,
                 what="the second grab")
        self.assertIs(rig.server.conns[1].batch.resources, theirs,
                      "the second batch read the upstream through a grab")
        second.sendall(enable + ungrab)
        self.wait_applies(rig)
        self.assertEqual(rig.upstream.randr.reads_done, before,
                         "the upstream was asked for its tables mid-grab")
        self.assertEqual(self.layout.target("HEADLESS-1").stanza.pos, (0, 0))


class Rows(unittest.TestCase):
    """The policy table and the decoders, with no proxy in the way."""

    def test_every_randr_write_of_design_7_3_is_a_batch_row(self):
        for minor in (randr.SET_SCREEN_CONFIG, randr.SET_SCREEN_SIZE,
                      randr.SET_CRTC_CONFIG, randr.SET_CRTC_TRANSFORM,
                      randr.SET_OUTPUT_PRIMARY):
            row = policy.lookup(0, "RANDR", minor)
            self.assertEqual(row.other, policy.BATCH, "minor %d" % minor)
        for minor in (0, 5, 6, 8, 9, 15, 20, 25, 27, 28, 31):
            self.assertEqual(policy.lookup(0, "RANDR", minor).other,
                             policy.PASS, "read minor %d" % minor)
        for minor in (randr.CREATE_MODE, randr.ADD_OUTPUT_MODE):
            self.assertEqual(policy.lookup(0, "RANDR", minor).other,
                             policy.PASS, "minor %d" % minor)

    def test_the_grab_pair_is_a_batch_row_in_all_three_places(self):
        for op in (wire.OP_GRAB_SERVER, wire.OP_UNGRAB_SERVER):
            row = policy.lookup(op)
            self.assertEqual((row.shadow, row.root, row.other),
                             (policy.BATCH,) * 3)

    def test_the_rotation_word_becomes_xrandrs_own_words(self):
        """Bit 1 is `left`: `--rotate left` sent rotation 2
        (scratchpad/b7/cap1.log connection 8 request 22), and the reflections
        are bits 4 and 5."""
        self.assertEqual(randr.rotation_words(1), ("normal", "normal"))
        self.assertEqual(randr.rotation_words(2), ("left", "normal"))
        self.assertEqual(randr.rotation_words(4), ("inverted", "normal"))
        self.assertEqual(randr.rotation_words(8), ("right", "normal"))
        self.assertEqual(randr.rotation_words(1 | 16), ("normal", "x"))
        self.assertEqual(randr.rotation_words(2 | 32), ("left", "y"))
        self.assertEqual(randr.rotation_words(0), ("normal", "normal"))
        # and the pair is the one RANDR_VIEW reads a transform back out as
        self.assertEqual(wcore.RANDR_VIEW["270"], randr.rotation_words(2))

    def test_the_measured_requests_decode_to_their_measured_fields(self):
        grab, disable, screen, enable, ungrab = frames("xrandr-mode-write.hex")
        self.assertEqual(grab, GRAB)
        self.assertEqual(ungrab, UNGRAB)
        self.assertEqual(randr.decode_set_crtc_config(disable),
                         (0x3A, 0, 0, 0, 1, (), 0x304A204B))
        self.assertEqual(randr.decode_set_crtc_config(enable),
                         (0x3A, 0, 0, 0x3F, 1, (0x3B,), 0x304A204B))
        self.assertEqual(randr.decode_set_screen_size(screen),
                         (1680, 1050, 469, 293))

    def test_offs_screen_size_is_the_servers_own_minimum(self):
        """16x16 with 4mm x 4mm: what `--off` asks for once nothing is left on
        the screen (tests/fixtures/xw11/xrandr-off.hex)."""
        _grab, _disable, screen = frames("xrandr-off.hex")
        self.assertEqual(randr.decode_set_screen_size(screen),
                         (randr.MIN_SCREEN, randr.MIN_SCREEN, 4, 4))

    def test_the_screen_config_request_decodes_to_its_size_id(self):
        _info, _cwa, _select, setcfg = frames("xrandr-s.hex")
        self.assertEqual(randr.decode_set_screen_config(setcfg), (1, 1, 0))

    def test_the_measured_badmatch_has_the_layout_wire_error_writes(self):
        """The fixture's own fields, packed back by `wire.error`, are the
        fixture: the error a server writes for a refused `RRSetScreenSize` and
        the error this proxy writes have the same 32-byte layout, and code 8
        with minor 7 is what that refusal is made of. That the proxy really
        writes THIS packet is `Failure`'s claim, not this one's -- here the
        oracle is the byte layout."""
        (want,) = frames("randr-badmatch.hex")
        got = wire.error(wire.ERR_MATCH, struct.unpack_from("<H", want, 2)[0],
                         struct.unpack_from("<I", want, 4)[0], want[10],
                         randr.SET_SCREEN_SIZE)
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
