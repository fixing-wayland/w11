"""wxrandr GNOME backend: org.gnome.Mutter.DisplayConfig over w11common.dbus_mini.

Mutter (gnome-shell's compositor) has no zwlr_output_management; its display
API is the session-bus object /org/gnome/Mutter/DisplayConfig, callable by
any client on the user bus — no extension, no root:

- GetCurrentState() -> (serial, monitors, logical_monitors, properties). One
  monitor per connected connector (== the xrandr output; disabled ones
  included) with vendor/product/serial, mm sizes and its mode list — opaque
  mode ids ("1920x1080@60.000", "1920x1080i@59.940"), refresh as a double,
  is-current/is-preferred/is-interlaced flags and the per-mode list of
  supported scales. One logical monitor per enabled screen region: (x, y,
  scale, transform 0..7 (wl_output numbering; see MUTTER_RANDR_VIEW), primary,
  member monitors — several members == mirror). Sizes are never sent: the
  logical size is the mode size, swapped for 90/270, then in layout-mode 1
  (logical) roundf(px / scale) and in layout-mode 2 (physical — the GNOME 46
  default without "Fractional Scaling") the raw pixels. Read every time.
- ApplyMonitorsConfig(serial, method, logical_monitors, properties): the
  WHOLE layout in one call — atomic, exactly xrandr's model. method 0
  verifies only, 1 applies temporarily (xrandr semantics, no dialog), 2 also
  writes ~/.config/monitors.xml, after which gnome-shell asks "Keep changes?"
  for 20 s. Every connected monitor not listed is disabled. Mutter validates:
  exactly one primary, no overlap, every logical monitor edge-adjacent to
  another (a hole is "not adjacent"), min x = min y = 0, scale exactly one of
  the mode's supported scales, mirror members with identical modes, only mode
  ids it handed out (no custom modes). Rejections come back as D-Bus errors
  whose text is relayed verbatim after `xrandr: GNOME's Mutter refused this
  layout: ` -- Mutter's words, said in Mutter's name, because we never refuse
  a layout ourselves. Measured: with two monitors an overlap never produces
  "Logical monitors overlap"; adjacency is checked first and every layout
  that is not exactly edge-adjacent, overlap and gap alike, comes back
  "Logical monitors not adjacent". Nothing is half-applied.
- MonitorsChanged fires after a successful apply; the serial bumps on every
  change and a stale serial is AccessDenied: the state is re-read and the
  same plan re-sent once, but only when the monitors and layout it was built
  from are still what Mutter has (a serial bump alone); a hotplug or a
  concurrent re-layout in that window is "cancelled by a concurrent change".
- Holes: X tolerates a gap, Mutter does not, so an output that touched a
  neighbour's right/bottom edge keeps touching it when that edge moves
  (`--output A --rotate left` / `--mode SMALLER` / `-s` / `-o` in the middle of
  a row shift the outputs to its right along, one warning each) unless it
  was positioned explicitly in the same invocation (keep_adjacent). An
  output whose neighbour went `--off` is not moved: Mutter's own
  "not adjacent" is the answer, re-place it in the same call.
- GNOME 50 quirk (verified): after a temporary re-primary the old logical
  monitor keeps `primary=true` in GetCurrentState until it is rebuilt, so
  several may be flagged; the legacy GetResources output property
  `primary` tracks the real one (what XWayland and the shell use) and
  breaks the tie. The serial does not bump on temporary applies there.

Mapping to the wxrandr model (core.OutputState / core.Target):
    output name          connector          enabled  in some logical monitor
    x, y                 logical monitor x/y (its coordinate space)
    w, h                 derived (logical_size)   scale/transform  per region
    --same-as            one logical monitor with several members
    --primary            the primary flag (real; state.primary is synced)
    --brightness/--gamma no LUT API: warn + succeed
    --newmode/--addmode  state file as elsewhere; applying one needs a real
                         mode with the same size (and rate) -> `cannot find mode`
"""

import collections
import struct

from w11common import session as wsession
from w11common.dbus_mini import Bus, DBusError, Variant
from hacks.display import core, gnome_overlap, monitors_xml
from hacks.display.core import Fatal, Mode, OutputState, round_half_away, warn  # noqa: F401

class Flavor(collections.namedtuple(
        "Flavor", "name dest path iface desktop compositor config_name keep_dialog")):
    """One compositor speaking this D-Bus interface: the three names it answers on, and the four words the
    messages need. Muffin is Mutter's fork under Cinnamon's own bus name -- `GetCurrentState` and
    `ApplyMonitorsConfig` are byte-for-byte the signatures below, and a real mode change applied and read
    back with only the three names swapped [M recon2/cinnamon.md §2.2, §4; displayconfig-introspect.txt] --
    so it is a flavour of this backend and not a backend of its own.

    Every string that names GNOME or Mutter to a user is a field here, because a Cinnamon user reading
    "GNOME's Mutter refused this layout" about their own session has been told something false about which
    program said no."""

    __slots__ = ()

    @property
    def match(self) -> str:
        return "type='signal',interface='%s',member='MonitorsChanged'" % self.iface

    @property
    def persist_warning(self) -> str:
        """The confirmation dialog, by the words it puts on the screen. Cinnamon's is its own string --
        `usr/share/cinnamon/js/ui/windowManager.js:77 _("Keep these display settings?")`, with `Keep changes`
        on the button at :90 -- and the same 20-second `complete_display_change(false)` shape as GNOME's
        [M recon2/cinnamon.md §2.2]."""
        return ('%s will ask "%s" for 20 s; confirm the dialog or the layout reverts\n'
                % (self.desktop, self.keep_dialog))


MUTTER = Flavor(name="mutter", dest="org.gnome.Mutter.DisplayConfig",
                path="/org/gnome/Mutter/DisplayConfig",
                iface="org.gnome.Mutter.DisplayConfig",
                desktop="GNOME", compositor="Mutter",
                config_name=monitors_xml.NAME, keep_dialog="Keep changes?")
