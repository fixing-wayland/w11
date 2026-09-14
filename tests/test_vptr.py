"""zwlr_virtual_pointer_v1: the wire, the coordinates, and the policy.

Everything here runs against a **real Wayland socket** served by a fake
compositor that speaks the wire format (`PointerCompositor` below): the client is
`w11common/wayland_mini.py` unmodified, and every assertion is about the bytes
the compositor received. A mock of our own client would have proved only that
we can call our own methods.

The compositor serves three heads, and they are the rig's own: one at a
negative origin, one at the origin, and one at scale 1.5 whose `wl_output`
scale is a *lie* (2) that only `zxdg_output_v1` corrects. That layout is what
makes the coordinate assertions worth anything -- absolute motion here is a
ratio over the whole layout bounding box in logical pixels, so a wrong origin,
a wrong extent or a per-output map would all land somewhere plausible on a
single 1920x1080 screen and nowhere near the target on this one.

The selection policy is tested in both directions, like the keyboard's: with
kernel devices available nothing may reach the protocol even where the
compositor offers it, and with none available the protocol is what moves.
"""

import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ...and the tests directory itself, for the bare `import support` /
# `import wl_fake` below: running this file by path puts it on sys.path
# for free, `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# every test file carries this itself: the suite is run file by file, where
# conftest.py never loads, and a tool that hands itself over would not be
# the code under test
os.environ["W11_PASSTHROUGH"] = "never"
os.environ.setdefault("WDOTOOL_LAYOUT", "us")

import wl_fake
from support import RecorderDev, abs_report
from w11common.wayland_mini import WlConn
from wdotool import daemon
from hacks.input import uinput, vptr


def cosmic_globals(served):
    """cosmic-comp's own recorded registry, minus what this fake serves itself.

    tests/fixtures/registries/cosmic.txt is the 53 globals a COSMIC 1.7 session announced
    [M recon2/cosmic.md §4, globals.txt]. Replaying it rather than hand-writing a pair is what makes a test
    named "on COSMIC" mean anything: if cosmic-comp ever grows a virtual pointer, the fixture changes and
    these tests go red, which is the correct outcome."""
    return [g for g in wl_fake.registry_fixture("cosmic") if g[0] not in served]

# What a daemon that could not open /dev/uinput says today, and must keep
# saying when there is no protocol to fall back to either.
UINPUT_ERROR = ("cannot create uinput devices: [Errno 13] Permission denied: "
                "'/dev/uinput' (wdotool injects input via /dev/uinput; run it "
                "as root)")

# The rig, as three (logical x, y, w, h, wl_output scale, mode w, mode h).
# Virtual-3 is the scale-1.5 panel: 1920x1080 of pixels, 1280x720 logical,
# and a `wl_output.scale` of 2 because that event cannot carry 1.5.
HEADS = (
    ("Virtual-1", -1920, -540, 1920, 1080, 1, 1920, 1080),
    ("Virtual-2", 0, 0, 1920, 1080, 1, 1920, 1080),
    ("Virtual-3", 1920, 0, 1280, 720, 2, 1920, 1080),
)
BBOX = (-1920, -540, 5120, 1620)


# ---------------------------------------------------------------------------
# a compositor, on the wire


class PointerCompositor(wl_fake.Server):
    """Enough of a wlroots compositor to be a virtual pointer's peer.

    On top of wl_fake.Server's wl_display/wl_registry: a wl_seat, the three
    wl_outputs of HEADS with their zxdg_output_v1 logical geometry, and
    zwlr_virtual_pointer_manager_v1 + zwlr_virtual_pointer_v1, recording
    every request made on the pointer. Knobs:

      manager_version  None -> the global is not advertised at all (Mutter and
                       KWin: measured, they implement neither protocol)
      refuse_create    answer create_virtual_pointer with a protocol error
      with_seat        advertise a wl_seat at all (the seat argument is
                       allow-null in this protocol)
      with_xdg_output  advertise zxdg_output_manager_v1 (without it the
                       geometry falls back to wl_output, whose scale lies)
      extra_globals    more (interface, version) rows to advertise and bind
                       nothing for -- a recorded registry, so that "which
                       compositor is this" is a question about real bytes
    """

    MANAGER = vptr.MANAGER
    PREFIX = "wdotool-vp-"
    BACKLOG = 8

    def __init__(self, manager_version=2, refuse_create=False, with_seat=True,
                 with_xdg_output=True, extra_globals=()):
        self.refuse_create = refuse_create
        self.with_seat = with_seat
        self.with_xdg_output = with_xdg_output
        self.extra_globals = list(extra_globals)
        self.created = []        # (seat_id, pointer object id)
        self.with_output = []    # create_virtual_pointer_with_output (never)
        super().__init__(manager_version)

    def advertise(self):
        out = [("wl_seat", 7)] if self.with_seat else []
        out += [("wl_output", 4)] * len(HEADS)
        if self.with_xdg_output:
            out.append(("zxdg_output_manager_v1", 3))
        return out + self.extra_globals

    def new_state(self):
        return {"seat": None, "mgr": None, "vp": None,
                "outputs": {}, "xdg": None}

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_seat":
            state["seat"] = new_id
            self._send(conn, new_id, 0, struct.pack("<I", 3))  # caps
        elif iface == "wl_output":
            head = HEADS[self.names[name][1]]
            state["outputs"][new_id] = head
            self._output_events(conn, new_id, head)
        elif iface == "zxdg_output_manager_v1":
            state["xdg"] = new_id
        elif iface == self.MANAGER:
            state["mgr"] = new_id

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid == state["xdg"] and opcode == 1:  # get_xdg_output(id, output)
            new_id, out = struct.unpack_from("<II", body)
            head = state["outputs"].get(out)
            if head is not None:
                _n, lx, ly, lw, lh, _s, _mw, _mh = head
                self._send(conn, new_id, 0, struct.pack("<ii", lx, ly))
                self._send(conn, new_id, 1, struct.pack("<ii", lw, lh))
            return
        if oid == state["mgr"] and opcode in (0, 2):
            if opcode == 0:                     # create_virtual_pointer
                seat, new_id = struct.unpack_from("<II", body)
            else:                               # ...with_output (v2)
                seat, _out, new_id = struct.unpack_from("<III", body)
                self.with_output.append(new_id)
            if self.refuse_create:
                self._error(conn, oid, 0, "no virtual pointer for you")
                return
            state["vp"] = new_id
            self.created.append((seat, new_id))
            self.events.append(("create", new_id))
            return
        if state["vp"] is not None and oid == state["vp"]:
            self._pointer_request(opcode, body)

    def _pointer_request(self, opcode, body):
        if opcode == vptr._VP_MOTION:
            t, dx, dy = struct.unpack_from("<Iii", body)
            self.events.append(("motion", dx / 256.0, dy / 256.0))
        elif opcode == vptr._VP_MOTION_ABSOLUTE:
            _t, x, y, xe, ye = struct.unpack_from("<IIIII", body)
            self.events.append(("motion_absolute", x, y, xe, ye))
        elif opcode == vptr._VP_BUTTON:
            _t, btn, st = struct.unpack_from("<III", body)
            self.events.append(("button", btn, st))
        elif opcode == vptr._VP_AXIS:
            _t, ax, val = struct.unpack_from("<IIi", body)
            self.events.append(("axis", ax, val / 256.0))
        elif opcode == vptr._VP_FRAME:
            self.events.append(("frame",))
        elif opcode == vptr._VP_AXIS_SOURCE:
            (src,) = struct.unpack_from("<I", body)
            self.events.append(("axis_source", src))
        elif opcode == vptr._VP_AXIS_STOP:
            _t, ax = struct.unpack_from("<II", body)
            self.events.append(("axis_stop", ax))
        elif opcode == vptr._VP_AXIS_DISCRETE:
            _t, ax, val, disc = struct.unpack_from("<IIii", body)
            self.events.append(("axis_discrete", ax, val / 256.0, disc))
        elif opcode == vptr._VP_DESTROY:
            self.destroyed += 1
            self.events.append(("destroy",))

    def _output_events(self, conn, oid, head):
        _name, lx, ly, _lw, _lh, scale, mw, mh = head
        body = (struct.pack("<iiiii", lx, ly, 300, 200, 0)
                + wl_fake.wstr("wdotool") + wl_fake.wstr("fake")
                + struct.pack("<i", 0))
        self._send(conn, oid, 0, body)                       # geometry
        # mode(flags=current, width, height, refresh)
        self._send(conn, oid, 1, struct.pack("<Iiii", 1, mw, mh, 60000))
        self._send(conn, oid, 3, struct.pack("<i", scale))   # scale

    # -- readers used by the tests
    def of(self, *kinds):
        return [e for e in self.events if e[0] in kinds]

    def last(self, kind):
        for e in reversed(self.events):
            if e[0] == kind:
                return e
        raise AssertionError(f"no {kind} request was made: {self.events}")


