"""The write side: the eight core requests that move a window, and the overlay.

Design section 3.2's second table, one handler per row, the same shape as
`xw11/req_read.py`: `fn(server, conn, request)` is called by `Server.handle()`
and answers **None** for a CONSUME row -- the client gets nothing, upstream
gets a `NoOperation` in the request's place -- and **None** for an EDIT row
too, which forwards the frame untouched. The two rows that are EDIT here
(`ChangeWindowAttributes` and `ChangeProperty` on the ROOT) are PASS in the
design and are handed to a handler only so that it can SEE them.

What this file is for, in one measurement: `xdotool windowmove 6291468 100 100`
is 34 requests of which exactly one is a `ConfigureWindow`
[recon/tools.md 4.3, 11 row 10] -- the prologue is 90 % of every command's
traffic and the command itself is one frame. That one frame is decoded here at
the offsets of recon/wire.md 4.2 and turned into the backend call the clone
would have made, which is what makes `xdotool` on a compositor with no X window
in it do what `wdotool` does.

Three rules the handlers share:

* **a refusal is silence on the wire and one line in the log.** X gives a
  client no error when a redirecting window manager ignores its
  `ConfigureWindow`, `MapWindow` or focus request -- the request is void and
  the window manager did what it wanted -- so a `CmdError` from a backend is
  never an X error here (design section 3.3). `str(exc)` is the clone's own
  sentence, and the log is the substitute for the diagnostics the issue names
  as lost.
* **a CONSUMEd write arms a settle poll.** `Server.settle(xid)` re-lists at
  +50 ms and +250 ms, because sway emits `move` for a workspace move and
  nothing at all for a floating `move position` [recon/seams.md 2.4]: the
  `ConfigureNotify` after `xdotool windowmove` comes from the diff of two
  listings and not from a compositor event (design section 5.2).
* **the value lists are walked in BIT ORDER.** `ConfigureWindow` and
  `ChangeWindowAttributes` both carry one CARD32 per set bit of their mask, in
  ascending bit order and nothing else [recon/wire.md 4.2], so the slot a value
  is in is a function of the bits BELOW it.
"""

import struct

from w11common.errors import CmdError
from xw11 import policy, wire

SHADOW = "shadow"

#: `ConfigureWindow`'s value mask, bit by bit [recon/wire.md 4.2]. `x` and `y`
#: are INT16 in a CARD32 slot (libX11 stores the field into a CARD32 word, so
#: -1 arrives as 0xFFFFFFFF), `width`/`height`/`border_width` are CARD16 and
#: `stack_mode` is a CARD8.
CW_X = 0x01
CW_Y = 0x02
CW_WIDTH = 0x04
CW_HEIGHT = 0x08
CW_BORDER_WIDTH = 0x10
CW_SIBLING = 0x20
CW_STACK_MODE = 0x40

#: `stack_mode`: xdotool's `windowraise` sends `Above`, which is 0 [M
#: recon/tools/caps/xwl.jsonl MARK 12, `{"stack_mode": 0}`], and `wmctrl -a`
#: sends the same one before its `MapWindow` [MARK 38].
STACK_ABOVE = 0
STACK_BELOW = 1

#: The names of the other three, for the one log line each earns.
STACK_NAMES = {2: "TopIf", 3: "BottomIf", 4: "Opposite"}


def _entry(server, xid):
    """The registry's entry for an id, read THROUGH the 20 ms TTL -- the rule
    of design section 4.8 and `req_read._entry`'s: a write handler that read
    `by_shadow` straight would act on the geometry of whatever the last re-list
    saw."""
    if server.shadows is None:
        return None
    server.shadows.snapshot()
    return server.shadows.by_shadow.get(xid)


