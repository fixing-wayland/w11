"""The read side: the seven requests that make a native toplevel a window.

Design section 3.2's first table, one handler per row. Each is called by
`Server.handle()` with `(server, conn, request)` and answers in the shape the
class it was registered for expects: **bytes** for ANSWER (the reply, or a
32-byte error, which the placeholder machinery of `client.answer` writes in
stream order), a **callable** `fn(packet) -> bytes` for EDIT, and **None** for
"the proxy has nothing to say", which `Server.handle` turns back into a plain
forward -- so a request the registry cannot answer behaves exactly like X.

What this file is for, in one measurement: with one `foot` toplevel focused and
no X client anywhere, `xdotool search --class foot` prints nothing and exits 1,
`getactivewindow` exits 1, `wmctrl -l` prints "Cannot get client list
properties." and exits 1, and `xprop -root` has no `_NET_CLIENT_LIST` at all --
sway deletes the property rather than emptying it [recon/env.md 2.7]. Every one
of those is a read, and every one of them is answered here.

Three rules the handlers share:

* **the frame is decoded no further than the answer needs.** The window is
  already resolved (`Request.target`, `Request.xid`); everything else is read
  off the frame with one `struct.unpack_from` at the offset recon/wire.md 4.1
  names, and a frame too short to hold it is left to upstream, which answers
  `BadLength` itself.
* **every packet is built through `wire.reply`**, so the code, the sequence and
  the length word cannot be got wrong in one place and right in another; the
  fixed 24 bytes are packed at the offsets of recon/wire.md 4.1 and the tests
  compare the bytes against that layout, never against a field getter.
* **an id the registry does not know passes.** A dead shadow, a pixmap, a real
  X window: upstream answers `BadWindow` itself with the serial the tools print
  [recon/tools.md 9], and inventing that error here would be more code for the
  same bytes (design section 3.1).
"""

import struct

from wdotool import backend as backend_mod
from xw11 import policy, wire
from xw11 import shadow as shadow_mod
from xw11 import xtest as xtest_mod

#: `GetWindowAttributes`' fixed fields that are neither the visual nor the
#: colormap nor `map_state` (design section 4.5). `backing_planes` is all-ones:
#: it is CreateWindow's own default and it is what the live xterm on the rig
#: answers [M 2026-09-10, headless sway, `GetWindowAttributes(0x40000c)` ->
#: `backing_planes=0xffffffff backing_pixel=0x0 save_under=0
#: map_is_installed=1 override_redirect=0`]. `bit_gravity` is 0 (Forget), which
#: is CreateWindow's default; that same xterm answers 1, because xterm asks for
#: NorthWest -- a shadow has no client to have asked.
_CLASS_INPUT_OUTPUT = 1
_BIT_GRAVITY = 0
_WIN_GRAVITY = 1                 # NorthWest
_BACKING_PLANES = 0xFFFFFFFF

#: `map_state`: `IsViewable` when the compositor says the toplevel is visible,
#: `IsUnmapped` otherwise. The predicate is `Window.visible`, which is the one
#: `wdotool search --onlyvisible` uses (wdotool/window_cmds.py:173), so the
#: clone and the proxy agree about the same window.
MAP_UNMAPPED = 0
MAP_VIEWABLE = 2

#: `Request.target` for an id the registry minted, the name `xw11/client.py`
#: gives it and `xw11/policy.py` keys a row on.
SHADOW = "shadow"


def _root_of(conn) -> int:
    """Screen 0's root, from this client's own setup reply. Every shadow is on
    it: both captured setups carry exactly one screen, and the proxy's shadow
    ids are minted on the connection whose setup named that root."""
    if conn.setup is None or not conn.setup.roots:
        return 0
    return conn.setup.roots[0]


def _entry(server, xid):
    """The shadow an id names, read THROUGH the 20 ms TTL.

    `snapshot()` first and always, because design section 4.8's freshness rule
    is the registry's and not the root's: `xdotool getwindowname`,
    `getwindowpid`, `getwindowgeometry` and `xprop -id` send no root request at
    all, so a handler that read `by_shadow` straight answered whatever the last
    re-list saw for as long as nobody asked the root anything. Measured on a
    two-window rig: with the pump's `invalidate()` called and 100 ms gone by,
    `GetProperty(_NET_WM_NAME)` still answered the previous title
    [M 2026-09-10, tests/test_xw11_read.py::TheRegistryIsReadThroughTheTtl].
    `snapshot()` is a dict lookup when the list is fresh."""
    if server.shadows is None:
        return None
    server.shadows.snapshot()
    return server.shadows.by_shadow.get(xid)


