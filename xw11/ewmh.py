"""`SendEvent`: the EWMH `ClientMessage`s, decoded and routed to the backend.

Design section 3.4's table. Every EWMH verb the four tools have is one of
these: `wmctrl -a`, `-R`, `-c`, `-r -e`, `-r -b`, `-s`, `-k`, `xdotool
windowactivate`, `windowminimize`, `windowstate`, `set_desktop`. They are all
one shape on the wire -- `SendEvent(destination, propagate = False, mask =
SubstructureNotify|SubstructureRedirect = 0x180000)` carrying a 32-byte
`ClientMessage` of format 32 [recon/tools.md 4.4, measured field by field] --
and the routing is decided by two fields INSIDE that event and not by the
request's own destination:

* `window` at offset 4 of the event is the TARGET: the window for a per-window
  message, the root for a desktop one. `wmctrl -c <n>` sends its
  `_NET_CLOSE_WINDOW` to the ROOT with `cm_window = <the window>`
  [M recon/tools/caps/xwl.jsonl MARK 46], which is why the policy row cannot
  decide this and the handler must;
* `type` at offset 8 is the verb.

**The real-id rule** (design section 3.4's last row, the dual-plane rule
`wwmctl` already follows [recon/seams.md 2.1]): a routed type sent to a REAL X
window passes untouched when the xwm answers it itself -- the compositor would
otherwise do it twice -- and is routed through that window's paired compositor
handle when the xwm does not. The xwm answers it itself in two cases, and
`_NET_SUPPORTED` only names the first:

* an EWMH type in upstream's own `_NET_SUPPORTED`. wlroots' Xwayland root names
  19 atoms and has no `_NET_WM_DESKTOP`, no `_NET_MOVERESIZE_WINDOW` and no
  desktop atom at all [recon/env.md 2.1], so `wmctrl -R <an xterm>` moves an X
  window between workspaces through sway's own IPC while `wmctrl -a <that
  xterm>` still goes to the xwm;
* an ICCCM type -- `WM_PROTOCOLS`, `WM_CHANGE_STATE` -- which NO
  `_NET_SUPPORTED` list ever names (the atom hint is an EWMH invention and
  those two predate it), and which the xwm reads on its own dedicated path
  (`xwm_handle_wm_protocols_message`, `xwm_handle_wm_change_state`). Testing
  those against `_NET_SUPPORTED` would eat every one of them: a client's
  `_NET_WM_PING` pong about its own real window would never reach the
  compositor that pinged it, and the client would look hung on Mutter, KWin and
  wlroots alike.

`WM_PROTOCOLS` is narrower still, on a shadow as well: design section 3.4 routes
it only for the shape a polite closer sends -- the message addressed TO the
window it is about, with an empty event mask. A `WM_PROTOCOLS` to the root, or
one carrying a real mask, is somebody else's traffic and passes.

A routed message is CONSUMEd even when the backend refuses: X answers a
`ClientMessage` with nothing at all, so there is nothing to say to the client
and the log carries the reason (design section 3.3).
"""

import struct

from wdotool import backend as backend_mod
from xw11 import policy, req_write, wire

#: `SendEvent` is 11 words, 44 bytes: the header, `destination` at 4,
#: `event_mask` at 8, and the 32-byte event at 12 [recon/wire.md 4.2].
EVENT_AT = 12
SEND_EVENT_BYTES = 44

#: `_NET_MOVERESIZE_WINDOW`'s flag bits: gravity in 0-7, then one bit per
#: value that is present. `wmctrl -r <n> -e 0,10,10,300,200` sends `0xf00`,
#: which is all four [M recon/tools/caps/xvfb.jsonl MARK 39, and
#: recon/tools.md 4.4].
MR_X = 1 << 8
MR_Y = 1 << 9
MR_WIDTH = 1 << 10
MR_HEIGHT = 1 << 11

