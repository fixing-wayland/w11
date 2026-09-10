#!/usr/bin/env python3
"""`SendEvent`: one case per row of the measured `ClientMessage` table.

Every message here comes out of `tests/fixtures/xw11/caps/` -- the bytes
`xdotool` and `wmctrl` really sent, decoded field by field in recon/tools.md
4.4 -- or is packed by hand at recon/wire.md 4.4's offsets for a case no tool
in the survey sends (`WM_PROTOCOLS` to the window itself, an unknown type).

What the file is defending:

* the route is decided by the ClientMessage's own `window` and `type` and NOT
  by the request's destination: `wmctrl -c <n>` sends `_NET_CLOSE_WINDOW` to
  the ROOT with the window in the event [M recon/tools/caps/xwl.jsonl MARK 46];
* `_NET_WM_STATE`'s two axes fold into one `set_state` call wherever the
  backend names the pair (GNOME), and into two where it does not (sway, KWin)
  -- `backend.state_steps`, because sending the axes one after the other
  corrupts a Mutter window's saved rectangle (wdotool/backend.py:100);
* the dual-plane rule: a routed type on a REAL X id passes when upstream's own
  `_NET_SUPPORTED` names it and is routed through that window's compositor
  handle when it does not. wlroots' Xwayland has no `_NET_WM_DESKTOP`, no
  `_NET_MOVERESIZE_WINDOW` and no desktop atom at all [recon/env.md 2.1];
* a routed message is CONSUMEd even when the backend refuses -- X answers a
  `ClientMessage` with nothing, so there is nothing to say to the client and
  the log carries the reason (design section 3.3);
* everything else passes byte for byte.
"""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

from support import FakeBackend, fake_view, fake_window            # noqa: E402
from w11common.errors import CmdError                              # noqa: E402
from xw11 import ewmh, policy, wire                                # noqa: E402
# `Capture` and `WriteCase` are batch 4's own, in batch 4's other file: the
# shared doubles live in tests/support.py and nothing else may go there, so a
# helper two files of one batch share is imported rather than copied.
from test_xw11_write import Capture, WriteCase, foot_window        # noqa: E402

#: `SubstructureNotify|SubstructureRedirect`, which is the mask every EWMH
#: sender uses [recon/wire.md 4.2, recon/tools.md 4.4].
SUBSTRUCTURE = 0x180000


def client_message(window, type_atom, data, code=wire.EV_CLIENT_MESSAGE,
                   fmt=32):
    """The 32-byte event, packed at recon/wire.md 4.4's offsets: the code, the
    format in byte 1, the sequence, `window` at 4, `type` at 8 and five CARD32s
    at 12."""
    return (struct.pack("<BBHII", code, fmt, 0, window, type_atom)
            + struct.pack("<5I", *(list(data) + [0] * 5)[:5]))


class EwmhCase(WriteCase):
    """`WriteCase` with a `SendEvent` sender and a routed-type resolver."""

    num, upstream_num = 800, 801

    def send_message(self, name, window, data, dest=None, mask=SUBSTRUCTURE):
        """One `SendEvent` the way the tools send it: destination the root,
        the target in the event's own `window` field."""
        ev = client_message(window, self.atom(name), data)
        body = struct.pack("<II", self.root if dest is None else dest, mask) + ev
        return self.send(wire.OP_SEND_EVENT, 0, body)

    def send_capture_messages(self, name):
        """Every `SendEvent` of a capture, rewritten onto this rig and sent in
        the capture's own order."""
        cap = Capture(name)
        for frame in cap.of(wire.OP_SEND_EVENT):
            self.send_frame(cap.rewrite(frame, self.root, self.shadow(),
                                        self.conn.atom))
        return cap