def call(server, op, *args):
    """One backend call for a request the proxy has already decided to eat.

    Returns True when it landed. A `CmdError` -- which is every refusal in the
    tree, `NoSessionError` and `SoftCmdError` included (wdotool/ctx.py) -- is
    one log line carrying the backend's own sentence and no packet at all
    (design section 3.3). An `.unsupported` error means this compositor has no
    verb for the operation, which is a gap in OUR work: the line says so and
    names the route.

    Every call is made under `Server.block`, the one lock every window-backend
    call in this process takes -- the loop thread's here and the pump's on its
    own thread, which is what `wdotool/daemon.py` already does.
    """
    backend = server.backend
    fn = getattr(backend, op, None)
    if fn is None:
        server.say("the %s backend has no %s at all: nothing was done, and "
                   "nothing was said to the client -- not yet, and the route "
                   "is the backend's own (AGENTS.md rung 2)"
                   % (getattr(backend, "name", "?"), op))
        return False
    try:
        with server.block:
            got = fn(*args)
    except CmdError as e:
        if getattr(e, "unsupported", False):
            server.say("the %s backend cannot %s%s (%s): the request is "
                       "silence on the wire, which is what X gives a client "
                       "whose window manager ignored it -- not yet here, and "
                       "the route is a verb on that backend (AGENTS.md rung 2 "
                       "for a compositor with a scripting surface, rung 3 for "
                       "one that needs code of ours in it)"
                       % (getattr(backend, "name", "?"), op, _args(args), e))
        else:
            server.say("the %s backend refused %s%s (%s): the request is "
                       "silence on the wire, as X is when a window manager "
                       "ignores one, and this line is the diagnostic"
                       % (getattr(backend, "name", "?"), op, _args(args), e))
        return False
    if isinstance(got, str) and got:
        # `set_state` answers a one-line reason when the compositor ACCEPTED
        # the request and did not apply it (wdotool/backend.py:350) -- KWin
        # does it for a window rule. The clone prints that line and succeeds;
        # so does this, in the log, because the request still produces no
        # packet.
        server.say("the %s backend accepted %s%s and did not apply it: %s"
                   % (getattr(backend, "name", "?"), op, _args(args), got))
    return True


def _args(args) -> str:
    return "(%s)" % ", ".join(str(a) for a in args) if args else ""


def value_list(frame, offset, mask, bits):
    """`{bit: CARD32}` for a value list at `offset`, walked in bit order.

    A frame too short for the bits its own mask claims is left half-read
    rather than guessed at: the bits that are there are answered and upstream
    is not involved, because the request is CONSUMEd either way and X answers
    `BadLength` for a frame it cannot walk. Nothing measured sends one
    [recon/tools.md 4.3, 5].
    """
    out = {}
    for bit in bits:
        if not mask & bit:
            continue
        if offset + 4 > len(frame):
            break
        (out[bit],) = struct.unpack_from("<I", frame, offset)
        offset += 4
    return out


def _int16(word: int) -> int:
    """An INT16 field out of its CARD32 slot. libX11 writes the whole field
    into a CARD32 word, so `windowmove <w> -1 -1` arrives as 0xFFFFFFFF twice
    and means (-1, -1)."""
    return ((word & 0xFFFF) ^ 0x8000) - 0x8000


def _card16(word: int) -> int:
    return word & 0xFFFF


# -- ConfigureWindow ----------------------------------------------------------


