"""The X11 wire, as codecs: no sockets, no state, no I/O.

Everything here is measured. The splitter is `xtrace-master/parse.c:1978` and
the byte offsets are recon/wire.md sections 1-3, taken off a live Xvfb 21.1.22
and the box's own :355; the request and extension tables are recon/wire.md
section 10 (from /usr/share/xcb/*.xml) with the dynamic majors left out on
purpose -- an extension's major is per-server and is learned at run time, never
written down (RANDR is 140 on Xvfb and 139 on Xwayland [recon/tools.md 7,
recon/env.md 2.5]).

Two rules the rest of the proxy leans on:

* **byte 2-3 of a request is the whole framing.** No opcode table is needed to
  split a client's stream. A zero there means BIG-REQUESTS, but only for a
  connection that enabled it -- xtrace treats any zero as big and a real server
  does not, which is what `BAD_LENGTH` is for (recon/wire.md 2).
* **a server packet is 32 bytes, plus `4 * u32le[4]` when byte 0 is 1 (Reply)
  or `byte 0 & 0x7F` is 35 (GenericEvent).** Nothing else is variable
  (recon/wire.md 3.1, confirmed against the 136-byte XI_Motion in
  tests/fixtures/xw11/geprobe-136.hex).
"""

import struct
from typing import NamedTuple

from wdotool.x11_mini import _MAX_REPLY_WORDS


class WireError(Exception):
    """A packet that cannot be framed or parsed: a lying length, a setup reply
    whose screen walk runs off the end. Never raised for "not enough bytes yet",
    which is `None`."""


#: `split_request`'s third answer, beside a frame and "not yet": the client sent
#: a zero 16-bit length without enabling BIG-REQUESTS. Measured against Xvfb
#: 21.1.22 (R3, tests/fixtures/xw11/badlength-nobigreq.hex): the server answers
#: one 32-byte BadLength naming the request's own major, keeps the connection,
#: and still counts the request in the sequence.
BAD_LENGTH = "BAD_LENGTH"


def pad4(b: bytes) -> bytes:
    """Pad to a 4-byte boundary, the protocol's only alignment rule."""
    return b + b"\0" * (-len(b) % 4)


def padlen(n: int) -> int:
    """`n` rounded up to a multiple of 4 -- the padded length of an `n`-byte
    string in a setup request or an InternAtom."""
    return n + (-n % 4)


# -- framing ------------------------------------------------------------------


def split_request(buf, bigreq: bool):
    """One request off the head of `buf`.

    Returns `(nbytes, opcode, byte1)` when a whole request is there, `None`
    when more bytes are needed, `BAD_LENGTH` for the zero length a connection
    that never enabled BIG-REQUESTS is not allowed to send.

    `nbytes` counts the header: 4 bytes for the ordinary form, 8 for the big
    one. `byte1` is the request-specific data byte for a core request and the
    MINOR opcode for an extension request -- the reason it comes back here at
    all is that the policy table of xw11/policy.py keys on the pair.
    """
    if len(buf) < 4:
        return None
    opcode = buf[0]
    byte1 = buf[1]
    (words,) = struct.unpack_from("<H", buf, 2)
    if words:
        n = words * 4
        return (n, opcode, byte1) if len(buf) >= n else None
    if not bigreq:
        return BAD_LENGTH
    if len(buf) < 8:
        return None
    (big,) = struct.unpack_from("<I", buf, 4)
    n = big * 4
    if n < 8:
        # A big form claiming less than its own 8-byte header. Not measured --
        # R3 measured the no-BIG-REQUESTS case only -- but the two alternatives
        # are a negative frame length and a stream that never advances, so it
        # takes the same route as the measured one and the server's own answer
        # stays the oracle when someone captures it.
        return BAD_LENGTH
    return (n, opcode, byte1) if len(buf) >= n else None