def landed(ev, bbox=BBOX):
    """Where the compositor puts the cursor for one `motion_absolute`, in
    layout coordinates.

    The request carries a RATIO -- x/x_extent of the layout bounding box, in
    logical pixels -- so this is the compositor's own arithmetic, and it is
    the thing that has to come back exactly. (A live compositor additionally
    clamps a coordinate that falls in a hole between outputs to the nearest
    edge; that is its business, not the map's.)"""
    _kind, x, y, xe, ye = ev
    gx, gy, w, h = bbox
    return (gx + x * w / xe, gy + y * h / ye)


def compositor_pixel(axis_value, span):
    """The kernel path's arithmetic, for the two-path comparison: libinput's
    scale_axis() (value * span / (max - min + 1)) truncated to a pixel."""
    return axis_value * span // 32768


# ---------------------------------------------------------------------------
# daemons


def _over_the_socket(d, req):
    """One request through serve_client(), which is where an error becomes a
    reply -- the only place that decides whether a failing command's warnings
    reach the user at all."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        a.sendall((__import__("json").dumps(req) + "\n").encode())
        a.shutdown(socket.SHUT_WR)
        d.serve_client(b)
        return __import__("json").loads(a.makefile("r").readline())
    finally:
        a.close()


class VptrTest(unittest.TestCase):
    """Base: a fake compositor, and the environment pointed at it."""

    manager_version = 2
    comp_kw: dict = {}

    def setUp(self):
        self.comp = PointerCompositor(manager_version=self.manager_version,
                                   **self.comp_kw)
        self.addCleanup(self.comp.close)
        self._env = {k: os.environ.get(k) for k in
                     ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "WDOTOOL_VKBD",
                      "WDOTOOL_REL_MODE", "SWAYSOCK")}
        self.addCleanup(self._restore)
        os.environ["XDG_RUNTIME_DIR"] = self.comp.dir
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(self.comp.path)
        os.environ.pop("WDOTOOL_VKBD", None)
        os.environ.pop("WDOTOOL_REL_MODE", None)
        # No sway socket: the kernel path's relative moves are warps here, so
        # a test that sees `motion` on the wire saw it because the *protocol*
        # path chose it and not because sway happened to be running.
        os.environ["SWAYSOCK"] = "/nonexistent/wdotool-no-sway"

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def daemon(self, uinput=True):
        d = daemon._Daemon()
        d._reader = None                 # no key-state reads in a test
        if uinput:
            d.kb = RecorderDev()
            d.mouse = RecorderDev()
            d.tablet = RecorderDev()
            d.dev_error = None
        else:
            d.kb = d.mouse = d.tablet = None
            d.dev_error = UINPUT_ERROR
            d.create_devices = lambda: None    # the retry keeps failing
        self.addCleanup(d._drop_vptr)
        self.addCleanup(d._drop_vkbd)
        return d


# ---------------------------------------------------------------------------
# the wire


class Creation(VptrTest):
    def test_it_creates_a_pointer_and_binds_the_manager_at_version_one(self):
        """v2's only addition is `create_virtual_pointer_with_output`, which
        maps into one output's box and CONFINES the cursor to it -- measured,
        with a relative motion of +4000 stopping at that output's own edge.
        Every wdotool coordinate is a layout coordinate, so we bind v1 and
        say so on the wire."""
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.flush()
        self.assertEqual(vp.version, 1)
        self.assertIn((vptr.MANAGER, 1), self.comp.binds)
        self.assertEqual(len(self.comp.created), 1)
        self.assertEqual(self.comp.with_output, [],
                         "the per-output constructor confines the cursor")

    def test_it_passes_the_seat_it_bound(self):
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.flush()
        seat, _oid = self.comp.created[0]
        self.assertNotEqual(seat, 0)
        self.assertIn(("wl_seat", 7), self.comp.binds)

    def test_destroy_on_close(self):
        vp = vptr.VirtualPointer.open()
        vp.flush()
        vp.close()
        self.assertTrue(_eventually(lambda: self.comp.destroyed == 1))

    def test_a_second_close_is_harmless(self):
        vp = vptr.VirtualPointer.open()
        vp.close()
        vp.close()
        self.assertTrue(_eventually(lambda: self.comp.destroyed == 1))


class ACompositorWithNoSeat(VptrTest):
    comp_kw = {"with_seat": False}

    def test_the_seat_argument_is_null(self):
        """`seat` is allow-null in this protocol, and a NULL was accepted and
        still moved the cursor. A seat with no pointer capability is exactly
        where a virtual pointer is worth creating, so nothing here requires
        one."""
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.flush()
        self.assertEqual(self.comp.created[0][0], 0)


class ACompositorWithoutTheProtocol(VptrTest):
    manager_version = None      # Mutter; KWin (both measured)

    def test_open_says_so_and_does_not_raise_anything_else(self):
        with self.assertRaises(vptr.VptrError) as cm:
            vptr.VirtualPointer.open()
        self.assertIn("does not implement", str(cm.exception))
        self.assertIn(vptr.MANAGER, str(cm.exception))
        self.assertEqual(self.comp.created, [])


class ACompositorThatRefuses(VptrTest):
    comp_kw = {"refuse_create": True}

    def test_a_protocol_error_becomes_a_vptrerror(self):
        with self.assertRaises(vptr.VptrError) as cm:
            vptr.VirtualPointer.open()
        self.assertIn("no virtual pointer for you", str(cm.exception))


# ---------------------------------------------------------------------------
# coordinates


class TheLayout(VptrTest):
    def test_the_bbox_is_the_one_the_daemon_computes(self):
        self.assertEqual(daemon._wayland_bbox(), BBOX)

    def test_the_scaled_head_is_read_from_xdg_output_not_wl_output(self):
        """`wl_output.scale` reported 2 for the 1.5-scaled head, where
        `zxdg_output_v1.logical_size` gave the correct 1280x720. The bbox
        prefers xdg_output and is right; this pins that it does, by showing
        what the wl_output-only fallback would have said."""
        self.assertEqual(daemon._wayland_bbox(), BBOX)
        comp = PointerCompositor(with_xdg_output=False)
        self.addCleanup(comp.close)
        os.environ["XDG_RUNTIME_DIR"] = comp.dir
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(comp.path)
        self.assertEqual(daemon._wayland_bbox(), (-1920, -540, 4800, 1620),
                         "wl_output's scale of 2 shrinks the panel to 960x540 "
                         "and moves the layout's right edge 320px")


TARGETS = ((-1920, -540), (3199, 1079), (0, 0), (-1, -1), (1920, 0),
           (3199, 0), (2560, 360), (960, 540), (-960, 0), (0, -540),
           (3199, 719), (1919, 1079), (-1920, 539), (1000, 1000))


class AbsoluteMotion(VptrTest):
    """`motion_absolute(time, x, y, x_extent, y_extent)` is a ratio over the
    whole output layout in logical pixels -- not pixels, and not one output.
    So the forward map is the identity, and the round trip has to be exact:
    no quantisation means the kernel path's ceiling map (B7) has no
    counterpart here and no way to reintroduce the off-by-one it fixed."""

    def test_every_target_lands_with_no_error_at_all(self):
        d = self.daemon(uinput=False)
        for tx, ty in TARGETS:
            self.comp.events.clear()
            d.op_mousemove_abs(tx, ty, [])
            ev = self.comp.last("motion_absolute")
            self.assertEqual(ev[3:], (BBOX[2], BBOX[3]),
                             "the extents ARE the layout size")
            self.assertEqual(landed(ev), (float(tx), float(ty)),
                             f"target {tx},{ty}")

    def test_the_daemon_model_follows(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(2560, 360, [])
        self.assertEqual((d.px, d.py), (2560, 360))
        self.assertTrue(d.pos_known)

    def test_it_clamps_to_the_layout(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(99999, 99999, [])
        self.assertEqual(landed(self.comp.last("motion_absolute")),
                         (3199.0, 1079.0))
        d.op_mousemove_abs(-99999, -99999, [])
        self.assertEqual(landed(self.comp.last("motion_absolute")),
                         (-1920.0, -540.0))

    def test_a_frame_follows_the_motion(self):
        """Motion applies without one, but the receiving client then never
        sees `wl_pointer.frame`."""
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        kinds = [e[0] for e in self.comp.events if e[0] != "create"]
        self.assertEqual(kinds, ["motion_absolute", "frame"])

    def test_the_same_coordinate_twice_is_sent_twice(self):
        """The kernel path has to nudge an axis here (B2: the kernel drops an
        EV_ABS whose value has not changed). This one has nothing to work
        around -- and must not, because a physical mouse may have moved the
        cursor in between."""
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(100, 100, [])
        d.op_mousemove_abs(100, 100, [])
        moves = self.comp.of("motion_absolute")
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[0], moves[1])

    def test_a_zero_extent_is_floored_to_one(self):
        """x_extent == 0 is a silent no-op on the wire -- no motion event at
        all -- so a degenerate layout must not swallow the move."""
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.warp(0, 0, 0, 0, 0, 0)
        vp.flush()
        self.assertEqual(self.comp.last("motion_absolute")[3:], (1, 1))


class TheTwoPathsLandTogether(VptrTest):
    """The point of the branch: `wdotool mousemove X Y` puts the pointer at
    X,Y whichever device carries it. The kernel arithmetic is the tablet's
    (ceil to an axis value, libinput scales it back, the compositor truncates
    to a pixel); the protocol's is a ratio. Both have to end at the target."""

    def test_the_same_command_reaches_the_same_pixel(self):
        virt = self.daemon(uinput=False)
        kern = self.daemon(uinput=True)
        gx, gy, w, h = BBOX
        for tx, ty in TARGETS:
            self.comp.events.clear()
            kern.tablet.events.clear()
            virt.op_mousemove_abs(tx, ty, [])
            kern.op_mousemove_abs(tx, ty, [])
            ax, ay = abs_report(kern.tablet)
            self.assertEqual(
                (gx + compositor_pixel(ax, w), gy + compositor_pixel(ay, h)),
                (tx, ty), f"kernel path, target {tx},{ty}")
            self.assertEqual(landed(self.comp.last("motion_absolute")),
                             (float(tx), float(ty)),
                             f"protocol path, target {tx},{ty}")
        self.assertEqual((virt.px, virt.py), (kern.px, kern.py))


