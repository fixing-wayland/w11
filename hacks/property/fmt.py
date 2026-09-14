"""xprop's formatting machinery, ported from xprop.c
(1.2.8) with byte parity as the goal — including the jank.

Everything here is pure computation over bytes; no X, no compositor. The
notable bug-for-bug ports (all verified against oracle captures):

- -len N models Xlib's buffer shape: the fetch is capped at (N+3)/4 32-bit
  WORDS, but the byte budget is counted against sizeof(long)=8 bytes per
  32-bit item (Xlib hands xprop an array of longs), so `-len 8` yields one
  atom of a list and `-len 12` yields two. There is no "..." ellipsis;
  truncation just yields fewer/shorter fields.
- 32-bit values carry the format char's signedness: Xlib's _XRead32
  sign-extends each wire CARD32 into a long, and Extract_Value returns
  `*(long*)` for `i` but masks `& 0xffffffff` otherwise — so `32i` of
  0xFFFFFFFF prints -1 and of 0xfffffffb prints -5 (a real INTEGER dumps
  its negatives), while `c`/`x`/`a` stay unsigned (0xFFFFFFFF -> 4294967295
  / 0xffffffff). A CARDINAL's DEFAULT format is `0c` (unsigned), so a
  0xffffffff CARDINAL still prints 4294967295 unless you force `32i`. 16/8-
  bit `i` sign-extends the same way. All oracle-verified.
- out-of-range $n inside a ?(...) conditional reads one past the thunk list
  in C (heap garbage). We use a sentinel that is nonzero and equal to no
  small constant, which reproduces the observable behavior of every real
  dformat ("Initial state is ." under -len 8, empty gravity, ...).
- string thunks carry their length INCLUDING the terminating NUL, exactly
  like Extract_Len_String counts it; the `t` converter therefore renders a
  trailing NUL as a trailing `\\000` item, like XmbTextPropertyToTextList.
- the `\\<octal>` dformat escape really does drop the first digit into the
  case selector and parses the octal number from the SECOND digit on
  (Handle_Backslash + Scan_Octal), so `\\0101` is 'A' and `\\101` is '\\1'.
- Format_Icons' ascii art: brightness truncated to int, C-locale palette of
  71 glyphs, two chars per pixel, `(not shown)` when the display width +8
  exceeds the terminal width (152 when stdin is not a tty) or height > 144.

Deliberate divergences (documented here, unreachable in normal use):
- a dformat ending in a lone backslash stops cleanly instead of walking
  past the terminator into heap garbage;
- an icon block with fewer than two longs left is treated as truncated
  instead of reading past the buffer;
- `t` conversion supports STRING, UTF8_STRING (UTF-8 locales) and the
  latin-1 subset of COMPOUND_TEXT; anything else falls back to the quoted
  `s` rendering, which is also what Xlib's XConverterNotFound path does.
- a `\\<octal>` escape requires an immediate [0-7] digit; C's Scan_Octal
  uses sscanf("%lo"), which also skips leading whitespace and accepts a
  sign, so absurd dformats like `\\0 17` or `\\0-7` diverge (verified). Our
  form covers every real dformat character-for-character.
- conditional nesting past the interpreter recursion limit (a dformat like
  `?` + "("*100000) is a clean fatal ("maximum recursion depth exceeded"),
  where C recurses on the machine stack and only segfaults far deeper.
- the ?m<n> mask term shifts by an int whose count x86-64 masks to 6 bits
  (?m64 tests bit 0, ?m66 bit 2 — oracle-verified); a raw 1<<n would build
  a multi-gigabyte integer for a hostile n and OOM (CVE-shaped), so the
  count is truncated to (int) then &63, matching the C UB byte for byte.
"""

import re
import struct

MAXSTR = 500000

DEFAULT_FORMAT = b"0x"
DEFAULT_DFORMAT = b" = $0+\n"

# Stand-in for xprop's uninitialized one-past-the-end thunk reads: nonzero
# (truthy in ?$n), equal to no constant a real dformat compares against.
_GARBAGE = 1 << 62

_C_PALETTE = (b" .'`,^:\";~-_+<>i!lI?/\\|()1{}[]rcvunxzjft"
              b"LCJUYXZO0Qoahkbdpqwm*WMB8&%$#@")
_UTF8_PALETTE = (b" ", b"\342\226\221", b"\342\226\222", b"\342\226\223", b"\342\226\210")

UTF8_VALID = 0
UTF8_FORBIDDEN_VALUE = 1
UTF8_OVERLONG = 2
UTF8_SHORT_TAIL = 3
UTF8_LONG_TAIL = 4

