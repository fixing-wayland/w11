"""zwlr_virtual_pointer_v1: move, click and scroll with no kernel device.

WHY THIS EXISTS. `vkbd.py` took the keyboard half of wdotool off /dev/uinput
on wlroots, and could take nothing else: zwp_virtual_keyboard_v1 has four
requests -- keymap, key, modifiers, destroy -- and not one of them is a
pointer. wlroots ships the other half as a separate, equally unprivileged
protocol, and this is it. With both, `click`, `mousemove`,
`mousemove_relative`, `mousedown` and `mouseup` need no root, no group
membership and no device rule on sway and the wlroots family, exactly as
typing already does not.

WHO HAS IT. sway 1.11 advertises `zwlr_virtual_pointer_manager_v1` at v2 to
every client and restricts it to none: an ordinary session user creates a
pointer and drives it, and root reaching the same socket gets the identical
registry. Mutter and KWin implement neither this nor the keyboard protocol,
so GNOME and KDE keep the kernel devices and every word of this file is
inert there.

FOUR THINGS THE WIRE DOES DIFFERENTLY FROM uinput, all measured on sway
1.11 / wlroots 0.19.2 against a three-head layout (one head at a negative
origin, one at scale 1.5):

* **Absolute motion is a ratio over the whole output layout.**
  `motion_absolute(time, x, y, x_extent, y_extent)` places the cursor at
  `x/x_extent` of the layout bounding box, in *logical* coordinates -- not
  pixels, and not one output. Extents of 2, 100, 65535 and 1000000 all land
  the same point. So the forward map is the identity: `x = target - bbox_x`
  with `x_extent = bbox_w`, which landed 14 of 14 targets with 0.000 error.
  There is no axis quantisation here at all, which is why the kernel path's
  ceiling map (B7) and its unchanged-EV_ABS nudge (B2) have no counterpart
  below -- and no way to reintroduce the off-by-one they exist to fix.
* **Relative motion is exact.** A virtual pointer is not a libinput device
  (sway lists it with an empty libinput configuration), so `pointer_accel`
  and `accel_profile` cannot apply to it on any wlroots compositor. 1, 10,
  100, 500 and 1000 each moved exactly that many logical pixels, and 500
  separate one-pixel motions moved exactly 500. On the same seat a
  /dev/uinput mouse asked for 500 units of REL_X moved the cursor 858.33 --
  which is B1, and it cannot come back on this path.
* **Buttons are refcounted per seat.** press, press delivers ONE press;
  release, release delivers the up on the *second* release. The kernel
  drops a duplicate instead, so the daemon sends neither: it presses only
  what it is not already holding and releases only what it is, which makes
  the two paths behave identically and keeps `held` honest.
* **Scroll signs are Wayland's, not evdev's.** Positive vertical is scroll
  *down*, so wdotool's wheel buttons map 4 -> axis 0 negative, 5 -> axis 0
  positive, 6 -> axis 1 negative, 7 -> axis 1 positive (WHEEL below). Axis
  events are also *buffered until `frame()`* -- motion is applied without
  one but the receiving client then never sees `wl_pointer.frame` -- so
  every request here ends in a frame.

WHAT IS NOT DONE HERE YET: saying where the pointer is.
`zwlr_virtual_pointer_v1` has no events at all; zero arrive on the object
across motion, buttons and axes, and sway's IPC carries no cursor position
either. So the daemon's `getmouselocation` reports the position it put the
pointer at -- which on this path is exact, because the absolute map above
has no error -- and refuses to answer at all when it has not moved it. See
`daemon.POINTER_UNKNOWN`.

`xdotool getmouselocation` always answers, so this is a gap of ours and
gets a route. Two reach it. A wlr-layer-shell overlay is a protocol every
compositor in this family already speaks (AGENTS.md route 1) and its
`wl_pointer.motion` is the cursor, at the cost of a surface that eats the
events it reads -- it has to be unmapped again before the user's next
click. evdev is the other (route 4): integrate REL_X/REL_Y off
/dev/input, at the cost of read access to the devices and an anchor to
count from, since the kernel says how far the pointer moved and never
where it is.

LIFETIME is the keyboard's exactly: a held button does not survive the
client disconnecting (the release is delivered the instant the holder
exits), `destroy` releases too, and nothing survives a compositor restart --
which the client learns as a BrokenPipeError on its next write, an OSError,
which is the shape `_send` catches. Hence: ONE connection and ONE pointer
object for the life of anything held down.
"""

from w11common.wayland_mini import now_ms as _now_ms, roundtrip

MANAGER = "zwlr_virtual_pointer_manager_v1"
# sway advertises v2; we bind v1 on purpose. The only thing v2 adds is `create_virtual_pointer_with_output`, and
# that constructor maps the ratio into ONE output's logical box and CONFINES the cursor to it -- measured: a
# relative motion of +4000 from the leftmost head stopped at that head's own right edge. Every wdotool
# coordinate is a layout coordinate, so the plain constructor is the one we want, and binding v1 says so on the
# wire.
MAX_VERSION = 1