def configure_window(server, conn, req):
    """`xdotool windowmove/windowsize/windowraise`, and `wmctrl -r -e` where
    the tool has no `_NET_MOVERESIZE_WINDOW` to send.

    The value list is walked in bit order and every bit that has a backend verb
    becomes one call [recon/wire.md 4.2]:

    * `x` / `y` -> `move_window`, **one call for the pair**. One axis alone
      carries the window's current other axis out of the snapshot, because
      `WindowBackend.move_window` takes both and a compositor asked to move to
      `(x, 0)` would jump. `wmctrl -r <n> -e 0,10,10,300,200` against Xwayland
      sends exactly one `ConfigureWindow` with all four values
      [M recon/tools/caps/xwl.jsonl MARK 39: `{"x": 10, "y": 10, "width": 300,
      "height": 200}`, because wlroots' xwm does not name
      `_NET_MOVERESIZE_WINDOW` in `_NET_SUPPORTED` and wmctrl falls back];
    * `width` / `height` -> `resize`, the same way;
    * `stack_mode` `Above` -> `raise_`, `Below` -> `lower`. `TopIf`,
      `BottomIf` and `Opposite` are relative to a sibling and no backend in the
      tree has a verb for them: one log line each, NOT YET, and the route is
      the compositor's own stacking IPC (AGENTS.md rung 2);
    * `border_width` and `sibling` are ignored with a line. A Wayland toplevel
      has no border the client owns and no sibling to stack against; the four
      tools never send either [recon/tools.md 4.3, 5].
    """
    entry = _entry(server, req.xid)
    if entry is None or len(req.frame) < 12:
        return None
    (mask,) = struct.unpack_from("<H", req.frame, 8)
    got = value_list(req.frame, 12, mask,
                     (CW_X, CW_Y, CW_WIDTH, CW_HEIGHT, CW_BORDER_WIDTH,
                      CW_SIBLING, CW_STACK_MODE))
    win = entry.window
    if CW_X in got or CW_Y in got:
        x = _int16(got[CW_X]) if CW_X in got else int(win.x)
        y = _int16(got[CW_Y]) if CW_Y in got else int(win.y)
        call(server, "move_window", entry.handle, x, y)
    if CW_WIDTH in got or CW_HEIGHT in got:
        w = _card16(got[CW_WIDTH]) if CW_WIDTH in got else int(win.w)
        h = _card16(got[CW_HEIGHT]) if CW_HEIGHT in got else int(win.h)
        call(server, "resize", entry.handle, w, h)
    if CW_STACK_MODE in got:
        mode = got[CW_STACK_MODE] & 0xFF
        if mode == STACK_ABOVE:
            call(server, "raise_", entry.handle)
        elif mode == STACK_BELOW:
            call(server, "lower", entry.handle)
        else:
            server.say("ConfigureWindow stack_mode %s on shadow 0x%x: not yet "
                       "-- it stacks against a sibling and no backend in the "
                       "tree has that verb; the route is the compositor's own "
                       "stacking IPC (AGENTS.md rung 2). Ignored."
                       % (STACK_NAMES.get(mode, mode), req.xid))
    if CW_BORDER_WIDTH in got or CW_SIBLING in got:
        server.say("ConfigureWindow on shadow 0x%x carried %s: ignored. A "
                   "Wayland toplevel has no client-owned border and no sibling "
                   "to stack against, and nothing in xdotool, wmctrl, xprop or "
                   "xrandr sends either [recon/tools.md 4.3, 5]"
                   % (req.xid,
                      " and ".join(n for n, b in
                                   (("border_width", CW_BORDER_WIDTH),
                                    ("sibling", CW_SIBLING)) if b in got)))
    server.settle(req.xid)
    return None


# -- the four that name nothing but the window --------------------------------


def map_window(server, conn, req):
    """`xdotool windowmap` [recon/tools.md 4.3]."""
    return _one_call(server, req, "map")


def unmap_window(server, conn, req):
    """`xdotool windowunmap`."""
    return _one_call(server, req, "unmap")


def set_input_focus(server, conn, req):
    """`xdotool windowfocus`, which sends `revert_to = Parent, time = 0`
    [M recon/tools/caps/xwl.jsonl MARK 9]. Both fields are dropped: a
    compositor focuses a toplevel and has no notion of reverting to a parent,
    and no backend takes a timestamp."""
    return _one_call(server, req, "focus")


def destroy_window(server, conn, req):
    """`xdotool windowclose`, which is `DestroyWindow` and not `KillClient` and
    not `_NET_CLOSE_WINDOW` [recon/tools.md 4.3, M MARK 31]. `backend.close` is
    the polite close every clone sends."""
    return _one_call(server, req, "close")


def kill_client(server, conn, req):
    """`XKillClient`: the connection dies, not the window. `backend.kill`
    defaults to SIGKILL on the pid [recon/seams.md 2.1], which is what the X
    server does to a client it disconnects. Nothing in the four tools sends it
    [recon/tools.md 3]; the issue names it."""
    return _one_call(server, req, "kill")


def _one_call(server, req, op):
    entry = _entry(server, req.xid)
    if entry is None:
        return None
    call(server, op, entry.handle)
    server.settle(req.xid)
    return None


# -- the overlay (design section 4.7) -----------------------------------------


