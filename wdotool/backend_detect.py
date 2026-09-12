"""Backend detection.

Order: WDOTOOL_BACKEND override -> sway/i3 IPC socket -> Hyprland IPC socket -> KWin, GNOME or Cinnamon (one
ListNames on the session bus) -> Wayfire IPC socket -> one registry round trip, which picks wlr or COSMIC ->
clear error. The three D-Bus checks are ONE ListNames call over dbus_mini (no gdbus/busctl spawns); the
connection is kept for the process so the GNOME backend reuses it, and so is the registry.

Why each step sits where it does, all measured:

* Hyprland above the bus checks because a Hyprland session owns neither `org.kde.KWin` nor `org.gnome.Shell`
  [M recon2/hyprland.md §2, `busctl --user list`], so nothing below can shadow it and nothing it shadows can
  exist. It has to be above the registry probe, which would take it for a plain wlroots compositor -- honest
  for reading and wrong about ids, minimize and geometry [M hyprland.md §3].
* Cinnamon third among the bus names because a Cinnamon session owns neither of the other two
  [M recon2/cinnamon.md §2.2]. None of the three is ever swallowed: Mutter offers no foreign-toplevel protocol
  of either flavour (Muffin advertises 23 globals and neither one [M cinnamon.md §2.1]), and KWin implements
  neither zwlr_foreign_toplevel_manager_v1 nor ext_foreign_toplevel_list_v1 (checked against KWin 5.27 through
  6.6 and master), so the registry probe below could never succeed on any of them -- falling through would only
  replace a precise message (the GNOME bridge's install hint, a KWin failure) with "the compositor does not
  offer wlr-foreign-toplevel".
* Wayfire below the bus names -- a Plasma box with wayfire installed must not be misdetected, though Wayfire
  owns no bus name of its own [M recon2/wayfire.md §1.1] -- and above the registry probe, which would otherwise
  swallow it: Wayfire does advertise zwlr_foreign_toplevel_manager_v1, and the wlr floor is wrong there about
  ids, geometry, desktops and WM_CLASS [M wayfire.md §2.3, §2.4].
* COSMIC last, and only when the registry has both `ext_foreign_toplevel_list_v1` and
  `zcosmic_toplevel_info_v1` and no wlr manager: cosmic-comp publishes no zwlr_foreign_toplevel_manager_v1 at
  all, which is why every window command answered `no Wayland session found` there [M recon2/cosmic.md §2, §3].
  A compositor with both families keeps wlr, which is the older and better-tested path."""

import os

from w11common import session
from w11common.errors import CmdError
from wdotool.backend import program
from wdotool.ctx import NoSessionError

KWIN_NAME = "org.kde.KWin"
GNOME_NAME = "org.gnome.Shell"
CINNAMON_NAME = "org.Cinnamon"

#: The two names that, beside org.gnome.Shell, say the session really is GNOME.
#:
#: `org.gnome.Shell` is not proof of GNOME Shell.  On Ubuntu Budgie 10.10.2 (resolute-budgie golden,
#: measured with `busctl --user list` on 2026-09-09, recorded verbatim in
#: tests/fixtures/live/busnames-resolute-budgie-10.10.2.txt) it is owned by `budgie-power-dialog`, and the
#: session it belongs to is labwc: no org.gnome.Mutter.* name anywhere on that bus, and the compositor
#: advertises zwlr_foreign_toplevel_manager_v1.  Detection took that name for GNOME and every window command
#: on the flavor answered with the bridge's install hint instead of a window list ("the w11 bridge
#: extension is installed in ... but the running GNOME Shell has not loaded it", CI run 34308982263).
#:
#: This CORRECTS recon2/budgie.md, whose §2 says a Budgie session owns no `org.gnome.Shell` and whose §6
#: plans the hermetic detection test on that premise: that reading was taken before budgie-power-dialog
#: had claimed the name, and the golden's bus says otherwise.  Read the fixture, not the recon, for what
#: this session owns.
#:
#: Two names settle it and they are both a positive test rather than a blocklist of desktops.  Mutter's
#: DisplayConfig is gnome-shell's own, claimed by the same process, and it is what wxrandr's mutter backend
#: drives (`* mutter available org.gnome.Mutter.DisplayConfig on the session bus`, recorded in
#: tests/fixtures/live/noble-gnome-46.0-capture.txt); our bridge's name can only be owned by an extension
#: running INSIDE gnome-shell, so it is the honest answer to "is our own code in there".
MUTTER_NAME = "org.gnome.Mutter.DisplayConfig"
BRIDGE_NAME = "org.w11.Bridge"