class TheMeasuredMessages(EwmhCase):
    """One test per row of recon/tools.md 4.4, from that row's own capture."""

    num, upstream_num = 802, 803

    def test_windowactivate_sets_the_desktop_and_then_activates(self):
        """`xdotool windowactivate <w>` sends TWO messages: `_NET_CURRENT_
        DESKTOP [0, ...]` to the root and then `_NET_ACTIVE_WINDOW [2, ...]`
        to the window, source 2 (pager) [M xvfb.jsonl MARK 8, 47 requests].

        The openbox capture, for the reason `set_desktop`'s is: against a bare
        Xwayland the tool sends only the second message, because wlroots' xwm
        does not name `_NET_CURRENT_DESKTOP` in `_NET_SUPPORTED`
        [recon/env.md 2.1] -- `xdotool-windowactivate-bare.hex` is that
        41-request stream. Through this proxy the union names it, so both
        messages are what a live run produces."""
        self.send_capture_messages("xdotool-windowactivate")
        self.assertEqual([name for (name, _a) in self.backend.calls
                          if name in ("set_desktop", "activate")],
                         ["set_desktop", "activate"])
        self.assertEqual(self.calls("set_desktop"), [(0,)])
        self.assertEqual(self.calls("activate"), [(self.handle,)])

    def test_wmctrl_a_activates_with_its_own_source_zero(self):
        """`wmctrl -a <n>` sends the same pair with source **0**, not 2
        [M MARK 38]. The source indication is ignored: no backend takes one."""
        self.send_capture_messages("wmctrl-a")
        self.assertEqual(self.calls("activate"), [(self.handle,)])

    def test_wmctrl_r_e_moves_and_resizes_from_its_flag_bits(self):
        """`wmctrl -r <n> -e 0,10,10,300,200` sends `_NET_MOVERESIZE_WINDOW`
        with `[0xf00, 10, 10, 300, 200]` -- gravity 0, source 0, and bits 8-11
        for the four values [M xvfb.jsonl MARK 39]. It is the openbox capture
        because against a bare Xwayland wmctrl falls back to `ConfigureWindow`;
        through this proxy the atom IS in `_NET_SUPPORTED`, so this is the
        stream a live run produces."""
        self.send_capture_messages("wmctrl-r-e")
        self.assertEqual(self.calls("move_window"), [(self.handle, 10, 10)])
        self.assertEqual(self.calls("resize"), [(self.handle, 300, 200)])

    def test_a_negative_coordinate_arrives_as_a_signed_long(self):
        """`wmctrl -r <n> -e 0,-10,-10,300,200` puts `0xFFFFFFF6` in
        `data32[1..2]`: `data.l[]` is a signed long and the wire word is its
        low 32 bits. Read unsigned it is 4294967286, and the window lands off
        the far edge of a display that does not go that far. Its sibling on the
        core side is `ConfigureBits.
        test_a_negative_coordinate_arrives_as_an_int16_in_a_card32`."""
        self.send_message("_NET_MOVERESIZE_WINDOW", self.shadow(),
                          [ewmh.MR_X | ewmh.MR_Y, 0xFFFFFFF6, 0xFFFFFFF6])
        self.assertEqual(self.calls("move_window"), [(self.handle, -10, -10)])

    def test_windowminimize_is_wm_change_state_with_iconic(self):
        """`xdotool windowminimize` sends `WM_CHANGE_STATE [3, ...]`, which is
        `IconicState` [M xwl.jsonl MARK 13]."""
        self.send_capture_messages("xdotool-windowminimize")
        self.assertEqual(self.calls("minimize"), [(self.handle,)])

    def test_windowstate_add_becomes_one_set_state_call(self):
        """`xdotool windowstate --add MAXIMIZED_VERT <w>` sends
        `_NET_WM_STATE [1, atom(_NET_WM_STATE_MAXIMIZED_VERT), 0, 0, 0]`
        [M xvfb2.jsonl MARK 5]. The action is the backend's own encoding --
        0 remove, 1 add, 2 toggle -- and the name is the atom's suffix."""
        self.send_capture_messages("xdotool-windowstate-add")
        self.assertEqual(self.calls("set_state"),
                         [(self.handle, "MAXIMIZED_VERT", 1)])

    def test_wmctrl_b_add_sends_the_same_state_message(self):
        """`wmctrl -r <n> -b add,maximized_vert` [M xwl.jsonl MARK 40]."""
        self.send_capture_messages("wmctrl-b-add")
        self.assertEqual(self.calls("set_state"),
                         [(self.handle, "MAXIMIZED_VERT", 1)])

    def test_wmctrl_c_closes_the_window_named_inside_the_event(self):
        """`wmctrl -c <n>` sends `_NET_CLOSE_WINDOW` to the ROOT with the
        window in the event's own field [M MARK 46]: the request's destination
        says root and the route is the window's."""
        cap = self.send_capture_messages("wmctrl-c")
        (dest,) = struct.unpack_from("<I", cap.of(wire.OP_SEND_EVENT)[0], 4)
        self.assertEqual(dest, cap.root, "the capture's destination is the root")
        self.assertEqual(self.calls("close"), [(self.handle,)])

    def test_wmctrl_s_sets_the_desktop(self):
        """`wmctrl -s 1` is the cheapest command in the survey: the prologue,
        one `InternAtom`, one `SendEvent` [recon/tools.md 5, MARK 42]."""
        self.send_capture_messages("wmctrl-s")
        self.assertEqual(self.calls("set_desktop"), [(1,)])

    def test_set_desktop_sets_the_desktop(self):
        """`xdotool set_desktop 1` [M xvfb.jsonl MARK 25]. On a bare Xwayland
        the command exits 1 before it sends anything -- wlroots' xwm does not
        name `_NET_CURRENT_DESKTOP` in `_NET_SUPPORTED` [recon/env.md 2.1,
        recon/tools.md 4.10] -- which is the gap the proxy's union closes, so
        the openbox capture is the stream a run through the proxy makes."""
        self.send_capture_messages("xdotool-set_desktop")
        self.assertEqual(self.calls("set_desktop"), [(1,)])

    def test_wmctrl_R_moves_the_window_to_a_desktop_and_activates_it(self):
        """`wmctrl -R <n>` is `-a` plus one `_NET_WM_DESKTOP`
        [M xvfb2.jsonl MARK 8, 26 requests, which is recon/tools.md 5's
        count]."""
        self.send_capture_messages("wmctrl-R")
        self.assertEqual(self.calls("set_window_desktop"), [(self.handle, 0)])
        self.assertEqual(self.calls("activate"), [(self.handle,)])

    def test_wmctrl_k_passes_upstream_and_says_not_yet(self):
        """`wmctrl -k on` sends `_NET_SHOWING_DESKTOP [1, ...]` [M MARK 44].
        No backend in the tree has a show-the-desktop verb, so it is forwarded
        and one line names the route rather than a policy."""
        self.send_capture_messages("wmctrl-k")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.rig.upstream.ops[-2:],
                         [wire.OP_SEND_EVENT, wire.OP_GET_INPUT_FOCUS])
        said = self.log.carrying("_NET_SHOWING_DESKTOP")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("not yet", said[0])
        self.assertIn("rung 2", said[0])

    def test_every_routed_message_costs_upstream_exactly_one_noop(self):
        """A CONSUMEd request still costs one sequence number upstream, for
        ever [recon/wire.md 3.2a]."""
        self.send_message("_NET_CLOSE_WINDOW", self.shadow(), [0])
        self.assertEqual(self.rig.upstream.ops[:2],
                         [wire.OP_NO_OPERATION, wire.OP_GET_INPUT_FOCUS])


