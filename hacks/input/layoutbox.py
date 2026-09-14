"""What GNOME says the desktop is, when that is not what the pointer is mapped across.

Every absolute pointer coordinate is mapped across the layout bounding box that
`daemon._wayland_bbox()` reads off the Wayland wire, so which *pixel space* that box is
in decides where `mousemove` lands. On GNOME there are two, and Mutter says which one is
in force only on D-Bus:

    layout-mode 1 (logical)   a 1920x1080 head at 200% is a 960x540 desktop
    layout-mode 2 (physical)  the same head is a 1920x1080 desktop; the scale is
                              a UI factor and the layout coordinates are raw pixels

24.04's GNOME 46 is in physical mode until "Fractional Scaling" is switched on, and
`zxdg_output_v1.logical_size` carries whichever of the two is in force -- so the wire is
right in both, and an identity map on 24.04 is not a bug but that desktop's own space.

MEASURED on `noble-gnome-iso` (24.04, GNOME 46): turn Fractional Scaling on while a head
is already at 200% -- what a HiDPI laptop owner does -- and Mutter moves to logical mode
WITHOUT re-sending `logical_size`. DisplayConfig then says the desktop is 960x540 and the
wire still says 1920x1080. The state persists past a 90 s wait and past re-applying the
same scale, and clears the moment the scale is actually changed.

**AND THE STALE NUMBER IS THE ONE THE POINTER NEEDS.** Mutter maps an absolute device
across the same stale rectangle it is still advertising, so with the wire's box every
target lands exactly where it was asked for, measured against the KMS cursor plane on the
scanout in that very state (`repro/scale-1-gnome46-xdg-output-stale.sh`):

    box 1920x1080 (the wire)      asked 100,100 -> logical 100,100    asked 959,539 -> 959,539
    box  960x540  (DisplayConfig) asked 100,100 -> logical 200,200    asked 479,269 -> the corner

Taking DisplayConfig's box for the pointer -- which is what this file was first written
to do -- therefore lands every target at twice the coordinate asked for. It is not done,
and the measurement above is why. What IS wrong in that state is what the user is *told*:
`getdisplaygeometry` reports the stale 1920x1080 for a 960x540 desktop, so a script that
works out the middle of the screen from it aims at the bottom-right corner, and a
coordinate that looks legal (1600,1000) is off a desktop that ends at 959,539. Nothing on
the wire can tell that state from a legitimate physical-mode session -- mode 1920x1080,
`wl_output.scale` 2, xdg logical 1920x1080 are byte for byte what both send, and there the
same number is correct -- so the disagreement is found by asking Mutter, and the answer is
a diagnostic rather than a different box.

THE GATE. DisplayConfig is asked only when the wire carries the physical-mode signature: a
head whose advertised logical size *is* its raw mode size while it claims
`wl_output.scale` >= 2. Everything else is unambiguous, so a session at scale 1 (very
nearly all of them), a logical-mode session at any scale, KDE, sway and X11 never open a
bus, never make a call, and cannot be affected by anything in this file.
"""

import math
import time

# org.gnome.Mutter.DisplayConfig.  `wxrandr/mutter.py` is the backend that speaks all
# of this API and it is NOT imported here: the single-file `wdotool` zipapp bundles
# w11common and wdotool only (scripts/build-pyz.sh), so a wxrandr import would make this
# work from the .deb and quietly not from the zipapp -- the worst shape a diagnostic can
# have. What is needed of it instead is one arithmetic rule, `_logical_size` below, and a
# test asserts the two copies agree
# (test_scale_spaces.DisplayConfigSource.test_the_rounding_rule_is_the_backends_own).
DEST = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
IFACE = "org.gnome.Mutter.DisplayConfig"
LAYOUT_LOGICAL, LAYOUT_PHYSICAL = 1, 2

#: A wedged compositor must not hang the daemon (the Wayland read is bounded at 3s
#: for the same reason).
CALL_TIMEOUT = 2.0
#: After a failure -- no GNOME here, a bus that will not connect, a call that threw
#: -- do not try again for this long. A daemon outlives a compositor restart, so
#: "no GNOME" is not cached for ever.
RETRY_AFTER = 30.0


def _round_half_away(x: float) -> int:
    """C round(): halves go away from zero, which is what Mutter's roundf does and
    what Python's banker's round() does not (wxrandr.core.round_half_away)."""
    r = int(math.floor(abs(x) + 0.5))
    return r if x >= 0 else -r


def _logical_size(px_w: int, px_h: int, transform: int, scale: float,
                  layout_mode: int) -> tuple:
    """One logical monitor's size, Mutter's way: the mode swapped for a 90/270
    transform, then roundf(px / scale) in layout-mode 1 and the raw pixels in
    layout-mode 2, where the scale is a pure UI factor. The same rule as
    `wxrandr.mutter.logical_size`, which takes its transform as a name."""
    if transform % 2:                       # wl_output numbering: 1,3,5,7 rotate 90/270
        px_w, px_h = px_h, px_w
    if layout_mode == LAYOUT_PHYSICAL or not scale:
        return (px_w, px_h)
    return (_round_half_away(px_w / scale), _round_half_away(px_h / scale))


def _mode_size(o) -> tuple:
    """The head's raw mode, in the orientation it is displayed at."""
    w, h = o.get("w") or 0, o.get("h") or 0
    if (o.get("transform") or 0) % 2:  # 90/270 (+flipped) swap
        w, h = h, w
    return (w, h)


