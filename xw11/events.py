"""The events a compositor's changes become, and who is written to.

The part the clones cannot express. `wdotool`, `wwmctl` and `wxprop` answer a
question and exit; a client that selected an event mask **blocks**, issues no
further request and waits to be told. `xprop -spy` sends
`ChangeWindowAttributes(StructureNotify|PropertyChange)` -- `0x420000` -- on its
target and then nothing at all [M recon/tools.md 6, caps/events2.jsonl];
`xdotool behave <w> mouse-enter` sends `EnterWindow` -- `0x10` -- and then
nothing [M recon/tools.md 4.7, caps/events.jsonl]. Both die on a 6 s timeout
with RC 124 today [recon/tools.md 11 rows 29 and 51]. What ends that is this
file: a change the compositor reported, turned into the 32 bytes X would have
sent, written to every client whose mask has the bit.

Four rules, each measured:

* **32 bytes, and byte 0 is never 11.** Every core event is 32 bytes with the
  code in byte 0 and the sequence in bytes 2-3 [recon/wire.md 4.4]; the one
  exception is `KeymapNotify` (11), whose bytes 1..31 are the keymap and which
  has no sequence at all [recon/wire.md 3.1]. This file never builds one.
* **the sequence an event carries is the receiving client's own.** It is a
  WATERMARK of that connection's request stream, not a counter of its own:
  seven `NoOperation`s and a `ChangeProperty` gave `61/69/77` on three runs
  [M recon/wire.md 3.2b]. Two clients that have sent different numbers of
  requests get the same change with different numbers in bytes 2-3, which is
  why delivery patches per client and never once -- and the watermark is held
  one below any reply the client is still owed, because libxcb reads an event
  as proof that everything under it is finished (`watermark`).
* **byte 0 without `0x80`.** `0x80` means `SendEvent` [recon/wire.md 4.4], and
  these come from the server the proxy is standing in for.
* **over-delivery is free.** A `PropertyNotify` for an atom xprop is not
  watching costs the client ZERO requests -- four arrived per rename and only
  the `WM_NAME` one drew a `GetProperty` [M recon/tools.md 6 (b)]. So where
  the diff is ambiguous this file emits, and never suppresses.

The two-packet rule is X's own and is reproduced literally: `UnmapNotify`,
`DestroyNotify` and `ConfigureNotify` go out TWICE for one change -- once with
`event = window` to the `StructureNotify` selectors on that window, once with
`event = root` to the `SubstructureNotify` selectors on the root (design
section 5.3). A window watcher on the root and `xprop -spy` on the window are
two different clients and each gets the `event` field X would have given it.
"""

import collections
import struct

from xw11 import shadow as shadow_mod

#: The event masks a client can put on a shadow or the root that this file
#: delivers on [recon/wire.md 4.2's `CWEventMask`, design section 5.4]. The two
#: measured selections are `xprop -spy`'s `StructureNotify|PropertyChange`
#: (`0x420000`) and `behave`'s `EnterWindow` (`0x10`).
ENTER_WINDOW = 0x00000010
LEAVE_WINDOW = 0x00000020
POINTER_MOTION = 0x00000040
STRUCTURE_NOTIFY = 0x00020000
SUBSTRUCTURE_NOTIFY = 0x00080000
FOCUS_CHANGE = 0x00200000
PROPERTY_CHANGE = 0x00400000

#: The bits whose presence on a shadow arms the pointer sampler of design
#: section 5.6.
POINTER_MASK = ENTER_WINDOW | LEAVE_WINDOW | POINTER_MOTION

#: How often that sampler reads a pointer position while it is armed (design
#: section 5.6). 50 ms, which is `xdotool behave`'s own granularity of care:
#: it blocks on the socket and prints when told.
POINTER_POLL = 0.05

#: `PropertyNotify`'s `state` byte [recon/wire.md 4.4].
NEW_VALUE, DELETED = 0, 1