#: `WM_CHANGE_STATE`'s only interesting value: `IconicState`, which is how both
#: `xdotool windowminimize` and every ICCCM client ask to be minimized
#: [recon/tools.md 4.3: `data32 = [3, 0, 0, 0, 0]`].
ICONIC_STATE = 3

#: `_NET_WM_DESKTOP`'s "on every desktop" [recon/tools.md 4.4, `wmctrl -R`].
ALL_DESKTOPS = 0xFFFFFFFF

#: The prefix `_NET_WM_STATE` atoms carry and `WindowBackend.set_state` does
#: not: the backend contract takes the uppercase suffix ("MAXIMIZED_VERT")
#: [recon/seams.md 2.1, wdotool/backend.py:350].
STATE_PREFIX = "_NET_WM_STATE_"

#: The two routed types that are ICCCM, not EWMH. No `_NET_SUPPORTED` list
#: names either -- the atom hint came with the EWMH and these two predate it --
#: so the dual-plane rule cannot ask that list about them: on a REAL X id the
#: xwm reads them on its own path and they PASS.
ICCCM_TYPES = ("WM_PROTOCOLS", "WM_CHANGE_STATE")


def send_event(server, conn, req):
    """One `SendEvent`. `policy.FORWARD` to pass it on, None to eat it.

    Everything that is not a format-32 `ClientMessage` of a routed type passes
    byte for byte, which is most of what a display carries: selection
    handshakes, `WM_TAKE_FOCUS`, a client's own synthetic `KeyPress`.
    """
    frame = req.frame
    if len(frame) < SEND_EVENT_BYTES or server.shadows is None:
        return policy.FORWARD
    dest, mask = struct.unpack_from("<II", frame, 4)
    ev = frame[EVENT_AT:EVENT_AT + 32]
    if (ev[0] & 0x7F) != wire.EV_CLIENT_MESSAGE or ev[1] != 32:
        return _not_a_client_message(server, req, dest)
    window, type_atom = struct.unpack_from("<II", ev, 4)
    data = struct.unpack_from("<5I", ev, 12)
    name = server.own.atom_name(type_atom) if server.own is not None else None
    if name not in policy.ROUTED_TYPES:
        return policy.FORWARD
    if name in policy.ROUTED_ROOT_TYPES:
        return _root_route(server, name, window, data)
    if name == "WM_PROTOCOLS" and not (dest == window and mask == 0):
        # Not the closer's shape [design section 3.4]: a `_NET_WM_PING` pong
        # goes to the ROOT about the client's own window, and a WM's
        # `WM_TAKE_FOCUS` carries a mask. Both are traffic between somebody
        # else's two ends and the proxy has no business in either.
        return policy.FORWARD
    # THROUGH the 20 ms TTL, the same seam `req_write._entry` takes [design
    # section 4.8]: `moveresize_window` reads the window's current x/y for an
    # axis the message omits, and a toplevel that appeared since the last
    # re-list is otherwise "a window nobody knows".
    server.shadows.snapshot()
    entry = server.shadows.entry_for(window)
    if entry is None:
        # A window the registry does not know: a pixmap, a dead shadow, an
        # override-redirect X window the compositor never listed. Upstream
        # answers for it, `BadWindow` and all [recon/tools.md 9].
        return policy.FORWARD
    if entry.xid and _xwm_handles(server, name, type_atom, window):
        return policy.FORWARD
    got = WINDOW_ROUTES[name](server, entry, data, name)
    if got is policy.FORWARD:
        return got
    server.settle(entry.id)
    return None


