"""Region and cross-size output mirror on Cinnamon (Muffin), over `org.Cinnamon.Eval` -- AGENTS.md route 2.

wl-mirror cannot run here: muffin advertises 23 Wayland globals and neither `zwlr_screencopy_manager_v1` nor
`ext_image_copy_capture_manager_v1` is among them, and -- measured on `resolute-cinnamon-wayland` (Cinnamon
6.4.13 / muffin 6.4.1), 2026-09-14 -- muffin exports no `org.cinnamon.Muffin.ScreenCast`, its xdg portal serves
only `Screenshot`, and the ScreenCast machinery is compiled out of `libmuffin.so`. So there is no capture
*protocol* (rung 1), no capture *bus* of the Mutter shape (the ScreenCast one, rung 2) and no *portal*
ScreenCast (rung 4). What muffin does have is the same thing the window plane uses: `org.Cinnamon.Eval`,
ungated arbitrary JS inside the shell (see hacks/window/backend_cinnamon.py). That is the bus this rides -- a
*different* rung-2 surface than a ScreenCast bus, and the one that actually exists here.

**The picture, and why it does not need to capture anything.** A mirror on wlroots reads the source's pixels
back with a capture protocol and paints them into a client window. Cinnamon needs neither: the compositor is
already holding every actor's texture, so a `Clutter.Clone` of an on-screen actor draws that actor's live
content wherever the clone is placed, tracking it frame for frame with no readback. So `build_program` puts a
clipped `Clutter.Actor` on `global.stage` at the TARGET output's origin, sized to the region, and fills it with
one `Clutter.Clone` per on-screen actor found by walking `global.window_group` and `global.top_window_group`
(every `Meta.WindowActor` and `Meta.BackgroundActor`) plus `Main.uiGroup`'s `panel`, each clone offset by the
region's layout origin. This is Cinnamon's own expo / scale / magnifier pattern.

Measured on `resolute-cinnamon-wayland` (two heads, monitor 0 = 1280x800+0+0, monitor 1 = 1920x1080+1280+0),
2026-09-14: mirroring region `1000x700+0+0` of monitor 0 onto monitor 1's origin, a QMP `screendump` of head 1
cropped to the region compares against the same crop of head 0 at **`compare -metric AE` 0, RMSE 0** -- byte
identical -- over a native Wayland window (gnome-terminal), the XWayland desktop/wallpaper actors and the panel.
It stays 0 after the source's content changes (the terminal's clock ticked; source-vs-source AE 3350, mirror-
vs-source AE 0), because a `Clone` tracks its source, and 0 again after a second window opens, because the
`restacked` / `window-created` handlers re-walk the tree. `destroy_program` removes the actor and disconnects
the handlers; head 1 then shows its own content again (AE 700000).

**The shortcut that does NOT work**, recorded so nobody retries it: a single `Clutter.Clone` of `Main.uiGroup`
(what `magnifier.js` clones) does not mirror across heads -- on the same golden its crop mismatched head 0 at
every pixel (AE 700000 of 700000, RMSE 0.29): only the panel comes through, the windows and wallpaper are
black, because a clone of the whole UI group is drawn once for its own monitor and the per-monitor culling
leaves the other head's actors unpainted. The per-actor walk is the working pattern.

THE INTERPOLATION RULE, exactly hacks/window/cinnamon_js.py's: **only integers are ever interpolated, always
with `%d`.** The region and the target origin are the only inputs, and they are numbers; the actor names
(`panel`) and signal names (`restacked`, `window-created`) are literals in the program text, never arguments.
`eval()` runs whatever arrives, so a string reaching a program would be arbitrary code in the user's shell.

What is NOT here yet, named so the gap is a gap and not a silence: the in-compositor group is torn down by
`wmirror --stop`, by `--replace`, and when the session bus goes away, but a supervisor that watches the OUTPUT
layout and drops the mirror when the two heads come to share pixels or the target is unplugged -- what
hacks/mirror/supervise.py does for wl-mirror over `zwlr_output_manager_v1` -- is not wired for this path
(muffin has no wlr output manager; the same watch would read `org.cinnamon.Muffin.DisplayConfig`, AGENTS.md
route 2). Start-time geometry is fully policed by `core.decide`; only the live re-check while it runs is owed.
The second gap is `--scaling`: the group is placed 1:1 at the target's origin and clipped, so `fit`, `cover` and
`exact` are not applied on this path and an explicit `--scaling` is refused (`NO_SCALING`) rather than accepted
and quietly dropped. The route is this same program (rung 2) carrying a `set_scale` and a centring translation,
and the cost is one rig measurement of what `clip_to_allocation` does under a scaled actor before it ships.
"""