def _snapshot(server):
    """The registry's list, re-read if the 20 ms TTL is up. Called once per
    handler, so a `search` walk of five windows is served from one list
    [recon/tools.md 4.1, design section 4.8]."""
    return [] if server.shadows is None else server.shadows.snapshot()


# -- QueryTree ----------------------------------------------------------------


def query_tree(server, conn, req):
    """A shadow answers root/parent=root/no children; the root's reply is
    edited to carry the shadows after upstream's own children.

    Upstream's children are kept **verbatim, wlroots' four xwm internals
    included**: `QueryTree(root)` on a rig with one xterm answered
    `0x0040000c 0x00200001 0x00200002 0x00200003 0x00200004`
    [recon/seams.md 3], and those four are what `X11Conn.client_list()` falls
    back to when the root has no `_NET_CLIENT_LIST` -- a proxy that hid them
    would change what a tool walking the tree sees for no reason of its own.
    Shadows go after them, bottom to top, which is `QueryTree`'s own order
    [recon/wire.md 4.1] and `WindowBackend.list`'s (backend.py:295).

    `xdotool search` is the only caller that matters [recon/tools.md 10.4]:
    wmctrl never sends `QueryTree` and reaches shadows through
    `_NET_CLIENT_LIST` instead. Both routes read this registry and agree by
    construction.
    """
    if req.target == "shadow":
        root = _root_of(conn)
        return wire.reply(req.seq, 0, struct.pack("<IIH14x", root, root, 0))
    shadows = [e.shadow for e in _snapshot(server) if e.shadow]
    if not shadows:
        return None

    def edit(pkt):
        if len(pkt) < 32 or pkt[0] != 1:
            return pkt
        (have,) = struct.unpack_from("<H", pkt, 16)
        kids = pkt[32:32 + 4 * have]
        body = kids + struct.pack("<%dI" % len(shadows), *shadows)
        return (pkt[:16] + struct.pack("<H", have + len(shadows)) + pkt[18:32]
                + body)
    return edit


# -- GetGeometry, GetWindowAttributes, TranslateCoordinates -------------------


def get_geometry(server, conn, req):
    """The compositor's rect, in its own logical pixels.

    Ratio 1.0, measured rather than assumed: with `output HEADLESS-1 scale 2`
    on the rig, sway's tree rect and the xterm's `xwininfo` BOTH read
    640x360 -- Xwayland's screen shrinks with the logical size, so one X device
    pixel is one compositor logical pixel [R8, M 2026-09-10, headless sway on
    this box]. `tests/test_xw11_live.py::HiDpi` is that measurement kept.

    Whatever the backend says, and nothing else. On the wlr floor that is the
    whole OUTPUT: `zwlr_foreign_toplevel_management_v1` carries no rect, and
    `backend_wlr.py:414` answers `x=0, y=0, w=out_w, h=out_h` with "geometry
    unknown" written beside it -- measured through the proxy on the rig, where
    a `foot` on the wlr floor reads `Position: 0,0  Geometry: 1280x720`
    [M 2026-09-10]. That is the same number `wwmctl -lG` prints for it, which
    is the parity target; the route to a real rect is a protocol that carries
    one (AGENTS.md route 1, or 6 for a patched compositor), which is why sway,
    Hyprland and Wayfire -- all of which have their own IPC -- report a true
    one. A backend that answers a zero rect gets a zero rect here: zero is the
    wire's only honest "unknown" and `hit_test` already ignores such a window.
    """
    entry = _entry(server, req.xid)
    if entry is None:
        return None
    win = entry.window
    depth = conn.setup.root_depth if conn.setup is not None else 0
    # Every field is a fixed-width one and a compositor's numbers are not: the
    # origin is INT16 and the size CARD16, and a rect beyond either range would
    # raise `struct.error` inside a handler, which `Server.handle` does not
    # catch (it catches `CmdError`). X truncates such a rect and so does this.
    return wire.reply(req.seq, depth,
                      struct.pack("<IhhHHH10x", _root_of(conn),
                                  _clamp16(win.x), _clamp16(win.y),
                                  _clamp_card16(win.w), _clamp_card16(win.h), 0))