def split_server(buf):
    """The length of the packet at the head of `buf`, or `None` before its
    32-byte head has arrived.

    The length is answered from the head alone, deliberately: a 200 kB
    GetProperty reply and the 400 MB one libX11 asks for on every connection
    (recon/tools.md 2, request 4) are streamed through, never held whole, so the
    caller needs the size long before the bytes are all here.

    A length field over `x11_mini._MAX_REPLY_WORDS` (16 MiB of body) is a lying
    server and raises: the cap is on the FIELD, never on what a client asks for.
    """
    if len(buf) < 32:
        return None
    code = buf[0]
    if code == 1 or (code & 0x7F) == 35:
        (extra,) = struct.unpack_from("<I", buf, 4)
        if extra > _MAX_REPLY_WORDS:
            raise WireError("reply length field claims %d words" % extra)
        return 32 + 4 * extra
    return 32


# -- the setup ----------------------------------------------------------------


def parse_setup_request(buf):
    """`(order, nlen, dlen, total)` off a client's first 12 bytes, or `None`.

    `order` is byte 0 as it stands: 0x6C 'l' LSB-first, 0x42 'B' MSB-first.
    Every other field is in the client's own byte order, so a 'B' client's
    lengths read here are wrong on purpose -- the caller refuses it (nothing on
    this box has ever sent one, recon/wire.md 1.1) rather than mis-framing it.
    `total` is `12 + pad4(n) + pad4(d)`, xtrace-master/parse.c:1965's arithmetic.
    """
    if len(buf) < 12:
        return None
    order = buf[0]
    nlen, dlen = struct.unpack_from("<HH", buf, 6)
    return order, nlen, dlen, 12 + padlen(nlen) + padlen(dlen)


class Setup(NamedTuple):
    """A parsed setup reply, and the body it was parsed from.

    `roots` and `cmaps` are per screen, the way `x11_mini._handshake` keeps
    them. `root_depth` and `root_visual` are screen 0's (SCREEN +38 and +32):
    every shadow the proxy synthesizes is on the setup's root, which is that
    screen's, and both captured setups carry exactly one screen
    (tests/fixtures/xw11/setup-{xvfb,xwayland}.hex). `parsed` is how many body
    bytes the screen walk consumed -- equal to `len(body)` on a well-formed
    reply, which is the validation recon/wire.md 1.2 used to prove the offsets.
    """

    status: int
    rid_base: int
    rid_mask: int
    max_words: int
    roots: list
    cmaps: list
    root_depth: int
    root_visual: int
    min_keycode: int
    max_keycode: int
    body: bytes
    parsed: int


def parse_setup_reply(head8: bytes, body: bytes) -> Setup:
    """The server's setup reply, parsed but never consumed: `body` is kept so
    the caller can forward the reply verbatim (design section 2.3 -- the setup
    is never terminated or re-originated, because the per-connection
    `resource-id-base` is the client's and not ours, recon/wire.md 1.3).

    The walk is `x11_mini._handshake`'s (x11_mini.py:346-355) with the two
    fields it discards kept. A status other than 1 (Failed, Authenticate) parses
    no further: there are no screens in it.
    """
    if len(head8) < 8:
        raise WireError("setup reply head is %d bytes" % len(head8))
    status = head8[0]
    if status != 1:
        return Setup(status, 0, 0, 0, [], [], 0, 0, 0, 0, body, 0)
    try:
        rid_base, rid_mask = struct.unpack_from("<II", body, 4)
        (vlen,) = struct.unpack_from("<H", body, 16)
        (max_words,) = struct.unpack_from("<H", body, 18)
        nscreens, nformats = body[20], body[21]
        min_keycode, max_keycode = body[26], body[27]
        p = 32 + padlen(vlen) + 8 * nformats
        roots, cmaps, depths, visuals = [], [], [], []
        for _ in range(nscreens):
            root, cmap = struct.unpack_from("<II", body, p)
            (visual,) = struct.unpack_from("<I", body, p + 32)
            depth = body[p + 38]
            ndepths = body[p + 39]
            p += 40
            for _ in range(ndepths):
                (nvis,) = struct.unpack_from("<H", body, p + 2)
                p += 8 + 24 * nvis
            roots.append(root)
            cmaps.append(cmap)
            depths.append(depth)
            visuals.append(visual)
    except (struct.error, IndexError) as e:
        raise WireError("malformed setup reply: %s" % e) from e
    if not roots:
        raise WireError("setup reply carries no screens")
    if p > len(body):
        raise WireError("screen walk ran %d bytes past the body" % (p - len(body)))
    return Setup(status, rid_base, rid_mask, max_words, roots, cmaps,
                 depths[0], visuals[0], min_keycode, max_keycode, body, p)