import json

from w11common.dbus_mini import ERR, Bus, DBusError
from w11common.errors import CmdError

from . import core

BUS_NAME = "org.Cinnamon"
OBJECT_PATH = "/org/Cinnamon"
IFACE = "org.Cinnamon"
CALL_TIMEOUT = 10.0

#: what `--list`, a successful start and `--check` call this capture path.
KIND = "cinnamon"
ROUTE = "org.Cinnamon.Eval Clutter clone (AGENTS.md route 2)"

_GONE = ("cinnamon mirror: %s is no longer owned on the session bus "
         "(cinnamon restarting, or the session ended)" % BUS_NAME)

#: why an explicit `--scaling` is refused here instead of recorded and not done -- see the docstring above and
#: the "What is not yet on this path" paragraph of docs/WMIRROR.md. `%s` is the mode the caller asked for.
NO_SCALING = ("cinnamon mirror: --scaling %s is not yet done on this path: the Clutter clone group is placed "
              "1:1 at the target origin and clipped to the region, so fit, cover and exact are not applied "
              "here. The route is the same org.Cinnamon.Eval program (AGENTS.md route 2) with a set_scale and "
              "a centring translation on the group, at the cost of one rig measurement of how "
              "clip_to_allocation behaves under a scaled actor before it ships")


# -- the JS programs ----------------------------------------------------------
#
# One shared registry, `global.__w11m`, keyed by an integer token from `__seq`. A record holds the group actor
# and the two signal-handler ids so `destroy_program` can disconnect them. The programs are compact one-liners
# on purpose: they cross the bus on every start/stop/status, and tests/test_wmirror_cinnamon.py records each one
# and fails if the shape drifts.

def build_program(region, tx: int, ty: int) -> str:
    """The program that builds one mirror and returns its integer token.

    `region` is `(x, y, w, h)` in LAYOUT coordinates (what `--region` parses and `wxrandr --query` prints);
    `tx, ty` is the target output's layout origin, so the clipped group lands on the target head and each clone
    is offset by the region origin. Stays live: `restacked` (fires on any stacking change -- open, close, raise)
    and `window-created` re-walk the tree, and the group is kept on top."""
    x, y, w, h = region
    return (
        "(function(){"
        "const C=imports.gi.Clutter;const M=imports.gi.Meta;"
        "if(!global.__w11m)global.__w11m={__seq:0};"
        "var RX=%d,RY=%d,RW=%d,RH=%d,TX=%d,TY=%d;"
        "var tok=(global.__w11m.__seq=global.__w11m.__seq+1);"
        "var grp=new C.Actor({x:TX,y:TY,width:RW,height:RH,clip_to_allocation:true,reactive:false});"
        "function place(a){var p=a.get_transformed_position();"
        "grp.add_child(new C.Clone({source:a,x:p[0]-RX,y:p[1]-RY}));}"
        "function walk(a){if(a instanceof M.BackgroundActor||a instanceof M.WindowActor){place(a);return;}"
        "var ch=a.get_children();for(var i=0;i<ch.length;i++)walk(ch[i]);}"
        "function sync(){grp.remove_all_children();walk(global.window_group);walk(global.top_window_group);"
        "var uc=Main.uiGroup.get_children();for(var i=0;i<uc.length;i++)if(uc[i].name==='panel')place(uc[i]);}"
        "sync();global.stage.add_child(grp);global.stage.set_child_above_sibling(grp,null);"
        "var h1=global.display.connect('restacked',function(){sync();"
        "var pr=grp.get_parent();if(pr)pr.set_child_above_sibling(grp,null);});"
        "var h2=global.display.connect('window-created',function(){sync();});"
        "global.__w11m[tok]={grp:grp,h1:h1,h2:h2};return tok;})()"
        % (int(x), int(y), int(w), int(h), int(tx), int(ty)))