#: `FocusIn`/`FocusOut`'s `detail` and `mode`: `NotifyNonlinear` (3) and
#: `NotifyNormal` (0) [/usr/share/xcb/xproto.xml, `NotifyDetail`/`NotifyMode`].
#: Nonlinear because focus moves between two toplevels that are not each
#: other's ancestors, which is every focus change a compositor reports.
NOTIFY_NONLINEAR = 3
NOTIFY_NORMAL = 0

#: Event codes [recon/wire.md 4.4, /usr/share/xcb/xproto.xml].
MOTION_NOTIFY = 6
ENTER_NOTIFY = 7
LEAVE_NOTIFY = 8
FOCUS_IN = 9
FOCUS_OUT = 10
CREATE_NOTIFY = 16
DESTROY_NOTIFY = 17
UNMAP_NOTIFY = 18
MAP_NOTIFY = 19
CONFIGURE_NOTIFY = 22
PROPERTY_NOTIFY = 28

#: The one code this file never builds: `KeymapNotify` has no sequence field
#: and its 31 body bytes are the keymap [recon/wire.md 3.1].
KEYMAP_NOTIFY = 11


class Event(collections.namedtuple("Event", "target bit packet what")):
    """One packet and the mask that decides who gets it.

    `target` is the window whose `client.masks` entry is consulted -- the
    window itself for a `StructureNotify`, the ROOT for a `SubstructureNotify`,
    which is why this and the `event` field inside the packet are two different
    things. `what` is for the log and for a test to read; it is never on the
    wire.
    """

    __slots__ = ()


def _int16(n: int) -> int:
    """An INT16 field takes an INT16. `req_read._clamp16`'s rule, repeated
    rather than imported: a compositor is free to answer a rect that does not
    fit the wire (measured in `test_xw11_read.py`'s
    `GeometryAttributesCoordinates`, which is where the rule was written), X
    truncates such a field, and the `ConfigureNotify` for a window must not
    disagree with the `GetGeometry` for the same window."""
    return ((int(n) + 0x8000) & 0xFFFF) - 0x8000


def _card16(n: int) -> int:
    """A CARD16 field takes a CARD16, and a width is never negative: a backend
    answering one is answering "unknown", which is zero on the wire
    (`req_read._clamp_card16`)."""
    return max(int(n), 0) & 0xFFFF


def _pkt(code: int, byte1: int, body: bytes) -> bytes:
    """One 32-byte event: code, the code-specific byte, a zero sequence (the
    receiving client's own goes in at delivery) and the body, zero-padded."""
    out = struct.pack("<BBH", code, byte1, 0) + body
    if len(out) > 32:
        raise ValueError("event %d is %d bytes" % (code, len(out)))
    return out + b"\0" * (32 - len(out))


# -- the eleven builders (recon/wire.md 4.4, /usr/share/xcb/xproto.xml) --------


def create_notify(parent, window, x, y, w, h, border=0, override=0) -> bytes:
    """`CreateNotify` (16): 4-7 parent, 8-11 window, 12-13 x, 14-15 y, 16-17
    width, 18-19 height, 20-21 border_width, 22 override_redirect."""
    return _pkt(CREATE_NOTIFY, 0,
                struct.pack("<IIhhHHHBx", parent & 0xFFFFFFFF,
                            window & 0xFFFFFFFF, _int16(x), _int16(y),
                            _card16(w), _card16(h), _card16(border),
                            1 if override else 0))


def destroy_notify(event, window) -> bytes:
    """`DestroyNotify` (17): 4-7 event, 8-11 window, the rest pad."""
    return _pkt(DESTROY_NOTIFY, 0,
                struct.pack("<II", event & 0xFFFFFFFF, window & 0xFFFFFFFF))


def unmap_notify(event, window, from_configure=0) -> bytes:
    """`UnmapNotify` (18): 4-7 event, 8-11 window, 12 from_configure."""
    return _pkt(UNMAP_NOTIFY, 0,
                struct.pack("<IIB", event & 0xFFFFFFFF, window & 0xFFFFFFFF,
                            1 if from_configure else 0))


def map_notify(event, window, override=0) -> bytes:
    """`MapNotify` (19): 4-7 event, 8-11 window, 12 override_redirect."""
    return _pkt(MAP_NOTIFY, 0,
                struct.pack("<IIB", event & 0xFFFFFFFF, window & 0xFFFFFFFF,
                            1 if override else 0))


