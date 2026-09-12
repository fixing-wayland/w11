"""RandR writes: one grab, one layout, one apply.

xrandr 1.5.3 does not send a layout. It sends a **transaction**: `GrabServer`,
then -- in this fixed order, whichever way the framebuffer moves -- a
`SetCrtcConfig` disabling every crtc that has to change, a `SetScreenSize` (only
when the framebuffer really changes), a `SetCrtcConfig` re-enabling each of
them, an optional `SetOutputPrimary`, and `UngrabServer`
[recon/wire.md 6, `xrandr.c:1680 apply()`]. Measured twice in both directions
(1920x1080 <-> 1680x1050) and once per write path on this box's Xwayland
(scratchpad/b7/cap1.log, 2026-09-10). A proxy that applied each request as it
came would turn a screen off and then resize a framebuffer that no longer has
one; this module accumulates until `UngrabServer` and applies **once**.

What it accumulates into is `wxrandr`'s own model, so the layout the compositor
gets is the one `wxrandr --output ... --mode ...` would have produced, bugs
included: `core.Stanza` per output, `core.build_targets`, the backend's
`verify`, the backend's `apply`. Four of the six backends are already atomic
whole-layout applies and sway's two-phase pair is the fifth
[recon/seams.md 6.2], which is why one `apply()` per grab is the natural shape
and not a compromise.

Three facts from the measurements decide the rest:

* **`SetCrtcConfig` must be answered at once.** `XRRSetCrtcConfig` blocks on the
  reply, so a proxy that deferred it would never see the `UngrabServer` it is
  waiting for -- the grab would deadlock. It is answered `status 0` with the
  request's own `config_timestamp` and recorded (design section 7.3).
* **The last write for a crtc wins.** Disable-then-enable on one crtc is the
  measured shape of every `--mode`, `--rotate` and `--scale`
  [recon/wire.md 6 requests 20 and 22]; on two heads it is a disable of the crtc
  that moves, one `SetScreenSize`, and one enable per crtc (R2, measured
  2026-09-10 on `WLR_HEADLESS_OUTPUTS=2`, scratchpad/b7/cap2.log connection 14).
* **An empty grab commits nothing.** `--dryrun` sends `GrabServer` and
  `UngrabServer` with nothing between them [recon/wire.md 6; measured again here
  as connection 9 of cap1.log], and a proxy that took that for "apply the
  current layout" would make `--dryrun` the one flag that changes something.

`SetScreenSize` is recorded and never applied: it names a framebuffer, and the
layout the compositor is given has none. It is not lost -- it is what a failure
is attributed to (section 7.5 and `_fail` below), and it is validated against
the layout box the targets really produce. A `--fb` with no crtc change under it
is the one shape where that leaves nothing to do, and it says so and keeps
xrandr's exit 0 (`commit`). `--panning`, free `--transform` matrices, `--set`
and gamma stay Xwayland's; those are rows in docs/XW11.md, each with the route
that would close it.

Two numbers on the wire mean the opposite of what they look like, and both are
measured rather than reasoned about: a `--scale` factor is handed to the
compositor as its RECIPROCAL (`scale_words`), because X grows the screen where
Wayland shrinks the head; and a locally answered reply for a write that has to
apply first is HELD until it has (`conn.hold_reply`), because X answers
`SetCrtcConfig` and `SetScreenConfig` after the crtc moved and a script whose
next line reads the geometry is owed the new one.
"""

import struct
import threading
import time

from xw11 import policy, wire

# -- the minors this module names ---------------------------------------------
#
# Minors, unlike majors, ARE fixed by the extension: RANDR's major is 139 on
# Xwayland and 140 on Xvfb [recon/tools.md 7, recon/env.md 2.5] and is never
# written down anywhere in this package, but minor 21 is `SetCrtcConfig` on
# every server there is (/usr/share/xcb/randr.xml, xcb-proto 1.17.0).
SET_SCREEN_CONFIG = 2
GET_SCREEN_INFO = 5
SET_SCREEN_SIZE = 7
GET_OUTPUT_INFO = 9
CREATE_MODE = 16
ADD_OUTPUT_MODE = 18
GET_CRTC_INFO = 20
SET_CRTC_CONFIG = 21
GET_SCREEN_RESOURCES_CURRENT = 25
SET_CRTC_TRANSFORM = 26
SET_OUTPUT_PRIMARY = 30
GET_OUTPUT_PRIMARY = 31

#: `SetConfig.Failed` (randr.xml): the RandR 1.1 refusal, which travels in the
#: reply's status byte and never as an error packet.
SET_CONFIG_FAILED = 3

#: How far a real mode may sit from a client-made one's modeline and still be
#: the mode it means: `resolve_real_mode`'s own tolerance
#: (`wxrandr/core.py:1128`, `match_mode(..., tolerance=1.0)`).
CUSTOM_MODE_TOLERANCE_HZ = 1.0

#: `Rotation` (randr.xml): the four rotations are bits 0-3 and the two
#: reflections bits 4-5, which is exactly the order `wxrandr.core.ROTATIONS` and
#: `REFLECTIONS` are written in (core.py:80) -- so the words xrandr's own
#: `--rotate`/`--reflect` take come straight out of those two tuples and no
#: third table exists to drift. Measured: `--rotate left` sends rotation 2
#: (scratchpad/b7/cap1.log connection 8 request 22).
ROTATE_MASK = 0x0F
REFLECT_SHIFT = 4
REFLECT_MASK = 0x03

#: `ModeFlag` (randr.xml). Only the two that change the refresh arithmetic are
#: named: `mode_refresh_hz` doubles vtotal for DoubleScan and halves it for
#: Interlace (wxrandr/core.py:170, xrandr.c:554).
MODE_INTERLACE = 0x10
MODE_DOUBLESCAN = 0x20

#: 16.16 fixed point, which is what a `TRANSFORM` is made of (render.xml
#: `FIXED`). `--scale 2x2` sends 0x00020000 on the diagonal and 0x00010000 in
#: the corner (measured, cap1.log connection 10 request 22).
FIXED_ONE = 1 << 16

#: The smallest framebuffer xrandr will ask for, and the one `--off` asks for
#: when it turns the last output off: `SetScreenSize(16, 16, 4, 4)`
#: (cap1.log connection 6 request 20, against a server whose
#: `GetScreenSizeRange` minimum is 16x16).
MIN_SCREEN = 16


def mode_flag_words(flags: int) -> tuple:
    """The `wxrandr.core.mode_refresh_hz` flag words a `ModeInfo`'s flag word
    carries. Everything else in that word (the sync polarities, CSync, the
    multiplex bits) leaves the refresh alone and is dropped here rather than
    carried into a `Mode` that would then compare unequal to the compositor's."""
    got = []
    if flags & MODE_INTERLACE:
        got.append("interlace")
    if flags & MODE_DOUBLESCAN:
        got.append("doublescan")
    return tuple(got)


class ModeInfo:
    """One 32-byte `ModeInfo` out of a `GetScreenResources[Current]` reply, plus
    the name that lives in the reply's trailing name block.

    The refresh is computed from the modeline and never from a rounded number,
    because that is what xrandr prints: all sixteen modes of this box's
    `HEADLESS-1` come back to the second decimal `xrandr -q` shows -- 59.86,
    59.38, 59.29, 59.97, 59.63, 59.88, 59.50, 59.90, 59.71, 59.95, 58.14, 59.90,
    59.92, 59.27, 59.28 -- from `dot_clock / (htotal * vtotal)`
    (measured 2026-09-10, scratchpad/b7/cap1.log connection 3 reply 12 against
    the `-q` of connection 1).
    """

    __slots__ = ("mode", "width", "height", "dot_clock", "hsync_start",
                 "hsync_end", "htotal", "hskew", "vsync_start", "vsync_end",
                 "vtotal", "flags", "name")

    def __init__(self, fields, name):
        (self.mode, self.width, self.height, self.dot_clock, self.hsync_start,
         self.hsync_end, self.htotal, self.hskew, self.vsync_start,
         self.vsync_end, self.vtotal, _name_len, self.flags) = fields
        self.name = name

    @property
    def size_name(self) -> str:
        """`WxH` -- what every compositor's mode list is keyed by, and what
        `wxrandr.core._find_mode_for` matches a stanza's `mode` against."""
        return "%dx%d" % (self.width, self.height)

    @property
    def custom(self) -> bool:
        """A mode a client made with `RRCreateMode`, told apart the way xrandr
        tells them apart: its name is its own and not its size. `--newmode
        b7cap ...` then `--addmode` is measured working on today's Xwayland
        [recon/env.md 2.4; cap1.log connections 12 and 13]."""
        return bool(self.name) and self.name != self.size_name

    def refresh_hz(self) -> float:
        from wxrandr import core
        return core.mode_refresh_hz(self.dot_clock / 1e6, self.htotal,
                                    self.vtotal, mode_flag_words(self.flags))

    def __repr__(self):
        return "ModeInfo(0x%x, %s, %r)" % (self.mode, self.size_name, self.name)