def destroy_program(token: int) -> str:
    """Disconnect the handlers, destroy the group, drop the record. `'ok'` if it was there, `'gone'` if not --
    both are success (a stop of a mirror the compositor already dropped is done, not an error)."""
    return (
        "(function(){var tok=%d;if(global.__w11m&&global.__w11m[tok]){var m=global.__w11m[tok];"
        "try{if(m.h1)global.display.disconnect(m.h1);}catch(e){}"
        "try{if(m.h2)global.display.disconnect(m.h2);}catch(e){}"
        "try{m.grp.destroy();}catch(e){}delete global.__w11m[tok];return 'ok';}return 'gone';})()"
        % int(token))


def status_program(token: int) -> str:
    """`true` while the group is still parented on the stage, else `false`. Its own `JSON.stringify`, so it is
    read through `Eval._json`."""
    return ("(function(){var tok=%d;return JSON.stringify(!!(global.__w11m&&global.__w11m[tok]"
            "&&global.__w11m[tok].grp&&global.__w11m[tok].grp.get_parent()));})()" % int(token))


# -- Eval client --------------------------------------------------------------

class Eval:
    """One `org.Cinnamon.Eval` round trip, shaped like hacks/window/backend_cinnamon.py's `_eval`/`_json` (the
    same bus, the same decode-once / decode-twice split, the same `(false, text)` -> one line)."""

    def __init__(self, bus: Bus | None = None):
        self.bus = bus or Bus()

    def eval(self, script: str):
        try:
            ok, out = self.bus.call(BUS_NAME, OBJECT_PATH, IFACE, "Eval", "s",
                                    (script,), timeout=CALL_TIMEOUT)
        except DBusError as e:
            if e.name in (ERR + "ServiceUnknown", ERR + "NameHasNoOwner"):
                raise CmdError(_GONE) from None
            raise CmdError("cinnamon mirror: Eval failed: %s" % e) from None
        if not ok:
            raise CmdError("cinnamon mirror: Eval failed: %s" % (out or "(no message)"))
        try:
            return json.loads(out) if out != "" else None
        except ValueError:
            raise CmdError("cinnamon mirror: Eval returned malformed JSON: %s" % out[:200]) from None

    def json(self, script: str):
        raw = self.eval(script)
        if not isinstance(raw, str):
            raise CmdError("cinnamon mirror: expected a JSON string, got %r" % (raw,))
        try:
            return json.loads(raw)
        except ValueError:
            raise CmdError("cinnamon mirror: Eval returned malformed JSON: %s" % raw[:200]) from None


def available(bus: Bus | None = None) -> bool:
    """Is `org.Cinnamon` owned on the session bus? -- i.e. can we Eval here at all. Never raises: a missing bus
    is a `False`, not a traceback, so `--check` and the start fork degrade cleanly on a non-Cinnamon session."""
    try:
        return (bus or Bus()).name_has_owner(BUS_NAME)
    except (DBusError, OSError):
        return False


# -- lifetime -----------------------------------------------------------------
#
# A cinnamon record carries `kind: "cinnamon"` and a `token`, and NO pids: the mirror lives inside muffin, not
# in a process we forked. `supervise.reap` skips it (its `if rec.get("kind")` guard) and this module's `reap`
# owns its liveness instead, over Eval.