def _xwm_handles(server, name: str, type_atom: int, window: int) -> bool:
    """The dual-plane rule for a REAL X id: does the xwm answer this message
    itself?

    Two ways it does, and `_NET_SUPPORTED` can only answer for one of them:

    * an EWMH type upstream's own list names, read from the list `OwnConn`
      fetched at open [design section 2.4] and never from a name written down
      here -- the answer is the xwm's and it differs per compositor;
    * an ICCCM type, which that list never names at all. wlroots reads
      `WM_PROTOCOLS` in `xwm_handle_wm_protocols_message` and `WM_CHANGE_STATE`
      in `xwm_handle_wm_change_state`, Mutter and KWin in their own equivalents,
      and every one of them is the X client's window manager for that window.
      Asking `_NET_SUPPORTED` about these two would answer no forever and eat
      messages that are already handled one plane down.
    """
    if name in ICCCM_TYPES:
        server.debug_say("%s on real window 0x%x: an ICCCM message no "
                         "_NET_SUPPORTED list ever names, and the xwm reads it "
                         "on its own path -- passed" % (name, window))
        return True
    own = server.own
    if own is not None and type_atom in own.upstream_supported:
        server.debug_say("%s on real window 0x%x: upstream's _NET_SUPPORTED "
                         "names it, so its xwm does it" % (name, window))
        return True
    return False


def _not_a_client_message(server, req, dest: int):
    """A `SendEvent` of something else. It passes, and a synthetic event aimed
    at a SHADOW earns a line: upstream will answer `BadWindow` for an id it
    never minted. Delivering it to the shadow's own selectors instead is NOT
    YET -- the machinery is batch 5's event writer and the route is this proxy
    (AGENTS.md rung 5); `xdotool key --window <shadow>` is the one command that
    would use it [recon/tools.md 4.5]."""
    if req.target == req_write.SHADOW or server.shadows.is_shadow(dest):
        server.say("SendEvent of event code %d to shadow 0x%x: forwarded, and "
                   "upstream answers BadWindow for an id it never minted. "
                   "Delivering a client's synthetic event to a shadow's own "
                   "selectors is not yet done here; the route is this proxy "
                   "(AGENTS.md rung 5)"
                   % (req.frame[EVENT_AT] & 0x7F, dest))
    return policy.FORWARD


# -- the per-window routes ----------------------------------------------------


def active_window(server, entry, data, name):
    """`_NET_ACTIVE_WINDOW` -> `backend.activate`. `data32[0]` is the source
    indication and is ignored: xdotool sends 2 (pager) and wmctrl sends 0
    [recon/tools.md 4.4], and no backend takes one."""
    req_write.call(server, "activate", entry.handle)


def close_window(server, entry, data, name):
    """`_NET_CLOSE_WINDOW` -> `backend.close` (`wmctrl -c`)."""
    req_write.call(server, "close", entry.handle)


def wm_state(server, entry, data, name):
    """`_NET_WM_STATE` -> `set_state` per name, folded through
    `backend.state_steps`.

    `data32[0]` is the action -- 0 remove, 1 add, 2 toggle -- which is exactly
    `WindowBackend.set_state`'s own encoding (wdotool/backend.py:350), and
    `data32[1..2]` are one or two state atoms. `state_steps`
    (wdotool/backend.py:100) turns the names into the calls that express them:
    two maximize axes side by side become ONE call wherever the backend names
    the pair (GNOME's "MAXIMIZED"), because Mutter unmaximizes to the window's
    current frame rect and a second single-axis call carries the still
    maximized half into its target.

    Measured senders: `xdotool windowstate --add MAXIMIZED_VERT` sends
    `[1, 0x134, 0, 0, 0]` and `wmctrl -r <n> -b add,maximized_vert` sends the
    same pair [M recon/tools/caps/xvfb2.jsonl MARK 5, xwl.jsonl MARK 40].
    """
    action = int(data[0])
    names = []
    for atom in (data[1], data[2]):
        if not atom:
            continue
        got = server.own.atom_name(atom) if server.own is not None else None
        if got and got.startswith(STATE_PREFIX):
            names.append(got[len(STATE_PREFIX):])
        else:
            server.say("_NET_WM_STATE on 0x%x names atom %d (%s), which is not "
                       "a _NET_WM_STATE_* name: ignored"
                       % (entry.id, atom, got or "unresolved"))
    if not names:
        return
    for state, folded in backend_mod.state_steps(server.backend, names):
        server.debug_say("_NET_WM_STATE action %d %s -> set_state(%s)"
                         % (action, ",".join(folded), state))
        req_write.call(server, "set_state", entry.handle, state, action)