class CrtcInfo:
    """One `GetCrtcInfo` reply: where a crtc is, what it runs, and which
    outputs it drives."""

    __slots__ = ("crtc", "x", "y", "width", "height", "mode", "rotation",
                 "outputs")

    def __init__(self, crtc, x, y, width, height, mode, rotation, outputs):
        self.crtc = crtc
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.mode = mode
        self.rotation = rotation
        self.outputs = tuple(outputs)


class OutputInfo:
    """One `GetOutputInfo` reply. The **name** is the whole point: on Xwayland
    it is the compositor's own output name (`HEADLESS-1`, not `XWAYLAND0`
    [recon/env.md 2.4]), which is exactly the name `wxrandr`'s backends key a
    layout by, so no mapping between the two planes exists to be wrong."""

    __slots__ = ("output", "crtc", "name", "modes", "mm_width", "mm_height",
                 "connection")

    def __init__(self, output, crtc, name, modes, mm_width, mm_height,
                 connection):
        self.output = output
        self.crtc = crtc
        self.name = name
        self.modes = tuple(modes)
        self.mm_width = mm_width
        self.mm_height = mm_height
        self.connection = connection


class Resources:
    """The upstream's RandR tables as one object: what the proxy read on its own
    connection at `GrabServer` (design section 7.4 step 1).

    Complete rather than clever: ten round trips at 70 us [recon/env.md 5.3] is
    under a millisecond, and the alternative -- watching the client's own read
    phase go past -- needs this path as its fallback anyway, for the client that
    read nothing (design section 7.4, decided (A)).
    """

    def __init__(self):
        self.timestamp = 0
        self.config_timestamp = 0
        self.crtcs = {}          # crtc id -> CrtcInfo
        self.outputs = {}        # output id -> OutputInfo
        self.modes = {}          # mode id -> ModeInfo
        self.primary = 0
        self.sizes = ()          # the RandR 1.1 size table, GetScreenInfo
        self.size_id = 0
        self.rotation = 1

    def output_name(self, output: int):
        info = self.outputs.get(output)
        return None if info is None else info.name

    def crtc_outputs(self, crtc: int) -> tuple:
        info = self.crtcs.get(crtc)
        return () if info is None else info.outputs

    def name_for(self, crtc: int, outputs) -> str:
        """The output name a `SetCrtcConfig` is about: the first output it
        names, else -- for a disable, whose output list is empty
        [recon/wire.md 6 request 20] -- the one the snapshot says that crtc was
        driving. Empty when neither knows, which is the one case the commit
        refuses to guess about."""
        for output in tuple(outputs) + self.crtc_outputs(crtc):
            name = self.output_name(output)
            if name:
                return name
        return ""


# -- parsing the replies -------------------------------------------------------
#
# Offsets are recon/wire.md section 6, taken off /usr/share/xcb/randr.xml and
# confirmed byte for byte against a live Xwayland 24.1.10 under sway
# (scratchpad/b7/cap1.log, 2026-09-10): crtc 0x20, output 0x21 named
# `HEADLESS-1`, sixteen modes 0x41..0x50, config timestamp 0x3163dfd0.


def parse_screen_resources(pkt: bytes, into: Resources = None) -> Resources:
    """`GetScreenResources` / `GetScreenResourcesCurrent` -- identical shape."""
    res = Resources() if into is None else into
    if len(pkt) < 32 or pkt[0] != 1:
        return res
    res.timestamp, res.config_timestamp = struct.unpack_from("<II", pkt, 8)
    n_crtcs, n_outputs, n_modes, names_len = struct.unpack_from("<HHHH", pkt, 16)
    off = 32
    need = off + 4 * n_crtcs + 4 * n_outputs + 32 * n_modes + names_len
    if len(pkt) < need:
        return res
    crtcs = struct.unpack_from("<%dI" % n_crtcs, pkt, off) if n_crtcs else ()
    off += 4 * n_crtcs
    outputs = struct.unpack_from("<%dI" % n_outputs, pkt, off) if n_outputs else ()
    off += 4 * n_outputs
    raw = []
    for _ in range(n_modes):
        raw.append(struct.unpack_from("<IHHIHHHHHHHHI", pkt, off))
        off += 32
    names = pkt[off:off + names_len]
    at = 0
    for fields in raw:
        name_len = fields[11]
        name = names[at:at + name_len].decode("latin-1", "replace")
        at += name_len
        res.modes[fields[0]] = ModeInfo(fields, name)
    for crtc in crtcs:
        res.crtcs.setdefault(crtc, None)
    for output in outputs:
        res.outputs.setdefault(output, None)
    return res


def parse_crtc_info(pkt: bytes, crtc: int):
    """`GetCrtcInfo`. `status` is byte 1 and a non-zero one means the crtc is
    not there any more; the caller drops it rather than recording a rectangle
    the server did not stand behind."""
    if len(pkt) < 32 or pkt[0] != 1 or pkt[1] != 0:
        return None
    x, y, width, height, mode, rotation, _rots, n_out = struct.unpack_from(
        "<hhHHIHHH", pkt, 12)
    outputs = ()
    if n_out and len(pkt) >= 32 + 4 * n_out:
        outputs = struct.unpack_from("<%dI" % n_out, pkt, 32)
    return CrtcInfo(crtc, x, y, width, height, mode, rotation, outputs)


def parse_output_info(pkt: bytes, output: int):
    """`GetOutputInfo`, name included."""
    if len(pkt) < 36 or pkt[0] != 1 or pkt[1] != 0:
        return None
    crtc, mm_w, mm_h = struct.unpack_from("<III", pkt, 12)
    connection = pkt[24]
    n_crtcs, n_modes, _n_pref, n_clones, name_len = struct.unpack_from(
        "<HHHHH", pkt, 26)
    off = 36 + 4 * n_crtcs
    modes = ()
    if n_modes and len(pkt) >= off + 4 * n_modes:
        modes = struct.unpack_from("<%dI" % n_modes, pkt, off)
    off += 4 * n_modes + 4 * n_clones
    name = pkt[off:off + name_len].decode("latin-1", "replace")
    return OutputInfo(output, crtc, name, modes, mm_w, mm_h, connection)


def parse_screen_info(pkt: bytes, into: Resources) -> Resources:
    """`GetScreenInfo` -- the RandR 1.1 size table `xrandr -s` indexes into.
    Measured on this box: sixteen sizes, `sizeID` 0 = 1280x720 and 1 = 800x600,
    which is the `sizeID` `-s 800x600` really sent (cap1.log connection 11)."""
    if len(pkt) < 32 or pkt[0] != 1:
        return into
    n_sizes, size_id, rotation, _rate, _n_info = struct.unpack_from(
        "<HHHHH", pkt, 20)
    sizes = []
    off = 32
    for _ in range(n_sizes):
        if len(pkt) < off + 8:
            break
        sizes.append(struct.unpack_from("<HHHH", pkt, off))
        off += 8
    into.sizes = tuple(sizes)
    into.size_id = size_id
    into.rotation = rotation
    return into