def change_property(server, conn, req):
    """`xprop -set`, `xdotool set_window --name`, and anything else a client
    writes on a window.

    On a shadow the write goes into the overlay and nothing reaches the
    compositor: no backend in the tree renames a toplevel, so
    `xdotool set_window --name` changes what X clients read and not what the
    compositor's own title bar says. That is NOT YET and the route is a
    per-window title verb on the compositor's bus (AGENTS.md rung 2 -- none of
    the six has one today), which is a row in the docs rather than a refusal:
    every X client on the display, `xprop` included, reads the new name.

    On the ROOT the frame is forwarded untouched (design section 3.2) and one
    line is logged when the name is one the proxy answers from the compositor:
    the write reaches upstream and the read still answers the compositor's
    number, because the compositor is the single source (design section 4.6).
    """
    if len(req.frame) < 24:
        return None
    _win, atom, type_atom = struct.unpack_from("<III", req.frame, 4)
    fmt = req.frame[16]
    (units,) = struct.unpack_from("<I", req.frame, 20)
    if req.target != SHADOW:
        _say_root_write(server, atom, "ChangeProperty")
        return None
    entry = _entry(server, req.xid)
    if entry is None or fmt not in (8, 16, 32):
        return None
    data = bytes(req.frame[24:24 + units * (fmt // 8)])
    # `req.byte1` IS the mode: Replace 0, Prepend 1, Append 2
    # [recon/wire.md 4.2], which is `shadow.REPLACE/PREPEND/APPEND`.
    server.shadows.write(entry, atom, type_atom, fmt, data, req.byte1)
    return None


def delete_property(server, conn, req):
    """`xprop -remove` [recon/tools.md 6]: a tombstone in the overlay, which
    hides a synthesized name for the life of the entry (design section 4.7)."""
    if len(req.frame) < 12:
        return None
    _win, atom = struct.unpack_from("<II", req.frame, 4)
    entry = _entry(server, req.xid)
    if entry is None:
        return None
    server.shadows.delete(entry, atom)
    return None


def _say_root_write(server, atom: int, what: str) -> None:
    """A client wrote a root property the proxy answers itself.

    Matched by ATOM ID against `policy.OVERRIDES` rather than by resolving the
    id to a name: `OwnConn.atom_name` falls back to a synchronous `GetAtomName`
    for an id it has not seen, on the loop thread, and a client is free to
    write any property it likes on the root. The seven override names were
    interned at open, so the id is already in the table [design section 2.4].
    """
    own = server.own
    if own is None:
        return
    for name in policy.OVERRIDES:
        if own.atom_id(name) != atom:
            continue
        server.say("%s(root, %s) forwarded; reads still answer the "
                   "compositor's own number, because the compositor is the "
                   "single source for that name (design section 4.6)"
                   % (what, name))
        return


# -- ChangeWindowAttributes (design section 5.4) ------------------------------

#: `ChangeWindowAttributes`' value mask, in bit order. Only `CWEventMask`
#: (bit 11) is read; the other thirteen are a client's own business and are
#: forwarded or dropped with the frame.
CWA_BITS = tuple(1 << n for n in range(15))


def change_window_attributes(server, conn, req):
    """The mask a client selects, recorded per connection.

    `xprop -spy` sends `StructureNotify|PropertyChange` = `0x420000` on its
    target and then blocks [recon/tools.md 6], and `xdotool behave` does the
    same on the window it watches [recon/tools.md 4.7]; batch 5 delivers to
    whoever is in this table. A shadow's mask arrives on the FIRST
    `ChangeWindowAttributes` it ever gets, because a shadow has no
    `CreateWindow` to have carried one.

    On the root the request is forwarded **and** recorded: upstream keeps
    delivering its own root events for the X plane and the proxy still has to
    know who wants `SubstructureNotify` and `PropertyChange` for the ones it
    synthesizes (design section 3.2). On a real X window it never gets here --
    that row is PASS and upstream's own bookkeeping is the truth.
    """
    if len(req.frame) < 12:
        return None
    (mask,) = struct.unpack_from("<I", req.frame, 8)
    got = value_list(req.frame, 12, mask, CWA_BITS)
    if wire.CW_EVENT_MASK in got:
        conn.masks[req.xid] = got[wire.CW_EVENT_MASK]
        server.debug_say("client mask 0x%x on 0x%x"
                         % (got[wire.CW_EVENT_MASK], req.xid))
    return None


HANDLERS = {
    wire.OP_CHANGE_WINDOW_ATTRIBUTES: change_window_attributes,
    wire.OP_DESTROY_WINDOW: destroy_window,
    wire.OP_MAP_WINDOW: map_window,
    wire.OP_UNMAP_WINDOW: unmap_window,
    wire.OP_CONFIGURE_WINDOW: configure_window,
    wire.OP_CHANGE_PROPERTY: change_property,
    wire.OP_DELETE_PROPERTY: delete_property,
    wire.OP_SET_INPUT_FOCUS: set_input_focus,
    wire.OP_KILL_CLIENT: kill_client,
}


def install(server) -> None:
    """Put the write handlers into a server's table, the way
    `req_read.install` does. A key written twice is a collision two batches
    would both have to see."""
    server.handlers.update(HANDLERS)