def wire_is_ambiguous(outs) -> bool:
    """Does this wire state have the physical-mode signature described above?

    True for a head that advertises a logical size equal to its raw mode size while
    claiming a scale of 2 or more -- which is either GNOME 46 in physical layout mode
    (correct) or GNOME 46 that has stopped updating logical_size (stale). Nothing else on
    the wire is ambiguous, so nothing else asks."""
    for o in outs or ():
        if not o.get("lw") or not o.get("lh"):
            continue                       # no xdg_output for this head: nothing to doubt
        if (o.get("scale") or 1) < 2:
            continue                       # an unscaled head is the same in both modes
        if (o["lw"], o["lh"]) == _mode_size(o):
            return True
    return False


class MutterSource:
    """What DisplayConfig says the desktop is, over one bus kept open across commands.

    Opening a session bus is an auth handshake and a Hello; doing that per pointer
    command would be absurd, and the gate above means this object is not even asked
    on a session that does not need it."""

    def __init__(self):
        self.bus = None
        self.next_try = 0.0

    def close(self):
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
            self.bus = None

    def _connect(self):
        """A Bus on the graphical session's bus if DisplayConfig is on it, else None.
        `wxrandr.mutter.probe()` in miniature, over w11common, for the reason at the
        top of the file."""
        from w11common import session as wsession
        from w11common.dbus_mini import Bus, DBusError

        hit = wsession.find_session_bus()
        if hit is None:
            return None
        try:
            bus = Bus(hit[1])
        except (DBusError, OSError, ValueError):
            return None
        try:
            if bus.name_has_owner(DEST):
                return bus
        except DBusError:
            pass
        bus.close()
        return None

    def bbox(self):
        """(min_x, min_y, w, h) as Mutter lays the desktop out, or None: no GNOME
        on this session, no bus, or anything at all going wrong -- there is then
        nothing to compare and nothing is said."""
        now = time.monotonic()
        if now < self.next_try:
            return None
        try:
            if self.bus is None:
                self.bus = self._connect()
            if self.bus is None:
                self.next_try = now + RETRY_AFTER
                return None
            return self._bbox_over(self.bus)
        except Exception:
            # A dropped bus is the common case (the compositor restarted): let go of
            # it, and let the next command past the gate reconnect.
            self.close()
            self.next_try = now + RETRY_AFTER
            return None

    def _bbox_over(self, bus):
        _serial, monitors, logical, props = bus.call(
            DEST, PATH, IFACE, "GetCurrentState", timeout=CALL_TIMEOUT)
        layout_mode = int(props.get("layout-mode") or LAYOUT_LOGICAL)
        current = {}
        for spec, modes, _mprops in monitors:
            for mode in modes:                          # (id, w, h, rate, pref_scale, scales, props)
                if mode[6].get("is-current"):
                    current[spec[0]] = (mode[1], mode[2])
                    break
        boxes = []
        for lm in logical:
            x, y, scale, transform, _primary, members = lm[0], lm[1], lm[2], lm[3], lm[4], lm[5]
            size = next((current[s[0]] for s in members if s[0] in current), None)
            if size is None:            # a logical monitor with no current mode: Mutter
                continue                # is mid-change, and this reading is not usable
            w, h = _logical_size(size[0], size[1], int(transform),
                                 float(scale), layout_mode)
            boxes.append((x, y, w, h))
        if not boxes:
            return None
        minx = min(b[0] for b in boxes)
        miny = min(b[1] for b in boxes)
        return (minx, miny,
                max(b[0] + b[2] for b in boxes) - minx,
                max(b[1] + b[3] for b in boxes) - miny)


def _fmt(box) -> str:
    return "%dx%d+%d+%d" % (box[2], box[3], box[0], box[1])


def disagreement(wire_box, desktop_box) -> str:
    """The line said once when the two sources differ."""
    return ("GNOME is advertising a layout it is not drawing: the Wayland output "
            "geometry says %s and GNOME's own DisplayConfig says %s. Mutter 46 does "
            "this when Fractional Scaling is switched on under an already-scaled "
            "monitor. The pointer is not affected -- absolute motion is mapped across "
            "the same advertised layout, so mousemove still lands on the coordinate "
            "you ask for -- but getdisplaygeometry reports %dx%d for a %dx%d desktop, "
            "so a coordinate worked out from it is wrong. Change the scale once to "
            "clear it."
            % (_fmt(wire_box), _fmt(desktop_box),
               wire_box[2], wire_box[3], desktop_box[2], desktop_box[3]))


def check(box, outs, source=None, warn=None):
    """Say so, once, if GNOME's two accounts of the layout disagree.

    Returns `box` unchanged, always: the wire's box is the space the compositor maps
    absolute pointer motion across, in every state measured, the broken one included
    (see the file docstring -- taking the other number here lands every target at
    twice the coordinate asked for). `warn(tag, msg)` is called once per distinct
    disagreement."""
    try:
        if warn is None or not wire_is_ambiguous(outs):
            return box
        other = (source or _SOURCE).bbox()
        if other is None or other == box:
            return box
        warn("bbox-mutter:%s:%s" % (_fmt(box), _fmt(other)), disagreement(box, other))
    except Exception:
        pass            # a diagnostic is never the thing that breaks a pointer command
    return box


#: The daemon is one process with one layout; a module-level source keeps the bus.
_SOURCE = MutterSource()