#: Cinnamon's copy. The saved file is `~/.config/cinnamon-monitors.xml` (the string in
#: libmuffin.so.0.0.0), and the validator, error strings and all, is Mutter's own: `Logical monitors not
#: adjacent`, `Logical monitors overlap`, `Logical monitor scales must be identical`, `Config contains
#: multiple primary logical monitors` [M recon2/cinnamon.md §2.2].
MUFFIN = Flavor(name="cinnamon", dest="org.cinnamon.Muffin.DisplayConfig",
                path="/org/cinnamon/Muffin/DisplayConfig",
                iface="org.cinnamon.Muffin.DisplayConfig",
                desktop="Cinnamon", compositor="Muffin",
                config_name="cinnamon-monitors.xml",
                keep_dialog="Keep these display settings?")

#: the default flavour's names, kept as module constants because wxrandr/cli.py's
#: `_probe_mutter()` and the tests read them
DEST = MUTTER.dest
PATH = MUTTER.path
IFACE = MUTTER.iface
APPLY_SIG = "uua(iiduba(ssa{sv}))a{sv}"
VERIFY, TEMPORARY, PERSISTENT = 0, 1, 2
LAYOUT_LOGICAL, LAYOUT_PHYSICAL = 1, 2
MONITORS_CHANGED_TIMEOUT = 5.0
CANCELLED = ("output configuration cancelled by a concurrent change; " "try again\n")
_MATCH = MUTTER.match
PERSIST_WARNING = MUTTER.persist_warning
BACKUP_NOTE = "the saved display configuration as it was is kept in %s\n"
# Measured on 24.04/GNOME 46: with Fractional Scaling off the session is in physical
# layout mode, Mutter writes the file with no <layoutmode> at all, and the positions in
# it are re-read in whatever layout mode the session has *then*.  Turn the setting on
# and a scale-2 output is half as wide as the gap its neighbour was saved at --
# "Logical monitors not adjacent" -- and the whole file goes, every other
# saved monitor set with it.  The layout is valid, Mutter accepted it, Mutter wrote it;
# it is the meaning of the numbers that changed.  Nothing on either side can prevent
# that, so the answer is to say so at the one moment the user is choosing to save.
NOTHING_TO_DO = ("--unsafe-gnome-overlap: the monitors are already where this asks "
                 "for them; nothing was written\n")
LAYOUT_MODE_WARNING = (
    "saved as physical-pixel positions (this session has Fractional Scaling off): if "
    "it is ever turned on, a scaled output changes width, this layout stops being "
    "adjacent and %s then refuses the whole saved file, every other layout in it "
    "too\n")


# -- pure helpers (unit-tested) ----------------------------------------------

def logical_size(px_w: int, px_h: int, sway_tf: str, scale: float,
                 layout_mode: int = LAYOUT_LOGICAL) -> tuple[int, int]:
    """Mutter's logical monitor size: transform swap, then roundf(px/scale) in layout-mode 1; raw pixels in
    layout-mode 2 (scale is a pure UI factor there). Differs from core.logical_size (wlroots truncates)."""
    if core.transform_swaps(sway_tf):
        px_w, px_h = px_h, px_w
    if layout_mode == LAYOUT_PHYSICAL or not scale:
        return (px_w, px_h)
    return (round_half_away(px_w / scale), round_half_away(px_h / scale))


# Mutter numbers transforms exactly as the wl_output spec does, so the measured table lives in core
# (WL_SPEC_RANDR_VIEW) next to the sway one it is a permutation of. These are this module's names for it.
MUTTER_RANDR_VIEW = core.WL_SPEC_RANDR_VIEW
MUTTER_FROM_SWAY = core.WL_SPEC_FROM_SWAY
to_transform = core.to_wl_spec_transform
from_transform = core.from_wl_spec_transform


def snap_scale(scale: float, supported) -> float:
    """Nearest of the mode's supported scales (ties -> the smaller one);
    Mutter accepts nothing else. Empty list -> 1.0."""
    supported = [float(s) for s in supported or ()]
    if not supported:
        return 1.0
    return min(supported, key=lambda s: (abs(s - scale), s))


# Mutter's mode list carries the interlace flag, so core.match_mode's
# default (progressive unless asked) is the right one here.
match_mode = core.match_mode


def _mode_from_wire(mid: str, w: int, h: int, rate: float, mp: dict) -> Mode:
    interlaced = bool(mp.get("is-interlaced", False))
    return Mode(w=w, h=h, refresh_mhz=int(round(rate * 1000)),
                preferred=bool(mp.get("is-preferred", False)),
                name=("%dx%di" % (w, h)) if interlaced else None,
                flags=("interlace",) if interlaced else (),
                mode_id=mid)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _text(e: DBusError) -> str:
    return (e.message or e.name) + "\n"


def _not_ok_why(reply) -> str:
    """' (struct-size: 88 bytes)' for a reply that named a check, and the empty
    string for the one that did not.

    The shape with no `check` and no `reason` is the one extension.js produces
    when the monitors.xml digest moved across an apply that went in, and
    `gnome_overlap.refusal_text()` renders it "refused (?): no reason given" --
    a sentence about a layout that is on screen.  So the caller composes its
    own line and this only appends what the reply actually carried."""
    check = (reply.get("check") or "").strip()
    reason = (reply.get("reason") or "").strip()
    if check and reason:
        return " (%s: %s)" % (check, reason)
    return (" (%s)" % (check or reason)) if (check or reason) else ""


def _refused(e: DBusError, flavor: Flavor = MUTTER) -> str:
    """A rejected ApplyMonitorsConfig, in the compositor's name.  We pass every layout on unchanged --
    overlaps included, which X11, KWin and wlroots all take -- so when one comes back refused the limit is the
    compositor's, and the line has to say so before quoting its own words (a two-monitor overlap gets "Logical
    monitors not adjacent", the same sentence a gap gets).

    Mutter's own sentence does not say what to do about it, and the usual cause -- `--output MIDDLE --off`,
    which leaves the row with a hole -- has one obvious answer, so adjacency refusals carry it.

    Muffin is where the name matters: it carries Mutter's validator with Mutter's strings, word for word
    [M recon2/cinnamon.md §2.2], so the only thing that tells a Cinnamon user which program refused them is
    this prefix."""
    line = "%s's %s refused this layout: " % (flavor.desktop, flavor.compositor) + _text(e)
    if "adjacent" in (e.message or "") or "overlap" in (e.message or ""):
        line = line.rstrip("\n") + (
            " (%s allows neither a gap nor an overlap between outputs; "
            "re-place the neighbours in the same command)\n" % flavor.desktop)
    return line


def _is_stale(e: DBusError) -> bool:
    return e.name.endswith("AccessDenied") and "stale" in (e.message or "").lower()


