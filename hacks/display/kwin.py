"""wxrandr KDE backend: kde_output_device_v2 + kde_output_management_v2 over
w11common.wayland_mini.

KWin has no zwlr_output_management and no D-Bus display API (org.kde.KWin
exposes activeOutputName() and nothing else; Plasma 5.27's org.kde.KScreen can
only replay the Meta+P presets). The one usable mechanism is the Wayland
protocol pair from plasma-wayland-protocols -- NOT from the kwin repo --
`kde_output_device_v2` (read) and `kde_output_management_v2` (write). It is
what kscreen-doctor and the System Settings KCM drive through libkscreen, and
it is unauthenticated: no portal, no polkit, no security-context check, so any
client holding the Wayland socket can use it (KWinDisplay::allowInterface
blacklists plasma_window_management, fake_input, screencast, ... and never
kde_output_*).

- Discovery has two shapes. Through Plasma 6.6 every output is a
  `kde_output_device_v2` wl_registry global (find them all -- find_global()
  returns only the first). **Plasma 6.7.0** is where that stopped: kwin commit
  7e32e00c, "wayland: Don't advertise kde-output-device-v2 globals anymore"
  (Vlad Zahorodnii, 2026-03-05, alongside 67f58528 "Implement
  kde-output-device-v2 v21"), first released in v6.7.0 and never backported --
  v6.6.6, cut four months later, still does
  `new OutputDeviceV2Interface(m_display, output)`, while v6.7.0's
  wayland_server.cpp constructs only `m_outputDeviceRegistry` and answers a
  hotplug with `m_outputDeviceRegistry->offer(output)`. From there the devices
  arrive as new_ids on `kde_output_device_registry_v2`, which the compositor
  refuses below v21:

      // OutputDeviceRegistryV2Private::..._bind_resource()
      if (resource->version() < 21) {
          wl_resource_post_error(resource->handle,
                                 error_unsupported_version,
                                 "unsupported version");

  and whose device resources take *the version the registry itself was bound
  at* -- `offer(target->client(), target->version(), ...)` ->
  `wl_resource_create(client, &kde_output_device_v2_interface, version, 0)`,
  the trailing 0 being what puts the id in the server's 0xff000000 range.
  There is no second bind and no separate device version to negotiate. Both
  paths are implemented; past that, the device object behaves identically
  either way, and the whole burst still lands inside the registry bind
  (`d->add(resource)` runs `kde_output_device_v2_bind_resource`, which sends
  geometry .. done synchronously), so one roundtrip publishes everything.
  Measured on KWin 6.7.4 (Ubuntu 26.10): `kde_output_device_v2` is not in
  wl_registry at all there, the registry is advertised at 23 and management at
  21, and query / mode / position / rotate / scale / --off / --primary /
  --same-as and a head plugged and unplugged all behave as on 6.6. An unplug
  on this path is the device's own `removed`, not a global going away.
- Versions move fast. KWin's own `s_version` per release, device / management:
  5.27 = 2/3, 6.0 = 6/7, 6.3 = 11/12, 6.4 and 6.5 = 16/16, 6.6 = 20/19,
  **6.7 = 23/21**, master = 25/22; the registry global shares the device's
  number (23 on 6.7.x, 25 on master). We bind LOW -- device 2 (the
  `name` event, i.e. the connector name, is since 2) and management
  min(advertised, 12) (`set_primary_output` is since 2, `failure_reason` since
  12) -- because a server only sends events whose `since` is <= the bound
  version, so binding low is the compatible choice and every field xrandr
  needs exists at device 2. Optional features are gated on the *bound*
  version, never assumed: on 5.27 a rejection carries no reason string at all.
  The registry is the exception that cannot be bound low: below 21 it is a
  fatal protocol error, so REG_MIN is a floor and not a preference.
- Modes are objects, like wlroots: `mode` is a server-allocated new_id whose
  own events carry size (hardware pixels, pre-transform, pre-scale) and
  refresh (mHz). We read the id off the wire and register a handler for it,
  and remember the object per Mode so apply can name it. There is no
  width/height/refresh form of the request.
- `done` is the atomic publish barrier: a device's fields are only visible to
  the snapshot once its `done` arrives.
- Coordinates are LOGICAL and the position is NOT scaled (output.cpp:637:
  `RectF(logicalPosition, transform.map(QSizeF(modeSize)) / scale)`, rounded
  only in geometry()). The logical size is therefore the transform-swapped
  mode size divided by the scale -- never core.logical_size's wlroots
  truncation -- but *how* that float becomes an integer changed with Plasma
  6: 5.27 rounds (1920 at 1.4 -> 1371, Mutter's layout-mode-1 rule) while
  6.6 takes the enclosing integer (1920 at 1.4 -> 1372, 1280 at 1.5 -> 854),
  both measured against the compositor's own geometry. Getting it wrong
  costs a one-pixel overlap KWin silently keeps, so the rule is gated on the
  advertised management version (>= 7 is Plasma 6).
- Scale is quantised server-side to 1/120 (`std::round(s * 120) / 120`) and a
  scale <= 0 is dropped silently. We quantise on both sides so the round trip
  is stable, and we marshal the wl_fixed ourselves as a plain int: wayland_mini
  `_marshal`'s "f" truncates, and it is shared with the wlroots path where
  changing it would change live sway behaviour (1.3 -> 1.296875).
- Apply is atomic and STRICTLY one-shot: a second `apply` on the same
  configuration object is a fatal `already_applied` protocol error that kills
  the connection, so every attempt builds a fresh configuration. A hotplug
  between create_configuration and apply silently invalidates the object (all
  setters become no-ops and apply answers "One of the relevant outputs is no
  longer available"); that one message is retried once from a fresh snapshot.
- Only deltas are sent. libkscreen compares every field first and skips the
  call entirely when nothing differs, and so do we: a no-op changeset still
  goes through Workspace::applyOutputConfiguration and still causes a modeset.
- KWin validates at apply: every ENABLED output needs x >= 0 and y >= 0
  ("Position of enabled output %1 is negative"), you may not disable every
  output, and an output may not mirror itself. The XML's "no gaps or overlaps"
  sentence is not enforced by the code, so xrandr's gaps and every overlap
  survive -- measured on Plasma 6 at two overlap widths: KWin takes the
  geometry and renders each output as a view onto one shared scene, so the
  shared region comes out byte-identical on both heads. We normalise the
  layout to the origin before sending and refuse the last-output disable
  client-side, with our own xrandr-shaped message; every rejection KWin does
  send is relayed in KWin's name.
- `--same-as` is a shared position for as long as a shared position is the
  mirror, and `set_replication_source` (management request 23, since v13) only
  where it is not. Measured on KWin 6.6: two outputs at one position are
  byte-identical (AE 0) when their pending LOGICAL rectangles coincide -- at
  equal size, and at different refresh rates alike -- because KWin draws every
  output as a view onto one shared scene. When the rectangles differ the
  smaller output shows a *crop* of the bigger one's scene, not a copy of it:
  at 1280x1024 against 1920x1080 it is exactly the top-left crop (AE 0 against
  that crop), at scale 2 it is the top-left quarter magnified, and rotated 90
  it is the leftmost 1080 columns turned on their side. Replication is what
  turns those into a copy: KWin fits the source's whole image into the
  replica's panel, aspect preserved and centred -- scale
  min(dst_w/src_w, dst_h/src_h) * src_scale, offset
  (dst_px - src_px / src_scale * dst_scale) / 2, measured to the pixel in both
  directions (96-row letterbox at 1024x768, first lit column 285 at 1920x1080)
  -- and the requested scale is overridden outright, so the clone comes out
  byte-identical where the sizes allow it. So the rectangles differing is the
  whole trigger and nothing else is: not the refresh rate, not `--same-as`
  itself. The rectangle compared against is the one the SCENE comes from,
  which is not always the output `--same-as` named -- see the chain below.
- A replicated output stops being a layout member: its `wl_output` global goes
  away, it leaves `kde_output_order_v1` (so it can never be the primary --
  set_priority on it is accepted and changes nothing), and it contributes
  nothing to the desktop bounding box. What the pair occupies is the SOURCE's
  rectangle, so that is what --query reports for the replica and what every
  relation measures from: measured with the replica's own panel size instead,
  `--right-of` a 1280x1024 replica of a 1920x1080 output landed the neighbour
  at x=1280, 640 px inside its source. Its own mode and transform still count
  (they are the panel the copy is fitted onto); its own position and scale are
  inert but still stored, and come back the moment the mirror ends. Clearing
  the source (the empty string) restores it completely, and so does disabling
  the source -- which is why the position we send for a replica is the one the
  query reports, the rectangle it comes back to.
- KWin will not copy a copy. A source that is itself replicating is accepted,
  stored and persisted, takes the output out of the layout like any other
  replication -- and is then painted never: measured, the panel kept its last
  frame byte-for-byte across a window move that repainted every other head,
  and two outputs replicating each other left `kde_output_order_v1` with
  neither of them in it. So `--same-as` resolves the source through the chain
  to the output whose scene it really shows and replicates that (a second
  replica letterboxes the same picture, which is the answer the user asked
  for); a chain that comes back to the output itself is nothing to replicate
  (`--same-as` the output that mirrors you is a shared position, and cannot
  build a loop); and a loop somebody else left is refused by name.
- Mirroring an output onto itself is KWin's own `failed` ("An output cannot
  mirror itself"); a uuid naming no enabled output is accepted
  and silently does nothing, so the source uuid always comes from a fresh
  snapshot. The replication source is persisted like everything else, as
  `replicationSource` in ~/.config/kwinoutputconfig.json.
- There is no temporary mode and no confirmation dialog. KWin persists every
  applied configuration itself (Plasma 6: OutputConfigurationStore::storeConfig
  -> ~/.config/kwinoutputconfig.json, marked Source::User; 5.27: kded5 kscreen
  watches the same libkscreen ConfigMonitor and writes
  ~/.local/share/kscreen/<hash>). The 15-second "Keep these settings?"
  countdown lives in the System Settings KCM, not in KWin. So --persistent is
  the only mode there is: we say so once per apply and print the command that
  restores the pre-apply snapshot.

Mapping to the wxrandr model (core.OutputState / core.Target):
    output name          `name` (device v2), else `uuid`
    x, y                 `geometry` x/y == request `position` (logical)
    w, h                 derived: transform-swap(mode) / scale, round-half-away
    make/model/serial    `geometry` make/model + `serial_number`
    mm, subpixel, EDID   `geometry` mm + subpixel, `edid` (base64)
    --primary            set_priority(dev, 1..N) (mgmt v3) -- measured:
                         set_primary_output (mgmt v2) is a no-op on both 5.27
                         and 6.6, so it is sent only as a courtesy alongside.
                         Read back from kde_output_order_v1 (its first entry
                         is the primary, on 5.27 as on 6.6), else the device
                         `priority` event, else the state file
    --same-as            the same position while that is a clone, else
                         set_replication_source (management v13 to send it,
                         device v13 to read it back) -- see below
    --brightness/--gamma no LUT here: zwlr_gamma_control_manager_v1 is probed
                         and, when absent as under KWin, warn + succeed
    --newmode/--addmode  applying one needs a real mode of the same size (and
                         rate) -> `cannot find mode`, as on Mutter; custom
                         modes proper need management v18 + capability 0x2000
"""