def parse_output_primary(pkt: bytes) -> int:
    if len(pkt) < 12 or pkt[0] != 1:
        return 0
    (primary,) = struct.unpack_from("<I", pkt, 8)
    return primary


# -- decoding the writes -------------------------------------------------------


def decode_set_crtc_config(frame: bytes):
    """`(crtc, x, y, mode, rotation, outputs)`; None when the frame is short.
    Layout: recon/wire.md 6, confirmed against
    `8b1508002000000000000000d0df633100000000420000000100000021000000`
    (cap1.log connection 3 request 22: crtc 0x20 at 0,0, mode 0x42, rotation 1,
    output 0x21)."""
    if len(frame) < 28:
        return None
    crtc, _ts, config_ts = struct.unpack_from("<III", frame, 4)
    x, y, mode, rotation = struct.unpack_from("<hhIH", frame, 16)
    rest = (len(frame) - 28) // 4
    outputs = struct.unpack_from("<%dI" % rest, frame, 28) if rest else ()
    return crtc, x, y, mode, rotation, outputs, config_ts


def decode_set_screen_size(frame: bytes):
    """`(width, height, mm_width, mm_height)`."""
    if len(frame) < 20:
        return None
    _win, width, height = struct.unpack_from("<IHH", frame, 4)
    mm_w, mm_h = struct.unpack_from("<II", frame, 12)
    return width, height, mm_w, mm_h


def decode_set_output_primary(frame: bytes) -> int:
    if len(frame) < 12:
        return 0
    (output,) = struct.unpack_from("<I", frame, 8)
    return output


def decode_transform_scale(frame: bytes):
    """A `SetCrtcTransform`'s matrix as `(sx, sy)`, or None when it is not a
    pure scale.

    xrandr's `--scale` sends exactly `[[sx,0,0],[0,sy,0],[0,0,1]]` in 16.16
    fixed point and nothing else does
    (`8b1a0e00 20000000 00000200 ...`, cap1.log connection 10 request 22:
    2.0 on both diagonals, 1.0 in the corner, filter `bilinear`). Anything with
    a shear or a translation in it is a transform no output-management protocol
    carries; it answers `BadValue` -- which is also the byte Xwayland answers
    for the whole request today [recon/env.md 2.4] -- and the row in
    docs/XW11.md names the route that would close it."""
    if len(frame) < 44:
        return None
    m = struct.unpack_from("<9i", frame, 8)
    if m[1] or m[2] or m[3] or m[5] or m[6] or m[7]:
        return None
    if m[8] != FIXED_ONE or m[0] <= 0 or m[4] <= 0:
        return None
    return m[0] / FIXED_ONE, m[4] / FIXED_ONE


def decode_set_screen_config(frame: bytes):
    """`(size_id, rotation, rate)` of the RandR 1.1 `xrandr -s` path."""
    if len(frame) < 24:
        return None
    _win, _ts, _cts = struct.unpack_from("<III", frame, 4)
    size_id, rotation, rate = struct.unpack_from("<HHH", frame, 16)
    return size_id, rotation, rate


def rotation_words(rotation: int):
    """A RandR rotation word as xrandr's own `(rotate, reflect)` pair. The two
    tuples are `wxrandr.core`'s, so the words a stanza carries are the words
    `--rotate` and `--reflect` parse into and `RANDR_VIEW` (core.py:109) reads
    a compositor's transform back out as."""
    from wxrandr import core
    bits = rotation & ROTATE_MASK
    rotate = core.ROTATIONS[bits.bit_length() - 1] if bits else "normal"
    reflect = core.REFLECTIONS[(rotation >> REFLECT_SHIFT) & REFLECT_MASK]
    return rotate, reflect


# -- the batch -----------------------------------------------------------------


class RandrBatch:
    """Everything one client asked for between `GrabServer` and `UngrabServer`.

    Outside a grab (`--nograb`, or a client that never grabs) one of these holds
    exactly one request and is committed where it stands -- the same code, a
    batch of one (design section 7.4)."""

    def __init__(self, resources=None, grabbed=False):
        self.resources = resources
        self.grabbed = grabbed
        #: crtc -> (x, y, mode, rotation, outputs). Last write wins: the
        #: measured disable-then-enable pair is two writes for one crtc.
        self.crtcs = {}
        self.scale = {}          # crtc -> (sx, sy)
        self.screen = None       # (w, h, mm_w, mm_h) from SetScreenSize
        self.screen_seq = None   # the sequence a failure is attributed to
        self.primary = None      # the output id SetOutputPrimary named
        self.last_crtc_seq = None
        self.screen_config = None    # the RandR 1.1 path's (size_id, rotation)
        #: the sequence of the request that CLOSED this batch -- the
        #: `UngrabServer`, or the lone write outside a grab. An error the proxy
        #: writes carries this and not the sequence of the request that
        #: conceptually failed; `_fail` says why, at length.
        self.commit_seq = None
        #: the sequence whose reply is held until this batch has applied, for
        #: the batches of one that have one (`SetCrtcConfig` and
        #: `SetScreenConfig` outside a grab). X answers those after the crtc
        #: moved; so does this.
        self.hold_seq = None
        #: what to send instead of that reply when the apply fails. RandR 1.1
        #: refuses in the reply's `status` byte and not with an error
        #: (randr.xml `SetConfig`: 3 is `Failed`), which is the byte `xrandr -s`
        #: turns into `Failed to change the screen configuration!` and exit 1.
        self.fail_reply = None

    @property
    def empty(self) -> bool:
        return (not self.crtcs and not self.scale and self.screen is None
                and self.primary is None and self.screen_config is None)

    @property
    def noprimary(self) -> bool:
        """`xrandr --noprimary`: a `SetOutputPrimary` naming output `None`,
        which is the id 0 on the wire, against `None` here for a batch that
        carried no such request at all.

        Measured 2026-09-11 on this box, real xrandr 1.5.3 through a tee in
        front of an Xvfb :91 (scratchpad/b7/noprimary-wire.txt): `xrandr
        --noprimary` sends `24000100` (GrabServer, seq 19),
        `8c1e03001f02000000000000` -- RANDR major 140, minor 30
        `RRSetOutputPrimary`, the root `0x21f`, output `00000000` -- then
        `25000100` (Ungrab) and the `GetInputFocus` sync. `xrandr --output
        screen --noprimary` sends the same four requests and no
        `SetCrtcConfig`: --noprimary clears the screen's primary, it does not
        touch the output it is spelled next to."""
        return self.primary == 0

    def record_crtc(self, decoded, seq) -> None:
        crtc, x, y, mode, rotation, outputs, _cts = decoded
        self.crtcs[crtc] = (x, y, mode, rotation, outputs)
        self.last_crtc_seq = seq


def scale_words(batch: RandrBatch, crtc: int):
    """The compositor scale a `--scale` asks for -- the RECIPROCAL of xrandr's.

    xrandr's `--scale s` means "this crtc reads an s-times bigger rectangle of
    the framebuffer": on X, `--scale 2x2` on a 1280x720 output makes the screen
    2560x1440 and everything on it smaller (which is also why xrandr sends
    `SetScreenSize 2560x1440` with it, cap1.log connection 10 request 21). A
    Wayland output's `scale` runs the other way. Measured 2026-09-10 on this
    box's headless sway: `swaymsg output HEADLESS-1 scale 2` leaves the output
    rect 640x360 and `scale 0.5` leaves it 2560x1440. So the number handed to
    the compositor is `1/s`, and `xrandr --scale 2x2` through the proxy leaves
    the client looking at the same 2560x1440 X would have given it -- which is
    the rule, X wins where the two disagree. `wxrandr --scale` keeps the
    compositor's own meaning; the row in docs/XW11.md names both."""
    sx, sy = batch.scale[crtc]
    return 1.0 / sx, 1.0 / sy