def wm_desktop(server, entry, data, name):
    """`_NET_WM_DESKTOP` -> `set_window_desktop`, and `0xFFFFFFFF` ->
    `set_state(STICKY, add)`: "on every desktop" is a state on every backend in
    the tree and a desktop number on none of them. `wmctrl -R <n>` sends it
    with the desktop it just read [recon/tools.md 4.4, 5]."""
    if data[0] == ALL_DESKTOPS:
        req_write.call(server, "set_state", entry.handle, "STICKY", 1)
        return
    req_write.call(server, "set_window_desktop", entry.handle, int(data[0]))


def _int32(word: int) -> int:
    """One of `_NET_MOVERESIZE_WINDOW`'s coordinates out of its `data32` slot.
    `data.l[]` is a signed long and the wire word is its low 32 bits, so
    `wmctrl -r <n> -e 0,-10,-10,300,200` puts `0xFFFFFFF6` on the wire and
    means -10 -- the same INT16-in-a-CARD32 story `req_write._int16` tells for
    `ConfigureWindow`, one width up."""
    return ((word & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


def moveresize_window(server, entry, data, name):
    """`_NET_MOVERESIZE_WINDOW` -> `move_window` and `resize`.

    `data32[0]`'s bits 8-11 say which of `data32[1..4]` are present; the low
    byte is the gravity, which no backend takes (a compositor places a toplevel
    itself) and which the measured sender leaves at 0 anyway
    [`0xf00` = x|y|w|h, source 0; recon/tools.md 4.4]. A missing axis keeps the
    window's current one, the way `ConfigureWindow` does.
    """
    flags = int(data[0])
    win = entry.window
    if flags & (MR_X | MR_Y):
        x = _int32(data[1]) if flags & MR_X else int(win.x)
        y = _int32(data[2]) if flags & MR_Y else int(win.y)
        req_write.call(server, "move_window", entry.handle, x, y)
    if flags & (MR_WIDTH | MR_HEIGHT):
        w = int(data[3]) if flags & MR_WIDTH else int(win.w)
        h = int(data[4]) if flags & MR_HEIGHT else int(win.h)
        req_write.call(server, "resize", entry.handle, w, h)


def change_state(server, entry, data, name):
    """`WM_CHANGE_STATE` with `IconicState` -> `backend.minimize`. Every other
    value is ICCCM's `NormalState` and friends, which a compositor expresses by
    activating the window; nothing measured sends one [recon/tools.md 4.3].

    Native toplevels only: `send_event`'s ICCCM arm passes the message for a
    real X id, where the xwm reads it itself (wlroots'
    `xwm_handle_wm_change_state` -> sway's `handle_request_minimize`), so
    `xdotool windowminimize <the xterm>` does through the proxy exactly what it
    does without one. Sway's own answer to that request is a flag and no
    visible change; making it move the window is the compositor's gap, and
    doing it here on a window the xwm manages would be a second, different
    minimize on top of the first.
    """
    if int(data[0]) == ICONIC_STATE:
        req_write.call(server, "minimize", entry.handle)
        return
    server.say("WM_CHANGE_STATE %d on 0x%x: only IconicState (3) has a backend "
               "verb; not yet for the rest, and the route is the compositor's "
               "own window state (AGENTS.md rung 2). Consumed."
               % (data[0], entry.id))


def protocols(server, entry, data, name):
    """`WM_PROTOCOLS`/`WM_DELETE_WINDOW` sent to the window itself, mask 0 --
    what a polite closer sends, and what design section 4.4's `WM_PROTOCOLS`
    property promises will work. Without it the closer's fallback is
    `XKillClient`, which is SIGKILL.

    `send_event` has already checked the shape (destination = the window the
    message is about, empty mask) and that the entry is a native toplevel: a
    real X window's own `WM_DELETE_WINDOW` is the xwm's to deliver, and a
    `_NET_WM_PING` pong -- `WM_PROTOCOLS` to the ROOT about the client's own
    window -- must reach the compositor that pinged or the client looks hung.
    """
    delete = server.own.atom_id("WM_DELETE_WINDOW") if server.own else 0
    if delete and int(data[0]) == delete:
        req_write.call(server, "close", entry.handle)
        return
    got = server.own.atom_name(data[0]) if server.own is not None else None
    server.say("WM_PROTOCOLS %s on 0x%x: only WM_DELETE_WINDOW is routed -- "
               "the rest are messages a window manager sends TO a client. "
               "Consumed." % (got or data[0], entry.id))


# -- the root routes ----------------------------------------------------------


def current_desktop(server, name, window, data):
    """`_NET_CURRENT_DESKTOP` -> `backend.set_desktop` (`wmctrl -s 1`,
    `xdotool set_desktop 1`, and the first of `windowactivate`'s two
    messages)."""
    req_write.call(server, "set_desktop", int(data[0]))


def number_of_desktops(server, name, window, data):
    """`_NET_NUMBER_OF_DESKTOPS` -> `backend.set_num_desktops` (`wmctrl -n`).
    Most backends refuse: sway has no verb for creating a workspace that holds
    no window, and the refusal is one log line (design section 3.3)."""
    req_write.call(server, "set_num_desktops", int(data[0]))


def showing_desktop(server, name, window, data):
    """`_NET_SHOWING_DESKTOP` (`wmctrl -k on`) is NOT YET: no backend in the
    tree has a "show the desktop" verb, so there is nothing to route it to.

    It passes upstream, where wlroots' xwm ignores it as well, and one line
    says what would close the gap: the compositor's own verb over its own bus
    (AGENTS.md rung 2) -- sway's `move scratchpad` over its IPC, KWin's
    `showingDesktop` over D-Bus, Mutter's overview through the bridge we
    already install (rung 3 there). `wmctrl -k` is 10 requests and the cheapest
    command in the whole survey [recon/tools.md 5]; the work is one verb per
    backend, not a protocol.
    """
    server.say("_NET_SHOWING_DESKTOP %d: not yet -- no window backend in this "
               "tree has a show-the-desktop verb, so the message is forwarded "
               "to the X server unchanged. The route is the compositor's own "
               "one over its own bus (AGENTS.md rung 2; rung 3 on GNOME, where "
               "the bridge is ours)" % (data[0],))
    return policy.FORWARD


def _root_route(server, name, window, data):
    got = ROOT_ROUTES_BY_NAME[name](server, name, window, data)
    if got is policy.FORWARD:
        return got
    server.settle(window)
    return None


#: name -> the per-window route, one per `policy.ROUTED_WINDOW_TYPES`.
WINDOW_ROUTES = {
    "_NET_ACTIVE_WINDOW": active_window,
    "_NET_CLOSE_WINDOW": close_window,
    "_NET_WM_STATE": wm_state,
    "_NET_WM_DESKTOP": wm_desktop,
    "_NET_MOVERESIZE_WINDOW": moveresize_window,
    "WM_CHANGE_STATE": change_state,
    "WM_PROTOCOLS": protocols,
}

#: name -> the root route, one per `policy.ROUTED_ROOT_TYPES`.
ROOT_ROUTES_BY_NAME = {
    "_NET_CURRENT_DESKTOP": current_desktop,
    "_NET_NUMBER_OF_DESKTOPS": number_of_desktops,
    "_NET_SHOWING_DESKTOP": showing_desktop,
}

HANDLERS = {wire.OP_SEND_EVENT: send_event}


def install(server) -> None:
    """Put the `SendEvent` router into a server's table, the way
    `req_read.install` and `req_write.install` do."""
    server.handlers.update(HANDLERS)