#: the toplevel protocols the registry step chooses between
WLR_TOPLEVEL = "zwlr_foreign_toplevel_manager_v1"
EXT_TOPLEVEL = "ext_foreign_toplevel_list_v1"
COSMIC_TOPLEVEL = "zcosmic_toplevel_info_v1"

_bus = None          # dbus_mini.Bus for this process, once connected
_names = None        # cached ListNames result (None = no bus reachable)
_probed = False
_registry = None     # cached {interface: version} (None = no compositor reachable)
_registry_probed = False
_registry_conn = None    # the WlConn that registry was read on, kept for the chosen backend


def _sway():
    from wdotool.backend_sway import SwayBackend
    return SwayBackend()


def _not_built(exc, module, name):
    """Turn "the maker's own module is missing" into the refusal every arm of detect() already handles.

    A maker whose module is not in this install has to answer the way a maker whose protocol is not there
    answers -- with a CmdError -- because detect()'s arms and the WDOTOOL_BACKEND path are both written around
    that one exception, and a traceback out of `wdotool key a` on a real Hyprland session is not an answer.
    Only the maker's own module is swallowed: a ModuleNotFoundError from deeper inside a backend that IS
    installed is that backend's bug and has to stay visible."""
    if exc.name is not None and exc.name != module and not exc.name.startswith(module + "."):
        raise exc
    raise CmdError("%s backend: not built into this install" % name) from None


def _hypr():
    try:
        from wdotool.backend_hypr import HyprBackend
    except ModuleNotFoundError as exc:
        _not_built(exc, "wdotool.backend_hypr", "hypr")
    return HyprBackend()


def _wayfire():
    try:
        from wdotool.backend_wayfire import WayfireBackend
    except ModuleNotFoundError as exc:
        _not_built(exc, "wdotool.backend_wayfire", "wayfire")
    return WayfireBackend()


def _wlr():
    from wdotool.backend_wlr import WlrBackend
    # detection's own connection, whose registry has already been read: plan A 1.0 step 6, and the reason a
    # session opens ONE connection and not two.  A forced `WDOTOOL_BACKEND=wlr` reaches here without having
    # asked for the registry, and session_conn() reads it then -- the same round trip the backend's own
    # constructor would have paid, spent once.
    return WlrBackend(conn=session_conn())


def _cosmic():
    try:
        from wdotool.backend_cosmic import CosmicBackend
    except ModuleNotFoundError as exc:
        _not_built(exc, "wdotool.backend_cosmic", "cosmic")
    return CosmicBackend(conn=session_conn())


def _kwin():
    from wdotool.backend_kwin import KwinBackend
    return KwinBackend(bus=session_bus(), names=session_names())


def _gnome():
    from wdotool.backend_gnome import GnomeBackend
    return GnomeBackend(bus=session_bus(), names=session_names())


def _cinnamon():
    from wdotool.backend_cinnamon import CinnamonBackend
    return CinnamonBackend(bus=session_bus(), names=session_names())


#: WDOTOOL_BACKEND's accepted spellings. `i3` is an alias of `sway` and buys
#: nothing but the spelling -- the dialect is read off GET_VERSION, not off the
#: variable -- so the refusal below names the eight canonical tokens.
_MAKERS = {"sway": _sway, "i3": _sway, "hypr": _hypr, "wayfire": _wayfire,
           "wlr": _wlr, "cosmic": _cosmic, "kwin": _kwin, "gnome": _gnome,
           "cinnamon": _cinnamon}
_FORCED_NAMES = "sway, hypr, wayfire, wlr, cosmic, kwin, gnome, cinnamon"