def stanzas_for(batch: RandrBatch, log=None):
    """The batch as `(stanzas, customs)`: one `wxrandr.core.Stanza` per crtc it
    touched, and output name -> the custom `ModeInfo` a `SetCrtcConfig` named,
    which `Applier.apply` puts through `resolve_real_mode`'s rule.

    This is the whole translation, and everything after it is code the `wxrandr`
    CLI already runs: `build_targets` settles modes and transforms, the backend
    resolves positions and applies. Nothing here decides a layout -- it decides
    what the layout REQUEST says.
    """
    from wxrandr import core
    res = batch.resources or Resources()
    out = []
    customs = {}
    # A crtc the batch knows only from a `SetCrtcTransform` is in here too: the
    # measured `--scale` sends a disable, the transform and an enable in one
    # grab [cap1.log connection 10], but `--nograb` splits those into three
    # batches of one and the transform's own batch would otherwise name no
    # output and apply nothing.
    for crtc in sorted(set(batch.crtcs) | set(batch.scale)):
        got = batch.crtcs.get(crtc)
        outputs = got[4] if got is not None else ()
        name = res.name_for(crtc, outputs)
        if not name:
            if log is not None:
                log("crtc 0x%x names no output this proxy knows: its half of "
                    "the layout is dropped rather than guessed at" % crtc)
            continue
        if got is None:
            out.append(core.Stanza(name, scale=scale_words(batch, crtc)))
            continue
        x, y, mode, rotation, outputs = got
        if not mode and not outputs:
            # The measured shape of `--off` and of the disable half of every
            # `--mode`: mode None and an EMPTY output list, byte-identical
            # between the two [recon/wire.md 6]. Which of the two it was is
            # decided by whether an enable for the same crtc followed, and
            # `record_crtc`'s last-write-wins has already decided it.
            out.append(core.Stanza(name, off=True))
            continue
        rotate, reflect = rotation_words(rotation)
        spec = None
        rate = None
        info = res.modes.get(mode)
        if info is not None:
            # A custom mode -- one a client made with `RRCreateMode` and
            # attached with `RRAddOutputMode`, both of which Xwayland takes
            # [recon/env.md 2.4] -- reaches the compositor as its SIZE and its
            # RATE and never as a modeline, because no output-management
            # protocol carries one. A `Stanza` holds exactly that much, and
            # whether it is APPLICABLE is `resolve_real_mode`'s question
            # (core.py:1103), asked of the compositor's own mode list in
            # `Applier._settle_customs` once `build_targets` has one. Recording
            # it here is what lets that question be asked at all.
            spec = info.size_name
            rate = info.refresh_hz() or None
            if info.custom:
                customs[name] = info
                if log is not None:
                    log("xw11: crtc 0x%x was given the custom mode %r; it goes "
                        "to the compositor as %s at %.2f Hz, its own "
                        "modeline's" % (crtc, info.name, spec, rate or 0.0))
        stanza = core.Stanza(name, mode=spec, rate=rate, pos=(x, y),
                             rotate=rotate, reflect=reflect,
                             scale=(scale_words(batch, crtc)
                                    if crtc in batch.scale else None),
                             primary=bool(batch.primary
                                          and batch.primary in outputs))
        out.append(stanza)
    if batch.primary and not any(s.primary for s in out):
        # `xrandr --output X --primary` sends an enable with it (grab, one
        # `SetCrtcConfig`, `SetOutputPrimary`, ungrab -- cap1.log connection 5,
        # tests/fixtures/xw11/xrandr-primary.hex), and the flag then rides on
        # that crtc's stanza. This is for the batch that has no crtc in it:
        # `--nograb` makes every request a batch of its own, so the
        # `SetOutputPrimary` arrives alone, and so does one from a client that
        # sets the primary and nothing else. Without a stanza to carry it
        # `wxrandr`'s `State.primary` would never be written and the two planes
        # would disagree about which output is primary -- the one thing design
        # section 7.3 says recording it is for.
        name = res.output_name(batch.primary)
        if name:
            out.append(core.Stanza(name, primary=True))
        elif log is not None:
            log("xw11: SetOutputPrimary named output 0x%x, which this proxy's "
                "RandR tables do not have: Xwayland's own copy of the flag "
                "still moved, wxrandr's did not" % batch.primary)
    return out, customs


def screen_config_stanza(batch: RandrBatch):
    """`xrandr -s WxH`'s one stanza (design section 7.6).

    `sizeID` indexes the `GetScreenInfo` table the proxy read on its own
    connection, and the output it names is the primary one, else the first one
    a crtc is driving -- which is what the 1.1 protocol means by "the screen".
    A size that is not in the table never reaches the wire at all: xrandr checks
    the list first and refuses client-side (`Size 1024x768 not found in
    available modes` [recon/env.md 2.4]), and that list is upstream's, passed
    through."""
    from wxrandr import core
    res = batch.resources or Resources()
    size_id, rotation, _rate = batch.screen_config
    if size_id >= len(res.sizes):
        raise core.Fatal("size id %d is not in the screen's size table\n"
                         % size_id)
    width, height, _mm_w, _mm_h = res.sizes[size_id]
    name = res.output_name(res.primary)
    if not name:
        for crtc in sorted(res.crtcs):
            name = res.name_for(crtc, ())
            if name:
                break
    if not name:
        raise core.Fatal("no output to set the screen size on\n")
    rotate, reflect = rotation_words(rotation)
    return [core.Stanza(name, mode="%dx%d" % (width, height), rotate=rotate,
                        reflect=reflect)]


# -- the apply -----------------------------------------------------------------