class RelativeMotion(VptrTest):
    """`motion(time, dx, dy)` in wl_fixed, and it is exact: a virtual pointer
    is not a libinput device, so no acceleration profile can apply to it. B1
    -- 500 requested pixels moving 858 through /dev/uinput -- cannot come
    back here."""

    def test_it_is_sent_as_pixels(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        self.comp.events.clear()
        d.op_mousemove_rel(-50, 5, [])
        self.assertEqual(self.comp.of("motion", "motion_absolute"),
                         [("motion", -50.0, 5.0)])
        self.assertEqual((d.px, d.py), (-50, 5))

    def test_a_frame_follows_it(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_rel(1, 0, [])
        self.assertEqual([e[0] for e in self.comp.events if e[0] != "create"],
                         ["motion", "frame"])

    def test_it_does_not_warp_where_the_kernel_path_would(self):
        """There is no sway socket in this environment, so the kernel path
        emits the target as an absolute warp (B1). The protocol path must not
        copy that: it has nothing to work around, and a warp would need a
        position model it may not have."""
        self.assertIs(self.daemon()._rel_absolute(), True)
        self.assertIs(self.daemon()._rel_absolute(virtual=True), False)

    def test_it_is_right_before_the_daemon_knows_where_the_pointer_is(self):
        d = self.daemon(uinput=False)
        self.assertFalse(d.pos_known)
        d.op_mousemove_rel(37, -11, [])
        self.assertEqual(self.comp.of("motion"), [("motion", 37.0, -11.0)],
                         "the motion asked for, not one computed from 0,0")

    def test_it_does_not_invent_a_position_it_never_knew(self):
        """A delta applied to a position nobody knows is a guess, and B6's
        rule is that a guess is never reported as known. `mousemove_relative`
        used to set pos_known unconditionally, so on this path -- where
        nothing can be asked -- the very next `getmouselocation` answered a
        fabricated coordinate with rc 0."""
        d = self.daemon(uinput=False)
        self.assertFalse(d.pos_known)
        d.op_mousemove_rel(37, -11, [])
        self.assertFalse(d.pos_known, "still nobody knows")
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "pointer"})
        self.assertEqual(str(cm.exception), daemon.POINTER_UNKNOWN)

    def test_the_unknown_position_refusal_owes_a_route(self):
        """`xdotool getmouselocation` always answers, so a session where ours does not is a gap of ours and
        AGENTS.md wants the rung that would close it in the sentence.  `daemon.POINTER_UNKNOWN` used to stop
        at what the protocol and sway's IPC do not carry; it was expectedFailure here from 2026-09-09 until
        the string was moved the same day.

        Both routes are worked out in wdotool/vptr.py's header and the sentence carries the short form of
        each: a wlr-layer-shell overlay whose `wl_pointer.motion` IS the cursor (route 1, at the cost of a
        surface that eats the events it reads) or evdev off /dev/input (route 4, at the cost of read access
        and an anchor to count from)."""
        # the shape the rest of the tree prints, not the bare word: `route` alone would turn green on any
        # edit that happened to use it, and the deliverable is the rung
        self.assertIn("AGENTS.md route 1", daemon.POINTER_UNKNOWN)
        self.assertIn("route 4", daemon.POINTER_UNKNOWN)
        self.assertIn("not yet", daemon.POINTER_UNKNOWN)
        # and the head the two byte-for-byte pins in this file read is still the head
        self.assertTrue(daemon.POINTER_UNKNOWN.startswith("wdotool does not know where the pointer is:"))

    def test_a_position_that_was_known_survives_it_exactly(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(1000, 500, [])
        d.op_mousemove_rel(-40, 7, [])
        self.assertEqual(d.handle({"op": "pointer"}),
                         {"ok": True, "x": 960, "y": 507, "known": True})

    def test_rel_mode_abs_still_forces_a_warp(self):
        os.environ["WDOTOOL_REL_MODE"] = "abs"
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(100, 100, [])
        self.comp.events.clear()
        d.op_mousemove_rel(10, 10, [])
        self.assertEqual(landed(self.comp.last("motion_absolute")),
                         (110.0, 110.0))

    def test_sub_pixel_deltas_survive_the_fixed_encoding(self):
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.move(1, 0)
        vp.flush()
        self.assertEqual(self.comp.of("motion"), [("motion", 1.0, 0.0)])


# ---------------------------------------------------------------------------
# buttons and scroll


class Buttons(VptrTest):
    """`button()` takes raw evdev codes and does not validate them; all eight
    wdotool buttons arrive verbatim, and they are the numbers daemon._BTN
    already uses for the kernel device."""

    ALL = ((1, uinput.BTN_LEFT), (2, uinput.BTN_MIDDLE),
           (3, uinput.BTN_RIGHT), (8, uinput.BTN_SIDE),
           (9, uinput.BTN_EXTRA), (10, uinput.BTN_FORWARD),
           (11, uinput.BTN_BACK), (12, uinput.BTN_TASK))

    def test_every_button_arrives_as_its_evdev_code(self):
        d = self.daemon(uinput=False)
        for btn, code in self.ALL:
            self.comp.events.clear()
            d.op_button(btn, True)
            d.op_button(btn, False)
            self.assertEqual(self.comp.of("button", "frame"),
                             [("button", code, 1), ("frame",),
                              ("button", code, 0), ("frame",)], btn)

    def test_the_codes_are_the_kernel_paths_codes(self):
        self.assertEqual(dict(self.ALL), daemon._Daemon._BTN)

    def test_an_invalid_button_is_refused_on_this_path_too(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.op_button(42, True)
        self.assertIn("invalid mouse button", str(cm.exception))

    def test_a_second_press_is_not_sent(self):
        """Button state is refcounted per seat: press, press then release
        would leave the button DOWN, where the kernel drops the duplicate and
        the release lands. Both paths drop it, so both behave the same."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        d.op_button(1, True)
        d.op_button(1, False)
        self.assertEqual(self.comp.of("button"),
                         [("button", uinput.BTN_LEFT, 1),
                          ("button", uinput.BTN_LEFT, 0)])
        self.assertEqual(d.btns, set())

    def test_a_release_we_do_not_hold_is_not_sent(self):
        d = self.daemon(uinput=False)
        d.op_button(1, False)
        self.assertEqual(self.comp.of("button"), [])

    def test_the_kernel_path_drops_the_same_duplicates(self):
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        d.op_button(1, True)
        d.op_button(1, False)
        d.op_button(1, False)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1),
                                          ("KEY", uinput.BTN_LEFT, 0)])

    def test_click_is_a_press_and_a_release(self):
        d = self.daemon(uinput=False)
        d.op_click(1, 2, 0)
        self.assertEqual(self.comp.of("button"),
                         [("button", uinput.BTN_LEFT, 1),
                          ("button", uinput.BTN_LEFT, 0)] * 2)
        self.assertEqual(len(self.comp.created), 1,
                         "the sink is chosen once for the whole run")


class Scroll(VptrTest):
    """The sign is Wayland's, not evdev's: positive vertical is scroll DOWN.
    So wdotool's wheel buttons map 4 -> axis 0 negative, 5 -> axis 0 positive,
    6 -> axis 1 negative, 7 -> axis 1 positive."""

    def test_the_four_wheel_buttons(self):
        want = {4: (vptr.AXIS_VERTICAL, -1), 5: (vptr.AXIS_VERTICAL, 1),
                6: (vptr.AXIS_HORIZONTAL, -1), 7: (vptr.AXIS_HORIZONTAL, 1)}
        d = self.daemon(uinput=False)
        for btn, (axis, direction) in want.items():
            self.comp.events.clear()
            d.op_button(btn, True)
            self.assertEqual(
                self.comp.of("axis_source", "axis_discrete", "frame"),
                [("axis_source", vptr.AXIS_SOURCE_WHEEL),
                 ("axis_discrete", axis, 15.0 * direction, direction),
                 ("frame",)], btn)

    def test_a_wheel_release_is_a_no_op(self):
        d = self.daemon(uinput=False)
        d.op_button(4, False)
        self.assertEqual(self.comp.of("axis_discrete", "axis"), [])

    def test_a_detent_is_a_notch_not_a_smooth_scroll(self):
        """`axis_discrete` is what carries `axis_value120` (120 per detent) to
        a wl_pointer >= 8 client; plain `axis` would arrive as a touchpad-ish
        smooth scroll of 15 units."""
        d = self.daemon(uinput=False)
        d.op_button(5, True)
        self.assertEqual(self.comp.of("axis"), [])
        self.assertEqual(self.comp.last("axis_discrete")[1:],
                         (vptr.AXIS_VERTICAL, 15.0, 1))

    def test_the_wheel_tables_are_the_same_four_gestures(self):
        self.assertEqual(sorted(vptr.WHEEL), sorted(daemon._Daemon._WHEEL))

    def test_they_are_opposite_in_the_vertical(self):
        """REL_WHEEL positive is up; Wayland's axis 0 positive is down. A
        table copied across without the flip would scroll backwards."""
        for btn in (4, 5):
            rel, value = daemon._Daemon._WHEEL[btn]
            axis, direction = vptr.WHEEL[btn]
            self.assertEqual(rel, uinput.REL_WHEEL)
            self.assertEqual(axis, vptr.AXIS_VERTICAL)
            self.assertEqual(value, -direction)

    def test_and_the_same_in_the_horizontal(self):
        for btn in (6, 7):
            rel, value = daemon._Daemon._WHEEL[btn]
            axis, direction = vptr.WHEEL[btn]
            self.assertEqual(rel, uinput.REL_HWHEEL)
            self.assertEqual(axis, vptr.AXIS_HORIZONTAL)
            self.assertEqual(value, direction)


# ---------------------------------------------------------------------------
# the policy


class ACosmicShapedSession(VptrTest):
    """The pointer half on COSMIC, over cosmic-comp's own registry.

    Measured on a live COSMIC 1.7: `wdotool type 'hello cosmic'` landed byte-exact through
    `zwp_virtual_keyboard_v1` on a box with no `/dev/uinput` node at all, and `wdotool mousemove 400 300`
    answered `cannot create uinput devices` because there is no `zwlr_virtual_pointer_manager_v1` to fall
    back to [M recon2/cosmic.md §3]. So COSMIC is the one session where the two halves come apart entirely,
    and this class is that session."""

    manager_version = None      # cosmic-comp advertises no virtual pointer
    comp_kw = {"extra_globals": cosmic_globals(
        ("wl_seat", "wl_output", "zxdg_output_manager_v1", vptr.MANAGER))}

    def test_the_session_these_tests_talk_to_offers_no_virtual_pointer(self):
        """Read off the socket every other test in this class uses, not out of the fixture file: what has to
        be true is what the daemon meets -- the recording minus the four globals this fake serves itself --
        and a `comp_kw` that subtracted the wrong ones would leave the file right and the session wrong. If
        cosmic-comp ever grows a virtual pointer the fixture changes and this goes red, which is correct."""
        conn = WlConn(self.comp.path)
        self.addCleanup(conn.close)
        ifaces = {iface for iface, _ver in conn.get_registry().values()}
        self.assertNotIn(vptr.MANAGER, ifaces)
        self.assertIn("zwp_virtual_keyboard_manager_v1", ifaces,
                      "the keyboard half is exactly what does work there")
        self.assertNotIn("zwlr_foreign_toplevel_manager_v1", ifaces)

    def test_vkbd_on_click_names_cosmic(self):
        """The refusal used to read `(Mutter and KWin do not; sway/wlroots does)`, which told a COSMIC user
        to look for a compositor bug that is not there."""
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0,
                      "vkbd_mode": "on"})
        said = str(cm.exception)
        self.assertIn("--vkbd on", said)
        self.assertIn(vptr.MANAGER, said)
        self.assertIn("Mutter, KWin and COSMIC do not", said)
        self.assertIn("sway, Hyprland and the wlroots family do", said)
        self.assertIn("--vkbd on type types there", said,
                      "and it points at the half that does work")
        self.assertEqual(d.mouse.events, [], "it must not silently use uinput")

    def test_the_forced_refusal_names_the_rung_auto_would_have_taken(self):
        """The other half of the sentence above, added 2026-09-09.  `--vkbd on` is the only way to meet this
        on COSMIC at all -- the very next test shows `auto` clicking through the kernel devices on the same
        session -- so the gap is the sink the user named, and the rung is the one they said no to.  Read off
        a real refusal and not off `daemon.VPTR_FORCED_ROUTE`, which would be the constant asserting itself:
        what has to be true is that _pick_pointer appends it to what vptr.py raised."""
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0,
                      "vkbd_mode": "on"})
        said = str(cm.exception)
        self.assertIn("not yet forced here", said)
        self.assertIn("AGENTS.md route 4", said)
        self.assertIn("/dev/uinput", said)
        self.assertTrue(said.endswith(daemon.VPTR_FORCED_ROUTE),
                        "the rung goes last, after the protocol's own reason")

    def test_vkbd_auto_click_falls_back_to_uinput(self):
        """`auto` is the kernel devices unless they cannot be used at all, and on COSMIC there is nothing
        else -- so a session with /dev/uinput clicks through it and never opens a pointer."""
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])
        self.assertEqual(self.comp.created, [])

    def test_without_uinput_a_click_is_the_uinput_error_and_nothing_else(self):
        """The measured line, byte for byte: `wdotool mousemove 400 300` -> `cannot create uinput devices`,
        rc 1, on the box with no /dev/uinput node [M cosmic.md §3]."""
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "mousemove_abs", "x": 400, "y": 300})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


class ThePolicy(VptrTest):
    """One sentence, and it is the keyboard's: the protocol moves when the
    kernel pointer cannot be opened and the compositor implements it,
    /dev/uinput in every other case; --vkbd on|off forces either."""

    def test_with_uinput_the_kernel_devices_move_even_here(self):
        d = self.daemon(uinput=True)
        d.op_mousemove_abs(100, 200, [])
        d.op_button(1, True)
        self.assertTrue(d.tablet.events)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])
        self.assertEqual(self.comp.created, [],
                         "no virtual pointer may be created at all")

    def test_without_uinput_the_protocol_moves(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(100, 200, [])
        d.op_button(1, True)
        self.assertEqual(len(self.comp.created), 1)
        self.assertEqual(landed(self.comp.of("motion_absolute")[0]),
                         (100.0, 200.0))
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 1))

    def test_the_notice_names_the_protocol_and_is_said_once(self):
        d = self.daemon(uinput=False)
        resp = d.handle({"op": "mousemove_abs", "x": 0, "y": 0})
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(any("zwlr_virtual_pointer_v1" in w
                            for w in resp["warnings"]), resp)
        self.assertTrue(any("no root and no device rule" in w
                            for w in resp["warnings"]), resp)
        resp = d.handle({"op": "mousemove_abs", "x": 1, "y": 1})
        self.assertEqual(resp["warnings"], [], "and only once")

    def test_without_uinput_and_without_the_protocol_nothing_changes(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=False)
        for req in ({"op": "mousemove_abs", "x": 3, "y": 4},
                    {"op": "mousemove_rel", "dx": 1, "dy": 1},
                    {"op": "button", "btn": 1, "down": True},
                    {"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0}):
            with self.assertRaises(RuntimeError) as cm:
                d.handle(req)
            self.assertEqual(str(cm.exception), UINPUT_ERROR, req["op"])

    def test_vkbd_off_keeps_the_kernel_path_and_its_error(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True,
                      "vkbd_mode": "off"})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)
        self.assertEqual(self.comp.created, [])

    def test_vkbd_on_uses_the_protocol_although_uinput_works(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "mousemove_abs", "x": 5, "y": 6, "vkbd_mode": "on"})
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        self.assertEqual(landed(self.comp.last("motion_absolute")), (5.0, 6.0))
        self.assertEqual(d.mouse.events, [])
        self.assertEqual(d.tablet.events, [],
                         "nothing may reach the kernel devices")

    def test_vkbd_on_without_the_protocol_is_a_clean_refusal(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0,
                      "vkbd_mode": "on"})
        self.assertIn("--vkbd on", str(cm.exception))
        self.assertIn("zwlr_virtual_pointer_v1", str(cm.exception))
        self.assertEqual(d.mouse.events, [],
                         "it must not silently use uinput")

    def test_the_environment_selects_it_too(self):
        os.environ["WDOTOOL_VKBD"] = "on"
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 1))

    def test_the_flag_beats_the_environment(self):
        os.environ["WDOTOOL_VKBD"] = "on"
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "off"})
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])
        self.assertEqual(self.comp.created, [])

    def test_a_nonsense_environment_value_is_auto_not_a_failure(self):
        os.environ["WDOTOOL_VKBD"] = "yes-please"
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])

    def test_an_unknown_mode_on_the_wire_is_a_rejected_request(self):
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True,
                      "vkbd_mode": "maybe"})
        self.assertIn("invalid vkbd_mode", str(cm.exception))

    def test_the_two_halves_are_chosen_independently(self):
        """This compositor implements the pointer protocol and not the
        keyboard one, which is the case that proves the choice is made twice:
        clicking works with no privilege while typing still reports the
        kernel keyboard's error."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 1))
        with self.assertRaises(RuntimeError) as cm:
            d.op_type("a", 0, False)
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


class ThePointerCommandsOverTheWire(VptrTest):
    def test_the_client_sends_the_mode_and_the_daemon_reads_it(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        c = FakeClient()
        c.click(1, 1, 0, vkbd_mode="on")
        self.assertEqual(sent["vkbd_mode"], "on")
        c.mousemove_abs(1, 2, vkbd_mode="off")
        self.assertEqual(sent["vkbd_mode"], "off")
        c.mousemove_rel(1, 2, vkbd_mode="on")
        self.assertEqual(sent["vkbd_mode"], "on")
        c.button(1, True, vkbd_mode="off")
        self.assertEqual(sent["vkbd_mode"], "off")

    def test_an_absent_mode_is_not_in_the_request(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        FakeClient().click(1, 1, 0)
        self.assertNotIn("vkbd_mode", sent)
        self.assertNotIn("layout_mode", sent)

    def test_the_flag_reaches_the_pointer_commands(self):
        from wdotool import input_cmds

        calls = []

        class Ctx:
            stack: list = []
            vkbd_mode = "on"

            def daemon(self):
                class D:
                    def click(self, *a, **kw):
                        calls.append(("click", kw))

                    def button(self, *a, **kw):
                        calls.append(("button", kw))

                    def mousemove_abs(self, *a, **kw):
                        calls.append(("abs", kw))

                    def mousemove_rel(self, *a, **kw):
                        calls.append(("rel", kw))

                    def seed_pointer(self, *a):
                        pass

                    def pointer(self):
                        return (0, 0, True)      # (x, y, known)

                return D()

            def resolve_windows(self, arg):
                return [None]

            def backend(self):
                return object()      # no `pointer`: the model stands

        ctx = Ctx()
        input_cmds.cmd_click(ctx, ["1"])
        input_cmds.cmd_mousedown(ctx, ["1"])
        input_cmds.cmd_mousemove(ctx, ["10", "20"])
        input_cmds.cmd_mousemove_relative(ctx, ["1", "2"])
        self.assertEqual([k for _n, k in calls],
                         [{"clearmods": False, "vkbd_mode": "on"}] * 2
                         + [{"clearmods": False, "vkbd_mode": "on"}] * 2)


class TheQueryThatComesBeforeTheMove(unittest.TestCase):
    """`mousemove` and `mousemove_relative` ask where the pointer is before
    they move it -- the first for `mousemove restore`, the second to count
    the delta from the real position (B1/B6). On sway with no /dev/uinput
    NOTHING can answer that: sway's IPC has no cursor position and the
    protocol has no events, so `daemon.pointer()` refuses. The query used to
    be mandatory, which made the refusal fail the move -- so on the one kind
    of session this whole path exists for, `wdotool mousemove 100 100` exited
    1 and moved nothing, every time, however well the protocol worked."""

    def _ctx(self, calls):
        from w11common.errors import CmdError

        class Ctx:
            stack: list = []

            def daemon(self):
                class D:
                    def mousemove_abs(self, *a, **kw):
                        calls.append(("abs", a))

                    def mousemove_rel(self, *a, **kw):
                        calls.append(("rel", a))

                    def seed_pointer(self, *a):
                        raise AssertionError("nothing to seed from")

                    def pointer(self):
                        raise CmdError(daemon.POINTER_UNKNOWN)

                return D()

            def resolve_windows(self, arg):
                return [None]

            def backend(self):
                return object()      # no pointer query on this compositor

        return Ctx()

    def test_an_absolute_move_still_moves(self):
        from wdotool import input_cmds
        calls = []
        ctx = self._ctx(calls)
        input_cmds.cmd_mousemove(ctx, ["100", "200"])
        self.assertEqual(calls, [("abs", (100, 200))])
        self.assertIsNone(ctx._last_mouse,
                          "and `mousemove restore` knows it has nothing")

    def test_a_relative_move_still_moves(self):
        from wdotool import input_cmds
        calls = []
        input_cmds.cmd_mousemove_relative(self._ctx(calls), ["10", "20"])
        self.assertEqual(calls, [("rel", (10, 20))])

    def test_restore_says_so_rather_than_moving_to_nowhere(self):
        from wdotool import input_cmds
        from w11common.errors import CmdError
        calls = []
        ctx = self._ctx(calls)
        input_cmds.cmd_mousemove(ctx, ["100", "200"])
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_mousemove(ctx, ["restore"])
        self.assertIn("Have no previous mouse position", str(cm.exception))


# ---------------------------------------------------------------------------
# held buttons: the lifetime, and the sink switch


class HeldButtons(VptrTest):
    def test_a_button_held_across_commands_keeps_one_pointer(self):
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        d.op_mousemove_abs(500, 500, [])
        d.op_button(1, False)
        self.assertEqual(len(self.comp.created), 1,
                         "one connection and one pointer, for the daemon")
        self.assertEqual(self.comp.of("button", "motion_absolute"),
                         [("button", 0x110, 1),
                          self.comp.last("motion_absolute"),
                          ("button", 0x110, 0)])
        self.assertEqual(d.btns, set())

    def test_a_drag_is_one_object_from_press_to_release(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(-1920, -540, [])
        d.op_button(1, True)
        d.op_mousemove_abs(2560, 360, [])
        d.op_mousemove_rel(10, 0, [])
        d.op_button(1, False)
        kinds = [e[0] for e in self.comp.events]
        self.assertEqual(kinds.count("create"), 1)
        self.assertEqual([k for k in kinds if k != "frame" and k != "create"],
                         ["motion_absolute", "button", "motion_absolute",
                          "motion", "button"])

    def test_a_hold_does_not_survive_a_switch_that_cannot_happen(self):
        """MEASURED ON sway 1.11 with /dev/uinput root-only: `--vkbd on
        mousedown 1` then `--vkbd off mouseup 1` reported the uinput error
        and left the LEFT BUTTON DOWN on the virtual pointer -- a drag
        outliving the command that failed, on exactly the kind of session
        this path exists for. _own_pointer() sits below the raise in
        _pick_pointer(), so it never ran."""
        d = self.daemon(uinput=False)         # no kernel pointer at all
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        warnings = []
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": False,
                      "vkbd_mode": "off"}, None, warnings)
        self.assertEqual(str(cm.exception), UINPUT_ERROR,
                         "the failure is still the kernel device's")
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0),
                         "released on the pointer that was holding it")
        self.assertEqual(d.btns, set())
        self.assertTrue(any("were released" in w for w in warnings), warnings)

    def test_the_release_is_reported_on_the_failing_reply(self):
        """A command that fails can still have changed something. The reply
        an error produces used to drop every warning the command had
        collected, so the one line that says a button was let go of was said
        to nobody."""
        d = self.daemon(uinput=False)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        resp = _over_the_socket(d, {"op": "button", "btn": 1, "down": False,
                                    "vkbd_mode": "off"})
        self.assertFalse(resp["ok"], resp)
        self.assertTrue(any("were released" in w
                            for w in resp.get("warnings", [])), resp)

    def test_an_ordinary_error_still_carries_no_warnings_key(self):
        d = self.daemon(uinput=False)
        resp = _over_the_socket(d, {"op": "button", "btn": 1, "down": True,
                                    "vkbd_mode": "off"})
        self.assertEqual(resp, {"ok": False, "error": UINPUT_ERROR})

    def test_auto_does_not_change_pointers_under_a_held_button(self):
        """`wdotool --vkbd on mousedown 1` and then a plain `wdotool mouseup
        1`: the second command must not switch to the kernel device, because
        a button held on one cannot be released on the other."""
        d = self.daemon(uinput=True)          # uinput works in this one
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        d.handle({"op": "mousemove_abs", "x": 10, "y": 10})   # auto
        d.handle({"op": "button", "btn": 1, "down": False})   # auto
        self.assertEqual(d.mouse.events, [])
        self.assertEqual(d.tablet.events, [],
                         "nothing may leak to the kernel devices mid-hold")
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0))
        self.assertEqual(d.btns, set())

    def test_forcing_off_under_a_held_button_releases_it_on_the_holder(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        resp = d.handle({"op": "button", "btn": 3, "down": True,
                         "vkbd_mode": "off"})
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0),
                         "released on the pointer that was holding it")
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_RIGHT, 1)])
        self.assertTrue(any("were released" in w for w in resp["warnings"]),
                        resp)
        self.assertTrue(any("left" in w for w in resp["warnings"]), resp)
        self.assertEqual(d.btns, {uinput.BTN_RIGHT})

    def test_forcing_on_under_a_kernel_held_button_releases_it_there(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "off"})
        d.mouse.events.clear()
        resp = d.handle({"op": "button", "btn": 1, "down": True,
                         "vkbd_mode": "on"})
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 0)])
        self.assertEqual(self.comp.of("button"), [("button", 0x110, 1)])
        self.assertTrue(any("were released" in w for w in resp["warnings"]),
                        resp)

    def test_nothing_is_said_when_nothing_is_held(self):
        d = self.daemon(uinput=True)
        for mode in ("on", "off", "on"):
            resp = d.handle({"op": "click", "btn": 1, "repeat": 1,
                             "delay_ms": 0, "vkbd_mode": mode})
            self.assertEqual(resp["warnings"], [], mode)

    def test_a_key_and_a_button_are_held_independently(self):
        """Two connections and two objects on purpose: the keyboard's
        troubles must not drop the pointer's held button."""
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        d.op_key("ctrl", "down", 0, False, None, None, "off")   # kernel keys
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        self.assertTrue(d._btns_virtual)
        self.assertFalse(d._down_virtual)
        d.handle({"op": "button", "btn": 1, "down": False})
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0))