_UTF8_ERRORS = {
    UTF8_FORBIDDEN_VALUE: b"<Invalid UTF-8 string: Forbidden value> ",
    UTF8_OVERLONG: b"<Invalid UTF-8 string: Overlong encoding> ",
    UTF8_SHORT_TAIL: b"<Invalid UTF-8 string: Tail too short> ",
    UTF8_LONG_TAIL: b"<Invalid UTF-8 string: Tail too long> ",
}


class FatalError(Exception):
    """Rendered by the CLI as `<prog>: error: <msg>` + exit 1, exactly like
    dsimple.c's Fatal_Error."""


# -- xprop.c's built-in windowPropTable, verbatim ---------------------------

_ARC_DFORMAT = (b":\n"
                b"\t\tarc at $0, $1\n"
                b"\t\tsize: $2 by $3\n"
                b"\t\tfrom angle $4 to angle $5\n")

_RECTANGLE_DFORMAT = (b":\n"
                      b"\t\tupper left corner: $0, $1\n"
                      b"\t\tsize: $2 by $3\n")

_RGB_COLOR_MAP_DFORMAT = (b":\n"
                          b"\t\tcolormap id #: $0\n"
                          b"\t\tred-max: $1\n"
                          b"\t\tred-mult: $2\n"
                          b"\t\tgreen-max: $3\n"
                          b"\t\tgreen-mult: $4\n"
                          b"\t\tblue-max: $5\n"
                          b"\t\tblue-mult: $6\n"
                          b"\t\tbase-pixel: $7\n"
                          b"\t\tvisual id #: $8\n"
                          b"\t\tkill id #: $9\n")

_WM_HINTS_DFORMAT = (b":\n"
                     b"?m0(\t\tClient accepts input or input focus: $1\n)"
                     b"?m1(\t\tInitial state is "
                     b"?$2=0(Don't Care State)"
                     b"?$2=1(Normal State)"
                     b"?$2=2(Zoomed State)"
                     b"?$2=3(Iconic State)"
                     b"?$2=4(Inactive State)"
                     b".\n)"
                     b"?m2(\t\tbitmap id # to use for icon: $3\n)"
                     b"?m5(\t\tbitmap id # of mask for icon: $7\n)"
                     b"?m3(\t\twindow id # to use for icon: $4\n)"
                     b"?m4(\t\tstarting position for icon: $5, $6\n)"
                     b"?m6(\t\twindow id # of group leader: $8\n)"
                     b"?m8(\t\tThe urgency hint bit is set\n)")

_WM_ICON_SIZE_DFORMAT = (b":\n"
                         b"\t\tminimum icon size: $0 by $1\n"
                         b"\t\tmaximum icon size: $2 by $3\n"
                         b"\t\tincremental size change: $4 by $5\n")

_WM_SIZE_HINTS_DFORMAT = (
    b":\n"
    b"?m0(\t\tuser specified location: $1, $2\n)"
    b"?m2(\t\tprogram specified location: $1, $2\n)"
    b"?m1(\t\tuser specified size: $3 by $4\n)"
    b"?m3(\t\tprogram specified size: $3 by $4\n)"
    b"?m4(\t\tprogram specified minimum size: $5 by $6\n)"
    b"?m5(\t\tprogram specified maximum size: $7 by $8\n)"
    b"?m6(\t\tprogram specified resize increment: $9 by $10\n)"
    b"?m7(\t\tprogram specified minimum aspect ratio: $11/$12\n"
    b"\t\tprogram specified maximum aspect ratio: $13/$14\n)"
    b"?m8(\t\tprogram specified base size: $15 by $16\n)"
    b"?m9(\t\twindow gravity: "
    b"?$17=0(Forget)"
    b"?$17=1(NorthWest)"
    b"?$17=2(North)"
    b"?$17=3(NorthEast)"
    b"?$17=4(West)"
    b"?$17=5(Center)"
    b"?$17=6(East)"
    b"?$17=7(SouthWest)"
    b"?$17=8(South)"
    b"?$17=9(SouthEast)"
    b"?$17=10(Static)"
    b"\n)")

_WM_STATE_DFORMAT = (b":\n"
                     b"\t\twindow state: ?$0=0(Withdrawn)"
                     b"?$0=1(Normal)?$0=3(Iconic)\n"
                     b"\t\ticon window: $1\n")

