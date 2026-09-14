"""The wlr floor's geometry, stacking and state-tail route for XWayland windows: AGENTS.md route 5.

`zwlr_foreign_toplevel_management_v1` carries no rectangle, no stacking and only four state bits, so
`windowmove`, `windowsize`, `windowraise`, `windowlower` and the `_NET_WM_STATE` tail (SHADED/ABOVE/BELOW/
SKIP_*) refused on this floor. But every wlroots compositor runs an X window manager, and for an XWayland
window the original `xdotool`/`wmctrl` reach it with a real `ConfigureWindow` and a real `_NET_WM_STATE`
ClientMessage -- so we send the same, and whether it lands is then the xwm's business, byte for byte what
those originals get. Measured 2026-09-14 on the resolute-labwc golden (labwc 0.9.3) on an xterm through the
real Xwayland: `windowmove 300 200` lands, `windowsize 640 400` lands (640x394, cell quantisation),
`wmctrl -b add,{above,below,shaded,skip_taskbar,skip_pager}` each land in `_NET_WM_STATE` and read back on,
and `windowraise` does not restack (the wlroots xwm drops the stack mode). A NATIVE toplevel has no X id and
keeps the refusal (labwc.sh:137-140 and river.sh pin the substring).

The peer is the real fake X server (`FakeXServer`), whose ConfigureWindow and `_NET_WM_STATE` ClientMessage
handling this file exercises; `apply_configure`/`apply_net_wm_state` model an xwm that honours the request
(labwc) versus one that drops it (cosmic-comp), so both sides of the parity are here.
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["W11_PASSTHROUGH"] = "never"

from w11common.errors import CmdError                       # noqa: E402
from hacks.window import x11_mini                           # noqa: E402
from hacks.window.backend_wlr import BASE_ID                # noqa: E402
from test_backend_wlr import FakeXPlane, WlrTest, top       # noqa: E402


def _configures(srv):
    return [e for e in srv.log if e[0] == "ConfigureWindow"]


def _sends(srv):
    return [e for e in srv.log if e[0] == "SendEvent"]


def _states(srv, xid):
    """The `_NET_WM_STATE` atom names the fake server holds for `xid`."""
    entry = srv.props.get((xid, "_NET_WM_STATE"))
    if entry is None:
        return set()
    atoms = struct.unpack("<%dI" % (len(entry[2]) // 4), entry[2])
    return {srv._names.get(a, a) for a in atoms}


class XPlaneGeometry(FakeXPlane, WlrTest):
    """`windowmove`/`windowsize`/`windowraise`/`windowlower` on an XWayland window go out as ConfigureWindow;
    a native one keeps the refusal."""

    XTERM = 0x40000C
    # the xterm toplevel joins to the X client (title "xtermwin" / app_id "xterm"); the foot one does not.
    TOPLEVELS = (top("xtermwin", "xterm"), top("footwin", "foot"))

    def test_windowmove_on_an_xwayland_window_sends_x_and_y_only(self):
        srv = self.x_server()
        _comp, b = self.backend()
        self.assertIsNone(b.move_window(BASE_ID, 300, 200))
        cfgs = _configures(srv)
        self.assertEqual(len(cfgs), 1, cfgs)
        _op, win, fields = cfgs[0]
        self.assertEqual(win, self.XTERM)
        self.assertEqual(fields, {"x": 300, "y": 200})
        # and labwc's xwm honours it, so the listing reads the new origin back (route-5 end to end)
        row = next(w for w in b.list() if w.id == BASE_ID)
        self.assertEqual((row.x, row.y), (300, 200))

    def test_windowsize_sends_width_and_height_only(self):
        srv = self.x_server()
        _comp, b = self.backend()
        self.assertIsNone(b.resize(BASE_ID, 640, 400))
        _op, win, fields = _configures(srv)[0]
        self.assertEqual(win, self.XTERM)
        self.assertEqual(fields, {"width": 640, "height": 400})
        row = next(w for w in b.list() if w.id == BASE_ID)
        self.assertEqual((row.w, row.h), (640, 400))

    def test_windowraise_and_lower_send_the_stack_mode_the_xwm_may_drop(self):
        srv = self.x_server()
        _comp, b = self.backend()
        self.assertIsNone(b.raise_(BASE_ID))
        self.assertIsNone(b.lower(BASE_ID))
        self.assertEqual([c[2] for c in _configures(srv)],
                         [{"stack_mode": x11_mini.STACK_ABOVE},
                          {"stack_mode": x11_mini.STACK_BELOW}])

    def test_geometry_is_client_rect_is_true_for_xwayland_and_false_for_native(self):
        """The hook `move_resize` (-e) reads to zero the frame extents: an XWayland window's move/resize go
        out as a ConfigureWindow on the client rectangle, so the server-side title bar must not be folded
        into the size. A native toplevel has no X id and never reaches the X plane."""
        self.x_server()
        _comp, b = self.backend()
        footwid = next(w.id for w in b.list() if w.class_ == "foot")
        self.assertTrue(b.geometry_is_client_rect(BASE_ID))
        self.assertFalse(b.geometry_is_client_rect(footwid))

    def test_a_native_toplevel_keeps_the_pinned_refusal_and_reaches_no_x_plane(self):
        srv = self.x_server()
        _comp, b = self.backend()
        footwid = next(w.id for w in b.list() if w.class_ == "foot")
        for call, args, op in ((b.move_window, (footwid, 1, 2), "windowmove"),
                               (b.resize, (footwid, 3, 4), "windowsize"),
                               (b.raise_, (footwid,), "windowraise"),
                               (b.lower, (footwid,), "windowlower")):
            with self.assertRaises(CmdError) as cm:
                call(*args)
            self.assertTrue(getattr(cm.exception, "unsupported", False), op)
            # labwc.sh:137-140 and river.sh grep this exact substring on a native window
            self.assertTrue(str(cm.exception).startswith(
                op + " is not supported by the wlr backend: "
                "zwlr_foreign_toplevel_management_v1 carries no geometry"), str(cm.exception))
        self.assertEqual(_configures(srv), [], "a native window must send nothing to the X server")


class XPlaneState(FakeXPlane, WlrTest):
    """The `_NET_WM_STATE` tail (SHADED/ABOVE/BELOW/SKIP_*) on an XWayland window is the ClientMessage
    `wmctrl -b` sends; a native one keeps NO_SUCH_STATE."""

    XTERM = 0x40000C
    TOPLEVELS = (top("xtermwin", "xterm"), top("footwin", "foot"))
    TAIL = ("ABOVE", "BELOW", "SHADED", "SKIP_TASKBAR", "SKIP_PAGER")

    def test_the_tail_lands_and_reads_back_where_the_xwm_honours_it(self):
        srv = self.x_server()   # apply_net_wm_state True -> labwc's full EWMH xwm
        _comp, b = self.backend()
        for s in self.TAIL:
            self.assertIsNone(b.set_state(BASE_ID, s, 1))
        self.assertEqual(_states(srv, self.XTERM),
                         {"_NET_WM_STATE_%s" % s for s in self.TAIL})
        # a remove takes one back out, as `wmctrl -b remove,above` does
        self.assertIsNone(b.set_state(BASE_ID, "ABOVE", 0))
        self.assertNotIn("_NET_WM_STATE_ABOVE", _states(srv, self.XTERM))

    def test_an_xwm_that_drops_the_message_is_still_rc0_and_prints_nothing(self):
        """cosmic-comp's Smithay xwm drops the tail (measured 2026-09-14) -- and so does the original
        `wmctrl` there. Fire-and-forget: the message goes out, set_state returns None, nothing lands."""
        srv = self.x_server()
        srv.apply_net_wm_state = False
        _comp, b = self.backend()
        self.assertIsNone(b.set_state(BASE_ID, "ABOVE", 1))
        self.assertEqual(len(_sends(srv)), 1, "the ClientMessage still went on the wire")
        self.assertEqual(_states(srv, self.XTERM), set())

    def test_a_native_toplevel_keeps_no_such_state(self):
        srv = self.x_server()
        _comp, b = self.backend()
        footwid = next(w.id for w in b.list() if w.class_ == "foot")
        with self.assertRaises(CmdError) as cm:
            b.set_state(footwid, "ABOVE", 1)
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertTrue(str(cm.exception).startswith(
            "windowstate ABOVE is not supported by the wlr backend: "
            "zwlr_foreign_toplevel_management_v1 carries maximized, minimized, activated and "
            "fullscreen and no other state"), str(cm.exception))
        self.assertEqual(_sends(srv), [], "a native window sends no ClientMessage")

    def test_unsupported_states_is_the_tail_and_not_the_four_bits(self):
        _comp, b = self.backend()
        us = b.unsupported_states()
        for tail in ("MODAL", "STICKY", "SHADED", "SKIP_TASKBAR", "SKIP_PAGER",
                     "ABOVE", "BELOW", "DEMANDS_ATTENTION", "FOCUSED"):
            self.assertIn(tail, us, tail)
        for native in ("FULLSCREEN", "MAXIMIZED_VERT", "MAXIMIZED_HORZ", "HIDDEN"):
            self.assertNotIn(native, us, native)

    def test_state_route_is_x_plane_only_for_an_xwayland_tail_state(self):
        """wwmctl reads this to send a tail state once: the backend's route for an XWayland tail state IS the
        same ClientMessage wwmctl's fallback already sends. False for a native toplevel (no X id, refusal) and
        for a state the handle's array carries (the Wayland setter), so wwmctl still reaches those the usual
        way."""
        self.x_server()
        _comp, b = self.backend()
        footwid = next(w.id for w in b.list() if w.class_ == "foot")
        self.assertTrue(b.state_route_is_x_plane(BASE_ID, "ABOVE"))
        self.assertFalse(b.state_route_is_x_plane(footwid, "ABOVE"))     # native: no X id
        self.assertFalse(b.state_route_is_x_plane(BASE_ID, "FULLSCREEN"))  # the handle carries it


if __name__ == "__main__":
    unittest.main()