class Applier:
    """`wxrandr`'s backend, chosen once per proxy life and kept.

    Lazily: a proxy that never sees a RandR write never probes a compositor for
    one. The probe's own connection is reused rather than dialled twice, which
    is the shape `wxrandr/cli.py:1220-1233` already has, and `State` is keyed by
    the session's Wayland socket (`cli.py:1292`) so the proxy and a `wxrandr`
    run in the same session share one primary/custom-mode store.

    `persistent` is never passed through here. A long-lived proxy would inherit
    one wrapper's `WXRANDR_PERSIST` and hand it to every client for the rest of
    the session; `wxrandr --persistent` stays the front end for it (design
    section 7.4, decided (A)).
    """

    def __init__(self, log=None, env=None):
        self._log = log
        self.env = env
        self.backend = None
        self.state = None
        self.name = None
        self.tried = False
        self.reason = None
        #: one apply at a time, whichever client asked: the backends hand the
        #: compositor a whole layout and two overlapping ones would race
        self.lock = threading.Lock()

    def say(self, text: str) -> None:
        if self._log is not None:
            self._log(text)

    def ensure(self):
        """The backend, or None with `self.reason` set. Never raises: a proxy
        whose session has no output management still has to forward."""
        import os

        from w11common import session as wsession
        from wxrandr import cli as wcli
        from wxrandr import core
        if self.backend is not None or self.tried:
            return self.backend
        self.tried = True
        env = os.environ if self.env is None else self.env
        probes = {}
        try:
            name, source, note, probes = wcli.chosen_backend(None, env,
                                                             probes=probes)
        except Exception as e:                      # a probe that broke its word
            self.reason = "backend detection failed: %s" % e
            self.say("xw11: " + self.reason)
            return None
        if name == "x11":
            # `chosen_backend` answers x11 when the session is an X11 one, which
            # is where wxrandr hands over to the real xrandr. A proxy in front of
            # an Xwayland is never in one, and if the environment says otherwise
            # the honest thing is to forward and say so.
            self.reason = ("the session reads as X11 (%s), where xrandr's own "
                           "writes already work" % (note or source))
            self.say("xw11: RandR writes pass through: " + self.reason)
            self._close_probes(probes)
            return None
        try:
            self.backend = self._build(name, probes)
        except Exception as e:
            self.reason = "%s is not usable in this session: %s" % (name, e)
            self.say("xw11: RandR writes pass through: " + self.reason)
            self._close_probes(probes)
            return None
        self._close_probes(probes, keep=self.backend)
        hit = wsession.find_wayland_socket()
        key = hit[2] if hit else getattr(self.backend, "sockpath", "?")
        self.state = core.State(key)
        self.name = name
        self.say("xw11: RandR writes apply through the %s backend (%s), state "
                 "keyed by %s" % (name, source if note is None else note, key))
        return self.backend

    @staticmethod
    def _build(name, probes):
        from wxrandr import core
        from wxrandr import kwin as kwin_mod

        def reuse(bname):
            p = probes.get(bname)
            return p.handle if p is not None and p.available else None
        if name == "sway":
            return core.SwayBackend(core.SwayIPC(reuse("sway")),
                                    core.wlr_snapshot_safe())
        if name == "kwin":
            return kwin_mod.KwinOutputs(conn=reuse("kwin"))
        if name == "mutter":
            from wxrandr import mutter as mutter_mod
            return mutter_mod.MutterOutputs(bus=reuse("mutter"))
        if name == "cinnamon":
            from wxrandr import mutter as mutter_mod
            return mutter_mod.MutterOutputs(bus=reuse("cinnamon"),
                                            flavor=mutter_mod.MUFFIN)
        if name == "hypr":
            from wxrandr import hypr as hypr_mod
            return hypr_mod.HyprOutputs(ipc=reuse("hypr"))
        return core.WlrOutputs(conn=reuse("wlr"))

    def _close_probes(self, probes, keep=None) -> None:
        handles = {id(getattr(keep, h, None)) for h in ("ipc", "wlr", "conn",
                                                        "bus")}
        for p in probes.values():
            if p.handle is not None and id(p.handle) not in handles:
                p.close()

    def apply(self, stanzas, screen=None, customs=None, noprimary=False):
        """`build_targets` -> the custom-mode rule -> the primary -> `verify` ->
        `apply`, under the one lock. Returns the targets it applied; raises
        `wxrandr.core.Fatal` the way the CLI does, and the caller turns that
        into an X error (section 7.5).

        `noprimary` is `xrandr --noprimary`'s half of the same field, routed the
        way `wxrandr --noprimary` routes it (`cli.py:1592-1601`).

        `ensure()` is inside the lock and not before it: it sets `tried` before
        the probe finishes, so two first applies from two clients -- each on its
        own worker thread -- would have the second read `tried` true and
        `backend` still None and refuse a session that has a backend.

        The primary is settled BETWEEN `build_targets` and `verify`, which is
        where `wxrandr/cli.py:1591-1603` settles it and for the same two
        reasons: Mutter's `verify` sends method 0 with the plan a real apply
        would send, and both backends read the wanted primary out of
        `State.primary` when they build that plan (`mutter.py:623`,
        `kwin.py:1027`). Written after the apply instead -- which is what this
        did until 2026-09-11 -- neither backend ever saw it, and `xrandr
        --output Virtual-2 --primary` through the proxy read back Virtual-1 on
        all 13 GNOME/KDE flavors of CI run 34628777544
        (`goal2/recon/gaps.md 1b #8`)."""
        from wxrandr import core
        with self.lock:
            backend = self.ensure()
            if backend is None:
                raise core.Fatal(
                    "no output-management backend in this session: %s\n"
                    % (self.reason or "not detected"))
            outputs = backend.snapshot(self.state)
            targets = core.build_targets(outputs, stanzas, self.state)
            if customs:
                self._settle_customs(targets, customs)
            before = self.state.primary
            self._want_primary(stanzas, targets, noprimary)
            try:
                backend.verify(self.state, targets)
                fresh = backend.apply(self.state, targets, persistent=False)
            except Exception:
                # `wxrandr/cli.py:1591` keeps `primary_before` for its dryrun
                # branch for this reason -- "nothing was sent, so nothing may
                # be claimed about the compositor". The CLI needs it only
                # there because a `Fatal` from verify/apply ends that process;
                # this Applier outlives the refusal and serves the next client,
                # so a refused `--output X --mode bad --primary` that left the
                # wanted primary in memory would have the NEXT batch plan with
                # it, and `wxrandr --query` -- reading the state file, which
                # this path never saves -- would disagree with it. Mutter and
                # KWin re-sync the field from the compositor in their next
                # snapshot (`mutter.py:506`, `kwin.py:1208`); sway, the wlr
                # floor and Hyprland have nothing to re-sync it from, which is
                # where it would stick.
                self.state.primary = before
                raise
            self._record_primary(stanzas, fresh or outputs, noprimary)
        if screen is not None:
            self._check_screen(fresh or outputs, screen)
        return targets

    @staticmethod
    def _settle_customs(targets, customs) -> None:
        """`resolve_real_mode`'s rule for the modes a client minted itself
        (wxrandr/core.py:1103), applied where a `Stanza` can carry it.

        A compositor is handed a mode object or a `WxH` and never a modeline, so
        a `--newmode` mode is applicable exactly when a REAL mode of the same
        size sits within `CUSTOM_MODE_TOLERANCE_HZ` of it -- and `wxrandr
        --output X --mode <a custom name>` says `cannot find mode` when none
        does. `_find_mode_for` alone would not: it matches by name and then
        takes the nearest refresh with no threshold at all (core.py:1072), so a
        93.75 Hz modeline against a list that has only 59.86 would come back
        59.86 and the client would be told its mode was set.
        `--newmode`+`--addmode`+`--mode` through the proxy refuses what the CLI
        refuses, and for the same reason.

        A VIRTUAL output is the carve-out `_find_mode_for` already makes
        (core.py:1065): a headless head has no mode list at all -- measured
        2026-09-10, `swaymsg -t get_outputs` reports `modes: []` for
        `HEADLESS-1`, which is what sets `virtual_modes` -- and the compositor
        drives whatever size it is handed. There is no real mode to match
        against and none is needed, which is why `--newmode`+`--addmode`+
        `--mode` is measured applying on this box's sway.

        `core.match_mode` itself is not the call, for the reason `kwin.py:797`
        passes `interlace_known=False`: it keeps only modes with a `mode_id`,
        and a `mode_id` is Mutter's and KWin's (core.py:196) -- sway's, wlroots'
        and Hyprland's mode lists carry none at all, so it would find nothing
        there and refuse every custom mode on three of the six backends,
        including the `--newmode b7cap` that is measured applying on this box's
        sway. "Real" here is what "has an id" means on the two backends that
        have ids: a mode the COMPOSITOR listed, rather than one
        `_find_mode_for` minted for a virtual output (core.py:1065)."""
        from wxrandr import core
        by_name = {t.name: t for t in targets}
        for name, info in customs.items():
            t = by_name.get(name)
            if t is None or not t.enabled:
                continue
            if t.output.virtual_modes:
                continue
            want = info.refresh_hz() or None
            real = [m for m in t.output.modes
                    if (m.w, m.h) == (info.width, info.height) and not m.custom]
            best = None
            if real:
                best = (min(real, key=lambda m: abs(m.refresh_hz - want))
                        if want else real[0])
                if want and abs(best.refresh_hz - want) > CUSTOM_MODE_TOLERANCE_HZ:
                    best = None
            if best is None:
                raise core.Fatal("cannot find mode %s\n"
                                 % (info.name or info.size_name))
            t.mode = best

    def _want_primary(self, stanzas, targets, noprimary=False) -> None:
        """The primary the batch asked for, into `State` BEFORE the backend
        plans anything -- which is the whole of what routes `--primary` to each
        backend's own primary verb.

        Neither backend takes a primary as an argument: both read `State.primary`
        while they build the layout they are about to send. Mutter's `plan` puts
        the flag on the logical monitor holding that connector
        (`wxrandr/mutter.py:623`) and `apply` skips a temporary layout only when
        `_canon(plan) == current_config` (`mutter.py:1017`) -- a comparison that
        includes the primary flag, so a `--primary` that changes nothing else
        still sends `ApplyMonitorsConfig`. KWin's `plan` turns `state.primary !=
        self.primary` into the `set_priority(dev, 1..N)` record with the named
        output first (`kwin.py:1027-1042`, the verb measured to move KWin's
        primary; `set_primary_output` is accepted and ignored on 5.27 and 6.6),
        and `apply` sends it even when no crtc record came with it
        (`kwin.py:1184`).

        Written AFTER the apply -- where it was until 2026-09-11 -- the backends
        planned with the primary the session already had, so nothing moved:
        measured that day on the resolute-kde golden (KWin 6.5, two virtual
        heads), `DISPLAY=:20 xrandr --output Virtual-2 --primary` exited 0, the
        proxy logged `applied a RandR batch of 1 crtc(s) in 9 ms through kwin`
        and `wxrandr --query` still printed `Virtual-1 primary`; the same run
        with `--nograb` split it into two batches of one (SetCrtcConfig seq 24,
        SetOutputPrimary seq 25) and read back Virtual-1 as well. CI run
        34628777544 had the same answer on all 13 GNOME/KDE flavors
        (`goal2/recon/gaps.md 1b #8`).

        `t.stanza is s` is `wxrandr/cli.py:1602`'s own guard: an `--output` name
        the compositor does not have only warns in `build_targets`
        (`core.py:1284`), and recording a primary for it would leave `--query`
        naming an output nothing can be primary on.

        `--noprimary` is the same field cleared, and is `cli.py:1592-1601` line
        for line: the two backends that require a primary keep theirs and say
        so, and `State.primary` goes to None either way. On Mutter that leaves
        `plan` falling back to `self.primary` (`mutter.py:625`), so the plan
        equals the current configuration, `_canon` matches and nothing is sent;
        on KWin `want` is falsy, so no `set_priority` record is built and the
        apply sends nothing (`kwin.py:1028`, `kwin.py:1184`). On sway, the wlr
        floor and Hyprland nothing re-syncs the field, so the clear stands --
        which is what `xrandr -q` reads back out of Xwayland, where
        `SetOutputPrimary` PASSed.

        Measured 2026-09-11 through this proxy on a real headless sway (two
        heads, xrandr 1.5.3, scratchpad/b7/live_noprimary.py): `xrandr --output
        HEADLESS-2 --primary` then reads back HEADLESS-2 in BOTH planes
        (`xrandr -q` says `connected primary`, `wxrandr --query` says
        HEADLESS-2), and `xrandr --noprimary` leaves both empty, grabbed and
        `--nograb` alike. With the clear dropped -- the same run with
        `RandrBatch.noprimary` forced False -- `xrandr -q` cleared its flag and
        `wxrandr --query` still printed HEADLESS-2, which is the disagreement
        this closes."""
        if noprimary:
            self._say_kept_primary()
            self.state.primary = None
        for s in stanzas:
            if s.primary and any(t.name == s.name and t.stanza is s
                                 for t in targets):
                self.state.primary = s.name
                return

    def _say_kept_primary(self) -> None:
        """`wxrandr/cli.py:1594-1599`'s two warnings, into the proxy log.

        A compositor that insists on having a primary output is told to drop it
        and keeps it; the line names which output that is, so that the next
        `xrandr -q` -- which reads Xwayland's own copy, now cleared -- is not
        the first anyone hears of the disagreement. The CLI's third guard, `not
        any(s.primary for s in opts.stanzas)`, has no counterpart here: one
        batch carries one `SetOutputPrimary`, and its output id is either 0 or
        an output, never both."""
        have = getattr(self.backend, "primary", None)
        if not have:
            return
        flavor = getattr(self.backend, "flavor", None)
        if flavor is not None:
            self.say("xw11: %s requires a primary output; keeping %s"
                     % (flavor.desktop, have))
        if self.name == "kwin":
            # neither `set_priority` nor `set_primary_output` has an inverse:
            # KWin's output order always has a first entry (`kwin.py:1028`)
            self.say("xw11: KWin keeps a primary output; keeping %s" % have)

    def _record_primary(self, stanzas, fresh, noprimary=False) -> None:
        """What the compositor ended up with, saved -- only for a batch that
        asked for a primary at all.

        `State.primary` is not what we wanted by now: Mutter re-syncs it from
        `GetCurrentState` in the snapshot its `apply` returns
        (`mutter.py:506`) and KWin overwrites it with `kde_output_order_v1`'s
        first entry (`kwin.py:1208`), which is the rule
        `wxrandr/cli.py:1632` keeps too -- the state file records the primary
        the compositor HAS, never one we merely asked for, so that a KWin too
        old for `set_priority` cannot make the next `--query` lie. A backend
        with no primary verb of its own (the wlr floor, sway, Hyprland) leaves
        what `_want_primary` put there, which is the half `xrandr -q` reads back
        out of Xwayland -- `SetOutputPrimary` PASSes and is answered there
        [recon/env.md 2.4] -- and the two planes then agree."""
        if not noprimary and not any(s.primary for s in stanzas):
            return
        still = {o.name for o in fresh or ()}
        if still and self.state.primary not in still:
            self.state.primary = None
        self.state.save()

    def _check_screen(self, outputs, screen) -> None:
        """The framebuffer the client asked for, against the layout that came
        out. Nothing is refused on a mismatch -- xrandr sizes the framebuffer
        from the same arithmetic and a compositor may round differently -- but a
        layout that does not fit what the client thinks the screen is, is the
        thing the log has to have said when someone comes asking."""
        from wxrandr import core
        want_w, want_h = screen[0], screen[1]
        x0, y0, x1, y1 = core.layout_box(outputs)
        if (x1 - x0, y1 - y0) != (want_w, want_h):
            self.say("xw11: the client asked for a %dx%d framebuffer and the "
                     "layout is %dx%d; Wayland has no framebuffer to set, so "
                     "the compositor's own size stands"
                     % (want_w, want_h, x1 - x0, y1 - y0))

    def close(self) -> None:
        if self.backend is not None:
            try:
                self.backend.close()
            except OSError:
                pass
            self.backend = None