WINDOW_PROP_TABLE = (
    (b"ARC", b"16iiccii", _ARC_DFORMAT),
    (b"ATOM", b"32a", None),
    (b"BITMAP", b"32x", b": bitmap id # $0\n"),
    (b"CARDINAL", b"0c", None),
    (b"COLORMAP", b"32x", b": colormap id # $0\n"),
    (b"CURSOR", b"32x", b": cursor id # $0\n"),
    (b"DRAWABLE", b"32x", b": drawable id # $0\n"),
    (b"FONT", b"32x", b": font id # $0\n"),
    (b"INTEGER", b"0i", None),
    (b"PIXMAP", b"32x", b": pixmap id # $0\n"),
    (b"POINT", b"16ii", b" = $0, $1\n"),
    (b"RECTANGLE", b"16iicc", _RECTANGLE_DFORMAT),
    (b"RGB_COLOR_MAP", b"32xcccccccxx", _RGB_COLOR_MAP_DFORMAT),
    (b"STRING", b"8s", None),
    (b"UTF8_STRING", b"8u", None),
    (b"WINDOW", b"32x", b": window id # $0+\n"),
    (b"VISUALID", b"32x", b": visual id # $0\n"),
    (b"WM_COLORMAP_WINDOWS", b"32x", b": window id # $0+\n"),
    (b"WM_COMMAND", b"8s", b" = { $0+ }\n"),
    (b"WM_HINTS", b"32mbcxxiixx", _WM_HINTS_DFORMAT),
    (b"WM_ICON_NAME", b"8t", None),
    (b"WM_ICON_SIZE", b"32cccccc", _WM_ICON_SIZE_DFORMAT),
    (b"WM_NAME", b"8t", None),
    (b"WM_PROTOCOLS", b"32a", b": protocols  $0+\n"),
    (b"WM_SIZE_HINTS", b"32mii", _WM_SIZE_HINTS_DFORMAT),
    (b"_NET_WM_ICON", b"32o", None),
    (b"WM_STATE", b"32cx", _WM_STATE_DFORMAT),
)


# xprop's fontPropTable: the formats -font uses instead of the window ones. A font property not named here falls
# back to the default "0x", which is why an unknown one prints as a bare hex number.
FONT_PROP_TABLE = tuple(
    (name, fmt, None) for name, fmt in (
        (b"FOUNDRY", b"32a"), (b"FAMILY_NAME", b"32a"),
        (b"WEIGHT_NAME", b"32a"), (b"SLANT", b"32a"),
        (b"SETWIDTH_NAME", b"32a"), (b"ADD_STYLE_NAME", b"32a"),
        (b"PIXEL_SIZE", b"32c"), (b"POINT_SIZE", b"32c"),
        (b"RESOLUTION_X", b"32c"), (b"RESOLUTION_Y", b"32c"),
        (b"SPACING", b"32a"), (b"AVERAGE_WIDTH", b"32c"),
        (b"CHARSET_REGISTRY", b"32a"), (b"CHARSET_ENCODING", b"32a"),
        (b"QUAD_WIDTH", b"32i"), (b"RESOLUTION", b"32c"),
        (b"MIN_SPACE", b"32c"), (b"NORM_SPACE", b"32c"),
        (b"MAX_SPACE", b"32c"), (b"END_SPACE", b"32c"),
        (b"SUPERSCRIPT_X", b"32i"), (b"SUPERSCRIPT_Y", b"32i"),
        (b"SUBSCRIPT_X", b"32i"), (b"SUBSCRIPT_Y", b"32i"),
        (b"UNDERLINE_POSITION", b"32i"), (b"UNDERLINE_THICKNESS", b"32i"),
        (b"STRIKEOUT_ASCENT", b"32i"), (b"STRIKEOUT_DESCENT", b"32i"),
        (b"ITALIC_ANGLE", b"32i"), (b"X_HEIGHT", b"32i"),
        (b"WEIGHT", b"32i"), (b"FACE_NAME", b"32a"),
        (b"COPYRIGHT", b"32a"), (b"AVG_CAPITAL_WIDTH", b"32i"),
        (b"AVG_LOWERCASE_WIDTH", b"32i"), (b"RELATIVE_SETWIDTH", b"32c"),
        (b"RELATIVE_WEIGHT", b"32c"), (b"CAP_HEIGHT", b"32c"),
        (b"SUPERSCRIPT_SIZE", b"32c"), (b"FIGURE_WIDTH", b"32i"),
        (b"SUBSCRIPT_SIZE", b"32c"), (b"SMALL_CAP_SIZE", b"32i"),
        (b"NOTICE", b"32a"), (b"DESTINATION", b"32c"),
        (b"FONT", b"32a"), (b"FONT_NAME", b"32a"),
    ))


# -- scanners (Scan_Long / Scan_Octal / Skip_Digits) ------------------------

def _msg(b: bytes) -> str:
    return b.decode("latin-1")