import contextlib
import math
import struct
import time

from w11common import session as wsession
from hacks.display import core
from hacks.display.core import Fatal, Mode, OutputState, warn
# Plasma 5.27's logical-size rule is Mutter's layout-mode-1 rule (transform
# swap, then C roundf of px/scale); reuse the tested helper instead of a copy.
from hacks.display.mutter import logical_size as round_logical_size
from hacks.display.mutter import round_half_away

DEV = "kde_output_device_v2"
REG = "kde_output_device_registry_v2"
MGMT = "kde_output_management_v2"
ORDER = "kde_output_order_v1"
GAMMA = "zwlr_gamma_control_manager_v1"

# Bind low: every field xrandr needs exists at device 2, and a server only
# sends events whose `since` is <= the bound version.
DEV_WANT = 13        # `name` (the connector) is since 2 and is the reason to
                     # bind above 1; `replication_source` is since 13 and is the reason to stop at 13. Reading
                     # `priority` would need 18; kde_output_order_v1 gives the primary on 5.27 too, so the
                     # device goes no higher.
MGMT_WANT = 13       # set_primary_output since 2, failure_reason since 12,
                     # set_replication_source since 13
REG_WANT = 25        # the highest kde_output_device_v2 in the XML today
REG_MIN = 21         # binding the registry lower is error_unsupported_version
ORDER_WANT = 1       # kde_output_order_v1 is v1 from 5.27 to master

PRIMARY_MGMT = 2     # set_primary_output (measured: a no-op on 5.27 and 6.6)
PRIORITY_MGMT = 3    # set_priority -- what actually moves the primary
REASON_MGMT = 12     # failure_reason
CEIL_MGMT = 7        # Plasma 6.0+: the logical size is ceil(px/scale)
PRIORITY_DEV = 18    # the readable `priority` event
REPL_MGMT = 13       # set_replication_source
REPL_DEV = 13        # the readable `replication_source` event
CUSTOM_MODES_MGMT = 18   # create_mode_list / set_custom_modes
CAP_CUSTOM_MODES = 0x2000

# Wayland reserves 0xff000000.. for server-allocated object ids.
SERVER_ID_BASE = 0xFF000000

SCALE_STEPS = 120    # kde_output_configuration_v2_scale: round(s * 120) / 120
APPLY_TIMEOUT = 10.0
SETTLE_ROUNDS = 4

# kde_output_configuration_v2 requests we use (opcodes are stable 5.27..master)
REQ_ENABLE, REQ_MODE, REQ_TRANSFORM = 0, 1, 2
REQ_POSITION, REQ_SCALE, REQ_APPLY, REQ_DESTROY = 3, 4, 5, 6
REQ_SET_PRIMARY = 10
REQ_SET_PRIORITY = 11
REQ_SET_REPLICATION = 23

INVALID_HINT = "no longer available"
SAVE_WARNING = ("KWin applies and saves this layout immediately: there is no "
                "temporary mode and no confirmation dialog\n")

SUBPIXEL = {0: "unknown", 1: "none", 2: "horizontal rgb", 3: "horizontal bgr",
            4: "vertical rgb", 5: "vertical bgr"}


# -- pure helpers (unit-tested) ----------------------------------------------