# wire opcodes -- manager
_MGR_CREATE_VIRTUAL_POINTER = 0
_MGR_DESTROY = 1
# _MGR_CREATE_VIRTUAL_POINTER_WITH_OUTPUT = 2  # v2; deliberately unused

# wire opcodes -- zwlr_virtual_pointer_v1
_VP_MOTION = 0
_VP_MOTION_ABSOLUTE = 1
_VP_BUTTON = 2
_VP_AXIS = 3
_VP_FRAME = 4
_VP_AXIS_SOURCE = 5
_VP_AXIS_STOP = 6
_VP_AXIS_DISCRETE = 7
_VP_DESTROY = 8

# wl_pointer.button_state
_RELEASED, _PRESSED = 0, 1

# wl_pointer.axis
AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1
# wl_pointer.axis_source
AXIS_SOURCE_WHEEL = 0

# One wheel detent, as a real wheel reports it: value 15.0 with a discrete step of 1, which reaches the client
# as `axis_value120` 120 at wl_pointer >= 8 and as `axis_discrete` 1 at v5-7.
WHEEL_STEP = 15.0

# wdotool's wheel "buttons" -> (axis, direction), in WAYLAND's sign: positive vertical is scroll down and
# positive horizontal is scroll right, which is the opposite of REL_WHEEL and the same as REL_HWHEEL. The kernel
# path's table is daemon._WHEEL; these two are the same four gestures.
WHEEL = {4: (AXIS_VERTICAL, -1),    # wheel up
         5: (AXIS_VERTICAL, 1),     # wheel down
         6: (AXIS_HORIZONTAL, -1),  # wheel left
         7: (AXIS_HORIZONTAL, 1)}   # wheel right


class VptrError(Exception):
    """The virtual-pointer path is not usable. Always caught by the daemon, which says why and falls back to the
    kernel devices (or, when there is no kernel device either, reports the kernel device's own reason)."""