def get_window_attributes(server, conn, req):
    """44 bytes, `len = 3` [recon/wire.md 4.1]. The visual and the colormap are
    the setup's own, the masks are what the clients selected."""
    entry = _entry(server, req.xid)
    if entry is None:
        return None
    setup = conn.setup
    visual = setup.root_visual if setup is not None else 0
    cmap = setup.cmaps[0] if setup is not None and setup.cmaps else 0
    visible = bool(getattr(entry.window, "visible", True))
    fixed = struct.pack("<IHBBIIBBBBI", visual, _CLASS_INPUT_OUTPUT,
                        _BIT_GRAVITY, _WIN_GRAVITY, _BACKING_PLANES, 0,
                        0, 1, MAP_VIEWABLE if visible else MAP_UNMAPPED, 0,
                        cmap)
    body = struct.pack("<IIH2x", all_masks(server, req.xid),
                       conn.masks.get(req.xid, 0), 0)
    return wire.reply(req.seq, 0, fixed, body)


def all_masks(server, xid: int) -> int:
    """`all_event_masks`: the OR of every client's mask on this window, which
    is what the field means. `client.masks` is written by batch 4's
    `ChangeWindowAttributes` handler and is empty until then, so this answers 0
    for now and changes with that batch and not with this one."""
    got = 0
    for conn in getattr(server, "conns", ()):
        got |= conn.masks.get(xid, 0)
    return got


def translate_coordinates(server, conn, req):
    """`(x + src_x, y + src_y)` towards the root, and the inverse away from it.

    `child` is the window under the destination point, over the merged list --
    `backend.hit_test`, the one rule every backend and `getmouselocation`
    already share (backend.py:159), mapped from a handle onto the id a client
    is shown. `same_screen` is 1: wmctrl sends a `TranslateCoordinates` for
    every window it lists [recon/tools.md 5] and both windows are on the one
    screen both captured setups carry.

    A window of the pair that is neither the root nor a shadow -- at EITHER
    end, source or destination -- is the one case with no answer here: the
    proxy would need that window's own origin, which only upstream has. It is
    answered as though that window were the root and one log line says which
    end it was; the route is a synchronous `GetGeometry` on the proxy's own
    connection before the reply is built (AGENTS.md route 5, which is this
    proxy), at the cost of a round trip on the loop thread inside a request
    handler. Nothing measured sends either shape: wmctrl's destination is
    always the root and xdotool's always the root [recon/tools.md 4.2, 5].
    """
    if len(req.frame) < 16:
        return None
    src, dst, sx, sy = struct.unpack_from("<IIhh", req.frame, 4)
    shadows = server.shadows
    src_entry = _entry(server, src)
    dst_entry = _entry(server, dst)
    if src_entry is None and dst_entry is None:
        return None
    roots = conn.setup.roots if conn.setup is not None else ()
    ox = oy = 0
    if src_entry is not None:
        ox, oy = int(src_entry.window.x), int(src_entry.window.y)
    elif src not in roots:
        _say_foreign(server, "from", src)
    if dst_entry is not None:
        ox -= int(dst_entry.window.x)
        oy -= int(dst_entry.window.y)
    elif dst not in roots:
        _say_foreign(server, "to", dst)
    dx, dy = ox + sx, oy + sy
    child = 0
    if dst_entry is None and shadows is not None:
        wins = [e.window for e in _snapshot(server) if e.window is not None]
        child = shadows.resolve(backend_mod.hit_test(wins, dx, dy))
    return wire.reply(req.seq, 1,
                      struct.pack("<Ihh16x", child, _clamp16(dx), _clamp16(dy)))


def _say_foreign(server, way: str, xid: int) -> None:
    """One window of the pair is neither the root nor a shadow. Both directions
    say it, because both are the same missing number: that window's own origin,
    which only upstream has."""
    server.say("TranslateCoordinates %s window 0x%x, which is neither the root "
               "nor a shadow, with a shadow at the other end: answered "
               "relative to the root. Reading that window's own origin is not "
               "yet done here, and the route is a GetGeometry on the proxy's "
               "own connection before the reply is built (AGENTS.md route 5, "
               "which is this proxy), at the cost of a round trip on the loop "
               "thread inside a handler" % (way, xid))