class MaximizePairFolded(EwmhCase):
    """`backend.state_steps` (wdotool/backend.py:100): two maximize axes in one
    message are ONE call on a backend that names the pair and two on one that
    does not."""

    num, upstream_num = 804, 805

    def both_axes(self, action=1):
        atoms = [self.atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                 self.atom("_NET_WM_STATE_MAXIMIZED_HORZ")]
        self.send_message("_NET_WM_STATE", self.shadow(), [action] + atoms)

    def test_a_backend_that_names_the_pair_gets_one_call(self):
        """GNOME answers "MAXIMIZED": Mutter unmaximizes to the window's
        current frame rect, so a second single-axis call that arrives before
        the client answered the first configure carries the still maximized
        half into its target."""
        self.backend.maximize_pair_state = lambda: "MAXIMIZED"
        self.both_axes()
        self.assertEqual(self.calls("set_state"),
                         [(self.handle, "MAXIMIZED", 1)])

    def test_a_backend_that_does_not_gets_one_call_per_axis(self):
        """sway and KWin settle each axis before they answer, so there is
        nothing to fold."""
        self.backend.maximize_pair_state = lambda: None
        self.both_axes()
        self.assertEqual(self.calls("set_state"),
                         [(self.handle, "MAXIMIZED_VERT", 1),
                          (self.handle, "MAXIMIZED_HORZ", 1)])

    def test_remove_carries_the_action_through_the_fold(self):
        self.backend.maximize_pair_state = lambda: "MAXIMIZED"
        self.both_axes(action=0)
        self.assertEqual(self.calls("set_state"),
                         [(self.handle, "MAXIMIZED", 0)])

    def test_an_atom_that_is_not_a_state_name_is_ignored_with_a_line(self):
        """A `_NET_WM_STATE` whose payload is not a `_NET_WM_STATE_*` atom
        names nothing the backend contract has."""
        self.send_message("_NET_WM_STATE", self.shadow(),
                          [1, self.atom("WM_NAME")])
        self.assertEqual(self.calls("set_state"), [])
        self.assertEqual(len(self.log.carrying("not a _NET_WM_STATE_* name")), 1,
                         self.log.lines)