def start(recs: dict, source: str, target: str, region, dst, bus: Bus | None = None):
    """Build the mirror over Eval and write its record. Returns None on success, else the lines to print.

    `dst` is the target output (a wxcore.OutputState): its layout origin is where the group is placed, in the
    same coordinates as `region`. A region that covers the whole source is a valid mirror too, so `region` is
    never None here -- `core.decide` has already refused the cases the layout expresses on its own.

    No `scaling`: the group is placed 1:1 and clipped, so there is no mode to record. The record carries no
    `scaling` key and `fmt_record` prints `scaling 1:1`, because a record that carried `fit` would have `--list`
    claiming a letterbox that never happened -- wmirror/cli.py refuses an explicit `--scaling` here instead."""
    try:
        ev = Eval(bus)
    except DBusError as e:
        return ["cinnamon mirror: %s" % e]
    prog = build_program(region, dst.x, dst.y)
    try:
        token = ev.eval(prog)
    except CmdError as e:
        return [str(e)]
    if not isinstance(token, int):
        return ["cinnamon mirror: the build program did not return a token (got %r)" % (token,)]
    recs[target] = {"kind": KIND, "source": source, "target": target,
                    "region": list(region), "token": token,
                    "route": ROUTE}
    return None


def _alive_on(ev: "Eval", rec: dict) -> bool:
    """Is that mirror still on the stage, over an Eval client already open? Any failure -- a dead bus, a
    malformed answer -- reads as gone, so a stale record is reaped rather than kept forever."""
    token = rec.get("token")
    if not isinstance(token, int):
        return False
    try:
        return bool(ev.json(status_program(token)))
    except (CmdError, DBusError, OSError):
        return False


def alive(rec: dict, bus: Bus | None = None) -> bool:
    """Is that mirror still on the stage? A bus we cannot reach means the compositor is gone, so the mirror is
    gone -- False."""
    try:
        return _alive_on(Eval(bus), rec)
    except (CmdError, DBusError, OSError):
        return False


def stop_record(rec: dict, bus: Bus | None = None) -> bool:
    """Destroy the mirror. True if it was there to destroy. A bus that is already gone took the mirror with it,
    so that is a successful stop of nothing, not a failure."""
    token = rec.get("token")
    if not isinstance(token, int):
        return False
    try:
        return Eval(bus).eval(destroy_program(token)) == "ok"
    except (CmdError, DBusError, OSError):
        return False


def reap(recs: dict, bus: Bus | None = None) -> bool:
    """Drop the cinnamon records whose actor is gone. Opens one bus only if there is a cinnamon record to
    check, so a session with none pays nothing. Mutates `recs`; returns whether anything changed."""
    tokens = [t for t, r in recs.items() if isinstance(r, dict) and r.get("kind") == KIND]
    if not tokens:
        return False
    try:
        ev = Eval(bus)
    except (DBusError, OSError):
        # the whole session bus is unreachable: every cinnamon mirror is gone with it.
        for t in tokens:
            del recs[t]
        return True
    changed = False
    for t in tokens:
        if not _alive_on(ev, recs[t]):
            del recs[t]
            changed = True
    return changed


def fmt_record(target: str, rec: dict) -> str:
    """One `--list` line for a cinnamon mirror -- also what a successful start prints. Shaped like
    `core.fmt_record`'s wl-mirror line, with the Eval route where the helper pid would be."""
    bits = ["%s <- %s" % (target, rec.get("source", "?"))]
    region = rec.get("region")
    if region:
        bits.append("region %s" % core.fmt_region(region))
    # 1:1, not `rec.get("scaling")`: nothing on this path scales, and a record written before that was said
    # out loud may still carry a stale mode -- the line says what the mirror does, not what was asked for.
    bits.append("scaling 1:1")
    bits.append("%s tok %s" % (rec.get("route") or ROUTE, rec.get("token", "?")))
    return "  ".join(bits)