def _clamp16(n: int) -> int:
    """An INT16 field takes an INT16. A compositor rect plus a client's own
    offset can leave the range; X truncates and so does this."""
    return ((int(n) + 0x8000) & 0xFFFF) - 0x8000


def _clamp_card16(n: int) -> int:
    """A CARD16 field takes a CARD16, and a width is never negative: a backend
    answering one is answering "unknown", which is zero on the wire."""
    return max(int(n), 0) & 0xFFFF


# -- GetProperty (design section 4.7) -----------------------------------------


def get_property(server, conn, req):
    """The server's own answer, honoured field by field, out of the synthesized
    table plus the overlay.

    Measured against Xvfb 21.1.22 from a raw socket rather than read off the
    spec (2026-09-10, scratchpad b3/getprop_probe.py, a 16-byte STRING on the
    root):

    * an **absent** name answers `type None(0), format 0, bytes_after 0,
      nitems 0` and never an error, whatever the offset or the type asked for
      -- the shape recon/env.md 2.7 caught on the live sway root;
    * a **type mismatch** answers the ACTUAL type and format with
      `bytes_after = len(value), value_len 0` and no data, and it is checked
      BEFORE the offset (a mismatch with a nonsense offset still replies).
      wmctrl names `WINDOW`, `STRING` and `UTF8_STRING` explicitly and reads a
      mismatch as absent [recon/tools.md 5];
    * `long_offset` is in 32-BIT UNITS: offset 4 on a 16-byte value answers
      empty with `bytes_after 0`, and offset **5 answers `BadValue` with
      `bad = 5`**, the long_offset itself and not the byte offset. Both
      designs said "empty"; the server says otherwise and X is the oracle;
    * `delete = 1` deletes only when the read finished the property
      (`bytes_after == 0`) and the type matched, and the reply still carries
      the value. A partial read with `delete = 1` deletes nothing.

    `long_length` is a ceiling and allocates nothing: xprop asks 125000 units
    (500 kB), wmctrl 1024, libX11's own prologue 100000000 (400 MB)
    [recon/tools.md 2, 5, 6], and the slice is taken with `min` against what is
    actually there. wmctrl's 1024 ceiling on `_NET_CLIENT_LIST` is inherited
    whole, truncation and all -- a 2000-entry list answers `nitems 1024,
    bytes_after 3904` and wmctrl prints 1024 lines and stops [recon/tools.md 5].
    """
    if len(req.frame) < 24:
        return None
    win, atom, want_type, offset, length = struct.unpack_from("<IIIII", req.frame, 4)
    delete = bool(req.byte1)
    if req.target == "shadow":
        entry = _entry(server, req.xid)
        if entry is None:
            return None
        table = server.shadows.props_for(entry)
        return _property_reply(server, conn, req, table, atom, want_type,
                               offset, length, delete, entry)
    table = _root_table(server, atom)
    if table is None:
        return _supported_edit(server, conn, req, atom, want_type, offset)
    return _property_reply(server, conn, req, table, atom, want_type, offset,
                           length, delete, None)


def _root_table(server, atom):
    """The root's synthesized set when `atom` is one of design section 4.6's
    names, else None -- which means "not ours, and the request goes upstream"."""
    if server.shadows is None or server.own is None:
        return None
    if server.own.atom_names.get(atom) not in policy.OVERRIDES:
        return None
    return server.shadows.root_props()