def _to_int32(v: int) -> int:
    """C's (int) cast of a long: wrap to signed 32-bit."""
    return ((v & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000


def _scan_long(s: bytes, pos: int):
    """(value, new_pos); Fatal 'Bad number' unless a digit starts here."""
    if pos >= len(s) or not (0x30 <= s[pos] <= 0x39):
        raise FatalError("Bad number: %s." % _msg(s[pos:]))
    end = pos
    while end < len(s) and 0x30 <= s[end] <= 0x39:
        end += 1
    return int(s[pos:end]), end


def _c_div(a: int, b: int) -> int:
    """C integer division: truncation toward zero."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) or q == 0 else -q


def is_a_format(s) -> bool:
    b = s.encode("latin-1", "replace") if isinstance(s, str) else s
    return bool(b) and 0x30 <= b[0] <= 0x39


def is_a_dformat(s) -> bool:
    b = s.encode("latin-1", "replace") if isinstance(s, str) else s
    if not b or b[0] == 0x2D:  # '-'
        return False
    c = b[0]
    return not (0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A or c == 0x5F)


def get_format_size(fmt: bytes) -> int:
    size, _pos = _scan_long(fmt, 0)
    if size not in (0, 8, 16, 32):
        raise FatalError("bad format: %s" % _msg(fmt))
    return size


def get_format_char(fmt: bytes, i: int) -> int:
    _size, pos = _scan_long(fmt, 0)
    chars = fmt[pos:]
    if not chars:
        # C prints the advanced (empty) pointer: "bad format: "
        raise FatalError("bad format: ")
    if i >= len(chars):
        i = len(chars) - 1
    return chars[i]


# -- thunks -----------------------------------------------------------------

class Thunk:
    __slots__ = ("value", "extra_value", "extra_encoding")

    def __init__(self, value, extra_value=None, extra_encoding=None):
        self.value = value
        self.extra_value = extra_value        # bytes (may include final NUL)
        self.extra_encoding = extra_encoding  # type name for 't'


def _c_isprint(c: int) -> bool:
    return 0x20 <= c < 0x7F


def _format_char(c: int, unicode_: bool) -> bytes:
    if c in (0x5C, 0x22):  # backslash, double quote
        return b"\\" + bytes([c])
    if c == 0x0A:
        return b"\\n"
    if c == 0x09:
        return b"\\t"
    if not _c_isprint(c):
        if unicode_ and (c & 0x80):
            return bytes([c])
        return b"\\%03o" % c
    return bytes([c])


def format_string(data: bytes, unicode_: bool) -> bytes:
    nul = data.find(0)
    if nul >= 0:
        data = data[:nul]
    return b'"' + b"".join(_format_char(c, unicode_) for c in data) + b'"'


def is_valid_utf8(data: bytes) -> int:
    """Exact port of xprop's validator, quirks included."""
    codepoint = 0
    rem = 0
    for c in data:
        if not (c & 0x80):
            if rem > 0:
                return UTF8_SHORT_TAIL
            rem = 0
            codepoint = c
        elif (c & 0xC0) == 0x80:
            if rem == 0:
                return UTF8_LONG_TAIL
            rem -= 1
            codepoint |= (c & 0x3F) << (rem * 6)
            if codepoint == 0:
                return UTF8_OVERLONG
        elif (c & 0xE0) == 0xC0:
            if rem > 0:
                return UTF8_SHORT_TAIL
            rem = 1
            codepoint = (c & 0x1F) << 6
            if codepoint == 0:
                return UTF8_OVERLONG
        elif (c & 0xF0) == 0xE0:
            if rem > 0:
                return UTF8_SHORT_TAIL
            rem = 2
            codepoint = (c & 0x0F) << 12
        elif (c & 0xF8) == 0xF0:
            if rem > 0:
                return UTF8_SHORT_TAIL
            rem = 3
            codepoint = (c & 0x07) << 18
            if codepoint > 0x10FFFF:
                return UTF8_FORBIDDEN_VALUE
        else:
            return UTF8_FORBIDDEN_VALUE
    return UTF8_VALID


def _decode_one_utf8(data: bytes, i: int):
    """(char, nbytes) for a valid UTF-8 sequence at i, else (None, 0)."""
    c = data[i]
    if c < 0x80:
        return chr(c), 1
    if 0xC0 <= c < 0xF8:
        n = 2 if c < 0xE0 else 3 if c < 0xF0 else 4
        seq = data[i:i + n]
        if len(seq) == n:
            try:
                return seq.decode("utf-8"), n
            except UnicodeDecodeError:
                pass
    return None, 0


class Formatter:
    """The format/dformat engine plus the mapping table. `atom_name` maps an atom id to its name (server lookup
    on the X plane, the synthesized table on the native plane); None means 'undefined atom # 0x%x'."""

    def __init__(self, atom_name=None, notype=False, max_len=MAXSTR,
                 utf8_locale=False, truecolor=False, term_width=152):
        self.atom_name = atom_name or (lambda a: None)
        self.notype = notype
        self.max_len = max_len
        self.utf8_locale = utf8_locale
        self.truecolor = truecolor
        self.term_width = term_width
        # keyed by atom NAME (bytes): equivalent to xprop's atom-id keying,
        # since a live server maps names to atoms injectively
        self.mappings = []  # (name, format|None, dformat|None); later wins

    def setup_window_table(self):
        for name, f, d in WINDOW_PROP_TABLE:
            self.add_mapping(name, f, d)

    def setup_font_table(self):
        """xprop's Setup_Mapping in font mode: the FONT table replaces the window one entirely (`FONT` means
        "32a", the font's XLFD name, not a font id)."""
        for name, f, d in FONT_PROP_TABLE:
            self.add_mapping(name, f, d)

    def add_mapping(self, name: bytes, fmt, dfmt):
        self.mappings.append((name, fmt, dfmt))

    def lookup_formats(self, name: bytes, fmt, dfmt):
        """xprop's Lookup_Formats: last-added entry for `name` fills BOTH
        empty slots (even with None) and the search stops there."""
        for n, f, d in reversed(self.mappings):
            if n == name:
                if fmt is None:
                    fmt = f
                if dfmt is None:
                    dfmt = d
                break
        return fmt, dfmt

    # -- property data -> thunks -------------------------------------------

    def xlib_shape(self, size: int, wire: bytes):
        """Model XGetWindowProperty + Xlib's long-array buffer: fetch capped at (max_len+3)/4 32-bit words;
        32-bit items expand to 8 bytes each; byte budget = min(nitems*nbytes, max_len)."""
        cap_words = _c_div(self.max_len + 3, 4)
        if cap_words >= 0:
            wire = wire[:cap_words * 4]
        if size == 32:
            n = len(wire) // 4
            vals = struct.unpack("<%dI" % n, wire[:n * 4])
            buffer = struct.pack("<%dQ" % n, *vals)
            nbytes = 8
        elif size == 16:
            n = len(wire) // 2
            buffer = wire[:n * 2]
            nbytes = 2
        else:
            n = len(wire)
            buffer = wire
            nbytes = 1
        # C compares the byte count against max_len as an unsigned long,
        # so a negative max_len is a budget of ~2**64 bytes: unbounded.
        if self.max_len < 0:
            return buffer, n * nbytes
        return buffer, min(n * nbytes, self.max_len)

    def break_down(self, buffer: bytes, length: int, type_name, fmt: bytes, size: int) -> list:
        thunks = []
        pos = 0
        i = 0
        unit = size // 8
        while length >= unit and unit:
            c = get_format_char(fmt, i)
            if c in (0x73, 0x75, 0x74):  # s u t
                if size != 8:
                    raise FatalError("can't use format character 's' with "
                                     "any size except 8.")
                end = pos + length
                nul = buffer.find(0, pos, end)
                if nul < 0:
                    item = buffer[pos:end]
                    consumed = end - pos
                else:
                    item = buffer[pos:nul + 1]  # value counts the NUL
                    consumed = nul + 1 - pos
                thunks.append(Thunk(consumed, bytes(item), type_name if c == 0x74 else None))
                pos += consumed
                length -= consumed
            elif c == 0x6F:  # o
                if size != 32:
                    raise FatalError("can't use format character 'o' with "
                                     "any size except 32.")
                thunks.append(Thunk(length, bytes(buffer[pos:pos + length])))
                pos += length
                length = 0
            else:
                signed = (c == 0x69)  # 'i'
                if size == 8:
                    v = buffer[pos]
                    if signed and v >= 0x80:
                        v -= 0x100
                    step = 1
                elif size == 16:
                    v = int.from_bytes(buffer[pos:pos + 2], "little", signed=signed)
                    step = 2
                else:  # 32: Xlib's _XRead32 SIGN-extends the wire CARD32
                    # into a long (it reads through an `int *`), and Extract_Value returns *(long*) for 'i' but
                    # masks `& 0xffffffff` otherwise — i.e. the low 32 bits read with the format char's
                    # signedness. So 32i of 0xffffffff is -1 and of 0xfffffffb is -5 (a real INTEGER dumps its
                    # negatives), while 32c/32x/32a stay unsigned. All oracle-verified; the buffer's high 4
                    # bytes are the zero padding xlib_shape added, so slice the low 4.
                    v = int.from_bytes(buffer[pos:pos + 4], "little", signed=signed)
                    step = 8
                thunks.append(Thunk(v))
                pos += step
                length -= step
            i += 1
        return thunks

    # -- thunk -> string ------------------------------------------------------

    def format_thunk(self, t: Thunk, c: int) -> bytes:
        if c == 0x73:  # s
            return format_string(t.extra_value, False)
        if c == 0x75:  # u
            return self.format_len_unicode(t.extra_value)
        if c == 0x74:  # t
            return self.format_len_text(t.extra_value, t.extra_encoding)
        if c == 0x78:  # x
            return b"0x%x" % (t.value & 0xFFFFFFFFFFFFFFFF)
        if c == 0x63:  # c
            return b"%d" % (t.value & 0xFFFFFFFFFFFFFFFF)
        if c == 0x69:  # i
            return b"%d" % t.value
        if c == 0x62:  # b
            return b"True" if t.value else b"False"
        if c == 0x6D:  # m
            bits = [b"%d" % bit for bit in range(64) if (t.value >> bit) & 1]
            return b"{MASK: " + b", ".join(bits) + b"}"
        if c == 0x61:  # a
            name = self.atom_name(t.value & 0xFFFFFFFFFFFFFFFF)
            if name is None:
                return b"undefined atom # 0x%x" % (t.value & 0xFFFFFFFFFFFFFFFF)
            return name.encode("latin-1")
        if c == 0x6F:  # o
            return self.format_icons(t.extra_value)
        raise FatalError("bad format character: %s" % bytes([c]).decode("latin-1"))

    def format_thunk_i(self, thunks, fmt: bytes, i: int) -> bytes:
        if i >= len(thunks):
            return b"<field not available>"
        return self.format_thunk(thunks[i], get_format_char(fmt, i))

    def format_len_unicode(self, data: bytes) -> bytes:
        validity = is_valid_utf8(data)
        if validity != UTF8_VALID:
            err = _UTF8_ERRORS.get(validity, b"<Invalid UTF-8 string: Unknown error>")
            return err + format_string(data, False)
        return format_string(data, self.utf8_locale)

    # -- the 't' converter (XmbTextPropertyToTextList stand-in) --------------

    def format_len_text(self, data: bytes, encoding) -> bytes:
        items = self._text_to_locale(data, encoding)
        if items is None:  # XConverterNotFound path
            return format_string(data, False)
        out = bytearray(b'"')
        for idx, item in enumerate(items):
            out += self._escape_locale(item)
            if idx < len(items) - 1:
                out += b"\\000"
        out += b'"'
        return bytes(out)

    def _text_to_locale(self, data: bytes, encoding):
        parts = data.split(b"\0")
        if encoding == "STRING":
            if not self.utf8_locale:
                return parts  # Xlib's C locale is latin-1: identity
            return [p.decode("latin-1").encode("utf-8") for p in parts]
        if encoding == "UTF8_STRING":
            if not self.utf8_locale:
                return None
            try:
                for p in parts:
                    p.decode("utf-8")
            except UnicodeDecodeError:
                return None
            return parts
        if encoding == "COMPOUND_TEXT":
            try:
                decoded = [self._decode_compound_text(p) for p in parts]
            except ValueError:
                return None
            if not self.utf8_locale:
                return decoded
            return [p.decode("latin-1").encode("utf-8") for p in decoded]
        return None

    @staticmethod
    def _decode_compound_text(data: bytes) -> bytes:
        """Latin-1 subset of ISO 2022: GL=ASCII, GR=Latin-1, the two designation escapes that keep it that way.
        Anything else raises ValueError -> conversion failure -> quoted-string fallback."""
        out = bytearray()
        i = 0
        while i < len(data):
            c = data[i]
            if c == 0x1B:
                seq = data[i:i + 3]
                if seq in (b"\x1b(B", b"\x1b-A", b"\x1b(J"):
                    i += 3
                    continue
                raise ValueError("unsupported COMPOUND_TEXT escape")
            out.append(c)
            i += 1
        return bytes(out)

    def _escape_locale(self, item: bytes) -> bytes:
        """The mb loop of Format_Len_Text: printable locale chars pass raw,
        everything else is escaped one BYTE at a time."""
        if not self.utf8_locale:
            return b"".join(bytes([c]) if _c_isprint(c) else b"\\%03o" % c for c in item)
        out = bytearray()
        i = 0
        while i < len(item):
            ch, n = _decode_one_utf8(item, i)
            if ch is not None and (ch.isprintable() or ch == " "):
                out += item[i:i + n]
                i += n
            else:
                out += b"\\%03o" % item[i]
                i += 1
        return bytes(out)

    # -- _NET_WM_ICON ascii art ----------------------------------------------

    def format_icons(self, data: bytes) -> bytes:
        """xprop's Format_Icons. `data` is the byte budget's worth of the long array, so under `-len` it can be
        too short to hold even one (width, height) pair: C's loop then never runs, the function returns NULL,
        and glibc's printf renders that as "(null)". Past that point parity is unattainable in principle -- C
        keeps reading the Xlib buffer beyond the budget (an unsigned comparison turns its own truncation check
        into a no-op) and prints uninitialised heap; we stop at the budget instead."""
        n = len(data) // 8
        if n == 0:
            return b"(null)"
        vals = struct.unpack("<%dQ" % n, data[:n * 8])
        out = bytearray()
        i = 0
        while i < n:
            if n - i < 2:
                break  # C reads past the buffer here; treat as truncated
            width = vals[i]
            height = vals[i + 1]
            i += 2
            display_width, display_height = width, height
            if self.truecolor:
                display_height //= 2
            else:
                display_width *= 2
            if n - i < width * height:
                break
            out += b"\tIcon (%d x %d):\n" % (width, height)
            if display_width + 8 > self.term_width or height > 144:
                out += b"\t(not shown)\n"
                i += width * height
                continue
            for _h in range(display_height):
                out += b"\t"
                for _w in range(width):
                    pixel = vals[i]
                    i += 1
                    a = (pixel >> 24) & 0xFF
                    r = (pixel >> 16) & 0xFF
                    g = (pixel >> 8) & 0xFF
                    b = pixel & 0xFF
                    brightness = int((a / 255.0) *
                                     (1000 - ((299 * (r / 255.0)) +
                                              (587 * (g / 255.0)) +
                                              (114 * (b / 255.0)))))
                    if self.truecolor:
                        opacity = a / 255.0
                        j = i + width - 1
                        pixel2 = vals[j] if j < n else 0
                        out += b"\033[48;2;%d;%d;%d;" % (int(r * opacity), int(g * opacity), int(b * opacity))
                        op2 = ((pixel2 >> 24) & 0xFF) / 255.0
                        out += b"38;2;%d;%d;%dm\342\226\204" % (
                            int(((pixel2 >> 16) & 0xFF) * op2),
                            int(((pixel2 >> 8) & 0xFF) * op2),
                            int((pixel2 & 0xFF) * op2))
                    elif self.utf8_locale:
                        idx = (brightness * (len(_UTF8_PALETTE) - 1)) // 1000
                        out += _UTF8_PALETTE[idx] * 2
                    else:
                        idx = (brightness * (len(_C_PALETTE) - 1)) // 1000
                        out += _C_PALETTE[idx:idx + 1] * 2
                if self.truecolor:
                    out += b"\033[0m"
                    i += width
                out += b"\n"
            out += b"\n"
        return bytes(out)

    # -- dformat interpreter (Display_Property) ------------------------------

    def display(self, out: bytearray, thunks, dfmt: bytes, fmt: bytes):
        pos = 0
        while pos < len(dfmt):
            c = dfmt[pos]
            pos += 1
            if c == 0x29:  # ')'
                continue
            if c == 0x5C:  # backslash
                pos = self._backslash(out, dfmt, pos)
            elif c == 0x24:  # '$'
                pos = self._dollar(out, dfmt, pos, thunks, fmt)
            elif c == 0x3F:  # '?'
                pos = self._question(dfmt, pos, thunks, fmt)
            else:
                out.append(c)

    def _backslash(self, out: bytearray, dfmt: bytes, pos: int) -> int:
        if pos >= len(dfmt):
            return pos  # C walks past the NUL; we stop cleanly
        c = dfmt[pos]
        pos += 1
        if c == 0x6E:  # n
            out.append(0x0A)
        elif c == 0x74:  # t
            out.append(0x09)
        elif 0x30 <= c <= 0x37:  # octal: value parsed from the NEXT digit on
            m = re.match(rb"[0-7]+", dfmt[pos:])
            if not m:
                raise FatalError("Bad octal number: %s." % _msg(dfmt[pos:]))
            out.append(int(m.group(0), 8) & 0xFF)
            while pos < len(dfmt) and 0x30 <= dfmt[pos] <= 0x39:
                pos += 1  # Skip_Digits skips 8 and 9 too
        else:
            out.append(c)
        return pos

    def _dollar(self, out: bytearray, dfmt: bytes, pos: int, thunks, fmt: bytes) -> int:
        i, pos = _scan_long(dfmt, pos)
        if pos < len(dfmt) and dfmt[pos] == 0x2B:  # '+'
            pos += 1
            seen = False
            while i < len(thunks):
                if seen:
                    out += b", "
                seen = True
                out += self.format_thunk_i(thunks, fmt, i)
                i += 1
        else:
            out += self.format_thunk_i(thunks, fmt, i)
        return pos

    @staticmethod
    def _oob_thunk(thunks, i: int):
        """xprop reads thunks[i] out of range: index 0 of an *empty* list lands in Create_Thunk_List's single
        fresh (zeroed) malloc slot, so C reliably sees 0 there; any index past a *non-empty* list is real heap
        garbage. _GARBAGE stands in for the latter (nonzero, equal to no constant a dformat tests) so the
        observable output matches."""
        if not thunks:
            return 0
        return _GARBAGE

    def _mask_word(self, thunks, fmt: bytes):
        for j in range(len(fmt)):  # strlen of the FULL format, like C
            if get_format_char(fmt, j) == 0x6D:  # 'm'
                if j < len(thunks):
                    return thunks[j].value
                return self._oob_thunk(thunks, j)
        return 0

    def _scan_term(self, dfmt: bytes, pos: int, thunks, fmt: bytes):
        if pos < len(dfmt) and 0x30 <= dfmt[pos] <= 0x39:
            return _scan_long(dfmt, pos)
        if pos < len(dfmt) and dfmt[pos] == 0x24:  # '$'
            i, pos = _scan_long(dfmt, pos + 1)
            if i >= len(thunks):
                # C clamps i to thunk_count and reads thunks[thunk_count]
                return self._oob_thunk(thunks, i), pos
            return thunks[i].value, pos
        if pos < len(dfmt) and dfmt[pos] == 0x6D:  # 'm'
            i, pos = _scan_long(dfmt, pos + 1)
            word = self._mask_word(thunks, fmt)
            # C's Mask_Bit_I: `value & (1L << (int)i)`. On x86-64 the shift count is masked to 6 bits, so ?m64
            # tests bit 0 and ?m66 bit 2 (oracle-verified). A raw `1 << i` would build a multi-GB int for a
            # hostile i (?m34359738368 -> MemoryError); bounding the count keeps it cheap AND byte-identical to
            # the C UB.
            return (1 if word & (1 << (_to_int32(i) & 63)) else 0), pos
        raise FatalError("Bad term: %s." % _msg(dfmt[pos:]))

    def _scan_exp(self, dfmt: bytes, pos: int, thunks, fmt: bytes):
        if pos < len(dfmt) and dfmt[pos] == 0x28:  # '('
            v, pos = self._scan_exp(dfmt, pos + 1, thunks, fmt)
            if pos >= len(dfmt) or dfmt[pos] != 0x29:
                raise FatalError("Missing ')'")
            return v, pos + 1
        if pos < len(dfmt) and dfmt[pos] == 0x21:  # '!'
            v, pos = self._scan_exp(dfmt, pos + 1, thunks, fmt)
            return int(not v), pos
        v, pos = self._scan_term(dfmt, pos, thunks, fmt)
        if pos < len(dfmt) and dfmt[pos] == 0x3D:  # '='
            t, pos = self._scan_exp(dfmt, pos + 1, thunks, fmt)
            return int(v == t), pos
        return v, pos

    def _question(self, dfmt: bytes, pos: int, thunks, fmt: bytes) -> int:
        is_true, pos = self._scan_exp(dfmt, pos, thunks, fmt)
        if pos >= len(dfmt) or dfmt[pos] != 0x28:
            raise FatalError("Bad conditional: '(' expected: %s." % _msg(dfmt[pos:]))
        pos += 1
        if not is_true:
            pos = self._skip_past_right_paren(dfmt, pos)
        return pos

    @staticmethod
    def _skip_past_right_paren(dfmt: bytes, pos: int) -> int:
        nesting = 0
        while True:
            if pos >= len(dfmt):
                raise FatalError("Missing ')'.")
            c = dfmt[pos]
            pos += 1
            if c == 0x29 and nesting == 0:
                return pos
            if c == 0x28:
                nesting += 1
            elif c == 0x29:
                nesting -= 1
            elif c == 0x5C:
                pos += 1
        # not reached

    # -- Show_Prop from the type display on ----------------------------------

    def render_property(self, out: bytearray, propname: bytes, type_name, size: int, wire: bytes, fmt, dfmt):
        """Everything Show_Prop does after the existence checks: `(TYPE)`,
        format resolution, the type-mismatch line, thunks, dformat."""
        if not self.notype and type_name is not None:
            out += b"(" + type_name.encode("latin-1") + b")"
        fmt, dfmt = self.lookup_formats(propname, fmt, dfmt)
        if type_name is not None:
            fmt, dfmt = self.lookup_formats(type_name.encode("latin-1"), fmt, dfmt)
        if fmt is None:
            fmt = DEFAULT_FORMAT
        if dfmt is None:
            dfmt = DEFAULT_DFORMAT
        fsize = get_format_size(fmt)
        if fsize != size and fsize != 0:
            out += (b": Type mismatch: assumed size %d bits, "
                    b"actual size %d bits.\n" % (fsize, size))
            return
        buffer, length = self.xlib_shape(size, wire)
        thunks = self.break_down(buffer, length, type_name, fmt, size)
        self.display(out, thunks, dfmt, fmt)