# -- packers ------------------------------------------------------------------


def reply(seq: int, byte1: int, fixed24: bytes, body: bytes = b"") -> bytes:
    """A reply: `1`, a reply-specific byte, the sequence, the additional length
    in words, 24 bytes of fixed part, then the variable part (recon/wire.md 3.1).
    `body` is padded here; its padded length is what goes in the length word."""
    if len(fixed24) != 24:
        raise WireError("a reply's fixed part is 24 bytes, not %d" % len(fixed24))
    body = pad4(body)
    return (struct.pack("<BBHI", 1, byte1 & 0xFF, seq & 0xFFFF, len(body) // 4)
            + fixed24 + body)


def error(code: int, seq: int, bad: int, major: int, minor: int = 0) -> bytes:
    """A 32-byte error, in the live BadDrawable layout of recon/wire.md 3.1:
    `00 09 0400 efbeadde 0000 0e` for `error(9, 4, 0xdeadbeef, 14, 0)`."""
    return struct.pack("<BBHIHB21x", 0, code & 0xFF, seq & 0xFFFF,
                       bad & 0xFFFFFFFF, minor & 0xFFFF, major & 0xFF)


def event_seq(pkt: bytes, seq: int) -> bytes:
    """`pkt` with bytes 2-3 set to `seq`. KeymapNotify (code 11) is the one core
    event with no sequence field -- bytes 1..31 are the keymap -- and comes back
    untouched (recon/wire.md 3.1)."""
    if len(pkt) < 4 or (pkt[0] & 0x7F) == EV_KEYMAP_NOTIFY:
        return pkt
    return pkt[:2] + struct.pack("<H", seq & 0xFFFF) + pkt[4:]


#: The two substitutes of design section 3.1, as the bytes they are on the wire.
#: A request the proxy consumes still costs one sequence number upstream, for
#: ever, so something has to go up in its place (recon/wire.md 3.2a, 3.3).
NOOP = b"\x7f\x00\x01\x00"                 # NoOperation, opcode 127
GET_INPUT_FOCUS = b"\x2b\x00\x01\x00"      # GetInputFocus, opcode 43


# -- constants (recon/wire.md 10) ---------------------------------------------
#
# Core opcodes are fixed by the protocol and are written down. Extension majors
# are NOT: they are assigned per server at run time and differ between the two
# servers this project targets, so every one of them is learned from a
# QueryExtension reply and only the MINORS live here.

OP_CHANGE_WINDOW_ATTRIBUTES = 2
OP_GET_WINDOW_ATTRIBUTES = 3
OP_DESTROY_WINDOW = 4
OP_MAP_WINDOW = 8
OP_UNMAP_WINDOW = 10
OP_CONFIGURE_WINDOW = 12
OP_GET_GEOMETRY = 14
OP_QUERY_TREE = 15
OP_INTERN_ATOM = 16
OP_GET_ATOM_NAME = 17
OP_CHANGE_PROPERTY = 18
OP_DELETE_PROPERTY = 19
OP_GET_PROPERTY = 20
OP_LIST_PROPERTIES = 21
OP_SEND_EVENT = 25
OP_GRAB_SERVER = 36
OP_UNGRAB_SERVER = 37
OP_QUERY_POINTER = 38
OP_TRANSLATE_COORDINATES = 40
OP_WARP_POINTER = 41
OP_SET_INPUT_FOCUS = 42
OP_GET_INPUT_FOCUS = 43
OP_CREATE_GC = 55
OP_QUERY_EXTENSION = 98
OP_LIST_EXTENSIONS = 99
OP_GET_KEYBOARD_MAPPING = 101
OP_KILL_CLIENT = 113
OP_GET_MODIFIER_MAPPING = 119
OP_NO_OPERATION = 127

#: BIG-REQUESTS has one request and the reply carries the new ceiling as a
#: CARD32 of words at offset 8: 4194303 on both servers here (recon/wire.md 2).
BIGREQ_ENABLE = 0

#: XTEST minors (recon/wire.md 10; the major was 132 on Xvfb, 133 on Xwayland).
XTEST_GET_VERSION = 0
XTEST_COMPARE_CURSOR = 1
XTEST_FAKE_INPUT = 2
XTEST_GRAB_CONTROL = 3

#: RandR minors. `GetScreenResourcesCurrent` (25) is what xrandr 1.5 actually
#: calls; `SetScreenSize` (7) is the first thing Xwayland refuses with BadMatch
#: (recon/env.md 0.5).
RR_QUERY_VERSION = 0
RR_SET_SCREEN_CONFIG = 2
RR_SELECT_INPUT = 4
RR_GET_SCREEN_INFO = 5
RR_GET_SCREEN_SIZE_RANGE = 6
RR_SET_SCREEN_SIZE = 7
RR_GET_SCREEN_RESOURCES = 8
RR_GET_OUTPUT_INFO = 9
RR_GET_OUTPUT_PROPERTY = 15
RR_CREATE_MODE = 16
RR_ADD_OUTPUT_MODE = 18
RR_GET_CRTC_INFO = 20
RR_SET_CRTC_CONFIG = 21
RR_GET_CRTC_GAMMA = 23
RR_GET_SCREEN_RESOURCES_CURRENT = 25
RR_GET_CRTC_TRANSFORM = 27
RR_GET_PANNING = 28
RR_SET_PANNING = 29
RR_SET_OUTPUT_PRIMARY = 30
RR_GET_OUTPUT_PRIMARY = 31

#: Event codes. The proxy generates the first nine; 11 is here because it is the
#: one that must never have its sequence patched, and 35 because it is the one
#: with a variable length.
EV_KEY_PRESS = 2
EV_FOCUS_IN = 9
EV_FOCUS_OUT = 10
EV_KEYMAP_NOTIFY = 11
EV_CREATE_NOTIFY = 16
EV_DESTROY_NOTIFY = 17
EV_UNMAP_NOTIFY = 18
EV_MAP_NOTIFY = 19
EV_CONFIGURE_NOTIFY = 22
EV_PROPERTY_NOTIFY = 28
EV_CLIENT_MESSAGE = 33
EV_GENERIC = 35
#: bit 0x80 of byte 0: "this came from SendEvent"
EV_SENT = 0x80

#: The CWEventMask bit of ChangeWindowAttributes, which is how a client asks for
#: events on a window (recon/wire.md 10).
CW_EVENT_MASK = 0x0800

ERR_REQUEST = 1
ERR_VALUE = 2
ERR_WINDOW = 3
ERR_PIXMAP = 4
ERR_ATOM = 5
ERR_CURSOR = 6
ERR_FONT = 7
ERR_MATCH = 8
ERR_DRAWABLE = 9
ERR_ACCESS = 10
ERR_ALLOC = 11
ERR_COLOR = 12
ERR_GC = 13
ERR_ID_CHOICE = 14
ERR_NAME = 15
ERR_LENGTH = 16
ERR_IMPLEMENTATION = 17