def quantize_scale(scale: float) -> float:
    """KWin's own quantisation: std::round(scale * 120) / 120, matching fractional-scale-v1's 120ths. Applied to
    what we send AND to what we read back, so `--scale 1.3` compares equal to the 1.3 KWin stores (the wl_fixed
    round trip alone would give 1.30078125 and every run would look like a change). std::round, not Python's
    banker's round: 1.4375 * 120 is 172.5, which C rounds to 173 (1.4417) and Python to 172 (1.4333)."""
    return round_half_away(scale * SCALE_STEPS) / SCALE_STEPS


def to_fixed(value: float) -> int:
    """wl_fixed_from_double, rounding. Sent as a plain int arg: wayland_mini's "f" marshaller truncates and is
    shared with the wlroots path, where rounding would change live sway scales -- so it stays untouched."""
    return int(round(value * 256.0))


def from_fixed(raw: float) -> float:
    """A wl_fixed scale as KWin means it: 1/120-quantised."""
    return quantize_scale(raw)


def logical_size(px_w: int, px_h: int, sway_tf: str, scale: float, plasma6: bool = True) -> tuple[int, int]:
    """KWin's logical size: the transform-swapped mode size divided by the scale, made whole the way the running
    KWin does it.

    Plasma 6 takes the enclosing integer -- 1920/1.4 -> 1372, 1080/1.4 -> 772, 1280/1.5 -> 854, 1280/1.30833 ->
    979, all measured against the compositor's own geometry (XWayland's RandR and kscreen-doctor agree) -- while
    5.27 rounds: 1920/1.4 is 1371 there, and 1920/1.3 is 1477 on both, which is what rules truncation out. Being
    one pixel short is not cosmetic: the next output goes one pixel too far left and KWin silently keeps the
    overlap."""
    if not plasma6:
        return round_logical_size(px_w, px_h, sway_tf, scale)
    if core.transform_swaps(sway_tf):
        px_w, px_h = px_h, px_w
    if not scale:
        return (px_w, px_h)
    return (math.ceil(px_w / scale), math.ceil(px_h / scale))


# libkscreen's toKScreenRotation (waylandoutputdevice.cpp) reads the wl_output transform enum as the spec's
# counter-clockwise 90 implies, the same numbering Mutter uses: core.WL_SPEC_RANDR_VIEW is that table, and the
# 90<->270 swap against the sway names the renderer speaks follows from it.
KWIN_RANDR_VIEW = core.WL_SPEC_RANDR_VIEW
KWIN_FROM_SWAY = core.WL_SPEC_FROM_SWAY
to_transform = core.to_wl_spec_transform
from_transform = core.from_wl_spec_transform


def normalise(pos: dict) -> dict:
    """Shift a layout so no enabled output sits at a negative coordinate -- KWin answers `failed` with "Position
    of enabled output %1 is negative" otherwise. core.resolve_positions already anchors at 0,0; this is the
    guard that makes the property hold for every path into apply()."""
    if not pos:
        return pos
    min_x = min(x for x, _y in pos.values())
    min_y = min(y for _x, y in pos.values())
    dx = -min_x if min_x < 0 else 0
    dy = -min_y if min_y < 0 else 0
    if not (dx or dy):
        return pos
    return {n: (x + dx, y + dy) for n, (x, y) in pos.items()}


# KWin's modes are objects with a size and a refresh and nothing else -- no interlace flag -- so every candidate
# here is flagless and the shared matcher's progressive default excludes none of them.
match_mode = core.match_mode


def mirror_needs_replication(replica: tuple, source: tuple) -> bool:
    """Whether `--same-as` needs set_replication_source rather than plainly the same position, from the two
    PENDING logical rectangles.

    On KWin every output is a view onto one shared scene, so two outputs at one position already show identical
    pixels over the region they share -- measured byte-identical (AE 0) whenever the logical rectangles
    coincide, at a different refresh rate as much as at the same one. What a shared position cannot do is make
    the *whole* of one output be the other: where the rectangles differ, the smaller output shows a crop of the
    bigger one's scene (measured: the exact top-left crop at a smaller mode, the top-left quarter magnified at
    scale 2, the leftmost columns turned on their side at a swapped transform). That -- and only that -- is what
    replication is for.

    A mode of a different size, a different scale and an axis-swapping transform all reach here as one fact, the
    pending logical size, because that is the fact the pixels follow: 2560x1440 at scale 2 next to 1280x720 at
    scale 1 is a clone with no replication at all.

    `source` is the rectangle of the output whose scene is being shown, which is not always the one `--same-as`
    named: an output that is itself mirroring shows somebody else's scene, and KWin will not copy a copy (see
    KwinOutputs._root_of)."""
    return tuple(replica) != tuple(source)


def restore_command(outputs, primary: str | None = None, mirrors: dict | None = None) -> str:
    """The xrandr invocation that puts `outputs` back the way they were -- KWin has already saved the new layout
    by the time we could offer an undo, so the pre-apply snapshot is printed as a command the user can paste.

    Every property is spelled out, including the defaults: this line is the only undo there is, and a
    `--rotate`/`--scale` left out because the old value happened to be the default one would leave the *new*
    rotation or scale in place -- which, when nothing else differs, makes the whole command a no-op."""
    parts = []
    for o in outputs:
        parts += ["--output", o.name]
        if not o.active:
            parts.append("--off")
            continue
        if o.current is not None:
            parts += ["--mode", "%dx%d" % (o.current.w, o.current.h)]
            if o.current.refresh_mhz:
                parts += ["--rate", "%.2f" % o.current.refresh_hz]
        # A replicated output is put back with `--same-as`, not `--pos`: its position is inert while it mirrors,
        # and the position alone would restore the layout without the mirror.
        src = (mirrors or {}).get(o.name)
        if src:
            parts += ["--same-as", src]
        else:
            parts += ["--pos", "%dx%d" % (o.x, o.y)]
        rot, refl = core.RANDR_VIEW.get(o.transform, ("normal", "normal"))
        parts += ["--rotate", rot, "--reflect", refl, "--scale", "%g" % o.scale]
        if primary is not None and o.name == primary:
            parts.append("--primary")
    # The word matters: on a KDE image /usr/bin/xrandr exists, so a line beginning "xrandr" is pasted straight
    # into the real one, which answers BadMatch and changes nothing. Name the program that can carry it out --
    # argv[0] when we were invoked under a name that works, else "wxrandr".
    return (_undo_word() + " " + " ".join(parts)) if parts else ""


def _undo_word() -> str:
    """What to call ourselves in a line the user will paste back.

    Always our own name. On a KDE image /usr/bin/xrandr exists, so a line beginning `xrandr` is pasted into the
    real one, which answers BadMatch and changes nothing (measured on Plasma 6.6). argv[0] is no guide either,
    since the tool may be installed over the original or run as a module. The saved layout scripts call bare
    `wxrandr` for exactly the same reason.
    """
    return "wxrandr"


@contextlib.contextmanager
def wire(doing: str):
    """Socket errors as one xrandr line. The receive side was already guarded; the send side is where a
    compositor that hung up between two of our messages surfaces as a bare `[Errno 32] Broken pipe`."""
    try:
        yield
    except (OSError, struct.error) as e:
        raise Fatal("lost the connection to the compositor while %s (%s)\n" % (doing, e))
    except RuntimeError as e:
        raise Fatal("%s\n" % e)


class _Invalidated(Exception):
    """A hotplug landed between create_configuration and apply."""


# -- detection ----------------------------------------------------------------