def _supported_edit(server, conn, req, atom, want_type, offset):
    """`_NET_SUPPORTED` is the one root name that is EDITed rather than
    answered: upstream's list is upstream's, and the proxy appends to it
    (design section 4.6). The editor is registered here and None goes back, so
    `Server.handle` forwards the frame -- the row is ANSWER for every root
    `GetProperty` because the class is decided before the NAME is readable.

    An offset or a type the client narrowed with is honoured by leaving the
    reply alone: an edit that appended atoms to a chunk read at an offset
    would put them in the middle of the value.
    """
    if server.own is None or server.shadows is None:
        return None
    if atom != server.own.atom_id("_NET_SUPPORTED"):
        return None
    atom_type = server.own.atom_id("ATOM")
    if want_type not in (0, atom_type) or offset:
        return None

    def edit(pkt):
        if len(pkt) < 32 or pkt[0] != 1 or pkt[1] not in (0, 32):
            return pkt
        got_type, after, nitems = struct.unpack_from("<III", pkt, 8)
        if after or (got_type and got_type != atom_type):
            # A chunked read, or a type the server disagrees with: what comes
            # back is not a whole list and appending to it would corrupt it.
            return pkt
        have = struct.unpack_from("<%dI" % nitems, pkt, 32) if nitems else ()
        full = shadow_mod.supported_union(have, server.own)
        body = struct.pack("<%dI" % len(full), *full)
        return (pkt[:1] + b"\x20" + pkt[2:8] + struct.pack("<III", atom_type, 0,
                                                           len(full))
                + pkt[20:32] + body)
    conn.edit(edit, req.seq)
    return None