class Reconnecting(VptrTest):
    def test_a_compositor_restart_costs_the_hold_and_not_the_command(self):
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        self.comp.drop_clients()
        resp = d.handle({"op": "mousemove_abs", "x": 0, "y": 0})
        self.assertTrue(resp.get("ok"), resp)
        self.assertTrue(any("buttons wdotool was holding" in w
                            for w in resp["warnings"]), resp["warnings"])
        self.assertEqual(d.btns, set(),
                         "the compositor released what we held; so must we")
        self.assertEqual(len(self.comp.created), 2)

    def test_a_restart_with_nothing_held_says_nothing(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        self.comp.drop_clients()
        resp = d.handle({"op": "mousemove_abs", "x": 1, "y": 1})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp["warnings"], [])

    def test_no_button_survives_a_drop_in_the_daemons_model(self):
        """The worst outcome this path could have: a button the daemon
        believes is held on a pointer that no longer exists. Nothing could
        ever release it, and every later click would be a drag.

        The connection is checked before each command, so the ordinary
        restart is the graceful one above; this is the other half -- the
        pointer dying with an injection already in flight, where the write
        that fails is the release of the very button we hold, so the object's
        own `held` no longer lists it. _drop_vptr() must not trust that."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        sink, _virtual = d._pointer_sink()   # the object the op would use
        self.comp.drop_clients()
        with self.assertRaises(RuntimeError):
            with d._vp_guard():
                for _ in range(200):    # fill the socket buffer to notice
                    d._button(sink, 1, False)
                    d._button(sink, 1, True)
        self.assertEqual(d.btns, set(), "nothing may stay down after a drop")
        self.assertIsNone(d._vp)

    def test_a_compositor_that_does_not_come_back_is_a_clean_error(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        self.comp.close()                # socket gone, nothing to reconnect to
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True})
        self.assertEqual(str(cm.exception), UINPUT_ERROR,
                         "the error is the kernel device's, unchanged")

    def test_a_restart_falls_back_to_uinput_when_that_is_what_works(self):
        """uinput available, a button held on the virtual pointer, and the
        compositor gone: the hold cannot be honoured by anyone, so auto is
        free to choose again -- and chooses the kernel devices rather than
        failing the command."""
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        self.comp.drop_clients()
        self.comp.close()
        resp = d.handle({"op": "button", "btn": 3, "down": True})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_RIGHT, 1)])
        self.assertEqual(d.btns, {uinput.BTN_RIGHT})

    def test_the_protocol_withdrawn_mid_session_does_not_break_the_hold(self):
        """global_remove for the manager. A Wayland object outlives the global
        it came from, so the pointer we already have keeps clicking and our
        client must not choke on the event; only the next connection finds the
        protocol gone, and falls back cleanly."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.comp.withdraw_manager()
        d.op_mousemove_abs(0, 0, [])
        d.op_button(1, False)
        self.assertEqual(len(self.comp.created), 1)
        self.comp.manager_version = None
        self.comp.drop_clients()
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