class StickyDesktop(EwmhCase):
    """`_NET_WM_DESKTOP` (design section 3.4)."""

    num, upstream_num = 806, 807

    def test_a_desktop_number_moves_the_window(self):
        self.send_message("_NET_WM_DESKTOP", self.shadow(), [2])
        self.assertEqual(self.calls("set_window_desktop"), [(self.handle, 2)])

    def test_all_desktops_is_the_sticky_state_and_not_a_desktop(self):
        """`0xFFFFFFFF` means "on every desktop", which is a STATE on every
        backend in the tree and a desktop number on none of them."""
        self.send_message("_NET_WM_DESKTOP", self.shadow(), [0xFFFFFFFF])
        self.assertEqual(self.calls("set_state"), [(self.handle, "STICKY", 1)])
        self.assertEqual(self.calls("set_window_desktop"), [])


class DeleteWindowToShadow(EwmhCase):
    """The polite close: `WM_PROTOCOLS`/`WM_DELETE_WINDOW` sent to the window
    itself with mask 0, which is what design section 4.4's `WM_PROTOCOLS`
    property promises will work. Without it a closer's fallback is
    `XKillClient`, which is SIGKILL.

    The shape is the whole route: design section 3.4 routes `WM_PROTOCOLS`
    only for a message addressed TO the window it is about, with an empty
    mask. Every other `WM_PROTOCOLS` on a display is traffic between somebody
    else's two ends -- a `_NET_WM_PING` pong goes to the ROOT about the
    client's own window -- and passes."""

    num, upstream_num = 808, 809

    def test_a_delete_window_message_to_the_shadow_closes_it(self):
        """Destination the shadow, mask 0: the closer's shape, and the one
        this routes."""
        self.send_message("WM_PROTOCOLS", self.shadow(),
                          [self.atom("WM_DELETE_WINDOW")],
                          dest=self.shadow(), mask=0)
        self.assertEqual(self.calls("close"), [(self.handle,)])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_NO_OPERATION)

    def test_the_same_message_sent_to_the_root_passes(self):
        """A `WM_PROTOCOLS` whose destination is not the window it names is a
        pong, not a close: `_NET_WM_PING`'s reply goes to the root about the
        pinged window, and eating it would leave every X client through this
        proxy looking hung to a compositor that pings."""
        self.send_message("WM_PROTOCOLS", self.shadow(),
                          [self.atom("WM_DELETE_WINDOW")],
                          dest=None, mask=0)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)

    def test_the_same_message_with_an_event_mask_passes(self):
        """Mask 0 is the other half of the closer's shape: a `WM_PROTOCOLS`
        carrying `SubstructureNotify|SubstructureRedirect` is a manager's
        broadcast, not a request to this window."""
        self.send_message("WM_PROTOCOLS", self.shadow(),
                          [self.atom("WM_DELETE_WINDOW")],
                          dest=self.shadow(), mask=SUBSTRUCTURE)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)

    def test_another_protocol_is_consumed_with_a_line(self):
        """`WM_TAKE_FOCUS` and the rest are messages a window manager sends TO
        a client; only `WM_DELETE_WINDOW` is routed. In the closer's shape and
        about a shadow, there is no X client to deliver it to at all."""
        self.send_message("WM_PROTOCOLS", self.shadow(),
                          [self.atom("WM_TAKE_FOCUS")],
                          dest=self.shadow(), mask=0)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(self.log.carrying("WM_TAKE_FOCUS")), 1,
                         self.log.lines)