def _property_reply(server, conn, req, table, atom, want_type, offset, length,
                    delete, entry):
    """One `GetProperty` reply out of `{atom: (type, format, bytes)}`."""
    got = table.get(atom)
    if got is None:
        # Absent: format 0, type None(0), and no error whatever was asked for.
        return wire.reply(req.seq, 0, struct.pack("<III12x", 0, 0, 0))
    type_atom, fmt, value = got
    if want_type and want_type != type_atom:
        return wire.reply(req.seq, fmt,
                          struct.pack("<III12x", type_atom, len(value), 0))
    start = 4 * offset
    if start > len(value):
        return wire.error(wire.ERR_VALUE, req.seq, offset, wire.OP_GET_PROPERTY)
    take = min(len(value) - start, 4 * length)
    chunk = value[start:start + take]
    after = len(value) - start - len(chunk)
    if delete and not after and entry is not None:
        _tombstone(server, entry, atom)
    return wire.reply(req.seq, fmt,
                      struct.pack("<III12x", type_atom, after,
                                  len(chunk) // (fmt // 8)),
                      chunk)


def _tombstone(server, entry, atom) -> None:
    """`delete = 1` on a full read. A synthesized value cannot be deleted --
    the next read re-synthesizes it -- so the overlay records that this client
    asked, exactly as X does for a property the window manager keeps
    rewriting. Batch 5 owns the `PropertyNotify(Deleted)` that goes with it.
    """
    entry.overlay[atom] = shadow_mod.TOMBSTONE
    name = server.own.atom_name(atom) if server.own is not None else None
    server.debug_say("GetProperty(delete) on %s of shadow 0x%x: tombstoned"
                     % (name or atom, entry.shadow))


# -- ListProperties -----------------------------------------------------------


def list_properties(server, conn, req):
    """A shadow answers design section 4.4's order with the overlay's own names
    last; the root's reply is edited to the union.

    A shadow is visibly thinner than an X twin -- the live xterm on the rig
    carries 13 properties and a shadow carries twelve at most [M 2026-09-10,
    scratchpad b3/xterm_props.py] -- and the five that are missing
    (`WM_HINTS`, `WM_NORMAL_HINTS`, `_NET_WM_ICON`, `_NET_WM_USER_TIME`,
    `_NET_WM_ALLOWED_ACTIONS`) are rows in the docs with their routes, not a
    policy.
    """
    if req.target == "shadow":
        entry = _entry(server, req.xid)
        if entry is None:
            return None
        atoms = list(server.shadows.props_for(entry))
        return wire.reply(req.seq, 0, struct.pack("<H22x", len(atoms)),
                          struct.pack("<%dI" % len(atoms), *atoms))
    if server.shadows is None or server.own is None:
        return None
    mine = list(server.shadows.root_props())

    def edit(pkt):
        if len(pkt) < 32 or pkt[0] != 1:
            return pkt
        (have,) = struct.unpack_from("<H", pkt, 8)
        upstream = struct.unpack_from("<%dI" % have, pkt, 32) if have else ()
        extra = [a for a in mine if a not in upstream]
        body = pkt[32:32 + 4 * have] + struct.pack("<%dI" % len(extra), *extra)
        return (pkt[:8] + struct.pack("<H", have + len(extra)) + pkt[10:32]
                + body)
    return edit



# -- QueryPointer (design section 6.6) ----------------------------------------


def query_pointer(server, conn, req):
    """Where the pointer is, from the first source that knows.

    `getmouselocation` is one `QueryPointer(root)` and `xdotool mousemove`
    sends two before it moves anything [recon/tools.md 4.2, 4.5], on both
    generations of the tool -- so this one handler is what makes the read half
    of the pointer work whether the write half was a `WarpPointer` or a
    `FakeInput MotionNotify` [recon/wire.md 5.3].

    On the ROOT and on a real X window it is **EDIT**: the request goes
    upstream, so `mask` and an X-window `child` are Xwayland's own -- its XKB
    modifier state is fed by the compositor's seat -- and the reply's
    `root_x/root_y` are overwritten from `backend.pointer()`, else from the
    last position this proxy routed, else left exactly as upstream answered
    (real xdotool reads `x:640 y:360` off Xwayland on the headless rig today
    [recon/env.md 2.2]). `child` becomes the shadow under that point when the
    hit test names a native toplevel, and `win_x/win_y` move by the same delta
    as `root_x/root_y`, which is right for any window whose origin did not move
    under us and needs no second request to know.

    On a SHADOW it is **ANSWER**: forwarding a shadow id would draw
    `BadWindow` from a server that never minted it (design section 3.2), so the
    reply is built here with the same three sources -- the third one asked for
    off the proxy's own connection, since this path has no forwarded reply to
    read it from -- `same_screen = 1`, `child = 0` and a `mask` of what this
    proxy is holding.

    `child` is 0 and never a shadow. X's `child` is the child of the window
    ASKED ABOUT that contains the pointer, and a shadow has no children at all:
    `QueryTree` on one answers none (design section 4.1). A sibling toplevel
    under the pointer is not a child of this one, and naming it would describe
    a tree that does not exist.
    """
    where = xtest_mod.pointer_for(server, conn)
    if req.target != SHADOW:
        return _pointer_edit(server, conn, where)
    entry = _entry(server, req.xid)
    if entry is None:
        return None
    root = _root_of(conn)
    if where is None:
        where = _upstream_pointer(server)
    if where is None:
        # Not one of the three knows, and a shadow cannot be forwarded: the
        # answer is the window's own origin with the pointer at it, said once.
        # NOT YET: the route is the compositor's own cursor query (AGENTS.md
        # rung 1/2; GNOME and Wayfire have one, sway does not
        # [recon/seams.md 2.3]).
        server.say_once("shadow-pointer",
                        "QueryPointer on a shadow with no pointer source at "
                        "all -- not the compositor, not this proxy, not the "
                        "upstream: answered at the window's origin. Reading "
                        "sway's cursor is not yet done -- its IPC carries none "
                        "-- and the route is the compositor's own query where "
                        "it has one (AGENTS.md rung 2), or the daemon's model "
                        "once it has injected a motion")
        x, y = int(entry.window.x), int(entry.window.y)
    else:
        x, y = where
    win_x = x - int(entry.window.x)
    win_y = y - int(entry.window.y)
    return wire.reply(req.seq, 1,
                      struct.pack("<IIhhhhH6x", root, 0,
                                  _clamp16(x), _clamp16(y),
                                  _clamp16(win_x), _clamp16(win_y),
                                  server.xtest.modifier_mask()))


def _upstream_pointer(server):
    """Design section 6.6's third source, on the one path that cannot forward:
    `QueryPointer(root)` off the proxy's own connection, `(root_x, root_y)` at
    offsets 16 and 18 [recon/wire.md 4.1].

    Synchronous, on the loop thread, the same shape and the same round trip of
    ~70 us as `req_write.real_geometry` and `OwnConn.atom_name()`
    [xw11/upstream.py:396, recon/env.md 5.3]. It is paid only for a
    `QueryPointer` aimed AT A SHADOW with no other source: `getmouselocation`
    asks the root [recon/tools.md 4.2], which is the EDIT path and costs
    nothing extra. `mask` is deliberately not taken from this reply: nothing
    measured whether Xwayland's XKB state still follows the seat while a native
    toplevel holds the focus, and an unmeasured modifier is worse than none.

    Nor is the position seeded into the input daemon, which the first two
    sources are (design section 6.6): Xwayland's pointer is not the seat's.
    Measured 2026-09-11 -- a raw `WarpPointer` to (321, 123) straight at
    Xwayland moved its own `QueryPointer` there and the compositor's cursor not
    at all -- so seeding the daemon with this number would tell it the seat is
    somewhere it has never been, and the next relative move would start from a
    fiction.
    """
    own = server.own
    if own is None or not own.open:
        return None
    root = getattr(own, "root", 0)
    box = {}

    def took(pkt):
        box["p"] = struct.unpack_from("<hh", pkt, 16) if pkt[0] == 1 else None
    try:
        own.send(wire.OP_QUERY_POINTER, 0, struct.pack("<I", root),
                 on_reply=took)
        own._drain_until(lambda: "p" in box)
    except Exception as e:                        # UpstreamGone, and no more
        server.say("QueryPointer: asking the upstream where the pointer is "
                   "failed (%s)" % (e,))
        return None
    return box.get("p")


def _pointer_edit(server, conn, where):
    """The editor for a `QueryPointer` that went upstream.

    Registered even when no proxy-side source knows where the pointer is,
    because `child` still has to be answered: the third source of design
    section 6.6 is upstream's own reply, and the shadow under THAT position is
    what `getmouselocation` has to print. Without this, `window:` on the
    headless rig named an X window (or 0) with a `foot` sitting under Xwayland's
    own (640, 360) [recon/env.md 2.2] until something moved the pointer.
    """
    def edit(pkt):
        if len(pkt) < 32 or pkt[0] != 1:
            return pkt
        old_x, old_y, win_x, win_y = struct.unpack_from("<hhhh", pkt, 16)
        x, y = (old_x, old_y) if where is None else where
        child = xtest_mod.child_under(server, x, y)
        if not child:
            (child,) = struct.unpack_from("<I", pkt, 12)
        return (pkt[:12] + struct.pack("<Ihhhh", child, _clamp16(x), _clamp16(y),
                                       _clamp16(win_x + x - old_x),
                                       _clamp16(win_y + y - old_y))
                + pkt[24:])
    return edit


# -- GetInputFocus ------------------------------------------------------------


def get_input_focus(server, conn, req):
    """The shadow of the focused toplevel, when the compositor's focus is a
    native window; otherwise upstream's answer, untouched.

    `PointerRoot` (1) included, deliberately. With no window manager
    `GetInputFocus` answers 1, xdotool then reads `WM_STATE` on window `0x1`,
    the server answers `BadWindow` and xdotool prints Xlib's default fatal
    handler and exits 1 [recon/env.md 3]. That is a bug of xdotool's and
    AGENTS.md keeps bugs, so this touches nothing when there is no native
    window to name.
    """
    if server.shadows is None:
        return None

    def edit(pkt):
        if len(pkt) < 32 or pkt[0] != 1:
            return pkt
        # Through the TTL, like every other read (design section 4.8): the
        # focus moving between two NATIVE toplevels sends no root request, so
        # an editor that walked `order` straight named the window that used to
        # be focused until something else re-listed.
        server.shadows.snapshot()
        entry = server.shadows.focused_entry()
        if entry is None or not entry.shadow:
            return pkt
        return pkt[:8] + struct.pack("<I", entry.shadow) + pkt[12:]
    return edit


#: `Server.handlers`, keyed by `client.Request.key` -- the opcode for a core
#: request. A class whose row is on but whose handler answers None forwards, so
#: every row here degrades to X rather than to a hole.
HANDLERS = {
    wire.OP_QUERY_TREE: query_tree,
    wire.OP_GET_GEOMETRY: get_geometry,
    wire.OP_GET_WINDOW_ATTRIBUTES: get_window_attributes,
    wire.OP_TRANSLATE_COORDINATES: translate_coordinates,
    wire.OP_GET_PROPERTY: get_property,
    wire.OP_LIST_PROPERTIES: list_properties,
    wire.OP_GET_INPUT_FOCUS: get_input_focus,
    wire.OP_QUERY_POINTER: query_pointer,
}


def install(server) -> None:
    """Put the read handlers into a server's table. Called once, from
    `Server.__init__`; a later batch adds its own the same way, and a key
    written twice is a collision two batches would both have to see."""
    server.handlers.update(HANDLERS)