# -- the handlers --------------------------------------------------------------


def _batch_for(server, conn):
    """The batch this request belongs to: the open grab's, or a fresh one for a
    client that never grabbed. The snapshot is taken here in the second case --
    at the lone write rather than at a `GrabServer` that never came."""
    if conn.batch is None:
        conn.batch = RandrBatch(snapshot(server, conn), grabbed=False)
    return conn.batch


def _grabbed_elsewhere(server, conn):
    """The OTHER client whose grab is open upstream right now, if there is one.

    A `GrabServer` is forwarded, so while one client holds it the X server
    processes nobody else's requests -- the proxy's own connection included.
    Reading the RandR tables there would block the loop until the 5 s open
    timeout ran out and every other client on the display with it."""
    for other in getattr(server, "conns", ()) or ():
        if other is not conn and other.batch is not None and other.batch.grabbed:
            return other
    return None


#: The snapshot's own deadline, well clear of the ~10 round trips at 70 us it
#: takes [recon/env.md 5.3] and far short of `upstream.OPEN_TIMEOUT`: this read
#: happens on the loop thread at every `GrabServer`, and a wedged upstream may
#: not stop the proxy forwarding for five seconds.
SNAPSHOT_TIMEOUT = 1.0


def snapshot(server, conn=None):
    """The upstream's RandR tables, read on the proxy's OWN connection. None
    when there is no own connection (pass-through, or an upstream that refused
    us), and the commit then fails the batch rather than reporting a success it
    did not have (section 7.5).

    While another client holds the server grab, the tables THAT client read at
    its own `GrabServer` are handed over instead of a read that cannot answer:
    they are one grab old at most, and the ids in them (crtc, output, mode) are
    the same ids a layout change leaves alone."""
    other = _grabbed_elsewhere(server, conn) if conn is not None else None
    if other is not None:
        server.debug_say("another client holds the server grab: this batch "
                         "reads its RandR tables rather than the upstream's")
        return other.batch.resources
    own = server.ensure_upstream() if server is not None else None
    if own is None:
        return None
    try:
        return own.randr_snapshot(timeout=SNAPSHOT_TIMEOUT)
    except Exception as e:                          # the upstream went away
        server.say("xw11: reading the RandR tables failed (%s): this batch "
                   "cannot be applied" % e)
        return None