def reset():
    """Forget the cached bus/names/registry (tests re-detect against a fresh session)."""
    global _bus, _names, _probed, _registry, _registry_probed, _registry_conn
    if _bus is not None:
        try:
            _bus.close()
        except Exception:  # best effort on teardown
            pass
    if _registry_conn is not None:
        try:
            _registry_conn.close()
        except Exception:  # best effort on teardown
            pass
    _bus, _names, _probed = None, None, False
    _registry, _registry_probed, _registry_conn = None, False, None


def session_bus():
    """This process's connection to the graphical session's bus, or None
    when no bus can be found/joined. Connected once, cached."""
    global _bus, _probed, _names
    if _probed:
        return _bus
    _probed = True
    from w11common.dbus_mini import Bus, DBusError
    try:
        _bus = Bus()
        _names = _bus.list_names()
    except DBusError:
        if _bus is not None:
            _bus.close()
        _bus, _names = None, None
    return _bus


def session_names() -> list[str] | None:
    """Names on the session bus (one ListNames per process), or None when
    there is no bus."""
    session_bus()
    return _names


def session_registry() -> "dict[str, int] | None":
    """{interface: version} the session's compositor advertises, or None when there is no Wayland socket or it
    cannot be spoken to. One connection per process, opened, read and closed here.

    Detection used to pay this round trip inside `_wlr()` and throw the registry away, so a compositor that has
    ext-foreign-toplevel and no wlr manager -- which is exactly COSMIC -- could only be reported as "does not
    offer wlr-foreign-toplevel" [M recon2/cosmic.md §3]. Reading it once and choosing from it costs the same
    round trip. Since 2026-09-09 the connection is KEPT (`session_conn()`) and handed to `WlrBackend` /
    `CosmicBackend`, which is plan A 1.0 step 6 and closes the last gap this docstring named: a session used
    to open two connections, detection's and the backend's. A read that fails closes it here; a read that
    succeeds gives it to `reset()` to close."""
    global _registry, _registry_probed, _registry_conn
    if _registry_probed:
        return _registry
    _registry_probed = True
    import time
    from w11common.wayland_mini import WlConn
    hit = session.find_wayland_socket()
    if hit is None:
        return None
    # Two attempts, short backoff.  A connect or roundtrip that fails, or a
    # registry that comes back EMPTY (a real compositor always advertises
    # wl_compositor and friends, so {} means the roundtrip did not finish),
    # is retried once before the session is declared unreachable: under CI
    # load a momentary failure here reported COSMIC as "offers neither
    # wlr-foreign-toplevel nor the COSMIC toplevel protocols" although the
    # backend is fine on the same golden by hand [arch-cosmic, CI 34676441862].
    for attempt in range(2):
        conn = None
        try:
            conn = WlConn(hit[2])
            out: dict[str, int] = {}
            for iface, ver in conn.get_registry().values():
                out[iface] = max(ver, out.get(iface, 0))
            if out:
                _registry = out
                _registry_conn = conn
                conn = None          # kept, not closed: session_conn() hands it to the backend
                break
            _registry = None         # empty: an unfinished roundtrip, retry
        except (OSError, RuntimeError, ValueError):
            _registry = None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        if attempt == 0:
            time.sleep(0.25)
    return _registry


def session_conn():
    """The `WlConn` `session_registry()` read the registry on, or None when there is none to read.

    The two foreign-toplevel backends take it (`WlrBackend(conn=...)`, `CosmicBackend(conn=...)`): they use
    the connection they are given, never close one they did not open, and survive a constructor failure
    without taking it down [tests/test_wire_hardening.py:WlrBackendGuards]. Anything else in this module
    ignores it -- a KWin or GNOME session has no use for a Wayland registry connection."""
    session_registry()
    return _registry_conn


def _toplevel_family(reg: dict) -> str | None:
    """Which foreign-toplevel family a registry offers: `"wlr"`, `"cosmic"`, or None for neither.

    ONE copy of that rule, read by `detect()`'s last two arms and by `_has_toplevel()` above them, so the
    GNOME arm's reading of the registry cannot drift away from the arm that actually picks the backend.  A
    compositor with both families is `wlr`, the older and better-tested path (module docstring, COSMIC)."""
    if WLR_TOPLEVEL in reg:
        return "wlr"
    if EXT_TOPLEVEL in reg and COSMIC_TOPLEVEL in reg:
        return "cosmic"
    return None


