"""COSMIC's geometry, stacking and state-tail route for XWayland windows: AGENTS.md route 5.

The COSMIC toplevel protocols carry no rectangle, no stacking and a five-member state array, so
`windowmove`, `windowsize`, `windowraise`, `windowlower` and the `_NET_WM_STATE` tail (SHADED/ABOVE/BELOW/
SKIP_*) refused. cosmic-comp runs an Xwayland, so for an XWayland window the same `ConfigureWindow` and
`_NET_WM_STATE` ClientMessage the original `xdotool`/`wmctrl` send reach it -- and whether they land is then
cosmic-comp's Smithay xwm's business, byte for byte what those originals get. Measured 2026-09-14 on
arch-cosmic (cosmic-comp 1:1.8.0-1) on an xterm through the real Xwayland, minted id 1598061160 / X id
0x40000c: `windowsize 500 300` LANDS (500x300, `windowsize 720 480` -> 720x480), `windowmove` is a no-op
(Position stayed 398,154), and `wmctrl -b add,{above,below,shaded,skip_taskbar,skip_pager}` each leave
`_NET_WM_STATE` at `_NET_WM_STATE_FOCUSED` -- the same no-ops the real `xdotool windowmove`/`wmctrl -b` get
on that session, because the Smithay xwm drops the move and the state toggles and honours only the resize.
A NATIVE toplevel has no X id and keeps the refusal.

This file proves the CODE takes route 5 (the request goes out with the right fields); which of those
requests the compositor honours is the live measurement above, not the fake's to decide. The X rig is the
one `test_backend_wlr` and `test_backend_cosmic` already share (`CosmicXPlane`, X client 0x600012 joined to
the `cosmicxterm` toplevel).
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["W11_PASSTHROUGH"] = "never"

from w11common.errors import CmdError                        # noqa: E402
from hacks.window import x11_mini                            # noqa: E402
from test_backend_cosmic import CAPS, CAP_STICKY, CosmicTest, CosmicXPlane   # noqa: E402


def _configures(srv):
    return [e for e in srv.log if e[0] == "ConfigureWindow"]


def _sends(srv):
    return [e for e in srv.log if e[0] == "SendEvent"]


def _states(srv, xid):
    entry = srv.props.get((xid, "_NET_WM_STATE"))
    if entry is None:
        return set()
    atoms = struct.unpack("<%dI" % (len(entry[2]) // 4), entry[2])
    return {srv._names.get(a, a) for a in atoms}


class XPlaneGeometry(CosmicXPlane, CosmicTest):
    XTERM = 0x600012

    def test_windowmove_and_windowsize_on_an_xwayland_window_go_out_as_configurewindow(self):
        srv = self.x_server()
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicxterm")
        self.assertIsNone(b.move_window(wid, 300, 200))
        self.assertIsNone(b.resize(wid, 640, 400))
        cfgs = _configures(srv)
        self.assertEqual([(c[1], c[2]) for c in cfgs],
                         [(self.XTERM, {"x": 300, "y": 200}),
                          (self.XTERM, {"width": 640, "height": 400})])

    def test_windowraise_and_lower_send_the_stack_mode(self):
        srv = self.x_server()
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicxterm")
        self.assertIsNone(b.raise_(wid))
        self.assertIsNone(b.lower(wid))
        self.assertEqual([c[2] for c in _configures(srv)],
                         [{"stack_mode": x11_mini.STACK_ABOVE},
                          {"stack_mode": x11_mini.STACK_BELOW}])

    def test_the_move_the_xwm_drops_is_still_rc0(self):
        """cosmic-comp's Smithay xwm drops the move (measured); so does the original xdotool there. The
        request goes out and the command succeeds -- it is not a refusal."""
        srv = self.x_server()
        srv.apply_configure = False
        _comp, b = self.backend()
        self.assertIsNone(b.move_window(self.wid(b, "cosmicxterm"), 10, 20))
        self.assertEqual(len(_configures(srv)), 1)

    def test_geometry_is_client_rect_is_true_for_xwayland_and_false_for_native(self):
        """The hook `move_resize` (-e) reads to zero the frame extents on this floor: an XWayland window's
        resize goes out as a ConfigureWindow on the client rectangle, so cosmic-comp's server-side title bar
        must not be folded into the size (the review's must_fix: 640x360, not 640x396). A native toplevel has
        no X id."""
        self.x_server()
        _comp, b = self.backend()
        self.assertTrue(b.geometry_is_client_rect(self.wid(b, "cosmicxterm")))
        self.assertFalse(b.geometry_is_client_rect(self.wid(b, "cosmicterm")))

    def test_a_native_toplevel_keeps_the_refusal_and_reaches_no_x_plane(self):
        srv = self.x_server()
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicterm")   # a foot, not joined to any X client
        for call, args, op in ((b.move_window, (wid, 1, 2), "windowmove"),
                               (b.resize, (wid, 3, 4), "windowsize"),
                               (b.raise_, (wid,), "windowraise"),
                               (b.lower, (wid,), "windowlower")):
            with self.assertRaises(CmdError) as cm:
                call(*args)
            self.assertTrue(getattr(cm.exception, "unsupported", False), op)
            self.assertTrue(str(cm.exception).startswith(
                op + " is not supported by the cosmic backend: the COSMIC toplevel protocol has "
                "no move, resize, raise or lower"), str(cm.exception))
        self.assertEqual(_configures(srv), [])


class XPlaneState(CosmicXPlane, CosmicTest):
    XTERM = 0x600012
    TAIL = ("ABOVE", "BELOW", "SHADED", "SKIP_TASKBAR", "SKIP_PAGER")

    def test_the_tail_goes_out_as_the_clientmessage_wmctrl_sends(self):
        srv = self.x_server()   # apply_net_wm_state True: an EWMH xwm that reads it back
        _comp, b = self.backend()
        wid = self.wid(b, "cosmicxterm")
        for s in self.TAIL:
            self.assertIsNone(b.set_state(wid, s, 1))
        self.assertEqual(len(_sends(srv)), len(self.TAIL))
        self.assertEqual(_states(srv, self.XTERM),
                         {"_NET_WM_STATE_%s" % s for s in self.TAIL})

    def test_the_state_the_smithay_xwm_drops_is_still_rc0_and_silent(self):
        """The measured cosmic reality: the ClientMessage goes out, cosmic-comp drops it, `_NET_WM_STATE`
        stays put -- exactly what `wmctrl -b add,above` gets there, rc 0 and no stderr."""
        srv = self.x_server()
        srv.apply_net_wm_state = False
        _comp, b = self.backend()
        self.assertIsNone(b.set_state(self.wid(b, "cosmicxterm"), "ABOVE", 1))
        self.assertEqual(len(_sends(srv)), 1)
        self.assertEqual(_states(srv, self.XTERM), set())

    def test_a_native_toplevel_keeps_no_such_state(self):
        srv = self.x_server()
        _comp, b = self.backend()
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "SHADED", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertTrue(str(cm.exception).startswith(
            "windowstate SHADED is not supported by the cosmic backend: the COSMIC toplevel "
            "protocol carries maximized, minimized, activated, fullscreen and sticky and no other state"),
            str(cm.exception))
        self.assertEqual(_sends(srv), [])

    def test_unsupported_states_is_the_tail_with_sticky_left_out(self):
        _comp, b = self.backend()
        us = b.unsupported_states()
        for tail in ("MODAL", "SHADED", "SKIP_TASKBAR", "SKIP_PAGER",
                     "ABOVE", "BELOW", "DEMANDS_ATTENTION", "FOCUSED"):
            self.assertIn(tail, us, tail)
        # STICKY is COSMIC's fifth array member, so it is NOT reached through the X plane
        for native in ("FULLSCREEN", "MAXIMIZED_VERT", "MAXIMIZED_HORZ", "HIDDEN", "STICKY"):
            self.assertNotIn(native, us, native)

    def test_state_route_is_x_plane_only_for_an_xwayland_tail_state(self):
        """wwmctl reads this to send a tail state once, not twice, where the backend's route IS the same
        ClientMessage. True for an XWayland tail state, False for a native toplevel and for a state the
        handle's five-member array carries -- STICKY included, which is why it is not in `_TAIL_STATES`."""
        self.x_server()
        _comp, b = self.backend()
        xt = self.wid(b, "cosmicxterm")
        self.assertTrue(b.state_route_is_x_plane(xt, "ABOVE"))
        self.assertFalse(b.state_route_is_x_plane(self.wid(b, "cosmicterm"), "ABOVE"))  # native
        self.assertFalse(b.state_route_is_x_plane(xt, "STICKY"))   # the array carries it


class XPlaneStickyVersionGap(CosmicXPlane, CosmicTest):
    """STICKY on an XWayland window when the manager is older than v3 (no `set_sticky`).

    STICKY is COSMIC's fifth array member, so it is NOT in `_TAIL_STATES` and wwmctl does not pre-empt it --
    but real `wmctrl -b add,sticky` sends the `_NET_WM_STATE` ClientMessage regardless of the Wayland
    version. So an XWayland window takes the X plane here too (route 5), which also reaches a
    `wdotool windowstate` caller that has no X fallback of its own; a native toplevel keeps the version-gap
    refusal. The v2 trick is `test_backend_cosmic`'s own (`b.mgr_ver = 2` after the bind negotiated higher).
    """

    XTERM = 0x600012

    def test_sticky_below_manager_v3_takes_the_x_plane_for_an_xwayland_window(self):
        srv = self.x_server()
        _comp, b = self.backend(capabilities=CAPS + (CAP_STICKY,))
        b.mgr_ver = 2
        self.assertIsNone(b.set_state(self.wid(b, "cosmicxterm"), "STICKY", 1))
        self.assertEqual(len(_sends(srv)), 1)
        self.assertEqual(_states(srv, self.XTERM), {"_NET_WM_STATE_STICKY"})

    def test_sticky_below_manager_v3_still_refuses_a_native_toplevel(self):
        srv = self.x_server()
        _comp, b = self.backend(capabilities=CAPS + (CAP_STICKY,))
        b.mgr_ver = 2
        with self.assertRaises(CmdError) as cm:
            b.set_state(self.wid(b, "cosmicterm"), "STICKY", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("zcosmic_toplevel_manager_v1 is version 2 and set_sticky arrived in version 3",
                      str(cm.exception))
        self.assertEqual(_sends(srv), [])


if __name__ == "__main__":
    unittest.main()