def _canon(plan) -> list:
    """Order-free form of a logical-monitor plan, for change detection."""
    return sorted((p["x"], p["y"], float(p["scale"]), int(p["transform"]),
                   bool(p["primary"]),
                   tuple(sorted((c, mid, bool(us)) for c, mid, us in p["members"])))
                  for p in plan)


def _positioned(t: core.Target) -> bool:
    """The invocation says where this output goes (--pos or a relation)."""
    s = t.stanza
    return s is not None and (s.pos is not None or s.relation is not None)


def keep_adjacent(targets: list, dims: dict, pos: dict) -> list:
    """Mutter allows no gaps (X does): an output that shared its left (top) edge with a neighbour's right
    (bottom) edge before this invocation keeps touching it when that edge moves because the neighbour changed
    mode, rotation or scale (or followed another one itself). Explicit positions are the user's: an output
    placed here (--pos / --left-of ...) never moves, and nothing follows one — its old neighbours may no longer
    be neighbours at all — so Mutter's verdict on such a layout stands. Neighbours that went --off pull nothing
    either (nothing to stay adjacent to; Mutter reports the hole). Mutates `pos` (every enabled output, min x =
    min y = 0 as core.resolve_positions leaves it) and returns [(name, (x, y), neighbour)] for each output it
    moved, in move order."""
    old = {t.name: (t.output.x, t.output.y, t.output.w, t.output.h) for t in targets if t.output.active}
    fixed = {t.name for t in targets if _positioned(t)}
    movable = [t.name for t in targets
               if t.enabled and t.name in old and t.name in pos
               and t.name not in fixed]
    moves = {}
    for _ in range(len(movable) + 1):
        changed = False
        for n in movable:
            ox, oy, ow, oh = old[n]
            x, y = pos[n]
            # neighbours still enabled whose old right (bottom) edge was n's old left (top) edge with strict
            # overlap; n goes to the farthest of their new edges (touch one, overlap none)
            lefts = [(pos[m][0] + dims[m][0], m)
                     for m, (mx, my, mw, mh) in old.items()
                     if m != n and m in pos and m not in fixed
                     and mx + mw == ox and my < oy + oh and oy < my + mh]
            tops = [(pos[m][1] + dims[m][1], m)
                    for m, (mx, my, mw, mh) in old.items()
                    if m != n and m in pos and m not in fixed
                    and my + mh == oy and mx < ox + ow and ox < mx + mw]
            nx, via_x = max(lefts) if lefts else (x, None)
            ny, via_y = max(tops) if tops else (y, None)
            if (nx, ny) != (x, y):
                pos[n] = (nx, ny)
                moves[n] = (pos[n], via_x if nx != x else via_y)
                changed = True
        if not changed:
            break
    if moves:
        min_x = min(p[0] for p in pos.values())
        min_y = min(p[1] for p in pos.values())
        if min_x or min_y:
            for n, (x, y) in list(pos.items()):
                pos[n] = (x - min_x, y - min_y)
            for n in moves:
                moves[n] = (pos[n], moves[n][1])
    return [(n, p, via) for n, (p, via) in moves.items()]


# -- detection ----------------------------------------------------------------

def probe(addr: str | None = None, flavor: Flavor = MUTTER):
    """A Bus on the graphical session's D-Bus if this flavour's DisplayConfig is
    there, else None. Never raises (backend auto-detection)."""
    try:
        if addr is None:
            hit = wsession.find_session_bus()
            if not hit:
                return None
            addr = hit[1]
        bus = Bus(addr)
    except (DBusError, OSError, ValueError):
        return None
    try:
        if bus.name_has_owner(flavor.dest):
            return bus
    except DBusError:
        pass
    bus.close()
    return None


# -- wl_output enrichment -----------------------------------------------------

_SUBPIXEL = {0: "unknown", 1: "none", 2: "horizontal rgb", 3: "horizontal bgr",
             4: "vertical rgb", 5: "vertical bgr"}


def wl_output_info(sock_path: str | None = None) -> dict:
    """{connector: {"mm_w", "mm_h", "subpixel", "make", "model"}} from the compositor's wl_output globals (v4:
    the `name` event is the connector). Mutter 46 and 50 never put width-mm/height-mm into GetCurrentState (the
    XML documents them, the code does not emit them — verified with gdbus) but hands the EDID size to wl_output,
    which is what XWayland's RandR shows; reading it keeps the header/--listmonitors mm byte-identical. Never
    raises: {} when there is no reachable Wayland socket."""
    try:
        from w11common.wayland_mini import WlConn
        if sock_path is None:
            hit = wsession.find_wayland_socket()
            if hit is None:
                return {}
            sock_path = hit[2]
        conn = WlConn(sock_path)
    except (OSError, RuntimeError, ValueError):
        return {}
    try:
        conn.sock.settimeout(5.0)
        outs = []
        for gname, (iface, ver) in list(conn.get_registry().items()):
            if iface != "wl_output" or ver < 4:
                continue
            o = {"name": "", "mm_w": 0, "mm_h": 0, "subpixel": "unknown", "make": "", "model": ""}

            def handler(op, cur, fds, o=o):
                if op == 0:  # geometry(x, y, mm_w, mm_h, subpixel, make, model, tf)
                    cur.i32()
                    cur.i32()
                    o["mm_w"], o["mm_h"] = cur.i32(), cur.i32()
                    o["subpixel"] = _SUBPIXEL.get(cur.i32(), "unknown")
                    o["make"], o["model"] = cur.string(), cur.string()
                elif op == 4:  # name(connector)
                    o["name"] = cur.string()
            conn.on(conn.bind(gname, "wl_output", 4), handler)
            outs.append(o)
        conn.roundtrip()
        return {o["name"]: o for o in outs if o["name"]}
    except (OSError, RuntimeError, ValueError, struct.error):
        return {}
    finally:
        conn.close()


# -- the backend --------------------------------------------------------------