class TheDualPlaneRule(EwmhCase):
    """Design section 3.4's last row: which plane a message about a REAL X
    window goes to."""

    num, upstream_num = 810, 811

    #: What this rig's upstream names in its own `_NET_SUPPORTED`. Two of the
    #: nineteen wlroots' Xwayland publishes [recon/env.md 2.1] -- the point is
    #: that `_NET_WM_DESKTOP` is NOT among them, on the real one either.
    UPSTREAM_SUPPORTS = ("_NET_ACTIVE_WINDOW", "_NET_CLOSE_WINDOW")

    def make_backend(self):
        foot = foot_window(11)
        xterm = fake_window(12, title="WXL-Xterm", class_="XTerm",
                            instance="xterm", pid=99, x=0, y=0, w=800, h=600)
        return FakeBackend(
            windows=[foot, xterm],
            views=[fake_view(foot, xid=0, app_id="foot", instance="foot",
                             cls="foot"),
                   fake_view(xterm, xid=0x40000C, instance="xterm",
                             cls="XTerm")])

    def prepare_upstream(self, upstream):
        atoms = [upstream.intern(name) for name in self.UPSTREAM_SUPPORTS]
        upstream.set_prop(upstream.ROOTS[0], "_NET_SUPPORTED", "ATOM", 32,
                          struct.pack("<%dI" % len(atoms), *atoms))

    def setUp(self):
        super().setUp()
        self.xterm = self.shadows.by_xid[0x40000C]

    def test_a_type_upstream_supports_reaches_the_xwm_untouched(self):
        """The xwm handles it and the compositor would do it twice."""
        self.send_message("_NET_ACTIVE_WINDOW", 0x40000C, [2])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)
        self.assertEqual(self.backend.calls, [])

    def test_a_type_upstream_lacks_routes_through_the_paired_handle(self):
        """sway's xwm has no `_NET_WM_DESKTOP` [recon/env.md 2.1] and the
        compositor can still move its own toplevel: the handle is the pairing
        of design section 4.2, and the request is CONSUMEd."""
        self.send_message("_NET_WM_DESKTOP", 0x40000C, [3])
        self.assertEqual(self.calls("set_window_desktop"),
                         [(self.xterm.handle, 3)])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_NO_OPERATION)

    def test_a_message_about_a_shadow_never_consults_upstreams_list(self):
        """A shadow does not exist upstream: `_NET_ACTIVE_WINDOW` is in the
        list above and is still routed."""
        self.send_message("_NET_ACTIVE_WINDOW", self.shadow(), [2])
        self.assertEqual(self.calls("activate"), [(self.handle,)])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_NO_OPERATION)

    def test_a_delete_window_about_a_real_window_reaches_its_client(self):
        """`WM_PROTOCOLS` is ICCCM's and no `_NET_SUPPORTED` list on earth
        names it, so the list cannot be what decides this one: a real X window
        has a real X client at the other end, and the xwm delivers the message
        to it (wlroots' `xwm_handle_wm_protocols_message`). Routing it to
        `backend.close` instead would replace the client's own polite shutdown
        with the compositor's."""
        self.send_message("WM_PROTOCOLS", 0x40000C,
                          [self.atom("WM_DELETE_WINDOW")],
                          dest=0x40000C, mask=0)
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)
        self.assertEqual(self.backend.calls, [])

    def test_a_wm_ping_pong_about_a_real_window_passes_unremarked(self):
        """The pong a live X client sends every time the compositor pings it:
        `WM_PROTOCOLS [_NET_WM_PING, timestamp, window]` to the ROOT about its
        own window. Eating it would make the client look hung to Mutter, KWin
        and wlroots alike, all three of which ping."""
        ping = self.conn.atom("_NET_WM_PING")     # the client interns its own
        del self.rig.upstream.ops[:]
        self.send_message("WM_PROTOCOLS", 0x40000C, [ping, 0x12345, 0x40000C],
                          dest=None, mask=SUBSTRUCTURE)
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.log.carrying("_NET_WM_PING"), [])

    def test_wm_change_state_about_a_real_window_is_the_xwms(self):
        """The other ICCCM type: `xdotool windowminimize <the xterm>`. The xwm
        reads `WM_CHANGE_STATE` itself (`xwm_handle_wm_change_state` ->
        sway's `handle_request_minimize`), so through this proxy the command
        does what it does without one -- the X twin is untouched. What sway
        then makes of a minimize request is sway's own gap, closed by the
        compositor's verb over its own IPC (AGENTS.md rung 2)."""
        self.send_message("WM_CHANGE_STATE", 0x40000C, [ewmh.ICONIC_STATE])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)
        self.assertEqual(self.calls("minimize"), [])

    def test_the_same_change_state_about_a_native_toplevel_is_routed(self):
        """The negative twin of the row above: no xwm has ever heard of a
        native toplevel, so the ICCCM arm is about the REAL id and nothing
        else."""
        self.send_message("WM_CHANGE_STATE", self.shadow(),
                          [ewmh.ICONIC_STATE])
        self.assertEqual(self.calls("minimize"), [(self.handle,)])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_NO_OPERATION)