def _has_toplevel() -> bool:
    """Does this session's compositor publish a foreign-toplevel protocol of either flavour?

    The same question `detect()`'s last two arms ask -- through the same `_toplevel_family` they ask it
    with -- one step earlier, and off the same cached registry, so asking it twice costs one round trip and
    not two."""
    return _toplevel_family(session_registry() or {}) is not None


def detect():
    forced = os.environ.get("WDOTOOL_BACKEND")
    if forced:
        maker = _MAKERS.get(forced.strip().lower())
        if maker is None:
            raise CmdError("WDOTOOL_BACKEND=%s is not one of: %s" % (forced, _FORCED_NAMES))
        return maker()
    if session.find_sway_socket():
        try:
            return _sway()
        except CmdError:
            pass
    if session.find_hypr_socket():
        try:
            return _hypr()
        except CmdError:
            pass
    names = session_names() or []
    if KWIN_NAME in names:
        # Not swallowed either (see the module docstring): KWin offers no
        # foreign-toplevel protocol, so nothing below this could work here.
        return _kwin()
    if GNOME_NAME in names and (BRIDGE_NAME in names or MUTTER_NAME in names or not _has_toplevel()):
        # `org.gnome.Shell` alone is not proof of GNOME Shell (see MUTTER_NAME above: on Budgie 10.10.2 it
        # is budgie-power-dialog that owns it).  With one of GNOME's OWN two names beside it this is GNOME,
        # and the arm is never swallowed -- the GNOME error carries the bridge install hint and nothing
        # below it can work under Mutter.  With neither, the registry decides, and the module docstring's
        # own reason for not swallowing is what decides it: Mutter publishes no foreign-toplevel protocol
        # of either flavour, so a session that publishes one is not Mutter and the wlr/COSMIC arm below is
        # the honest answer.  A session that publishes neither still falls in here, where the bridge hint
        # is the most useful sentence there is.  The extra round trip is paid on that one shape of session
        # and never on GNOME, which always has DisplayConfig.
        return _gnome()
    if CINNAMON_NAME in names:
        # Not swallowed: Muffin advertises no foreign-toplevel protocol of
        # either flavour, so the registry step below could not answer here.
        return _cinnamon()
    if session.find_wayfire_socket():
        try:
            return _wayfire()
        except CmdError as e:
            # Plan A 1.2: the API GATE is an answer and is not swallowed.  Wayfire advertises the wlr
            # protocol, so falling through worked -- and left a user whose `plugins = ipc` is missing
            # `ipc-rules` on the capability floor, never told that two words in wayfire.ini buy the
            # window half.  Every other failure here (a stale socket file, a compositor that went away
            # mid-handshake) has no `.api_gate` and is still swallowed, which is what the fall-through
            # was for [requests-batch-1.md, from batch 6].
            if getattr(e, "api_gate", False):
                raise
    fam = _toplevel_family(session_registry() or {})
    # No swallow on these two arms, unlike the socket arms above: the registry we just read IS the evidence
    # that the protocol is there, so whatever the backend says on the way up is a better answer than the
    # sentence below, which would claim the compositor offers neither family and be wrong.
    if fam == "wlr":
        return _wlr()
    if fam == "cosmic":
        return _cosmic()
    bus_note = ("no session D-Bus reachable" if session_names() is None
                else "no KWin, GNOME Shell or Cinnamon on the session D-Bus")
    # rc 2, not 1: "there is no session to talk to" is a different answer to
    # a script than "the session is up and nothing matched" (B5).
    raise NoSessionError(
        "%s: no Wayland session found: no sway/i3, Hyprland or Wayfire IPC socket, %s, and "
        "the compositor offers neither wlr-foreign-toplevel nor the COSMIC toplevel protocols. "
        "Set WDOTOOL_BACKEND=sway|hypr|wayfire|wlr|cosmic|kwin|gnome|cinnamon to force one."
        % (program(), bus_note)
    )