class MutterOutputs:
    """Snapshot + one-call atomic apply over org.gnome.Mutter.DisplayConfig, or Muffin's copy of it.

    `flavor` is the whole difference between GNOME and Cinnamon here: the three names, the file the confirmed
    `--persistent` writes, and the words the messages use. Measured against a nested muffin 6.4.1 with only
    the names swapped -- `--query`, `--listmonitors`, and an `--output LVDS1 --mode 1024x768` that really
    changed the mode and read back changed [M recon2/cinnamon.md §4]."""

    name = "mutter"

    def __init__(self, bus: Bus | None = None, addr: str | None = None, wl_socket=None,
                 flavor: Flavor = MUTTER):
        """`wl_socket`: Wayland socket path for the wl_output enrichment
        (None = the session's, False = none)."""
        self.wl_socket = wl_socket
        self.flavor = flavor
        # shadows the class attribute: `--listproviders` prints it as the compositor's
        # name, and on Cinnamon that is the backend token the user typed, not "mutter"
        self.name = flavor.name
        if bus is None:
            if addr is None:
                hit = wsession.find_session_bus()
                if not hit:
                    raise DBusError("org.freedesktop.DBus.Error.NoServer", "no session D-Bus found")
                addr = hit[1]
            bus = Bus(addr)
        self.bus = bus
        try:
            owned = self.bus.name_has_owner(flavor.dest)
        except DBusError:
            self.bus.close()
            raise
        if not owned:
            self.bus.close()
            raise Fatal("%s is not on the session bus (not a %s session?)\n"
                        % (flavor.dest, flavor.desktop))
        self.serial = 0
        self.fingerprint = None      # monitors + layout the serial stood for
        self.props = {}
        self.layout_mode = LAYOUT_LOGICAL
        self.global_scale_required = False
        self.primary = None          # connector of the primary logical monitor
        self.scales = {}             # (connector, mode_id) -> supported scales
        self.underscan = {}          # connector -> currently underscanning
        self.current_config = []     # _canon() of what Mutter shows now
        self.last_method = None      # method of the last ApplyMonitorsConfig
        self._matched = False

    def close(self):
        self.bus.close()

    # -- query ---------------------------------------------------------------

    def get_current_state(self):
        try:
            return self.bus.call(self.flavor.dest, self.flavor.path, self.flavor.iface,
                                 "GetCurrentState")
        except DBusError as e:
            raise Fatal(_text(e))

    def snapshot(self, state: core.State) -> list:
        """OutputState list in Mutter's monitor order; records serial, layout mode, supported scales, the real
        primary (synced into state.primary — the state file never overrides Mutter here)."""
        serial, monitors, logical, props = self.get_current_state()
        wl = {} if self.wl_socket is False else wl_output_info(self.wl_socket or None)
        self.serial = serial
        self.fingerprint = self._fingerprint(monitors, logical)
        self.props = props
        self.layout_mode = _int(props.get("layout-mode")) or LAYOUT_LOGICAL
        self.global_scale_required = bool(props.get("global-scale-required", False))
        lm_of = {}
        for lm in logical:
            for spec in lm[5]:
                lm_of[spec[0]] = lm
        primary_lm = self._primary_lm(logical, lm_of)
        # Which CONNECTOR is primary, out of the primary logical monitor's members.  A mirror group has several
        # and Mutter names none of them -- the flag is on the group -- so its member order decides, and that
        # order is the order the group was built in, not a choice anybody made.  Mirroring A onto B therefore
        # used to move the primary to whichever came first, silently overwriting a --primary the user had set on
        # the other member.  Keep the user's choice whenever it is still in the group.
        if primary_lm is None:
            self.primary = None
        else:
            members = [spec[0] for spec in primary_lm[5]]
            self.primary = (state.primary if state.primary in members else members[0])
        self.scales = {}
        self.underscan = {}
        current_ids = {}
        outs = []
        for i, (spec, modes, mprops) in enumerate(monitors):
            connector, vendor, product, mserial = spec
            st = OutputState(name=connector, active=connector in lm_of,
                             ident=i + 1,
                             mm_w=_int(mprops.get("width-mm")),
                             mm_h=_int(mprops.get("height-mm")),
                             make=vendor or "Unknown",
                             model=product or "Unknown",
                             serial=mserial or "Unknown")
            self.underscan[connector] = bool(mprops.get("is-underscanning", False))
            w_info = wl.get(connector)
            if w_info:
                st.mm_w = st.mm_w or w_info["mm_w"]
                st.mm_h = st.mm_h or w_info["mm_h"]
                st.subpixel = w_info["subpixel"]
            current = None
            for (mid, w, h, rate, _pscale, scales, mp) in modes:
                m = _mode_from_wire(mid, w, h, rate, mp)
                self.scales[(connector, mid)] = ([float(s) for s in scales] or [1.0])
                st.modes.append(m)
                if mp.get("is-current"):
                    current = m
            if st.active:
                x, y, scale, transform, primary, _specs, _lp = lm_of[connector]
                st.x, st.y, st.scale = x, y, float(scale)
                st.transform = from_transform(transform)
                st.current = current if current is not None else (st.modes[0] if st.modes else None)
                if st.current is not None:
                    st.w, st.h = logical_size(st.current.w, st.current.h,
                                              st.transform, st.scale,
                                              self.layout_mode)
                    current_ids[connector] = st.current.mode_id
                st.primary = lm_of[connector] is primary_lm
            core.finish_modes(st, state.modes_for_output(connector))
            outs.append(st)
        self.current_config = _canon([
            {"x": lm[0], "y": lm[1], "scale": lm[2], "transform": lm[3],
             "primary": lm is primary_lm,
             "members": [(s[0], current_ids.get(s[0], ""),
                          self.underscan.get(s[0], False)) for s in lm[5]]}
            for lm in logical])
        state.primary = self.primary
        return outs

    @staticmethod
    def _fingerprint(monitors, logical) -> tuple:
        """What a plan was built from: the connectors with their mode ids and current mode, and the logical
        layout (order-free). Equal fingerprints under different serials mean nothing that matters to the plan
        changed (GNOME bumps the serial on its own as well)."""
        mons = tuple((spec[0], tuple(m[0] for m in modes),
                      next((m[0] for m in modes if m[6].get("is-current")),
                           None))
                     for spec, modes, _p in monitors)
        lms = tuple(sorted((lm[0], lm[1], float(lm[2]), int(lm[3]),
                            bool(lm[4]), tuple(sorted(s[0] for s in lm[5])))
                           for lm in logical))
        return mons, lms

    def _primary_lm(self, logical, lm_of):
        """The logical monitor that is really primary. One flagged: that one. Several (GNOME 50 keeps stale
        flags on in-place updates): the one holding the connector GetResources marks primary. None: None."""
        flagged = [lm for lm in logical if lm[4]]
        if len(flagged) <= 1:
            return flagged[0] if flagged else None
        try:
            _serial, _crtcs, outputs, _modes, _mw, _mh = self.bus.call(
                self.flavor.dest, self.flavor.path, self.flavor.iface, "GetResources")
            for out in outputs:
                if out[7].get("primary") and out[4] in lm_of:
                    return lm_of[out[4]]
        except (DBusError, ValueError, IndexError, TypeError, AttributeError):
            pass
        return flagged[0]

    # -- planning ------------------------------------------------------------

    def resolve_mode(self, t: core.Target, state: core.State) -> Mode:
        """The real mode an enabled target will run: the stanza's, else the current one, else the mode wxrandr
        disabled it at (state file), else the preferred one. A custom (--newmode) mode is only applicable when a
        real mode of the same size and rate exists -- and Mutter's modes say whether they are interlaced, so
        that has to match too."""
        return core.resolve_real_mode(t, state, interlace_known=True)

    def _scale_for(self, t: core.Target, mode: Mode) -> float:
        """The scale Mutter will accept: an output keeping its mode and scale keeps them verbatim (Mutter runs
        that combination right now, even if the scale came from monitors.xml and is not in supported_scales);
        anything else is snapped to the mode's supported list."""
        o = t.output
        if (o.active and o.current is not None
                and o.current.mode_id == mode.mode_id
                and abs(t.scale - o.scale) < 1e-9):
            return t.scale
        return snap_scale(t.scale, self.scales.get((t.name, mode.mode_id)) or [1.0])

    def predicted_dims(self, t: core.Target, state: core.State) -> tuple:
        """Pending logical size of an enabled target in Mutter's space (the dryrun/verbose plan and --fb checks
        use this instead of the wlroots prediction)."""
        m = self.resolve_mode(t, state)
        return logical_size(m.w, m.h, t.sway_tf, self._scale_for(t, m), self.layout_mode)

    @staticmethod
    def _floating(t: core.Target) -> bool:
        """Enabled now, was off, and nothing says where it goes: xrandr would
        drop it at 0,0 (an overlap Mutter refuses)."""
        return (t.enabled and not t.output.active
                and (t.stanza is None
                     or (t.stanza.pos is None and t.stanza.relation is None)))

    def _auto_place(self, targets, dims, pos):
        placed = [t.name for t in targets if t.enabled and not self._floating(t)]
        for t in targets:
            if not self._floating(t):
                continue
            if placed:
                right = max(placed, key=lambda n: (pos[n][0] + dims[n][0], -pos[n][1]))
                pos[t.name] = (pos[right][0] + dims[right][0], pos[right][1])
                warn("output %s enabled without a position; placing it " "right-of %s\n" % (t.name, right))
            else:
                pos[t.name] = (0, 0)
            placed.append(t.name)
        if pos:
            min_x = min(p[0] for p in pos.values())
            min_y = min(p[1] for p in pos.values())
            if min_x or min_y:
                for n, (x, y) in list(pos.items()):
                    pos[n] = (x - min_x, y - min_y)

    def plan(self, state: core.State, targets: list) -> list:
        """Targets -> logical monitors: modes resolved, scales snapped (warn when changed), positions from
        core.resolve_positions in Mutter's logical space, same-position outputs grouped into one (mirror)
        logical monitor, exactly one primary, floating outputs auto-placed."""
        real, scales = {}, {}
        for t in targets:
            if not t.enabled:
                continue
            m = self.resolve_mode(t, state)
            s = self._scale_for(t, m)
            if abs(s - t.scale) > 1e-6:
                warn("scale %g is not available for %s at %dx%d; using %g\n" % (t.scale, t.name, m.w, m.h, s))
            real[t.name], scales[t.name] = m, s
        by_name = {t.name: t for t in targets}
        dims = {n: logical_size(real[n].w, real[n].h, by_name[n].sway_tf,
                                scales[n], self.layout_mode) for n in real}
        pos = core.resolve_positions(targets, dims)
        for n, (x, y), via in keep_adjacent(targets, dims, pos):
            warn("output %s moved to +%d+%d to stay adjacent to %s\n" % (n, x, y, via))
        self._auto_place(targets, dims, pos)
        groups, index = [], {}
        for t in targets:
            if not t.enabled:
                continue
            key = pos[t.name]
            if key in index:
                groups[index[key]][1].append(t.name)
            else:
                index[key] = len(groups)
                groups.append((key, [t.name]))
        enabled = [t.name for t in targets if t.enabled]
        if state.primary in enabled:
            primary = state.primary
        elif self.primary in enabled:
            primary = self.primary
        else:
            primary = enabled[0] if enabled else None
        out = []
        for (x, y), names in groups:
            first = by_name[names[0]]
            for n in names[1:]:
                t = by_name[n]
                if ((real[n].w, real[n].h) != (real[first.name].w,
                                                real[first.name].h)
                        or t.sway_tf != first.sway_tf
                        or scales[n] != scales[first.name]):
                    raise Fatal("cannot mirror %s onto %s: Mutter needs the "
                                "same mode, rotation and scale (%s %s scale "
                                "%g vs %s %s scale %g)\n"
                                % (n, first.name, real[n].display_name,
                                   t.sway_tf, scales[n],
                                   real[first.name].display_name,
                                   first.sway_tf, scales[first.name]))
            out.append({"x": x, "y": y, "scale": scales[first.name],
                        "transform": to_transform(first.sway_tf),
                        "primary": primary in names,
                        "members": [(n, real[n].mode_id,
                                     self.underscan.get(n, False))
                                    for n in names]})
        return out

    @staticmethod
    def to_wire(plan: list) -> list:
        return [(p["x"], p["y"], float(p["scale"]), int(p["transform"]),
                 bool(p["primary"]),
                 [(c, mid, ({"underscanning": Variant("b", True)} if us else {}))
                  for c, mid, us in p["members"]])
                for p in plan]

    # -- apply ---------------------------------------------------------------

    def _call_apply(self, method: int, plan: list):
        self.last_method = method
        self.bus.call(self.flavor.dest, self.flavor.path, self.flavor.iface,
                      "ApplyMonitorsConfig", APPLY_SIG,
                      (self.serial, method, self.to_wire(plan), {}))

    def _send(self, method: int, plan: list):
        """ApplyMonitorsConfig with one stale-serial retry, and only when the re-read state still has the
        monitors and layout the plan was built from (a plan re-sent after a hotplug would silently leave the new
        monitor out, one re-sent after someone else's re-layout would undo it); every other rejection is
        Mutter's own message as a Fatal."""
        try:
            self._call_apply(method, plan)
        except DBusError as e:
            if not _is_stale(e):
                raise Fatal(_refused(e, self.flavor))
            serial, monitors, logical, _props = self.get_current_state()
            if self._fingerprint(monitors, logical) != self.fingerprint:
                raise Fatal(CANCELLED)
            self.serial = serial
            try:
                self._call_apply(method, plan)
            except DBusError as e2:
                if _is_stale(e2):
                    raise Fatal(CANCELLED)
                raise Fatal(_refused(e2, self.flavor))

    def verify(self, state: core.State, targets: list):
        """--dryrun: method 0 — Mutter validates, nothing changes."""
        self._send(VERIFY, self.plan(state, targets))

    def _file_layout_mode(self):
        """The layout mode monitors.xml is read in: this session's."""
        return (monitors_xml.PHYSICAL if self.layout_mode == LAYOUT_PHYSICAL
                else monitors_xml.LOGICAL)

    def _rots_with_the_layout_mode(self, plan, targets, state) -> bool:
        """True when this layout is adjacent as Mutter measures it now and would not be
        in the other layout mode -- see LAYOUT_MODE_WARNING.  Only ever asked in
        physical layout mode and only when something is scaled, so the ordinary layout
        (every scale 1, where the two modes are the same arithmetic) never gets here.
        """
        if self.layout_mode != LAYOUT_PHYSICAL or all(p["scale"] == 1 for p in plan):
            return False
        px = {}
        for t in targets:
            if t.enabled:
                m = self.resolve_mode(t, state)
                px[t.name] = (m.w, m.h, t.sway_tf)
        rects = []
        for p in plan:
            connector = p["members"][0][0]
            if connector not in px:
                return False                 # cannot tell: say nothing
            w, h, tf = px[connector]
            rects.append((p["x"], p["y"])
                         + logical_size(w, h, tf, p["scale"], LAYOUT_LOGICAL))
        return bool(monitors_xml.fault(rects))

    # -- the overlap route ---------------------------------------------------
    #
    # Everything below runs only for `wxrandr --unsafe-gnome-overlap`, and only
    # for a layout Mutter's own validator refuses.  The ordinary apply never
    # reaches it, does not import it and cannot enter it: `overlap_route`
    # returns None for every layout GNOME accepts, and `Session.apply` then
    # takes the same DisplayConfig path it always did.  See
    # hacks/display/gnome_overlap.py.

    def current_groups(self) -> list:
        """The running layout as connector groups, from the snapshot -- what the
        extension is told to expect, and what the printed undo command is built
        from."""
        return sorted(({"connectors": sorted(c for c, _mid, _us in entry[5]),
                        "x": int(entry[0]), "y": int(entry[1])}
                       for entry in self.current_config),
                      key=lambda g: (g["x"], g["y"], g["connectors"]))

    def overlap_route(self, state: core.State, targets: list):
        """`(plan, fault)` when this layout is one Mutter refuses on geometry, or
        None when Mutter would take it -- in which case nothing unsafe is needed
        and nothing unsafe happens.  Mutter's own rules, in Mutter's order
        (monitors_xml.fault, the verifier this tree already had)."""
        plan = self.plan(state, targets)
        dims = {}
        for t in targets:
            if not t.enabled:
                continue
            m = self.resolve_mode(t, state)
            dims[t.name] = logical_size(m.w, m.h, t.sway_tf,
                                        self._scale_for(t, m), self.layout_mode)
        rects = gnome_overlap.rects(plan, dims)
        fault = monitors_xml.fault(rects) if rects else None
        if fault is None:
            return None
        return plan, fault

    def _overlap_client(self, plan, force=None):
        """The checks that happen out here, before the extension is asked to do
        anything: is this a GNOME whose private layout has been measured, is the
        extension actually running, and is a position really the only thing this
        invocation changes.  Returns `(client, shell_version, want, expect)`.

        `force` is a `--unsafe-gnome-overlap-unmeasured` request or None, and the
        only one of the three it touches is the first.  The other two are
        certain refusals: an extension that is not on the bus cannot be talked
        into being there, and an invocation that changes a mode cannot be talked
        into being two words.

        The one bus call that happens on the way to a refusal is on the *first*
        check's failure path, and it writes nothing: `_unmeasured_facts()` below
        asks the extension to refuse in its own words so that the message can
        carry the numbers a maintainer needs."""
        not_gnome = gnome_overlap.not_gnome_reason(self.flavor.name)
        if not_gnome is not None:
            # The flavour, not the session: Muffin enforces Mutter's adjacency rule with Mutter's own strings
            # [M recon2/cinnamon.md §2.2], so a Cinnamon user meets this refusal for real, and the route
            # behind the flag does not reach them as it stands (gnome_overlap.CINNAMON_REASON): its struct
            # offsets are keyed on a Meta generation muffin does not number. Overlapping monitors are an X
            # feature and this is a gap, not a verdict -- the rung that would close it is one BELOW the
            # extension GNOME needs: `org.Cinnamon.Eval` reaching MetaMonitorsConfig with nothing installed
            # (AGENTS.md route 2), at the cost of an offset record measured per Cinnamon release instead of
            # per Meta generation [request filed in <scratchpad>/requests-unowned-batch-19.md for
            # gnome_overlap.py's own sentence].
            raise Fatal("%s only means anything on GNOME; %s\n"
                        % (gnome_overlap.FLAG, not_gnome))
        ov = gnome_overlap.Overlap(self.bus)
        version = ov.shell_version()
        no_force = gnome_overlap.force_reason(force, version) if force else None
        if no_force:
            raise Fatal("%s: %s\n" % (gnome_overlap.FORCE_FLAG, no_force))
        why = gnome_overlap.unsupported_reason(version, force)
        if why:
            if gnome_overlap.shell_major(version) is None:
                raise Fatal("%s: %s.  Nothing was changed; this layout needs a "
                            "compositor that will place it (KDE, wlroots and X "
                            "all do).\n" % (gnome_overlap.FLAG, why))
            # A build nobody has measured: the one refusal that is cautious
            # rather than certain, so it prints what a maintainer needs to add
            # it -- including the numbers, which the extension will hand over
            # with its own refusal at the same gate if it is there to ask.
            raise Fatal(gnome_overlap.unmeasured_refusal(
                version, self._unmeasured_facts(ov)))
        if not gnome_overlap.only_positions_differ(plan, self.current_config):
            raise Fatal("%s: this invocation changes more than where the monitors "
                        "are (a mode, a scale, a rotation, the primary or which "
                        "outputs mirror). That has to go through GNOME's own "
                        "configuration first: apply it without this flag, then "
                        "move the monitors with it.\n" % gnome_overlap.FLAG)
        if not ov.running():
            raise Fatal("%s: %s" % (gnome_overlap.FLAG, gnome_overlap.INSTALL_HINT))
        return ov, version, gnome_overlap.groups(plan), self.current_groups()

    def _unmeasured_facts(self, ov):
        """The extension's own measurements of a build it has just refused, or
        None when there is no extension to ask.

        It is a Probe: every guard runs, the first one refuses, and the reply
        carries what had been measured before it did -- the version strings, the
        libmutter mapped, and MetaMonitorsConfig's size out of the GType
        registry.  All public, none of it a private read, and it is the
        difference between a refusal a maintainer can act on and one that sends
        them to a debugger for numbers this already had."""
        try:
            if not ov.running():
                return None
            reply = ov.probe(self.layout_mode, self.current_groups())
        except (gnome_overlap.OverlapError, DBusError, OSError, ValueError):
            return None
        return reply.get("found")

    def overlap_available(self):
        """`(shell version, why the overlap route is not usable here or None)`,
        cheaply: the Shell's own public version property, our allowlist, and
        whether the extension owns its bus name.  Nothing is read out of
        gnome-shell -- this answers "would it work?", which is a question a GUI
        asks at startup and which must not cost a walk of Mutter's heap.

        Two answers come before the allowlist, and both are about which session this is rather than which
        build.  A Muffin flavour has no generation to look up at all.  And an X11 session owning
        org.gnome.Mutter.DisplayConfig -- GNOME-on-Xorg, which owns it exactly as GNOME-on-Wayland does
        [M recon2/gnome-xorg.md §4] -- reaches this because detection picks `mutter` off the bus name, and
        the true answer there is not "install the extension": the X server has been placing overlapping
        monitors all along.  Measured: `--gnome-overlap-status` on a GNOME-on-Xorg session answered
        `unavailable / shell: 46.0 / reason: the overlap extension is not running...`, pointing at an install
        that would change nothing [M recon2/gnome-xorg.md §4 item 7].  The session kind is asked the way
        every other caller asks it, so `W11_PASSTHROUGH` still means what it means everywhere else.

        The apply path is deliberately NOT given the x11 answer: `--unsafe-gnome-overlap` on an X11 session
        is refused before the handover, in these same words, by w11common/passthrough.py's caller in cli.py."""
        from w11common import passthrough
        ov = gnome_overlap.Overlap(self.bus)
        version = ov.shell_version() if self.flavor is MUTTER else None
        why = gnome_overlap.not_gnome_reason(
            self.flavor.name, passthrough.session_kind("xrandr"))
        if why is not None:
            return version, why
        why = gnome_overlap.unsupported_reason(version)
        if why is None and not ov.running():
            why = gnome_overlap.INSTALL_HINT
        return version, why

    def overlap_probe(self):
        """Every check inside gnome-shell, and not one byte written: the Probe
        `--gnome-overlap-allow` records the answer of.  Same gate, in the same
        order, as an apply -- an agreement must not be recordable on a
        compositor an apply would refuse."""
        version, why = self.overlap_available()
        if why:
            if gnome_overlap.generation_for(version) is None and \
                    gnome_overlap.shell_major(version) is not None:
                # the same maintainer message the apply gives, because
                # `--gnome-overlap-allow` on a new release is exactly where
                # somebody lands who is about to add one
                raise Fatal(gnome_overlap.unmeasured_refusal(
                    version, self._unmeasured_facts(gnome_overlap.Overlap(self.bus)),
                    force_hint=False))
            raise Fatal("%s: %s" % (gnome_overlap.ALLOW_FLAG, why
                                    if why.endswith("\n") else why + "\n"))
        return gnome_overlap.Overlap(self.bus).probe(self.layout_mode,
                                                     self.current_groups())

    def overlap_dryrun(self, state: core.State, targets: list, force=None) -> bool:
        """--dryrun --unsafe-gnome-overlap: print what a real run would do and
        run every guard inside the extension, writing nothing at all.  False
        when Mutter would accept the layout, so the ordinary dryrun answers."""
        route = self.overlap_route(state, targets)
        if route is None:
            return False
        plan, fault = route
        ov, version, want, expect = self._overlap_client(plan, force)
        moves = gnome_overlap.moves_text(expect, want)
        if not moves:
            warn(NOTHING_TO_DO)
            return True
        if force:
            core.warn_bare(gnome_overlap.forcing_warning(version))
        core.warn_bare(gnome_overlap.warning(
            version, moves, gnome_overlap.undo_command(expect)))
        warn("GNOME's rule this breaks: %s\n" % fault)
        reply = ov.probe(self.layout_mode, expect, force)
        if not reply.get("ok"):
            raise Fatal(self._overlap_refusal(reply, version, force))
        for check in reply.get("checks") or []:
            warn("overlap check %s: %s\n" % (check.get("name"), check.get("detail")))
        notes = gnome_overlap.notes_text(reply)
        if notes:
            warn(notes)
        warn("dryrun: nothing was written\n")
        return True

    def _overlap_refusal(self, reply, version, force):
        """A refusal from inside the extension, as the line to raise.

        A refusal that forcing could get past -- there is one, and it is
        `shell-version` -- becomes the maintainer's message with the numbers the
        reply carries, instead of a sentence the reader can do nothing with.
        Every other refusal is certain and stays one line: an unforceable check
        refused because something is wrong, and printing how to force would be
        offering a bypass that does not exist."""
        if not force and gnome_overlap.refusal_is_forceable(reply):
            return gnome_overlap.unmeasured_refusal(version, reply.get("found"))
        return "%s: %s" % (gnome_overlap.FLAG, gnome_overlap.refusal_text(reply))

    def apply_overlap(self, state: core.State, targets: list, force=None):
        """Apply a layout GNOME refuses, through the overlap extension.  None
        when Mutter would accept the layout: the caller then applies it the
        ordinary way, which is what happens for every layout but this one."""
        route = self.overlap_route(state, targets)
        if route is None:
            return None
        plan, fault = route
        ov, version, want, expect = self._overlap_client(plan, force)
        moves = gnome_overlap.moves_text(expect, want)
        if not moves:
            # already where it was asked to be: nothing to write, and so
            # nothing written.  The unchanged temporary apply above does the
            # same for every ordinary layout.
            warn(NOTHING_TO_DO)
            return self.snapshot(state)
        # The agreement decides how much is said, and nothing else.  It is read
        # here, between the last guard and the call, so that there is no
        # arrangement of this code in which it could be mistaken for one of
        # them: every refusal above has already happened, and every check inside
        # the extension is still to come.
        #
        # A forced run does not come here at all: it never reads the agreement,
        # so there is no arrangement of this code in which a recorded yes makes
        # a forced run quieter, and the paragraph is printed every single time.
        rec = None if force else gnome_overlap.load_consent()
        quiet, _why = gnome_overlap.consent_covers(rec, version)
        if quiet:
            warn(gnome_overlap.quiet_line(rec, fault))
        else:
            if force:
                core.warn_bare(gnome_overlap.forcing_warning(version))
            core.warn_bare(gnome_overlap.warning(
                version, moves, gnome_overlap.undo_command(expect)))
            warn("GNOME's rule this breaks: %s\n" % fault)
        core.record_lastmodes(state, targets)
        reply = ov.apply(self.layout_mode, expect, want, force)
        # `ok` and `applied` are two different answers, and the shape that has
        # both of them is the one this branch exists for: extension.js:1100-1108
        # re-digests ~/.config/monitors.xml after the apply, and a file that
        # moved across the call clears `ok` on a reply that already says
        # `applied: true`, with no `check` and no `reason`.  Raising the refusal
        # there printed "the overlap extension refused (?): no reason given"
        # about a layout that is on screen -- so a write that went in is
        # reported as a write that went in, and the exit status is the only
        # thing `ok: false` still decides.
        applied = bool(reply.get("applied"))
        if not reply.get("ok") and not applied:
            raise Fatal(self._overlap_refusal(reply, version, force))
        notes = gnome_overlap.notes_text(reply)
        if notes:
            warn(notes)
        core.warn_bare(gnome_overlap.applied_text(reply, quiet=quiet))
        # The audit the version string alone could not do: libmutter's
        # generation and MetaMonitorsConfig's size, as the checks have just
        # measured them, against what was agreed to.  A difference here needs a
        # build that kept its version and moved its private layout, which the
        # extension's own struct-size check turns into a refusal rather than a
        # bad write -- so this is bookkeeping, and what it does is withdraw.
        if quiet:
            drift = gnome_overlap.consent_drift(rec, gnome_overlap.facts(reply))
            if drift:
                gnome_overlap.forget_consent()
                warn("%s: %s" % (gnome_overlap.FLAG, drift))
        fresh = self.snapshot(state)
        if not reply.get("ok"):
            # Applied, and not ok.  Everything above has already been said --
            # including whatever applied_text() shouted about monitors.xml --
            # and the snapshot is taken, because the layout on screen is the
            # new one and the state file has to agree with the screen.  The
            # save is here and not at the caller because the caller is
            # cli.py:_do_apply, whose own `sess.state.save()` is three lines
            # past a call that is about to raise: without this the lastmodes
            # recorded above and the primary snapshot() just re-synced would be
            # thrown away on the one branch where the screen really did change.
            # What is left after that is the exit status: a run that ends here
            # is not a success, and a script must not read it as one.
            state.save()
            raise Fatal("%s: the extension applied this layout and then reported "
                        "it as not ok%s; the layout above is what is running now, "
                        "and `%s` puts it back.\n"
                        % (gnome_overlap.FLAG, _not_ok_why(reply),
                           gnome_overlap.undo_command(expect)))
        return fresh

    def apply(self, state: core.State, targets: list, persistent: bool = False) -> list:
        """One ApplyMonitorsConfig for the whole layout, then wait for MonitorsChanged (<= 5 s) and return the
        fresh snapshot. An unchanged temporary layout is not re-applied (no modeset for `--primary` on the
        primary); --persistent always writes, so monitors.xml gets it."""
        plan = self.plan(state, targets)
        method = PERSISTENT if persistent else TEMPORARY
        if method == TEMPORARY and _canon(plan) == self.current_config:
            return self.snapshot(state)
        core.record_lastmodes(state, targets)
        saved = None
        if method == PERSISTENT:
            warn(self.flavor.persist_warning)
            # Only this branch ever opens monitors.xml: a temporary apply, which is
            # what nearly every run does, does not go near the file.
            saved = monitors_xml.snapshot(uid=wsession.session_uid(),
                                          name=self.flavor.config_name)
            for line in monitors_xml.describe(saved, self._file_layout_mode(),
                                              desktop=self.flavor.desktop,
                                              compositor=self.flavor.compositor):
                warn(line)
            if self._rots_with_the_layout_mode(plan, targets, state):
                warn(LAYOUT_MODE_WARNING % self.flavor.desktop)
        if not self._matched:
            try:
                self.bus.add_match(self.flavor.match)
            except DBusError:
                pass
            self._matched = True
        try:
            self._send(method, plan)
        except BaseException:
            # the snapshot is holding the descriptor of the directory it read out of
            # (monitors_xml.Snapshot), and keep_backup(), which is what closes it, is
            # below this call and is not reached when the apply raises.  warandr drives
            # this method again and again in one process, so a leak here is a leak that
            # accumulates.
            if saved:
                saved.close()
            raise
        if saved:
            # Mutter accepted the layout, so its own writer may replace the file the
            # moment the user confirms the dialog -- and it writes the file whole, out
            # of what it holds in memory, which after a discarded read is only this
            # layout.  Keep what was there; a refused apply gets here not at all.
            backup = monitors_xml.keep_backup(saved)
            if backup:
                warn(BACKUP_NOTE % backup)
        try:
            self.bus.wait_signal(self.flavor.iface, "MonitorsChanged", MONITORS_CHANGED_TIMEOUT)
        except DBusError:
            pass
        return self.snapshot(state)