def configure_notify(event, window, x, y, w, h, above=0, border=0,
                     override=0) -> bytes:
    """`ConfigureNotify` (22): 4-7 event, 8-11 window, 12-15 above_sibling,
    16-17 x, 18-19 y, 20-21 width, 22-23 height, 24-25 border_width,
    26 override_redirect.

    `above_sibling` is 0 (`None`): no backend in the tree reports a stacking
    order -- sway's tree is a layout, not a stack (design section 4.6, R13) --
    and a proxy that invented a sibling would be telling a window watcher a
    stacking it made up."""
    return _pkt(CONFIGURE_NOTIFY, 0,
                struct.pack("<IIIhhHHHBx", event & 0xFFFFFFFF,
                            window & 0xFFFFFFFF, above & 0xFFFFFFFF,
                            _int16(x), _int16(y), _card16(w), _card16(h),
                            _card16(border), 1 if override else 0))


def property_notify(window, atom, state=NEW_VALUE, time=0) -> bytes:
    """`PropertyNotify` (28): 4-7 window, 8-11 atom, 12-15 time, 16 state.

    `time` is 0 everywhere here: no backend in the tree timestamps a change
    (`Window` and `View` carry none [recon/seams.md 2.1]), and `CurrentTime`
    is what X itself puts in a property event a window manager caused. Nothing
    the four tools do reads it [recon/tools.md 6]."""
    return _pkt(PROPERTY_NOTIFY, 0,
                struct.pack("<IIIB", window & 0xFFFFFFFF, atom & 0xFFFFFFFF,
                            time & 0xFFFFFFFF, state & 0xFF))


def focus_in(event, detail=NOTIFY_NONLINEAR, mode=NOTIFY_NORMAL) -> bytes:
    """`FocusIn` (9): byte 1 detail, 4-7 event, 8 mode."""
    return _pkt(FOCUS_IN, detail & 0xFF,
                struct.pack("<IB", event & 0xFFFFFFFF, mode & 0xFF))


def focus_out(event, detail=NOTIFY_NONLINEAR, mode=NOTIFY_NORMAL) -> bytes:
    """`FocusOut` (10): the same layout, byte for byte -- xproto.xml declares it
    as an `<eventcopy>` of `FocusIn` [recon/wire.md 4.4]."""
    return _pkt(FOCUS_OUT, detail & 0xFF,
                struct.pack("<IB", event & 0xFFFFFFFF, mode & 0xFF))


#: Byte 31 of a crossing event is a BITMASK, not a flag: bit 0 is `focus` and
#: bit 1 is `same-screen` (`Xlib.h`'s `ELFlagFocus 1`, `ELFlagSameScreen 2`;
#: xproto.xml calls the whole byte `same_screen_focus`). A `1` there says
#: "another screen, and the window has the focus", which is not what X sends
#: for a pointer entering a toplevel on the only screen this proxy has. Every
#: crossing here is on that one screen, so bit 1 is always set and bit 0 says
#: whether the window the event names is the focused one.
CROSSING_FOCUS = 0x01
CROSSING_SAME_SCREEN = 0x02


def _crossing(code, event, root, child, root_x, root_y, event_x, event_y,
              detail=NOTIFY_NONLINEAR, mode=NOTIFY_NORMAL, time=0, state=0,
              same_screen_focus=CROSSING_SAME_SCREEN) -> bytes:
    """`EnterNotify` (7) and `LeaveNotify` (8) share one layout
    [/usr/share/xcb/xproto.xml]: byte 1 detail, 4-7 time, 8-11 root, 12-15
    event, 16-19 child, 20-21 root_x, 22-23 root_y, 24-25 event_x, 26-27
    event_y, 28-29 state, 30 mode, 31 same_screen_focus."""
    return _pkt(code, detail & 0xFF,
                struct.pack("<IIIIhhhhHBB", time & 0xFFFFFFFF,
                            root & 0xFFFFFFFF, event & 0xFFFFFFFF,
                            child & 0xFFFFFFFF, _int16(root_x),
                            _int16(root_y), _int16(event_x), _int16(event_y),
                            state & 0xFFFF, mode & 0xFF,
                            same_screen_focus & 0xFF))