def probe(sock_path: str | None = None):
    """A live WlConn on the compositor socket when it advertises kde_output_management_v2, else None (the
    connection is closed again on the way out, so a non-KDE session is left with none). Never raises: this runs
    during backend auto-detection."""
    try:
        from w11common.wayland_mini import WlConn
        if sock_path is None:
            hit = wsession.find_wayland_socket()
            if hit is None:
                return None
            sock_path = hit[2]
        conn = WlConn(sock_path)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        conn.sock.settimeout(10.0)
        for iface, _ver in conn.get_registry().values():
            if iface == MGMT:
                return conn
    except (OSError, RuntimeError, ValueError, struct.error):
        pass
    conn.close()
    return None


# -- the backend --------------------------------------------------------------

class KwinOutputs:
    """Snapshot + one-shot atomic apply over kde_output_management_v2."""

    name = "kwin"

    def __init__(self, conn=None, socket_path: str | None = None):
        from w11common.wayland_mini import WlConn
        self._own_conn = conn is None
        if conn is None:
            if socket_path is None:
                hit = wsession.find_wayland_socket()
                if hit is None:
                    raise Fatal("cannot connect to the compositor " "(no wayland socket found)\n")
                socket_path = hit[2]
            conn = WlConn(socket_path)
        self.conn = conn
        try:
            # dispatch() clears the timeout again; _send restores it
            conn.sock.settimeout(10.0)
        except OSError:
            pass
        regs = conn.get_registry()
        mgmt = next(((n, v) for n, (i, v) in sorted(regs.items()) if i == MGMT), None)
        if mgmt is None:
            self.close()
            raise Fatal("compositor does not advertise %s (not a KDE Plasma " "session?)\n" % MGMT)
        self.mgmt_advertised = mgmt[1]
        self.mgmt_version = max(1, min(mgmt[1], MGMT_WANT))
        self.mgmt = conn.bind(mgmt[0], MGMT, self.mgmt_version)
        self.has_gamma = any(i == GAMMA for i, _v in regs.values())
        # Plasma 6 (management 7 and up) takes the enclosing integer for the
        # logical size where 5.27 rounds -- see logical_size()
        self.ceil_logical = self.mgmt_advertised >= CEIL_MGMT
        self.dev_version = 0
        self.registry = None          # kde_output_device_registry_v2 (6.7+)
        self.order = None             # kde_output_order_v1
        self.output_order = []        # connector names, KWin's own order
        self._order_pending = []
        self.primary_readable = False
        self._warned_registry = False
        self.devices = []             # device records, server announce order
        self._by_global = {}
        self._by_id = {}
        self.by_name = {}             # published name -> device record
        self.edid = {}                # name -> base64 EDID
        self.uuid = {}                # name -> KWin's stable uuid
        self.mirror = {}              # name -> the output it replicates
        self.replica_of = {}          # the same, the ones KWin blanks too
        self.previous_mirrors = {}    # the same, pre-apply
        self._warned_blank = set()    # outputs whose mirror KWin never paints
        self.primary = None           # the primary we believe KWin has
        self._primary_seen = False
        self._current = []            # the OutputStates of the last snapshot
        self.previous = []            # pre-apply snapshot, for recovery
        self.previous_primary = None
        self._warned_save = False
        self._discover()

    def close(self):
        if self._own_conn:
            try:
                self.conn.close()
            except OSError:
                pass

    @property
    def can_replicate(self) -> bool:
        """Both halves of mirroring, on the versions they need: `set_replication_source` (management v13) to
        send one, and `replication_source` (device v13) to read one back. Without the read-back a mirror could
        never be cleared again -- the delta against a source we cannot see is always empty, so `--right-of` on a
        replica would be silently inert, which is the one thing this policy is for."""
        return (self.mgmt_version >= REPL_MGMT and self.dev_version >= REPL_DEV)

    # -- discovery -----------------------------------------------------------

    def _discover(self):
        """Bind whatever announces outputs, both shapes. Idempotent: a second
        call picks up hotplugged globals and drops departed ones."""
        regs = self.conn.get_registry()
        if self.order is None:
            ordg = next(((n, v) for n, (i, v) in sorted(regs.items()) if i == ORDER), None)
            if ordg is not None:
                self.order = self.conn.bind(ordg[0], ORDER, min(ordg[1], ORDER_WANT))
                self.conn.on(self.order, self._on_order)
        reg = next(((n, v) for n, (i, v) in sorted(regs.items()) if i == REG), None)
        if reg is not None and self.registry is None:
            name, adv = reg
            ver = min(adv, REG_WANT)
            if ver >= REG_MIN:
                # the device resources this registry hands out are created at the version the REGISTRY was bound
                # at, so this is the device version too -- there is no per-device bind on this path
                self.dev_version = ver
                self.registry = self.conn.bind(name, REG, ver)
                self.conn.on(self.registry, self._on_registry)
            elif not self._warned_registry:
                # A registry we may not bind is only fatal when it is the only way outputs are published.
                # Nothing real advertises it below 21 -- the interface did not exist before then -- but if
                # something did while still exporting the globals, the globals path below still works and saying
                # "no outputs can be listed" would be a lie.
                self._warned_registry = True
                globals_too = any(i == DEV for i, _v in regs.values())
                warn("%s version %d is older than the %d this protocol "
                     "needs; %s\n"
                     % (REG, adv, REG_MIN,
                        "falling back to the per-output %s globals" % DEV
                        if globals_too else "no outputs can be listed"))
        if self.registry is not None:
            return                      # devices arrive as new_ids, not globals
        live = set()
        for name, (iface, adv) in sorted(regs.items()):
            if iface != DEV:
                continue
            live.add(name)
            if name in self._by_global:
                continue
            ver = min(adv, DEV_WANT)
            self.dev_version = ver
            self._add_device(self.conn.bind(name, DEV, ver), gname=name)
        for name in [n for n in self._by_global if n not in live]:
            self._by_global.pop(name)["gone"] = True

    def _on_order(self, op, cur, fds):
        """kde_output_order_v1: one `output(name)` per output, KWin's own order, then `done`; resent whenever
        the order changes. The first entry is what plasmashell and XWayland call the primary, and it is the only
        readable primary on 5.27 (the device `priority` event needs device v18, i.e. Plasma 6.3+). Shapes are
        checked rather than trusted: the XML is not in the kwin tree."""
        if op == 0 and cur.d:
            self._order_pending.append(cur.string())
        elif op == 1 and not cur.d:
            self.output_order = self._order_pending
            self._order_pending = []

    def _on_registry(self, op, cur, fds):
        """kde_output_device_registry_v2's `output` event: one new_id of kde_output_device_v2 per output. The
        interface has exactly two events -- `finished` (opcode 0, no arguments, the answer to `stop`, which we
        never send) and `output` (opcode 1, one new_id) -- so rather than trust one index we accept any event
        whose whole payload is a single server-range new_id. `finished` cannot look like that, and if the
        announcement ever moves opcode we still read it."""
        if len(cur.d) != 4:
            return
        oid = cur.u32()
        if oid < SERVER_ID_BASE or oid in self._by_id:
            return
        self._add_device(oid)

    def _add_device(self, oid: int, gname=None):
        d = {"id": oid, "global": gname, "name": "", "make": "Unknown",
             "model": "Unknown", "serial": "Unknown", "eisa": "", "uuid": "",
             "edid": "", "mm_w": 0, "mm_h": 0, "subpixel": 0, "transform": 0,
             "x": 0, "y": 0, "scale": 1.0, "enabled": False, "current": None,
             "modes": [], "caps": 0, "priority": None, "repl": "",
             "gone": False, "pub": None}
        self._by_id[oid] = d
        if gname is not None:
            self._by_global[gname] = d
        self.devices.append(d)
        self.conn.on(oid, lambda op, cur, fds, d=d: self._on_device(d, op, cur))
        return d

    # -- device events -------------------------------------------------------

    def _on_device(self, d, op, cur):
        if op == 0:      # geometry(x, y, mm_w, mm_h, subpixel, make, model, tf)
            d["x"], d["y"] = cur.i32(), cur.i32()
            d["mm_w"], d["mm_h"] = cur.i32(), cur.i32()
            d["subpixel"] = cur.i32()
            d["make"], d["model"] = cur.string(), cur.string()
            d["transform"] = cur.i32()
        elif op == 1:    # current_mode(object) -- only sent while enabled
            d["current"] = cur.u32()
        elif op == 2:    # mode(new_id): a server-allocated mode object
            mid = cur.u32()
            m = {"id": mid, "w": 0, "h": 0, "refresh": 0, "preferred": False, "gone": False}
            d["modes"].append(m)
            self.conn.on(mid, lambda op, cur, fds, m=m: self._on_mode(m, op, cur))
        elif op == 3:    # done: the atomic publish barrier
            self._publish(d)
        elif op == 4:
            d["scale"] = from_fixed(cur.fixed())
        elif op == 5:
            d["edid"] = cur.string()
        elif op == 6:
            d["enabled"] = bool(cur.i32())
        elif op == 7:
            d["uuid"] = cur.string()
        elif op == 8:
            d["serial"] = cur.string() or "Unknown"
        elif op == 9:
            d["eisa"] = cur.string()
        elif op == 10:
            d["caps"] = cur.u32()
        elif op == 14:   # name(connector) -- since device v2
            d["name"] = cur.string()
        elif op == 27:   # replication_source(uuid) -- since device v13
            d["repl"] = cur.string()
        elif op == 34:   # priority -- since device v18 (Plasma 6.3+),
            d["priority"] = cur.u32()    # globals included; we bind 13
        elif op == 36:   # removed -- since v21
            d["gone"] = True

    @staticmethod
    def _on_mode(m, op, cur):
        if op == 0:
            m["w"], m["h"] = cur.i32(), cur.i32()
        elif op == 1:
            m["refresh"] = cur.i32()     # mHz
        elif op == 2:
            m["preferred"] = True
        elif op == 3:
            m["gone"] = True             # the object is destroyed right after

    @staticmethod
    def _publish(d):
        pub = {k: v for k, v in d.items() if k not in ("pub", "modes")}
        pub["modes"] = [dict(m) for m in d["modes"] if not m["gone"]]
        d["pub"] = pub

    def _settle(self):
        for _ in range(SETTLE_ROUNDS):
            self.conn.roundtrip()
            if all(d["pub"] is not None for d in self.devices if not d["gone"]):
                return

    def _refresh(self):
        """Drain pending events (global add/remove included), bind whatever is
        new, then dispatch until every live device has crossed its `done`."""
        with wire("reading the output list"):
            self.conn.roundtrip()
            self._discover()
            self._settle()

    def _topology(self) -> tuple:
        """The device objects KWin has published. A hotplug moves this -- and
        silently invalidates any configuration built before it."""
        return tuple(sorted(d["id"] for d in self.live()))

    def _topology_moved(self, sig) -> bool:
        try:
            self._refresh()
        except Fatal:
            return False
        return self._topology() != sig

    def _read_primary(self, outs) -> str | None:
        """KWin's own primary, when it is readable: the first entry of kde_output_order_v1 (Workspace's output
        order -- what plasmashell and XWayland follow, and readable on 5.27 too), else the lowest device
        `priority` if we ever bind the device that high."""
        enabled = {o.name for o in outs if o.active}
        for name in self.output_order:
            if name in enabled:
                return name
        rank = sorted((d["pub"]["priority"], i)
                      for i, d in enumerate(self.live())
                      if d["pub"]["priority"] is not None
                      and d["pub"]["name"] in enabled)
        return self.live()[rank[0][1]]["pub"]["name"] if rank else None

    def live(self) -> list:
        return [d for d in self.devices if not d["gone"] and d["pub"]]

    # -- query ---------------------------------------------------------------

    def snapshot(self, state: core.State) -> list:
        """OutputState list in KWin's announce order, built from the published
        (post-`done`) device state."""
        self._refresh()
        if not self.live():
            # management without a single published device. Silently printing an empty screen and calling every
            # apply a success is worse than saying so -- and which of the two discovery paths came up empty is
            # the whole diagnosis, so the message names it. On the registry path this is also where a wrong
            # guess about the 6.7 protocol would land: a `stop`-less registry that announces nothing is
            # indistinguishable from one we are failing to read, so say both.
            if self.registry is not None:
                raise Fatal("%s announced no outputs (this compositor hands "
                            "them out through %s; wxrandr bound it at version "
                            "%d)\n" % (MGMT, REG, self.dev_version))
            raise Fatal("%s is advertised but the compositor announced no " "outputs\n" % MGMT)
        self.by_name, self.edid, self.uuid = {}, {}, {}
        self.mirror, self.replica_of = {}, {}
        outs = []
        for i, d in enumerate(self.live()):
            p = d["pub"]
            name = p["name"] or p["uuid"] or "output-%d" % (i + 1)
            st = OutputState(name=name, active=p["enabled"], ident=i + 1,
                             mm_w=p["mm_w"], mm_h=p["mm_h"],
                             make=p["make"] or "Unknown",
                             model=p["model"] or "Unknown",
                             serial=p["serial"] or "Unknown",
                             subpixel=SUBPIXEL.get(p["subpixel"], "unknown"))
            self.by_name[name] = d
            self.edid[name] = p["edid"]
            self.uuid[name] = p["uuid"]
            st.virtual_modes = not p["modes"]
            current = None
            for m in p["modes"]:
                mode = Mode(w=m["w"], h=m["h"], refresh_mhz=m["refresh"],
                            preferred=m["preferred"], mode_id="%d" % m["id"])
                st.modes.append(mode)
                if p["current"] == m["id"]:
                    current = mode
            if st.active:
                st.x, st.y = p["x"], p["y"]
                st.scale = p["scale"] or 1.0
                st.transform = from_transform(p["transform"])
                st.current = current if current is not None else (st.modes[0] if st.modes else None)
                if st.current is not None:
                    st.w, st.h = logical_size(st.current.w, st.current.h,
                                              st.transform, st.scale,
                                              self.ceil_logical)
            core.finish_modes(st, state.modes_for_output(name))
            outs.append(st)
        # A replicated output is not a layout member on KWin at all -- no wl_output, gone from
        # kde_output_order_v1, nothing of its own in the bounding box -- and its own position and scale are
        # inert while it mirrors. What it shows is the source's rectangle, so that is the rectangle we report
        # for it: the pair then reads as xrandr renders a mirror (one geometry, one position, each output
        # keeping its own mode table and its own starred current mode), and the Screen line agrees with the
        # desktop the compositor actually has.
        #
        # A source has to be a layout output itself for any of that to hold. Measured on KWin 6.6: a source that
        # is itself replicating is accepted and stored, takes the output out of the layout like any other
        # replication -- and then paints nothing at all, so the panel keeps its last frame for ever
        # (byte-identical screendumps across a window move that repainted every other head). We refuse to create
        # one; one somebody else left behind is reported with the geometry KWin stores for it and said out loud,
        # rather than dressed up as a mirror of a rectangle it is not showing.
        by_uuid = {u: n for n, u in self.uuid.items() if u}
        active = {o.name: o for o in outs if o.active}
        src_of = {}
        for o in outs:
            src = by_uuid.get(self.by_name[o.name]["pub"]["repl"] or None)
            # KWin ignores a source that is not enabled, and its own uuid is
            # `failed` ("An output cannot mirror itself") in the first place
            if src is not None and src != o.name and o.name in active and src in active:
                src_of[o.name] = src
        self.replica_of = src_of      # blank ones included: they are still
        for name, src in src_of.items():   # out of the layout, and still a
            if src in src_of:              # loop nothing may be mirrored onto
                self._warn_blank(name, src, src_of[src])
                continue
            self.mirror[name] = src
            o, source = active[name], active[src]
            o.x, o.y, o.w, o.h = source.x, source.y, source.w, source.h
        # The primary comes from the compositor whenever it can be read (kde_output_order_v1, advertised on 5.27
        # as on 6.6) -- the state file is only the record of what we set on a KWin that offers neither that nor
        # the device `priority` event, and it is read once so that a re-snapshot mid-apply cannot make a pending
        # --primary look already done.
        self._current = outs
        live_primary = self._read_primary(outs)
        if live_primary is not None:
            self.primary, self._primary_seen = live_primary, True
            self.primary_readable = True
            state.primary = live_primary
        elif not self._primary_seen:
            self.primary, self._primary_seen = state.primary, True
        return outs

    def _warn_blank(self, name: str, src: str, root: str):
        """A replication KWin accepts, stores and never paints. Said once per output per run: --query is where
        somebody meets the state their System Settings or an older wxrandr left, and a silent listing would show
        a rectangle for a panel that is frozen on its last frame."""
        if name in self._warned_blank:
            return
        self._warned_blank.add(name)
        warn("%s replicates %s, which replicates %s; KWin leaves an output "
             "that copies a copy blank\n" % (name, src, root))

    # -- planning ------------------------------------------------------------

    def resolve_mode(self, t: core.Target, state: core.State) -> Mode:
        """The real mode an enabled target will run: the stanza's, else the current one, else the mode wxrandr
        disabled it at (state file), else the preferred one. A custom (--newmode) mode is only applicable when a
        real mode of the same size and rate exists -- KWin needs a mode object, and creating one needs
        management v18 plus capability_custom_modes, which nothing we bind offers. Its modes carry no interlace
        flag, so that is not part of the match here."""
        return core.resolve_real_mode(t, state, interlace_known=False)

    def supports_custom_modes(self, name: str) -> bool:
        """Custom modes need management v18 (create_mode_list) *and* the device's capability_custom_modes bit.
        We bind management at min(advertised, 12), so this is False everywhere today -- it is the honest form of
        the check, not a guess."""
        d = self.by_name.get(name)
        caps = d["pub"]["caps"] if d and d["pub"] else 0
        return (self.mgmt_version >= CUSTOM_MODES_MGMT and bool(caps & CAP_CUSTOM_MODES))

    def _scale_for(self, t: core.Target) -> float:
        """1/120-quantised, and never <= 0: KWin drops a non-positive scale
        silently, so we keep the current one and say so."""
        if t.scale <= 0:
            cur = t.output.scale if t.output.active else 1.0
            warn("scale %g is not usable on KWin; keeping %g for %s\n" % (t.scale, cur, t.name))
            return quantize_scale(cur)
        return quantize_scale(t.scale)

    def predicted_dims(self, t: core.Target, state: core.State) -> tuple:
        """Pending logical size in KWin's space (the dryrun/verbose plan and
        the --fb checks use this instead of the wlroots prediction)."""
        m = self.resolve_mode(t, state)
        return logical_size(m.w, m.h, t.sway_tf, self._scale_for(t), self.ceil_logical)

    @staticmethod
    def _mode_object(pub, mode: Mode):
        """The device's mode object for `mode`: same size, nearest refresh. Matching by value rather than by the
        remembered object id is what survives a re-snapshot -- the server allocates a fresh id for every mode
        event, so ids from the previous snapshot mean nothing."""
        cands = [m for m in pub["modes"] if (m["w"], m["h"]) == (mode.w, mode.h)]
        if not cands:
            return None
        if mode.refresh_mhz:
            return min(cands, key=lambda m: abs(m["refresh"] - mode.refresh_mhz))["id"]
        return cands[0]["id"]

    @staticmethod
    def _root_of(graph: dict, name: str) -> str | None:
        """The output whose scene `name` ends up showing: itself, or the far end of the chain of mirrors it is
        in. `name` back again means the chain closes on the output itself -- it is already showing its own
        scene, so there is nothing to replicate -- and None means it closes on somebody else, a loop KWin takes
        and leaves blank."""
        seen, cur = {name}, name
        while cur in graph:
            cur = graph[cur]
            if cur in seen:
                return name if cur == name else None
            seen.add(cur)
        return cur

    def _mirror_plan(self, known: list, dims: dict) -> tuple:
        """(outputs to replicate -> their source, outputs to stop replicating, every output that will be showing
        another one's scene).

        The narrow rule, straight off the measurement: `--same-as` is a shared position while a shared position
        IS the clone -- which it is exactly when the two pending logical rectangles coincide -- and
        set_replication_source only when it is not.

        The rectangle compared against is the one the *scene* comes from. An output that is itself mirroring
        shows somebody else's scene, and KWin will not copy a copy: naming one as a source is accepted, stored,
        and then painted never (measured on KWin 6.6 -- the panel keeps its last frame). So `--same-as` follows
        the chain to its root and replicates that, which is also what makes a mirror of a mirror come out right,
        and it can never build a loop: a chain that comes back to the output itself means the output already
        shows that scene, so a shared position is all it takes.

        The replication source is touched only for an output this invocation *positions* (a relation or a
        `--pos`): that is what sets one, and it is also what has to clear one, because a position request on a
        replicating output would otherwise be silently inert. Every other output keeps the mirroring it has, so
        an unrelated invocation never dismantles a mirror somebody else set up."""
        want, drop, asked = {}, set(), {}
        by_name = {t.name: t for t in known}
        for t in known:
            s = t.stanza
            if s is None or not t.enabled:
                continue
            if s.relation is None and s.pos is None:
                continue
            drop.add(t.name)
            if s.relation is not None and s.relation[0] == "same-as":
                src = by_name.get(s.relation[1])
                # a `--same-as` onto a disabled output is xrandr's 0,0 landing,
                # and KWin ignores a source that is not enabled anyway
                if src is not None and src.enabled and src.name != t.name:
                    asked[t.name] = src.name
        # what each output will be showing after this apply: the `--same-as` of this invocation (a shared
        # position shows the source's scene just as much as a replication does), plus the mirrors this
        # invocation leaves alone -- minus the ones whose source it switches off, which KWin hands straight back
        # to themselves.
        graph = dict(asked)
        for name, src in self.replica_of.items():
            replica, source = by_name.get(name), by_name.get(src)
            if name in graph or name in drop:
                continue
            if replica is not None and source is not None and replica.enabled and source.enabled:
                graph[name] = src
        for name in sorted(asked):
            root = self._root_of(graph, name)
            if root is None:
                self._refuse_loop(name, asked[name], graph)
            if root == name:
                continue     # already showing that scene; the position is all
            if mirror_needs_replication(dims[name], dims[root]):
                want[name] = root
                drop.discard(name)
        # everything that will be out of the layout when this is applied -- what we are setting, plus what we
        # are leaving alone. A `--same-as` that stayed a shared position is NOT in it: it keeps a rectangle of
        # its own and is a layout output like any other.
        pending = {n: src for n, src in graph.items() if n not in drop and n in self.replica_of}
        pending.update(want)
        return want, drop, pending

    def _refuse_loop(self, name: str, src: str, graph: dict):
        chain, cur, seen = [src], src, {name, src}
        while cur in graph:
            cur = graph[cur]
            chain.append(cur)
            if cur in seen:
                break
            seen.add(cur)
        raise Fatal("cannot mirror %s onto %s: %s, a loop KWin accepts and "
                    "leaves blank\n" % (name, src, " mirrors ".join(chain)))

    def _refuse_replication(self, name: str, src: str, dims: dict):
        """The one case a KWin without replication cannot do, by name. The management request is what sends a
        mirror and the device event is what reads one back, and without the read-back a mirror could never be
        cleared again (the delta against a source we cannot see is always empty), so both have to be there and
        whichever is missing is the one named."""
        iface, need, has = MGMT, REPL_MGMT, self.mgmt_advertised
        if self.mgmt_version >= REPL_MGMT:
            iface, need, has = DEV, REPL_DEV, self.dev_version
        raise Fatal(
            "cannot mirror %s onto %s: at the same position %s would show a "
            "%dx%d crop of %s's %dx%d, and cloning it needs %s version %d "
            "(this KWin offers %d)\n"
            % (name, src, name, dims[name][0], dims[name][1], src,
               dims[src][0], dims[src][1], iface, need, has))

    def plan(self, state: core.State, targets: list) -> tuple:
        """(per-output delta records, primary device id or None).

        Deltas only, against the published device state: unmentioned outputs and unmentioned properties are left
        alone by KWin, and a changeset that changes nothing still costs a modeset. An output coming back from
        disabled gets the full set -- KWin's stored geometry for a disabled output is not what the query
        showed."""
        known = [t for t in targets if t.name in self.by_name]
        if known and not any(t.enabled for t in known):
            raise Fatal("cannot disable all outputs (KWin requires at least " "one enabled output)\n")
        modes, scales = {}, {}
        for t in known:
            if not t.enabled:
                continue
            modes[t.name] = self.resolve_mode(t, state)
            scales[t.name] = self._scale_for(t)
        by_target = {t.name: t for t in known}
        dims = {n: logical_size(modes[n].w, modes[n].h, by_target[n].sway_tf,
                                scales[n], self.ceil_logical) for n in modes}
        mirrors, unmirror, pending = self._mirror_plan(known, dims)
        if mirrors and not self.can_replicate:
            name, src = sorted(mirrors.items())[0]
            self._refuse_replication(name, src, dims)
        # A replicating output has no rectangle of its own on the desktop -- no wl_output, nothing in the
        # bounding box -- so what a relation has to measure from is the rectangle the pair occupies, which is
        # the source's and is what --query prints for it. Measured on KWin 6.6 with the panel's own size
        # instead: `--right-of` a 1280x1024 replica of a 1920x1080 output landed the neighbour at x=1280, 640 px
        # INSIDE its source, two overlapping panels on a desktop meant to be a row. A copy of a copy is the
        # exception: KWin paints it never, so it is not showing its source's rectangle either and keeps its own.
        layout = dict(dims)
        for name, src in pending.items():
            if src not in pending and name in dims and src in dims:
                layout[name] = dims[src]
        pos = normalise(core.resolve_positions(known, layout))
        records = []
        for t in known:
            d = self.by_name[t.name]
            p = d["pub"]
            rec = {"name": t.name, "dev": d["id"], "enable": None,
                   "mode": None, "transform": None, "position": None,
                   "scale": None, "repl": None}
            if not t.enabled:
                if p["enabled"]:
                    rec["enable"] = False
                    records.append(rec)
                continue
            full = not p["enabled"]
            if full:
                rec["enable"] = True
            obj = self._mode_object(p, modes[t.name])
            if obj is None:
                raise Fatal("cannot find mode %s\n" % modes[t.name].display_name)
            if full or obj != p["current"]:
                rec["mode"] = obj
            tf = to_transform(t.sway_tf)
            if full or tf != p["transform"]:
                rec["transform"] = tf
            src = mirrors.get(t.name)
            if src is not None:
                repl = self.uuid.get(src) or ""
                if not repl:
                    raise Fatal("cannot mirror %s onto %s: KWin reports no "
                                "uuid for %s\n" % (t.name, src, src))
            elif full or t.name in unmirror:
                # an output coming back from disabled is described in full, and
                # a positioned one must not stay silently mirrored
                repl = ""
            else:
                repl = None            # leave the mirroring it has alone
            if repl is not None and repl != p["repl"]:
                rec["repl"] = repl
            # "will KWin ignore this output's position?" -- which is not the same question as "does it carry a
            # replication source": a source that this invocation disables, or that never was enabled, hands the
            # output back to itself, position and all (measured).
            mirroring = t.name in pending
            xy = pos.get(t.name)
            # An output that goes on mirroring ignores its position, so one is sent only when the invocation
            # asked for it: the query reports a replica at its source's position, and re-sending that as a delta
            # would cost a modeset for nothing.
            asked = t.stanza is not None and (t.stanza.relation is not None or t.stanza.pos is not None)
            if (xy is not None and (full or xy != (p["x"], p["y"])) and (asked or not mirroring)):
                rec["position"] = xy
            if full or abs(scales[t.name] - p["scale"]) > 1e-9:
                rec["scale"] = scales[t.name]
            if any(rec[k] is not None for k in ("enable", "mode", "transform", "position", "scale", "repl")):
                records.append(rec)
        if self.primary in mirrors and state.primary in (None, self.primary):
            # measured: the replica leaves kde_output_order_v1, so KWin's primary moves on its own. Say which
            # output is losing it rather than let --query answer a different name next time.
            warn("%s mirrors %s and is no longer the primary output on "
                 "KWin\n" % (self.primary, mirrors[self.primary]))
        primary = None
        want = state.primary
        if want and want != self.primary:
            t = next((t for t in known if t.name == want and t.enabled), None)
            if t is not None and (want in mirrors or (want in self.mirror and want not in unmirror)):
                # measured: a replicated output never enters
                # kde_output_order_v1, so set_priority on it moves nothing
                warn("%s mirrors %s and cannot be the primary output on "
                     "KWin\n" % (want, mirrors.get(want)
                                 or self.mirror.get(want)))
            elif t is not None:
                if self.mgmt_version >= PRIORITY_MGMT:
                    primary = {"name": want,
                               "priority": self._priority_plan(want),
                               "output": (self.by_name[want]["id"]
                                          if self.mgmt_version >= PRIMARY_MGMT
                                          else None)}
                elif self.mgmt_version >= PRIMARY_MGMT:
                    primary = {"name": want, "priority": (), "output": self.by_name[want]["id"]}
                else:
                    warn("this KWin is too old for --primary (%s version "
                         "%d)\n" % (MGMT, self.mgmt_version))
        return records, primary

    def _priority_plan(self, want: str) -> list:
        """set_priority(dev, 1..N) over every live output, `want` first and the others keeping the order KWin
        already has. Priorities are one global sequence, so setting a single output's would leave two outputs
        sharing a rank; libkscreen sends the whole list too. This -- not set_primary_output, which is accepted
        and ignored on both 5.27 and 6.6 -- is what actually moves the primary."""
        names = list(self.by_name)
        ordered = [n for n in self.output_order if n in self.by_name]
        rest = [n for n in ordered + names if n != want]
        seq = [want] + sorted(set(rest), key=rest.index)
        return [(self.by_name[n]["id"], i + 1) for i, n in enumerate(seq)]

    # -- apply ---------------------------------------------------------------

    def _send(self, records: list, primary, sig=None):
        """One configuration object, the deltas, one apply. The object is never reused: a second apply on it is
        a fatal `already_applied` protocol error that takes the whole connection down."""
        cfg = self.conn.alloc()
        result = {}

        def on_config(op, cur, fds):
            if op == 0:
                result["ok"] = True
            elif op == 1:
                result["failed"] = True
            elif op == 2:                      # since management v12
                result["reason"] = cur.string()
        self.conn.on(cfg, on_config)
        with wire("sending the output configuration"):
            self._marshal_configuration(cfg, records, primary)
        deadline = time.monotonic() + APPLY_TIMEOUT
        with wire("applying the output configuration"):
            try:
                while not ("ok" in result or "failed" in result):
                    if time.monotonic() >= deadline:
                        break
                    self.conn.dispatch(timeout=1.0)
            finally:
                # dispatch() leaves the socket blocking; a compositor that
                # goes quiet must not hang the CLI on the post-apply re-read
                try:
                    self.conn.sock.settimeout(10.0)
                except OSError:
                    pass
        try:
            self.conn.send(cfg, REQ_DESTROY, [])
        except OSError:
            pass
        self.conn.handlers.pop(cfg, None)
        if "failed" in result:
            reason = result.get("reason")
            if reason and INVALID_HINT in reason:
                raise _Invalidated(reason)
            if sig is not None and self._topology_moved(sig):
                # Below management 12 there is no failure_reason at all, so on 5.27 the string can never say "no
                # longer available": the outputs having moved under the configuration is the evidence, and it is
                # the same evidence on 6.x.
                raise _Invalidated(reason or "the outputs changed")
            if reason:
                raise Fatal("KWin rejected the output configuration: %s\n" % reason)
            if self.mgmt_version < REASON_MGMT:
                raise Fatal("KWin rejected the output configuration (this "
                            "KWin is too old to report why)\n")
            raise Fatal("KWin rejected the output configuration\n")
        if "ok" not in result:
            raise Fatal("timed out waiting for the compositor to apply the " "output configuration\n")

    def _marshal_configuration(self, cfg, records, primary):
        self.conn.send(self.mgmt, 0, [("u", cfg)])   # create_configuration
        for rec in records:
            dev = rec["dev"]
            if rec["enable"] is not None:
                self.conn.send(cfg, REQ_ENABLE, [("u", dev), ("i", 1 if rec["enable"] else 0)])
            if rec["mode"] is not None:
                self.conn.send(cfg, REQ_MODE, [("u", dev), ("u", rec["mode"])])
            if rec["transform"] is not None:
                self.conn.send(cfg, REQ_TRANSFORM, [("u", dev), ("i", rec["transform"])])
            if rec["position"] is not None:
                x, y = rec["position"]
                self.conn.send(cfg, REQ_POSITION, [("u", dev), ("i", x), ("i", y)])
            if rec["scale"] is not None:
                # the wl_fixed is marshalled here, not by _marshal's "f"
                self.conn.send(cfg, REQ_SCALE, [("u", dev), ("i", to_fixed(rec["scale"]))])
            if rec["repl"] is not None:
                # set_replication_source(outputdevice, uuid) -- management
                # v13; the empty string is how a mirror is turned off
                self.conn.send(cfg, REQ_SET_REPLICATION, [("u", dev), ("s", rec["repl"])])
        if primary is not None:
            if primary["output"] is not None:
                # sent for the courtesy of a KWin that honours it; measured
                # to be a no-op on 5.27 and on 6.6, hence set_priority below
                self.conn.send(cfg, REQ_SET_PRIMARY, [("u", primary["output"])])
            for dev, rank in primary["priority"]:
                self.conn.send(cfg, REQ_SET_PRIORITY, [("u", dev), ("u", rank)])
        self.conn.send(cfg, REQ_APPLY, [])

    def _warn_saved(self):
        """Said once, and only once KWin really has saved something: the line below tells the user their layout
        is already on disk, and the restore command it prints *changes* the live configuration."""
        if self._warned_save:
            return
        self._warned_save = True
        warn(SAVE_WARNING)
        cmd = restore_command(self.previous, self.previous_primary, self.previous_mirrors)
        if cmd:
            warn("to restore the previous layout: %s\n" % cmd)

    def _rebind(self, targets: list, outputs: list) -> list:
        """Re-point targets at a fresh snapshot after a hotplug: the device
        and mode objects of the old one are gone."""
        by_name = {o.name: o for o in outputs}
        out = []
        for t in targets:
            fresh = by_name.get(t.name)
            if fresh is None:
                continue        # the output the plan named is no longer there
            t.output = fresh
            if t.mode is not None:
                twin = match_mode(fresh.modes, t.mode.w, t.mode.h, t.mode.refresh_hz or None)
                if twin is not None:
                    t.mode = twin
            out.append(t)
        return out

    def verify(self, state: core.State, targets: list):
        """--dryrun: KWin has no verify request (and building a configuration without applying it changes
        nothing), so this only re-runs the plan -- client-side rejections still surface, the compositor is not
        touched."""
        self.plan(state, targets)

    def apply(self, state: core.State, targets: list, persistent: bool = False) -> list:
        """One atomic configuration, applied once. Returns the fresh snapshot. `persistent` is accepted for
        contract parity and ignored: KWin persists every applied layout itself."""
        want = state.primary
        records, primary = self.plan(state, targets)
        if records or primary is not None:
            self.previous = list(self._current)
            self.previous_primary = self.primary
            self.previous_mirrors = dict(self.mirror)
            try:
                self._send(records, primary, self._topology())
            except _Invalidated:
                # a hotplug between create_configuration and apply silently
                # invalidated the object: rebuild from a fresh snapshot, once
                targets = self._rebind(targets, self.snapshot(state))
                # after the re-snapshot, not before: snapshot() overwrites state.primary with what
                # kde_output_order_v1 still reports, which would eat a --primary that has not been applied yet
                state.primary = want
                records, primary = self.plan(state, targets)
                if records or primary is not None:
                    self._send(records, primary, self._topology())
            if primary is not None:
                self.primary = primary["name"]
            # only now: KWin has applied and saved something
            self._warn_saved()
            core.record_lastmodes(state, targets)
        outs = self.snapshot(state)
        # the state file records the primary KWin has, never one we merely wanted: --primary on a compositor too
        # old to take it, or on an output that is not being enabled, must not make --query lie
        state.primary = self.primary
        return outs