def grab_server(server, conn, req):
    """`GrabServer` PASSes -- the upstream really is grabbed, which is what an
    X client asked for -- and opens the batch."""
    conn.batch = RandrBatch(snapshot(server, conn), grabbed=True)
    server.debug_say("seq=%d GrabServer: a RandR batch is open" % req.seq)
    return policy.FORWARD


def ungrab_server(server, conn, req):
    """`UngrabServer` PASSes and commits."""
    batch, conn.batch = conn.batch, None
    if batch is not None and batch.grabbed:
        commit(server, conn, batch, req.seq)
    return policy.FORWARD


def set_crtc_config(server, conn, req):
    """Recorded, and answered `status 0` **now**: `XRRSetCrtcConfig` blocks on
    this reply, so deferring it would hang the client inside its own grab and
    the `UngrabServer` that commits would never arrive."""
    decoded = decode_set_crtc_config(req.frame)
    if decoded is None:
        return policy.FORWARD
    batch = _batch_for(server, conn)
    batch.record_crtc(decoded, req.seq)
    reply = wire.reply(req.seq, 0, struct.pack("<I20x", decoded[6]))
    if not batch.grabbed:
        # A batch of one, and its reply is held until the apply returns: X
        # answers `SetCrtcConfig` after the crtc has moved, and a script whose
        # next line reads the geometry is owed the new one (section 7.6).
        commit(server, conn, batch, req.seq, hold=req.seq)
        conn.batch = None
    return reply


def set_screen_size(server, conn, req):
    """Recorded, never applied: Wayland has no framebuffer. It is what a failure
    is attributed to (section 7.5) and what the layout is checked against."""
    got = decode_set_screen_size(req.frame)
    if got is None:
        return policy.FORWARD
    batch = _batch_for(server, conn)
    batch.screen = got
    batch.screen_seq = req.seq
    server.debug_say("seq=%d SetScreenSize %dx%d recorded" % (req.seq, got[0],
                                                              got[1]))
    if not batch.grabbed:
        commit(server, conn, batch, req.seq)
        conn.batch = None
    return None


def set_crtc_transform(server, conn, req):
    """A pure scale becomes the stanza's `scale`; anything else is answered
    `BadValue`, which is also what Xwayland answers for every transform today
    [recon/env.md 2.4] -- so the bytes a client sees do not change, only which
    of them succeed."""
    if len(req.frame) < 8:
        return policy.FORWARD
    (crtc,) = struct.unpack_from("<I", req.frame, 4)
    scale = decode_transform_scale(req.frame)
    if scale is None:
        server.say("xw11: a RandR transform that is not a pure scale is not yet "
                   "applied (crtc 0x%x): no output-management protocol carries "
                   "a free matrix, and the route is a patched compositor "
                   "(AGENTS.md route 6)" % crtc)
        return wire.error(wire.ERR_VALUE, req.seq, 0,
                          server.major_for("RANDR"), SET_CRTC_TRANSFORM)
    batch = _batch_for(server, conn)
    batch.scale[crtc] = scale
    if not batch.grabbed:
        commit(server, conn, batch, req.seq)
        conn.batch = None
    return None


def set_output_primary(server, conn, req):
    """PASSes **and** is recorded: Xwayland accepts it and `xrandr -q` reads the
    flag back from there [recon/env.md 2.4], while `wxrandr` keeps its own in
    `State`. Recording it here is what makes the two agree.

    Outside a grab it is a batch of one like every other write, and for the same
    reason: `xrandr --nograb --output X --primary` sends this request and
    nothing else, and a batch left open here would sit on the connection until
    the client hung up and then be logged as a half-layout that was dropped --
    while `State.primary`, which is only written at commit, never moved."""
    batch = _batch_for(server, conn)
    batch.primary = decode_set_output_primary(req.frame)
    if not batch.grabbed:
        commit(server, conn, batch, req.seq)
        conn.batch = None
    return policy.FORWARD


def set_screen_config(server, conn, req):
    """The RandR 1.1 path: a batch of one, committed where it stands."""
    got = decode_set_screen_config(req.frame)
    if got is None:
        return policy.FORWARD
    batch = RandrBatch(snapshot(server, conn), grabbed=False)
    batch.screen_config = got
    res = batch.resources or Resources()
    fields = struct.pack("<IIIH10x", res.timestamp or 0,
                         res.config_timestamp or 0, root_of(conn), 0)
    # `Failed` (3) in the status byte is how RandR 1.1 refuses -- there is no
    # error packet on this path at all (randr.xml `SetConfig`), and `xrandr -s`
    # turns that byte into `Failed to change the screen configuration!` and
    # exit 1 (the string is in this box's /usr/bin/xrandr 1.5.3, and
    # `XRRSetScreenConfigAndRate`'s return is the byte it tests).
    batch.fail_reply = wire.reply(req.seq, SET_CONFIG_FAILED, fields)
    commit(server, conn, batch, req.seq, hold=req.seq)
    return wire.reply(req.seq, 0, fields)


def root_of(conn) -> int:
    setup = getattr(conn, "setup", None)
    roots = getattr(setup, "roots", None)
    return roots[0] if roots else 0


# -- the commit ----------------------------------------------------------------


def commit(server, conn, batch, seq, hold=None) -> None:
    """One batch, one apply, on a worker thread. `seq` is the sequence of the
    request that closed it, which is the sequence a failure carries (`_fail`).

    The thread is not an optimisation. Mutter's `ApplyMonitorsConfig` waits up
    to five seconds for `MonitorsChanged` [recon/seams.md 6.2], and a proxy that
    blocked its loop for that would stop forwarding for every other client on
    the display. The applying client's own reads are paused instead, so its
    `GetInputFocus` sync after `UngrabServer` [recon/wire.md 6 request 24] is
    answered after the layout changed -- which is what X does, and what makes
    `xrandr` exit only once the screen has moved.
    """
    from wxrandr import core
    batch.commit_seq = seq
    if batch.empty:
        # `--dryrun`'s grab. Nothing, and no line: a log that said "applied
        # nothing" once per --dryrun would be the log lying about its own
        # subject [recon/wire.md 6].
        return
    if hold is not None:
        batch.hold_seq = hold
        conn.hold_reply(hold)
    if batch.resources is None:
        # The own connection timed out, or Xwayland restarted mid-session, or
        # another client's grab was still open when this batch opened and it had
        # no tables of its own to lend. Every `SetCrtcConfig` in the batch has
        # already been answered `status 0` by now, so saying nothing here would
        # leave the client exiting 0 for a layout that was never applied --
        # which is the one outcome design section 7.5 rules out.
        _fail(server, conn, batch, core.Fatal(
            "the upstream's RandR tables could not be read, so the outputs "
            "this layout names cannot be resolved\n"))
        return
    try:
        if batch.screen_config is not None:
            stanzas, customs = screen_config_stanza(batch), {}
        else:
            stanzas, customs = stanzas_for(batch, log=server.say)
    except Exception as e:
        _fail(server, conn, batch, e)
        return
    if not stanzas:
        if batch.crtcs or batch.scale:
            # Every crtc in the batch named an output the snapshot does not
            # have. Nothing to apply, and nothing to pretend about -- the
            # `--noprimary` that may be riding along with it goes down with the
            # rest of the batch, the way a refused transaction does on X.
            _fail(server, conn, batch, core.Fatal(
                "no output this proxy's RandR tables know was named\n"))
            return
        if batch.screen is not None:
            # `xrandr --fb WxH` and nothing else: a framebuffer, with no crtc
            # change under it. On X the screen really grows past its outputs and
            # the pointer pans around it. Not yet here, and the exit code stays
            # xrandr's 0 rather than becoming an error X never gave: the lowest
            # route is rung 1, a scale below 1 on the head (`swaymsg output
            # HEADLESS-1 scale 0.5` -> a 2560x1440 rect, measured 2026-09-10),
            # at the cost of the outputs being scaled rather than a bigger
            # screen sitting behind them; a framebuffer genuinely larger than
            # every output, with panning, is rung 6, a patched compositor.
            server.say("xw11: a %dx%d framebuffer with no output change in the "
                       "same grab (`--fb`) is not yet applied: no "
                       "output-management protocol carries a screen bigger "
                       "than its outputs. The route is a scale below 1 on the "
                       "head (AGENTS.md route 1), which scales the outputs "
                       "instead of leaving a bigger screen behind them, or a "
                       "patched compositor (route 6) for a real panning "
                       "framebuffer" % (batch.screen[0], batch.screen[1]))
        elif not batch.noprimary:
            server.say("xw11: a RandR batch with nothing in it this proxy can "
                       "name an output by: nothing was applied")
        if not batch.noprimary:
            if batch.hold_seq is not None:
                # A reply on hold with nothing left to release it is a hang,
                # which is the one failure mode a proxy may not have.
                # Unreachable today -- only a batch of one holds, and a batch
                # of one always names its own crtc -- which is why it is
                # written down rather than reasoned about.
                conn.release_reply(batch.hold_seq)
            return
        # else it falls through to the apply: `xrandr --noprimary` names no
        # crtc and no output (measured, see `RandrBatch.noprimary`), and
        # `wxrandr --noprimary` is that same empty stanza list through the same
        # `build_targets` (`cli.py:1582`). What it changes is one field, and
        # that field is read while the backend plans.
    server.pause_reads(conn)
    server.run_worker(conn,
                      lambda: _do_apply(server, conn, batch, stanzas, customs))