def enter_notify(event, root, child=0, root_x=0, root_y=0, event_x=0,
                 event_y=0, **kw) -> bytes:
    return _crossing(ENTER_NOTIFY, event, root, child, root_x, root_y,
                     event_x, event_y, **kw)


def leave_notify(event, root, child=0, root_x=0, root_y=0, event_x=0,
                 event_y=0, **kw) -> bytes:
    return _crossing(LEAVE_NOTIFY, event, root, child, root_x, root_y,
                     event_x, event_y, **kw)


def motion_notify(event, root, child=0, root_x=0, root_y=0, event_x=0,
                  event_y=0, detail=0, time=0, state=0, same_screen=1) -> bytes:
    """`MotionNotify` (6): byte 1 detail (0 `Normal`), 4-7 time, 8-11 root,
    12-15 event, 16-19 child, 20-21 root_x, 22-23 root_y, 24-25 event_x,
    26-27 event_y, 28-29 state, 30 same_screen, 31 pad
    [/usr/share/xcb/xproto.xml]."""
    return _pkt(MOTION_NOTIFY, detail & 0xFF,
                struct.pack("<IIIIhhhhHBx", time & 0xFFFFFFFF,
                            root & 0xFFFFFFFF, event & 0xFFFFFFFF,
                            child & 0xFFFFFFFF, _int16(root_x),
                            _int16(root_y), _int16(event_x), _int16(event_y),
                            state & 0xFFFF, 1 if same_screen else 0))


# -- what a change becomes (design section 5.3) --------------------------------

#: A backend field name that moved -> the property names that changed with it,
#: in the order the X plane would emit them. `wxprop`'s own tables
#: (`_NATIVE_EVENT_PROPS`/`_VIEW_EVENT_PROPS`, wxprop/core.py:1031-1052) keyed
#: by the compositor's event vocabulary; `Shadows._update` hands over the FIELD
#: names it diffed instead (`recon/seams.md 4`, requests-batch-2.md item 7), so
#: the same mapping is written here against those. The `_NET_WM_STATE` crowd is
#: every field of `shadow._STATE_BITS`, plus the two tree-node flags a
#: view-less backend carries (`fullscreen`, `sticky`) [requests-batch-3.md].
PROPS_FOR_FIELD = {
    "class_": ("WM_CLASS",),
    "instance": ("WM_CLASS",),
    "cls": ("WM_CLASS",),
    "app_id": ("WM_CLASS",),
    "pid": ("_NET_WM_PID",),
    "desktop": ("_NET_WM_DESKTOP", "_NET_WM_STATE"),
    "ws_name": ("_NET_WM_DESKTOP", "_NET_WM_STATE"),
    "window_type": ("_NET_WM_WINDOW_TYPE",),
    "transient_for": ("WM_TRANSIENT_FOR",),
    "fullscreen": ("_NET_WM_STATE",),
    "maximized_h": ("_NET_WM_STATE",),
    "maximized_v": ("_NET_WM_STATE",),
    "above": ("_NET_WM_STATE",),
    "below": ("_NET_WM_STATE",),
    "sticky": ("_NET_WM_STATE",),
    "urgent": ("_NET_WM_STATE",),
    "minimized": ("_NET_WM_STATE",),
    "hidden": ("_NET_WM_STATE",),
    "skip_taskbar": ("_NET_WM_STATE",),
    "skip_pager": ("_NET_WM_STATE",),
}

#: The two root names every appearance, departure and reordering publishes. The
#: stacking list is a copy of the client list: no backend reports a stacking
#: order (design section 4.6, R13).
CLIENT_LIST_NAMES = ("_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING")

#: The root names a desktop change publishes. `_NET_DESKTOP_NAMES` only where
#: the backend answers `workspaces()` -- sway numbers its workspaces and wxprop
#: leaves the name list off there [recon/seams.md 4's `_SWAY_ROOT_EVENT_PROPS`].
DESKTOP_NAMES = ("_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS")