class TheRegistryIsReadThroughTheTtl(EwmhCase):
    """The router reads the registry through `snapshot()`, not straight out of
    `by_shadow` (design section 4.8, and `req_write._entry`'s own comment): a
    message that leaves an axis out takes the other from the window's CURRENT
    rect, and a toplevel that appeared since the last re-list would otherwise
    be "a window nobody knows" and pass to an upstream that never heard of
    it."""

    num, upstream_num = 816, 817

    def test_an_omitted_axis_comes_from_a_fresh_relist(self):
        """The foot starts at (10, 20) [design section 9.3] and has moved to
        640 since the last re-list. `_NET_MOVERESIZE_WINDOW` with only bit 9
        set carries y and nothing else; x is the compositor's own current
        number, not the one a snapshot up to 20 ms old remembers."""
        moved = foot_window(11, x=640, y=480)
        self.backend.windows[:] = [moved]
        self.backend.views_[:] = [fake_view(moved, xid=0, app_id="foot",
                                            instance="foot", cls="foot")]
        self.shadows.invalidate()
        self.send_message("_NET_MOVERESIZE_WINDOW", self.shadow(),
                          [ewmh.MR_Y, 0, 77])
        self.assertEqual(self.calls("move_window"), [(self.handle, 640, 77)])


class WhatPasses(EwmhCase):
    """Everything the table does not name."""

    num, upstream_num = 812, 813

    def test_an_unknown_client_message_type_passes(self):
        """A client's own message to its own window: not the proxy's business
        [design section 3.4's last row]."""
        self.send_message("WM_STATE", self.shadow(), [1, 0])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)

    def test_an_event_that_is_not_a_client_message_passes(self):
        """A synthetic `PropertyNotify`, which selection code sends."""
        ev = client_message(self.root, self.atom("WM_NAME"), [0],
                            code=wire.EV_PROPERTY_NOTIFY)
        self.send(wire.OP_SEND_EVENT, 0,
                  struct.pack("<II", self.root, SUBSTRUCTURE) + ev)
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)

    def test_a_client_message_of_format_8_passes(self):
        """Every EWMH message measured is format 32 [recon/tools.md 4.4]; a
        format-8 one carries 20 bytes and none of the fields this routes on."""
        ev = client_message(self.shadow(), self.atom("_NET_CLOSE_WINDOW"), [0],
                            fmt=8)
        self.send(wire.OP_SEND_EVENT, 0,
                  struct.pack("<II", self.root, SUBSTRUCTURE) + ev)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)

    def test_a_synthetic_event_aimed_at_a_shadow_says_not_yet(self):
        """`xdotool key --window <shadow>` would send one. It passes, upstream
        answers `BadWindow` for an id it never minted, and the line names what
        would close the gap."""
        ev = client_message(self.shadow(), self.atom("WM_NAME"), [0],
                            code=wire.EV_KEY_PRESS)
        self.send(wire.OP_SEND_EVENT, 0,
                  struct.pack("<II", self.shadow(), 1) + ev)
        said = self.log.carrying("SendEvent of event code 2")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("not yet", said[0])
        self.assertIn("rung 5", said[0])

    def test_a_routed_type_about_a_window_nobody_knows_passes(self):
        """A dead shadow, a pixmap, an override-redirect window the compositor
        never listed: upstream answers for it, `BadWindow` and all
        [recon/tools.md 9]."""
        self.send_message("_NET_CLOSE_WINDOW", 0x7654321, [0])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_SEND_EVENT)