class VirtualPointer:
    """One connection + one zwlr_virtual_pointer_v1.

    The four injecting calls -- `warp`, `move`, `button`, `wheel` -- are deliberately the ones
    `daemon._KernelPointer` answers to as well, so the daemon's pointer ops inject through either without
    knowing which. Everything this path does differently happens inside them.
    """

    def __init__(self, conn, vp_id: int, version: int):
        self.conn = conn
        self.vp = vp_id
        self.version = version
        self.closed = False
        # Evdev button codes we have told the compositor are down. The daemon tracks the same thing for its own
        # reasons (it must know which of the two sinks is holding them); this one exists so a dead connection
        # stops claiming to hold anything.
        self.held: set[int] = set()

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, socket_path: str | None = None,
             timeout: float = 2.0) -> "VirtualPointer":
        """Connect and create the pointer. Raises VptrError for every
        failure, including "this compositor does not have it"."""
        from w11common import session
        from w11common.wayland_mini import WlConn

        if socket_path is None:
            hit = session.find_wayland_socket()
            if hit is None:
                raise VptrError("no wayland socket found")
            socket_path = hit[2]
        try:
            conn = WlConn(socket_path)
        except OSError as e:
            raise VptrError(f"cannot connect to the compositor: {e}") from None
        # A wedged compositor must not hang the daemon while it holds the
        # injection lock.
        conn.sock.settimeout(timeout)
        try:
            found = conn.find_global(MANAGER)
            if found is None:
                # COSMIC joins Mutter and KWin here and nowhere else: cosmic-comp's 53 globals carry
                # zwp_virtual_keyboard_manager_v1 and no zwlr_virtual_pointer_manager_v1, so on COSMIC
                # typing needs no privilege and clicking needs /dev/uinput [M recon2/cosmic.md §3].
                raise VptrError(
                    f"this compositor does not implement {MANAGER} "
                    "(Mutter, KWin and COSMIC do not; sway, Hyprland and the wlroots family do). "
                    "COSMIC's keyboard half works: --vkbd on type types there with no privilege")
            version = min(found[1], MAX_VERSION)
            # The seat argument is allow-null in this protocol and a NULL was accepted and moved the cursor when
            # it was tried, but a seat that exists is the more precise statement -- and the seat's pointer
            # *capability* is deliberately not required, since a seat with no pointer is exactly where a virtual
            # one is worth creating.
            seat = conn.find_global("wl_seat")
            seat_id = 0
            if seat is not None:
                seat_id = conn.bind(seat[0], "wl_seat", min(seat[1], 7))
            mgr = conn.bind(found[0], MANAGER, version)
            # New object ids must be SENT in allocation order: reserving this one before the binds above have
            # gone out makes libwayland reject the bind ("invalid arguments for wl_registry#2.bind") and drop
            # the connection, which surfaces later as an unrelated BrokenPipeError. Bind first, allocate last.
            vp = conn.alloc()
            conn.send(mgr, _MGR_CREATE_VIRTUAL_POINTER,
                      [("u", seat_id), ("u", vp)])
            _roundtrip(conn, "create_virtual_pointer")
            return cls(conn, vp, version)
        except VptrError:
            conn.close()
            raise
        except (OSError, RuntimeError, ValueError) as e:
            conn.close()
            raise VptrError(f"virtual pointer setup failed: {e}") from None

    # -- the injection sink (daemon._KernelPointer's interface) ------------

    def warp(self, x: int, y: int, gx: int, gy: int, w: int, h: int):
        """Put the cursor at the layout coordinate (x, y).

        `motion_absolute` takes a ratio, so the extents ARE the layout size and the value is the offset from its
        origin -- exact, with no quantisation and no rounding either way. The caller has already clamped (x, y)
        into the layout; the max()es below are the wire's own requirement, since the arguments are unsigned and
        a negative would wrap into a huge value that clamps to the far edge.

        An `x_extent` of 0 is a silent no-op -- no motion event at all -- so a degenerate layout is floored at
        one pixel rather than dropped.
        """
        self._send(_VP_MOTION_ABSOLUTE,
                   [("u", _now_ms()),
                    ("u", max(int(x) - int(gx), 0)),
                    ("u", max(int(y) - int(gy), 0)),
                    ("u", max(int(w), 1)), ("u", max(int(h), 1))])
        self._frame()

    def move(self, dx: int, dy: int):
        """Relative motion, in logical pixels, delivered as wl_fixed. No acceleration curve can touch this (see
        the module docstring), so what is asked for is what the cursor moves; the compositor crosses outputs and
        clamps to the layout by itself."""
        self._send(_VP_MOTION, [("u", _now_ms()), ("f", dx), ("f", dy)])
        self._frame()

    def button(self, code: int, down: bool):
        """Press or release one evdev button code (BTN_LEFT 0x110 .. BTN_TASK 0x117 -- the numbers reach the
        client unchanged, and they are the ones daemon._BTN already uses)."""
        if down:
            self.held.add(code)
        else:
            self.held.discard(code)
        self._send(_VP_BUTTON, [("u", _now_ms()), ("u", code),
                                ("u", _PRESSED if down else _RELEASED)])
        self._frame()

    def wheel(self, btn: int):
        """One detent of wdotool's wheel button 4/5/6/7.

        `axis_discrete` rather than `axis`: a wheel click is a notch, and this is the request that tells the
        client so (value120 = 120 at wl_pointer >= 8). `axis_source` says which kind of scroll it is -- wheel is
        the default, but a frame that states it is one the client does not have to guess about.
        """
        axis, direction = WHEEL[btn]
        self._send(_VP_AXIS_SOURCE, [("u", AXIS_SOURCE_WHEEL)])
        self._send(_VP_AXIS_DISCRETE,
                   [("u", _now_ms()), ("u", axis),
                    ("f", WHEEL_STEP * direction), ("i", direction)])
        self._frame()

    def flush(self):
        """Round-trip once, so a protocol error the compositor raised over what we just sent is reported instead
        of noticed by nobody. The individual requests are not acknowledged one by one: that would put a round
        trip in the middle of every click of a `click --repeat`."""
        _roundtrip(self.conn, "pointer")

    def close(self):
        """Destroy the pointer and drop the connection. Safe to call on a pointer that already failed -- the
        socket is closed either way, so a long-lived daemon cannot leak one per compositor restart."""
        if not self.closed:
            self.closed = True
            try:
                self.conn.send(self.vp, _VP_DESTROY, [])
            except OSError:
                pass
        self.held.clear()
        try:
            self.conn.close()
        except OSError:
            pass

    # -- internals ---------------------------------------------------------

    def _frame(self):
        """Motion is applied without a frame, but the client then never gets `wl_pointer.frame`, and an axis is
        not delivered at all until one -- both measured. So every group of requests ends in a frame."""
        self._send(_VP_FRAME, [])

    def _send(self, opcode: int, args):
        if self.closed:
            raise VptrError("the virtual pointer is closed")
        try:
            self.conn.send(self.vp, opcode, args)
        except OSError as e:
            self.closed = True
            # The compositor released every button this pointer was holding when the connection went; `held`
            # must not go on claiming otherwise, or the daemon inherits a button it can never release.
            self.held.clear()
            raise VptrError(
                f"the compositor closed the connection ({e}); the mouse "
                "buttons wdotool was holding were released with it") from None


def _roundtrip(conn, what: str):
    return roundtrip(conn, what, VptrError)