def _do_apply(server, conn, batch, stanzas, customs=None):
    """The worker thread's body. Returns the callback the loop thread runs.

    Every exception is caught, `Fatal` and the rest alike: this runs on a thread
    the loop cannot see, and one that died silently would leave the applying
    client's reads paused for ever -- a hang instead of an error, which is the
    one failure mode a proxy may not have."""
    started = time.monotonic()
    try:
        targets = server.randr.apply(stanzas, screen=batch.screen,
                                     customs=customs,
                                     noprimary=batch.noprimary)
    except Exception as exc:
        failure = exc
        return lambda: _fail(server, conn, batch, failure)
    took = time.monotonic() - started
    return lambda: _applied(server, conn, batch, stanzas, targets, took)


def _applied(server, conn, batch, stanzas, targets, took) -> None:
    """The loop thread's half of a successful apply: the reply the client has
    been waiting on since before the layout moved, and then the line."""
    if batch.hold_seq is not None:
        conn.release_reply(batch.hold_seq)
    names = ", ".join("%s %s" % (t.name, "on" if t.enabled else "off")
                      for t in targets if t.changed)
    server.say(
        "xw11: applied a RandR batch of %d crtc(s) in %.0f ms through %s (%s)"
        % (len(stanzas), took * 1000.0, server.randr.name or "?",
           names or "no change"))


def _fail(server, conn, batch, exc) -> None:
    """The apply did not happen, and the client is told the way the server tells
    it: an error packet, not a log line and a zero exit (design section 7.5).

    **Which request it names, and which sequence it carries, are two different
    things.** The major and minor are the batch's: `BadMatch` minor 7
    (`RRSetScreenSize`) with `bad` = the root when it carried a `SetScreenSize`
    -- the bytes today's Xwayland answers that request with, measured twice
    (`000815003402000007008b00...`, tests/fixtures/xw11/randr-badmatch.hex, and
    recon/env.md 2.4's five printed lines) -- else `BadValue` minor 21
    (`RRSetCrtcConfig`). So Xlib's default handler prints the same block a real
    server's refusal prints and `xrandr` exits 1, which is what a script that
    checks `$?` is owed.

    The SEQUENCE is the one of the request that CLOSED the batch, not the
    `SetScreenSize`'s. Design section 7.5 said the `SetScreenSize`'s, and that
    is measured wrong: by the time an apply fails, the `SetCrtcConfig` after it
    has already been answered, and a 16-bit sequence that goes BACKWARDS makes
    libxcb widen it by 65536 (`xcb_in.c: read_packet` --
    `if(c->in.request_read < last_read) c->in.request_read += 0x10000`), after
    which every later reply reads as "already completed" and the connection is
    torn down. Measured 2026-09-10 on a live sway: with the SetScreenSize's
    sequence, `xrandr` printed `X connection to :83 broken (explicit kill or
    server shutdown)` instead of the error block and the five lines never
    appeared; with the `UngrabServer`'s, it prints them and exits 1. What the
    printed block loses is one number -- `Serial number of failed request` is
    the grab's last request rather than the SetScreenSize's -- and it is a row
    in the docs.
    """
    text = str(exc).strip() or exc.__class__.__name__
    seq = batch.commit_seq
    if seq is None:
        seq = batch.screen_seq if batch.screen_seq is not None \
            else batch.last_crtc_seq
    if batch.fail_reply is not None:
        pkt = batch.fail_reply
    elif batch.screen_seq is not None:
        pkt = wire.error(wire.ERR_MATCH, seq, root_of(conn),
                         server.major_for("RANDR"), SET_SCREEN_SIZE)
    elif batch.last_crtc_seq is not None:
        pkt = wire.error(wire.ERR_VALUE, seq, 0,
                         server.major_for("RANDR"), SET_CRTC_CONFIG)
    elif batch.primary is not None:
        # A batch that is nothing but a `--primary`. Xwayland took the request
        # itself (it PASSes), so what failed is `wxrandr`'s half; `bad` is the
        # output id, which is the value a `BadValue` is about.
        pkt = wire.error(wire.ERR_VALUE, seq, batch.primary,
                         server.major_for("RANDR"), SET_OUTPUT_PRIMARY)
    else:
        pkt = None
    server.say("xw11: the RandR layout was not applied: %s" % text)
    if pkt is None:
        return
    if batch.hold_seq is not None:
        # The client is still waiting on that request's reply: the refusal takes
        # its place rather than following it, so no sequence is answered twice.
        conn.release_reply(batch.hold_seq, pkt)
    else:
        # After the replies still in flight, never before them: this packet's
        # sequence is the highest the client has sent, and one that arrived
        # first would send libxcb's widening the wrong way.
        conn.write_after_replies(pkt)


def dropped(server, conn) -> None:
    """A client that went away with a batch open. xrandr dying inside Xlib's
    error handler mid-write is the measured case [recon/tools.md 7]: what it
    collected is half a layout -- a disable with no enable -- and applying half
    of it turns a screen off with nothing left to turn it back on. X applied
    each write as it came and so had no half; this proxy has one, and drops it.
    The upstream grab is released by the disconnect itself."""
    batch, conn.batch = conn.batch, None
    if batch is None or batch.empty:
        return
    server.say("xw11: a client left with a RandR batch open (%d crtc(s), "
               "%s): it is dropped, not applied -- half a layout is a screen "
               "turned off with nothing to turn it back on"
               % (len(batch.crtcs),
                  "a screen size" if batch.screen else "no screen size"))


#: `Server.handlers` keys: the core opcode for a core request, `(extension
#: name, minor)` for an extension one -- the extension by NAME, because RANDR's
#: major is the server's own.
HANDLERS = {
    wire.OP_GRAB_SERVER: grab_server,
    wire.OP_UNGRAB_SERVER: ungrab_server,
    ("RANDR", SET_SCREEN_CONFIG): set_screen_config,
    ("RANDR", SET_SCREEN_SIZE): set_screen_size,
    ("RANDR", SET_CRTC_CONFIG): set_crtc_config,
    ("RANDR", SET_CRTC_TRANSFORM): set_crtc_transform,
    ("RANDR", SET_OUTPUT_PRIMARY): set_output_primary,
}


def install(server) -> None:
    server.handlers.update(HANDLERS)