# ---------------------------------------------------------------------------
# getmouselocation


class WhereThePointerIs(VptrTest):
    """The protocol CANNOT say: `zwlr_virtual_pointer_v1` has no events at
    all, and sway's IPC carries no cursor position either. So the answer is
    exactly what wdotool put there -- which on this path is exact, because
    the absolute map has no error -- and a refusal when it put nothing."""

    def test_it_refuses_rather_than_answering_the_origin(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "pointer"})
        self.assertIn("does not know where the pointer is", str(cm.exception))
        self.assertIn("zwlr_virtual_pointer_v1", str(cm.exception))
        self.assertNotIn("run it as root", str(cm.exception),
                         "uinput is not the thing the user would have to fix")

    def test_it_answers_exactly_once_wdotool_has_moved_it(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(2560, 360, [])
        self.assertEqual(d.handle({"op": "pointer"}),
                         {"ok": True, "x": 2560, "y": 360, "known": True})

    def test_a_relative_move_makes_it_answerable_too(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        d.op_mousemove_rel(10, 20, [])
        self.assertEqual(d.handle({"op": "pointer"})["x"], 10)

    def test_seed_pointer_still_works(self):
        d = self.daemon(uinput=False)
        d.op_seed_pointer(1234, 56, [])
        self.assertEqual(d.handle({"op": "pointer"}),
                         {"ok": True, "x": 1234, "y": 56, "known": True})

    def test_without_the_protocol_the_uinput_error_is_still_the_answer(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "pointer"})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


# ---------------------------------------------------------------------------
# --clearmodifiers on a pointer command


class ClearModifiers(VptrTest):
    def test_a_click_with_no_kernel_device_still_clears_modifiers(self):
        """`click --clearmodifiers` used to demand /dev/uinput for the
        keyboard half whatever the pointer did, which would have left it the
        one pointer command that still needed root here. There is no virtual
        keyboard on this compositor, so the clear has nothing to do -- and
        must not be the thing that fails the click."""
        d = self.daemon(uinput=False)
        resp = d.handle({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0,
                         "clearmods": True})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(self.comp.of("button"),
                         [("button", 0x110, 1), ("button", 0x110, 0)])


class APointerThatFailsMidRelease(VptrTest):
    """T38, the pointer twin of tests/test_vkbd.py's
    ADeviceThatFailsMidRelease: `_own_pointer` walks the held buttons on the
    device that is holding them, and the walk stops at the first write that
    fails.

    The failure is scripted on the object rather than by hanging up on the
    socket, because the daemon writes both releases back to back with no
    round trip between them: whether the second one gets EPIPE depends on
    when the compositor's thread ran, and a test that depends on that is a
    test that fails on a loaded machine. What is being pinned here is the
    walk, not the socket -- Reconnecting above owns the real drop."""

    class Flaky:
        """The virtual pointer, delegating, until the `fail_at`th release."""

        def __init__(self, real, fail_at=2):
            self._real = real
            self.fail_at = fail_at
            self.releases = 0

        def button(self, code, down):
            if not down:
                self.releases += 1
                if self.releases >= self.fail_at:
                    raise vptr.VptrError("the compositor closed the connection")
            self._real.button(code, down)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def test_every_held_button_is_released_or_named(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        d.handle({"op": "button", "btn": 3, "down": True, "vkbd_mode": "on"})
        self.assertEqual(d.btns, {uinput.BTN_LEFT, uinput.BTN_RIGHT})
        d._vp = self.Flaky(d._vp, fail_at=2)
        self.comp.events.clear()

        resp = d.handle({"op": "button", "btn": 1, "down": True,
                         "vkbd_mode": "off"})
        self.assertTrue(resp.get("ok"), resp)
        # BTN_LEFT (0x110) sorts before BTN_RIGHT (0x111): the first release
        # reaches the compositor, the second is the one that fails.
        self.assertEqual(self.comp.of("button"),
                         [("button", uinput.BTN_LEFT, 0)])
        switch = [w for w in resp["warnings"] if "were released" in w]
        self.assertEqual(len(switch), 1, resp["warnings"])
        self.assertIn("left", switch[0])
        self.assertIn("right", switch[0])
        # the point: neither stays in the model, so the kernel pointer that
        # now owns the button starts from an honest empty set
        self.assertEqual(d.btns, {uinput.BTN_LEFT})   # the one just pressed
        self.assertFalse(d._btns_virtual)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])


def _eventually(pred, tries=100):
    import time

    for _ in range(tries):
        if pred():
            return True
        time.sleep(0.01)
    return pred()


if __name__ == "__main__":
    unittest.main(verbosity=1)