def _root(server) -> int:
    own = getattr(server, "own", None)
    return own.root if own is not None else 0


def _atom(server, name: str) -> int:
    """The id upstream gave a name. The forty-odd names of `policy.ATOMS` were
    interned when the proxy's own connection opened (design section 2.4), so
    this is a dict lookup and never a round trip on the loop thread."""
    own = getattr(server, "own", None)
    return own.atom_id(name) if own is not None else 0


def _rect(entry):
    win = entry.window
    return (int(getattr(win, "x", 0) or 0), int(getattr(win, "y", 0) or 0),
            int(getattr(win, "w", 0) or 0), int(getattr(win, "h", 0) or 0))


def _native(entry) -> bool:
    """Whether this entry is a shadow the proxy owns rather than a real X
    window. A real X window's per-window events are Xwayland's own and pass
    untouched (design section 5.3); the proxy adds only the root-level names
    for it, because upstream's `_NET_CLIENT_LIST` describes the X plane alone
    [recon/env.md 2.7]."""
    return bool(entry is not None and entry.shadow)


def structure_pair(server, packet_for, window, what):
    """One change, as X sends it: `event = window` to the `StructureNotify`
    selectors on the window and `event = root` to the `SubstructureNotify`
    selectors on the root (design section 5.3). `packet_for(event)` builds the
    packet with that `event` field."""
    root = _root(server)
    return [Event(window, STRUCTURE_NOTIFY, packet_for(window), what),
            Event(root, SUBSTRUCTURE_NOTIFY, packet_for(root), what)]


def synthesize(server, changes):
    """The change list of one `Shadows.refresh()`, as the events X would have
    sent, in X's own order.

    Per changed entry, in the order the registry reports them (list order,
    bottom to top): appearance is `CreateNotify` then `MapNotify`, departure is
    `UnmapNotify` then `DestroyNotify`. Focus is emitted after the per-entry
    events -- `FocusOut` on the window that lost it before `FocusIn` on the one
    that took it, which the diff cannot order for itself because it reports one
    change per entry in list order [requests-batch-2.md item 7]. The root's own
    `PropertyNotify`s come last and are deduplicated: one drain that saw a new
    window, a focus move and a reorder publishes `_NET_CLIENT_LIST` once.
    """
    out = []
    focus_out_evs, focus_in_evs = [], []
    root_names = []

    def root_name(name):
        if name not in root_names:
            root_names.append(name)

    for change in changes:
        entry = change.entry
        kind = change.kind
        if kind == shadow_mod.NEW:
            root_name(CLIENT_LIST_NAMES[0])
            root_name(CLIENT_LIST_NAMES[1])
            if not _native(entry):
                continue
            x, y, w, h = _rect(entry)
            root = _root(server)
            out.append(Event(root, SUBSTRUCTURE_NOTIFY,
                             create_notify(root, entry.shadow, x, y, w, h),
                             "CreateNotify 0x%x" % entry.shadow))
            # Nobody has selected on the shadow yet -- its first mask arrives
            # on the first ChangeWindowAttributes a client sends, which cannot
            # have happened before the client heard the window exists (design
            # section 5.4) -- so the map goes to the root's selectors only.
            out.append(Event(root, SUBSTRUCTURE_NOTIFY,
                             map_notify(root, entry.shadow),
                             "MapNotify 0x%x" % entry.shadow))
        elif kind == shadow_mod.GONE:
            root_name(CLIENT_LIST_NAMES[0])
            root_name(CLIENT_LIST_NAMES[1])
            if getattr(entry.window, "focused", False):
                root_name("_NET_ACTIVE_WINDOW")
            if not _native(entry):
                continue
            win = entry.shadow
            out += structure_pair(server, lambda ev, w=win: unmap_notify(ev, w),
                                  win, "UnmapNotify 0x%x" % win)
            out += structure_pair(server, lambda ev, w=win: destroy_notify(ev, w),
                                  win, "DestroyNotify 0x%x" % win)
        elif kind == shadow_mod.VISIBLE:
            if not _native(entry):
                continue
            win = entry.shadow
            shown = bool(getattr(entry.window, "visible", True))
            build = map_notify if shown else unmap_notify
            out += structure_pair(server, lambda ev, w=win, b=build: b(ev, w),
                                  win, "%s 0x%x"
                                  % ("MapNotify" if shown else "UnmapNotify", win))
            out += _props(server, win, ("WM_STATE", "_NET_WM_STATE"))
        elif kind == shadow_mod.GEOMETRY:
            if not _native(entry):
                continue
            win = entry.shadow
            x, y, w, h = _rect(entry)
            out += structure_pair(
                server,
                lambda ev, wi=win, r=(x, y, w, h): configure_notify(ev, wi, *r),
                win, "ConfigureNotify 0x%x" % win)
        elif kind == shadow_mod.TITLE:
            if not _native(entry):
                continue
            # WM_NAME first, then _NET_WM_NAME -- the order xterm publishes a
            # rename in and the order wxprop already emits [recon/seams.md 4].
            out += _props(server, entry.shadow, ("WM_NAME", "_NET_WM_NAME"))
        elif kind == shadow_mod.PROPS:
            if not _native(entry):
                continue
            names = []
            for field in change.names:
                for name in PROPS_FOR_FIELD.get(field, ()):
                    if name not in names:
                        names.append(name)
            out += _props(server, entry.shadow, names)
        elif kind == shadow_mod.FOCUS:
            root_name("_NET_ACTIVE_WINDOW")
            if not _native(entry):
                continue
            if getattr(entry.window, "focused", False):
                focus_in_evs.append(Event(entry.shadow, FOCUS_CHANGE,
                                          focus_in(entry.shadow),
                                          "FocusIn 0x%x" % entry.shadow))
            else:
                focus_out_evs.append(Event(entry.shadow, FOCUS_CHANGE,
                                           focus_out(entry.shadow),
                                           "FocusOut 0x%x" % entry.shadow))
        elif kind == shadow_mod.ORDER:
            root_name(CLIENT_LIST_NAMES[0])
            root_name(CLIENT_LIST_NAMES[1])
        elif kind == shadow_mod.DESKTOP:
            for name in DESKTOP_NAMES:
                root_name(name)
            if _has_desktop_names(server):
                root_name("_NET_DESKTOP_NAMES")

    out += focus_out_evs + focus_in_evs
    out += _props(server, _root(server), root_names)
    return out