class RefusalConsumed(EwmhCase):
    """Design section 3.4: a routed message is CONSUMEd even when the backend
    refuses. X answers a `ClientMessage` with nothing at all."""

    num, upstream_num = 814, 815

    def test_an_unsupported_set_num_desktops_still_yields_a_noop(self):
        """`wmctrl -n 4`: most backends have no verb for creating a workspace
        that holds no window, and `_unsupported` raises a `CmdError` with
        `.unsupported` (wdotool/backend.py:280)."""
        err = CmdError("set_num_desktops is not supported by this compositor")
        err.unsupported = True
        self.backend.raise_on("set_num_desktops", err)
        self.send_message("_NET_NUMBER_OF_DESKTOPS", self.root, [4])
        self.assertEqual(self.rig.upstream.ops[:2],
                         [wire.OP_NO_OPERATION, wire.OP_GET_INPUT_FOCUS])
        said = self.log.carrying("cannot set_num_desktops")
        self.assertEqual(len(said), 1, self.log.lines)
        self.assertIn("not yet", said[0])

    def test_a_refused_activate_is_silence_and_one_line(self):
        self.backend.raise_on("activate", CmdError("no such window any more"))
        self.send_message("_NET_ACTIVE_WINDOW", self.shadow(), [2])
        self.assertEqual(len(self.log.carrying("no such window any more")), 1,
                         self.log.lines)
        self.assertEqual(self.rig.upstream.ops[0], wire.OP_NO_OPERATION)

    def test_a_state_the_compositor_accepted_and_ignored_is_logged(self):
        """`set_state` answers a one-line REASON when the compositor accepted
        the request and did not apply it -- KWin does that for a window rule
        (wdotool/backend.py:350). The clone prints it and succeeds; so does
        this, in the log."""
        self.backend.set_state = lambda wid, state, action: "a window rule says no"
        self.send_message("_NET_WM_STATE", self.shadow(),
                          [1, self.atom("_NET_WM_STATE_FULLSCREEN")])
        self.assertEqual(len(self.log.carrying("a window rule says no")), 1,
                         self.log.lines)


class TheTableItself(unittest.TestCase):
    """The two tables that have to agree with each other."""

    def test_every_routed_type_has_a_handler(self):
        """A name in `policy.ROUTED_TYPES` with no handler would be the proxy
        telling `wmctrl` through `_NET_SUPPORTED` that it does something and
        then swallowing the request."""
        self.assertEqual(sorted(ewmh.WINDOW_ROUTES),
                         sorted(policy.ROUTED_WINDOW_TYPES))
        self.assertEqual(sorted(ewmh.ROOT_ROUTES_BY_NAME),
                         sorted(policy.ROUTED_ROOT_TYPES))

    def test_every_routed_type_is_interned_at_open(self):
        """The router resolves a type by NAME through `OwnConn.atom_name`, and
        a name the proxy never interned costs a `GetAtomName` round trip on the
        loop thread for every message that carries it."""
        for name in policy.ROUTED_TYPES:
            self.assertIn(name, policy.ATOMS, name)

    def test_the_routed_types_the_ewmh_owns_are_advertised(self):
        """`_NET_SUPPORTED` is what `wmctrl -d` and `xdotool set_desktop` gate
        on [recon/tools.md 4.10]. `WM_PROTOCOLS` is deliberately not in it: it
        is ICCCM's, and the shadow's own `WM_PROTOCOLS` property is what
        advertises it (design section 4.4)."""
        for name in policy.ROUTED_TYPES:
            if name == "WM_PROTOCOLS":
                self.assertNotIn(name, policy.SUPPORTED)
                continue
            if name == "_NET_SHOWING_DESKTOP":
                # not yet: forwarded and logged, so it is not claimed either
                self.assertNotIn(name, policy.SUPPORTED)
                continue
            self.assertIn(name, policy.SUPPORTED, name)


if __name__ == "__main__":
    unittest.main()