def _props(server, window, names):
    """A `PropertyNotify(NewValue)` per name, to the `PropertyChange` selectors
    on that window. A name upstream never interned is skipped rather than sent
    with atom 0: `policy.ATOMS` is interned at open, so this only happens on a
    proxy whose own connection is gone (design section 2.4)."""
    out = []
    for name in names:
        atom = _atom(server, name)
        if not atom:
            continue
        out.append(Event(window, PROPERTY_CHANGE,
                         property_notify(window, atom),
                         "PropertyNotify %s on 0x%x" % (name, window)))
    return out


def _has_desktop_names(server) -> bool:
    """Whether this backend names its workspaces. sway numbers them and wxprop
    publishes no `_NET_DESKTOP_NAMES` there [recon/seams.md 4], so a desktop
    change on sway is two atoms and on the GNOME bridge three."""
    shadows = getattr(server, "shadows", None)
    if shadows is None:
        return False
    try:
        return bool(shadows.workspace_names())
    except Exception:                                   # noqa: BLE001
        return False


# -- delivery (design section 5.4) ---------------------------------------------


#: The half-circle `client._SEQ_WINDOW` compares sequences over: a 16-bit
#: number that is more than 32768 behind the client's count is ahead of it.
_SEQ_WINDOW = 32768


def watermark(conn) -> int:
    """The sequence to stamp an event going to THIS client with.

    Not `conn.seq`. An event carries the number of the last request the server
    FINISHED on that connection [M recon/wire.md 3.2b], and a proxy that has
    ANSWERed a request has not finished it: the reply is still behind a
    `GetInputFocus` placeholder upstream, or held by `hold_reply` while an
    apply runs (`client.answer`, `client.hold_reply`). Stamping such an event
    with `conn.seq` puts a number down the wire ABOVE a reply that has not
    gone out yet, and libxcb's `read_packet` sets `request_completed =
    request_read - 1` on ANY packet but `KeymapNotify` -- so the client marks
    the outstanding request completed with no reply, and the reply that then
    arrives with a LOWER sequence is widened by 65536 and tears the connection
    down. That is the same teardown `client.write_after_replies` was written
    for (measured 2026-09-10 on a RandR batch), reached here with two
    `PropertyNotify`s and a pipelined `GetProperty`: seq 4, seq 4, then the
    reply for seq 3 [M scratchpad probe, 2026-09-11].

    So the watermark is one below the OLDEST sequence this connection still
    owes an answer for, and `conn.seq` when it owes none. It is monotone with
    what has already gone down -- upstream answers in stream order, so nothing
    above the oldest placeholder has been written -- and it is X's own meaning
    of the field. `xprop -spy` never notices: it pipelines nothing. An xcb
    status bar does.

    A reply FORWARDED upstream (a PASS the proxy did not answer) is invisible
    here -- knowing it is coming needs an opcode-has-reply table the proxy does
    not keep -- so a client that pipelines a passed-through reply-bearing
    request can still see an event stamped one too high. Nothing measured has
    (the events go out on the loop thread between drains, and upstream's reply
    is usually already forwarded); it is named in the report as a risk against
    design section 5.4.
    """
    oldest, far = None, -1
    for book in (getattr(conn, "placeholders", ()), getattr(conn, "holding", ())):
        for seq in book:
            behind = (conn.seq - seq) & 0xFFFF
            if behind > _SEQ_WINDOW:            # ahead of the count: not owed yet
                continue
            if behind > far:
                oldest, far = seq, behind
    if oldest is None:
        return conn.seq
    return (oldest - 1) & 0xFFFF


def deliver(server, evs) -> int:
    """Every event to every client that selected it, at once.

    Bytes 2-3 are patched per client on the way out, with `watermark(conn)` --
    that connection's own count, held back behind any reply it is still owed.
    An event never waits behind a held reply: it consumes no sequence
    [recon/wire.md 3.2a] and a client that is mid-`GetProperty` still gets it,
    stamped with the sequence before the one it is waiting on.
    """
    from xw11 import client as client_mod
    from xw11 import wire

    touched = []
    sent = 0
    for ev in evs:
        for conn in list(getattr(server, "conns", ())):
            if conn.state == client_mod.CLOSED:
                continue
            if not conn.selects(ev.target, ev.bit):
                continue
            conn.out_down += wire.event_seq(ev.packet, watermark(conn))
            sent += 1
            if conn not in touched:
                touched.append(conn)
    for conn in touched:
        server.pump(conn)
    return sent


def emit(server, changes) -> int:
    """`synthesize` then `deliver`, and the masks of a window that died dropped
    after its last event went out (design section 5.3: "masks on it dropped")."""
    evs = synthesize(server, changes)
    sent = deliver(server, evs)
    for change in changes:
        if change.kind == shadow_mod.GONE and _native(change.entry):
            drop_masks(server, change.entry.shadow)
    return sent


def drop_masks(server, xid: int) -> int:
    """Forget every client's mask on a window that no longer exists. X frees a
    window's event masks with the window; here the id can also be REUSED -- the
    mint hands out `rid_base | n` and a rebase restarts it (design section 4.3)
    -- so a mask left behind would deliver one window's events to a client that
    asked about another."""
    gone = 0
    for conn in list(getattr(server, "conns", ())):
        gone += conn.drop_mask(xid)
    return gone


# -- the pointer (design section 5.6) ------------------------------------------


def hit_test(entries, x: int, y: int):
    """The topmost shadow whose rect contains the point, or None.

    `WindowBackend.list()` answers bottom to top (its contract, and what
    `Shadows.order` keeps), so the LAST match wins. A real X window is not a
    candidate: Xwayland sends its own crossing events for those."""
    got = None
    for entry in entries:
        if not _native(entry) or entry.dead:
            continue
        ex, ey, ew, eh = _rect(entry)
        if ew <= 0 or eh <= 0:
            continue
        if ex <= x < ex + ew and ey <= y < ey + eh:
            got = entry
    return got


def pointer_events(server, entries, position, previous):
    """The crossing and motion events one pointer sample produces.

    `previous` is `(shadow, x, y)` from the last sample, or None for the first.
    A crossing is `LeaveNotify` on the shadow the pointer left and then
    `EnterNotify` on the one it entered -- X's own order, and the pair
    `xdotool behave <w> mouse-enter` is waiting for [recon/tools.md 4.7].
    Motion is emitted only INSIDE a shadow and only when the position moved:
    the source is sampled every 50 ms and a pointer nobody touched must not
    produce twenty events a second.
    """
    x, y = int(position[0]), int(position[1])
    entry = hit_test(entries, x, y)
    now = entry.shadow if entry is not None else 0
    was, was_x, was_y = previous if previous is not None else (0, None, None)
    root = _root(server)
    focused = _focused_shadow(entries)
    out = []
    if now != was:
        if was:
            # `event_x/y` are relative to the window the pointer LEFT, which is
            # still in the registry unless it left by being closed; a window
            # that is gone leaves the root-relative pair, because X's answer
            # for a window that no longer exists is no event at all and this
            # one is already better than silence.
            lx, ly, _lw, _lh = _rect_of(entries, was, x, y)
            out.append(Event(was, LEAVE_WINDOW,
                             leave_notify(was, root, root_x=x, root_y=y,
                                          event_x=x - lx, event_y=y - ly,
                                          same_screen_focus=_crossing_byte(
                                              was, focused)),
                             "LeaveNotify 0x%x" % was))
        if entry is not None:
            ex, ey, _w, _h = _rect(entry)
            out.append(Event(now, ENTER_WINDOW,
                             enter_notify(now, root, root_x=x, root_y=y,
                                          event_x=x - ex, event_y=y - ey,
                                          same_screen_focus=_crossing_byte(
                                              now, focused)),
                             "EnterNotify 0x%x" % now))
    elif entry is not None and (x, y) != (was_x, was_y):
        ex, ey, _w, _h = _rect(entry)
        out.append(Event(now, POINTER_MOTION,
                         motion_notify(now, root, root_x=x, root_y=y,
                                       event_x=x - ex, event_y=y - ey),
                         "MotionNotify 0x%x" % now))
    return out, (now, x, y)


def _crossing_byte(window: int, focused: int) -> int:
    """Byte 31 for a crossing that names `window`: same-screen always (one
    screen), plus the focus bit when that window is the focused one."""
    return CROSSING_SAME_SCREEN | (CROSSING_FOCUS if window and
                                   window == focused else 0)


def _focused_shadow(entries) -> int:
    """The focused entry's shadow id in one listing, 0 for none -- the same
    question `Shadows.focused()` answers, asked of the snapshot the sample
    already has rather than of a second re-list."""
    for entry in entries:
        if _native(entry) and not entry.dead \
                and getattr(entry.window, "focused", False):
            return entry.shadow
    return 0


def _rect_of(entries, shadow, x, y):
    """The rect of a shadow still in the registry, else an origin at the point
    itself (so `event_x/y` come out zero rather than a coordinate in a window
    that is gone)."""
    for entry in entries:
        if entry.shadow == shadow:
            return _rect(entry)
    return (x, y, 0, 0)


#: What the log says once when a client wants crossing events and the session
#: has no pointer to read. AGENTS.md's rule: a gap of ours, with the route and
#: what it costs.
NO_POINTER = (
    "a client selected EnterWindow/LeaveWindow/PointerMotion on a shadow and "
    "this session has no pointer position to read: sway's IPC carries no "
    "cursor and the daemon refuses to guess [recon/seams.md 2.3, 5.2], and "
    "nothing has been routed through this proxy yet. A physical mouse is not "
    "yet seen here; the route is AGENTS.md route 4 -- evdev, which the daemon "
    "already opens for the uinput path -- at the cost of read access to "
    "/dev/input. xdotool mousemove through this proxy fires these events "
    "today."
)
