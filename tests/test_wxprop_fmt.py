#!/usr/bin/env python3
"""unit tests for wxprop.fmt — xprop's formatting machinery.

Every expected byte string below was captured from real xprop 1.2.8 in the
devshell, or derived
from xprop.c with the live oracle confirming the whole pipeline. These run
offline: property data is reconstructed to match what the X server handed
the oracle.
"""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hacks.property import fmt as fmtmod
from hacks.property.fmt import FatalError, Formatter

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


def _icon_wire():
    """The prep icon (set_icon.py): 8x8 ARGB exercising the alpha/color
    buckets — the same icon xprop-icon.out was captured from."""
    vals = [8, 8]
    for _y in range(8):
        for x in range(8):
            if x == 0:
                px = 0x00000000
            elif x == 1:
                px = 0x40FF0000
            elif x == 2:
                px = 0x80808080
            else:
                level = (x * 255) // 7
                px = 0xFF000000 | (level << 16) | (level << 8) | level
            vals.append(px)
    return struct.pack("<%dI" % len(vals), *vals)


# xprop-icon.out, byte for byte (LC_ALL=C, stdout not a tty)
ICON_ORACLE = (
    b"_NET_WM_ICON(CARDINAL) = \tIcon (8 x 8):\n"
    + b"\t  ++lltt]]??--  \n" * 8
    + b"\n\n")


class FmtTest(unittest.TestCase):
    maxDiff = None

    def render(self, propname, tname, size, wire, fmt=None, dfmt=None,
               atoms=None, **kw):
        atoms = atoms or {}
        f = Formatter(atom_name=lambda a: atoms.get(a), **kw)
        f.setup_window_table()
        out = bytearray(propname)
        f.render_property(out, propname, tname, size, wire, fmt, dfmt)
        return bytes(out)

    # -- strings (8s) --------------------------------------------------------

    def test_wm_class(self):
        self.assertEqual(
            self.render(b"WM_CLASS", "STRING", 8, b"xterm\0XTerm\0"),
            b'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_string_escapes(self):
        got = self.render(b"S", "STRING", 8, b'a"b\\c\nd\te\x01\xff\0')
        self.assertEqual(got, b'S(STRING) = "a\\"b\\\\c\\nd\\te\\001\\377"\n')

    def test_string_list_with_trailing_empties(self):
        # _XKB_RULES_NAMES on the sway root, from xprop-root.out
        got = self.render(b"_XKB_RULES_NAMES", "STRING", 8,
                          b"evdev\0pc105\0us\0\0\0")
        self.assertEqual(
            got,
            b'_XKB_RULES_NAMES(STRING) = "evdev", "pc105", "us", "", ""\n')

    def test_empty_but_present_property(self):
        self.assertEqual(self.render(b"MT", "STRING", 8, b""),
                         b"MT(STRING) = \n")

    def test_wm_command_braces(self):
        self.assertEqual(self.render(b"WM_COMMAND", "STRING", 8, b"xeyes\0"),
                         b'WM_COMMAND(STRING) = { "xeyes" }\n')

    # -- -len jank (captured: xprop-len8, xprop2-len3, xprop2-len12) ---------

    def test_len_truncates_strings_mid_character(self):
        for n, want in ((3, b"xte"), (4, b"xter"), (8, b"xterm\", \"XT")):
            got = self.render(b"WM_CLASS", "STRING", 8, b"xterm\0XTerm\0",
                              notype=True, max_len=n)
            self.assertEqual(got, b'WM_CLASS = "%s"\n' % want, n)

    def test_len_counts_32bit_items_as_8_bytes(self):
        atoms = {201: "_NET_WM_STATE_MAXIMIZED_VERT",
                 202: "_NET_WM_STATE_MAXIMIZED_HORZ",
                 203: "_NET_WM_STATE_FOCUSED"}
        wire = struct.pack("<3I", 201, 202, 203)
        self.assertEqual(
            self.render(b"_NET_WM_STATE", "ATOM", 32, wire, atoms=atoms),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_VERT, "
            b"_NET_WM_STATE_MAXIMIZED_HORZ, _NET_WM_STATE_FOCUSED\n")
        self.assertEqual(  # -len 8 -> exactly one atom
            self.render(b"_NET_WM_STATE", "ATOM", 32, wire, atoms=atoms,
                        max_len=8),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_VERT\n")
        self.assertEqual(  # -len 12 -> two (the loop over-reads)
            self.render(b"_NET_WM_STATE", "ATOM", 32, wire, atoms=atoms,
                        max_len=12),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_MAXIMIZED_VERT, "
            b"_NET_WM_STATE_MAXIMIZED_HORZ\n")

    def test_len_zero_and_negative(self):
        wire = b"xterm\0XTerm\0"
        self.assertEqual(self.render(b"WM_CLASS", "STRING", 8, wire,
                                     max_len=0),
                         b"WM_CLASS(STRING) = \n")
        self.assertEqual(self.render(b"WM_CLASS", "STRING", 8, wire,
                                     max_len=-5),
                         b"WM_CLASS(STRING) = \n")

    # -- structured dumps (WM_HINTS / WM_SIZE_HINTS / WM_STATE) --------------

    def test_wm_hints_xterm(self):
        hints = struct.pack("<9I", 3, 1, 1, 0, 0, 0, 0, 0, 0)
        self.assertEqual(
            self.render(b"WM_HINTS", "WM_HINTS", 32, hints),
            b"WM_HINTS(WM_HINTS):\n"
            b"\t\tClient accepts input or input focus: True\n"
            b"\t\tInitial state is Normal State.\n")

    def test_wm_hints_xeyes_icons(self):
        # flags Input|State|IconPixmap|IconMask, input False
        hints = struct.pack("<9I", 39, 0, 1, 0x400001, 0, 0, 0, 0x400003, 0)
        self.assertEqual(
            self.render(b"WM_HINTS", "WM_HINTS", 32, hints),
            b"WM_HINTS(WM_HINTS):\n"
            b"\t\tClient accepts input or input focus: False\n"
            b"\t\tInitial state is Normal State.\n"
            b"\t\tbitmap id # to use for icon: 0x400001\n"
            b"\t\tbitmap id # of mask for icon: 0x400003\n")

    def test_wm_hints_len8_garbage_conditionals(self):
        # only the mask survives -len 8; xprop's out-of-range conditional
        # reads print nothing ("Initial state is .") — captured behavior
        hints = struct.pack("<9I", 3, 1, 1, 0, 0, 0, 0, 0, 0)
        self.assertEqual(
            self.render(b"WM_HINTS", "WM_HINTS", 32, hints, max_len=8),
            b"WM_HINTS(WM_HINTS):\n"
            b"\t\tClient accepts input or input focus: "
            b"<field not available>\n"
            b"\t\tInitial state is .\n")

    def test_wm_size_hints_xterm(self):
        vals = [856, 0, 0, 640, 720, 10, 17, 0, 0, 6, 13, 0, 0, 0, 0,
                4, 4, 1]
        wire = struct.pack("<18i", *vals)
        self.assertEqual(
            self.render(b"WM_NORMAL_HINTS", "WM_SIZE_HINTS", 32, wire),
            b"WM_NORMAL_HINTS(WM_SIZE_HINTS):\n"
            b"\t\tprogram specified size: 640 by 720\n"
            b"\t\tprogram specified minimum size: 10 by 17\n"
            b"\t\tprogram specified resize increment: 6 by 13\n"
            b"\t\tprogram specified base size: 4 by 4\n"
            b"\t\twindow gravity: NorthWest\n")

    def test_wm_size_hints_len8(self):
        vals = [856, 0, 0, 640, 720, 10, 17, 0, 0, 6, 13, 0, 0, 0, 0,
                4, 4, 1]
        wire = struct.pack("<18i", *vals)
        na = b"<field not available>"
        self.assertEqual(
            self.render(b"WM_NORMAL_HINTS", "WM_SIZE_HINTS", 32, wire,
                        max_len=8),
            b"WM_NORMAL_HINTS(WM_SIZE_HINTS):\n"
            b"\t\tprogram specified size: %s by %s\n"
            b"\t\tprogram specified minimum size: %s by %s\n"
            b"\t\tprogram specified resize increment: %s by %s\n"
            b"\t\tprogram specified base size: %s by %s\n"
            b"\t\twindow gravity: \n" % ((na,) * 8))

    def test_wm_state(self):
        wire = struct.pack("<II", 1, 0)
        self.assertEqual(
            self.render(b"WM_STATE", "WM_STATE", 32, wire),
            b"WM_STATE(WM_STATE):\n"
            b"\t\twindow state: Normal\n"
            b"\t\ticon window: 0x0\n")
        self.assertEqual(
            self.render(b"WM_STATE", "WM_STATE", 32, wire, max_len=8),
            b"WM_STATE(WM_STATE):\n"
            b"\t\twindow state: Normal\n"
            b"\t\ticon window: <field not available>\n")

    # -- atoms / windows / numbers -------------------------------------------

    def test_wm_protocols_two_spaces(self):
        self.assertEqual(
            self.render(b"WM_PROTOCOLS", "ATOM", 32, struct.pack("<I", 9),
                        atoms={9: "WM_DELETE_WINDOW"}),
            b"WM_PROTOCOLS(ATOM): protocols  WM_DELETE_WINDOW\n")

    def test_window_id_join(self):
        self.assertEqual(
            self.render(b"_NET_CLIENT_LIST", "WINDOW", 32,
                        struct.pack("<2I", 0x60000C, 0x40000A)),
            b"_NET_CLIENT_LIST(WINDOW): window id # 0x60000c, 0x40000a\n")

    def test_undefined_atom(self):
        self.assertEqual(
            self.render(b"A", "ATOM", 32, struct.pack("<I", 999)),
            b"A(ATOM) = undefined atom # 0x3e7\n")

    def test_cardinal_and_unsigned_32(self):
        self.assertEqual(
            self.render(b"_NET_WM_PID", "CARDINAL", 32,
                        struct.pack("<I", 49189)),
            b"_NET_WM_PID(CARDINAL) = 49189\n")
        # REGRESSION GUARD (task's concern): a CARDINAL 0xffffffff shown with
        # its DEFAULT format (0c -> unsigned 'c') prints 4294967295, NOT -1.
        # This is what `xprop _NET_WM_DESKTOP` and friends rely on.
        self.assertEqual(
            self.render(b"D", "CARDINAL", 32, struct.pack("<I", 0xFFFFFFFF)),
            b"D(CARDINAL) = 4294967295\n")
        # 'c' and 'x' stay unsigned regardless
        self.assertEqual(
            self.render(b"D", "CARDINAL", 32,
                        struct.pack("<I", 0xFFFFFFFF), fmt=b"32c"),
            b"D(CARDINAL) = 4294967295\n")
        # ...but an EXPLICIT 32i reads the low word SIGNED (oracle: -1), the
        # same rule that makes a negative INTEGER dump correctly (see below)
        self.assertEqual(
            self.render(b"D", "CARDINAL", 32,
                        struct.pack("<I", 0xFFFFFFFF), fmt=b"32i"),
            b"D(CARDINAL) = -1\n")

    def test_integer_negative_32bit(self):
        # a real INTEGER property with the high bit set dumps its NEGATIVE
        # value (oracle: _I(INTEGER) = -5 for wire 0xfffffffb) — the baseline
        # zero-extended it to 4294967291, a bug this pass fixes. Both the
        # default (0i -> 'i') and explicit 32i sign-extend; 32c does not.
        self.assertEqual(
            self.render(b"_I", "INTEGER", 32, struct.pack("<i", -5)),
            b"_I(INTEGER) = -5\n")
        self.assertEqual(
            self.render(b"_I", "INTEGER", 32, struct.pack("<i", -5),
                        fmt=b"32i"),
            b"_I(INTEGER) = -5\n")
        self.assertEqual(
            self.render(b"_I", "INTEGER", 32, struct.pack("<i", -5),
                        fmt=b"32c"),
            b"_I(INTEGER) = 4294967291\n")

    def test_signed_16(self):
        self.assertEqual(
            self.render(b"P", "POINT", 16, struct.pack("<hh", -3, 7)),
            b"P(POINT) = -3, 7\n")

    def test_mask_word(self):
        self.assertEqual(
            self.render(b"M", "CARDINAL", 32, struct.pack("<I", 0b1011),
                        fmt=b"32m"),
            b"M(CARDINAL) = {MASK: 0, 1, 3}\n")

    def test_conditional_mask_bit_shift_wraps_like_c(self):
        # C's Mask_Bit_I is `value & (1L << (int)i)`; on x86-64 the shift
        # count is masked to 6 bits. Oracle-verified: bit0-set value ->
        # ?m0 AND ?m64 both fire; bit2-set -> ?m66 fires (66 & 63 == 2).
        wire = struct.pack("<I", 1)          # bit 0 set
        self.assertEqual(
            self.render(b"M", "CARDINAL", 32, wire, fmt=b"32m",
                        dfmt=b"?m0(B0)?m64(B64)?m1(B1)?m65(B65)\n"),
            b"M(CARDINAL)B0B64\n")
        wire = struct.pack("<I", 4)          # bit 2 set
        self.assertEqual(
            self.render(b"M", "CARDINAL", 32, wire, fmt=b"32m",
                        dfmt=b"?m2(B2)?m66(B66)?m64(B64)\n"),
            b"M(CARDINAL)B2B66\n")

    def test_conditional_mask_huge_count_no_oom(self):
        # a hostile shift count must NOT build a multi-GB integer. ?m2^35:
        # (int)2^35 == 0 -> bit 0 (set) -> fires; regression for the
        # MemoryError the raw `1 << i` caused.
        wire = struct.pack("<I", 1)          # bit 0 set
        self.assertEqual(
            self.render(b"M", "CARDINAL", 32, wire, fmt=b"32m",
                        dfmt=b"?m34359738368(HIT)\n"),
            b"M(CARDINAL)HIT\n")
        # an even wilder count (more digits than fit an int) still returns
        # cleanly rather than raising OverflowError
        self.assertEqual(
            self.render(b"M", "CARDINAL", 32, struct.pack("<I", 0), fmt=b"32m",
                        dfmt=b"?m99999999999999999999(X)done\n"),
            b"M(CARDINAL)done\n")

    def test_bool(self):
        self.assertEqual(
            self.render(b"B", "CARDINAL", 32, struct.pack("<2I", 0, 2),
                        fmt=b"32b"),
            b"B(CARDINAL) = False, True\n")

    # -- format/dformat resolution -------------------------------------------

    def test_type_mismatch(self):
        self.assertEqual(
            self.render(b"WM_CLASS", "STRING", 8, b"xterm\0XTerm\0",
                        fmt=b"32x"),
            b"WM_CLASS(STRING): Type mismatch: assumed size 32 bits, "
            b"actual size 8 bits.\n")

    def test_inline_dformat_keeps_name_and_type(self):
        self.assertEqual(
            self.render(b"WM_CLASS", "STRING", 8, b"xterm\0XTerm\0",
                        fmt=b"8s", dfmt=b" instance=$0 class=$1\\n"),
            b'WM_CLASS(STRING) instance="xterm" class="XTerm"\n')

    def test_field_not_available(self):
        self.assertEqual(
            self.render(b"WM_NAME", "STRING", 8, b"yans", fmt=b"0s",
                        dfmt=b"$1\\n"),
            b"WM_NAME(STRING)<field not available>\n")

    def test_notype(self):
        self.assertEqual(
            self.render(b"WM_CLASS", "STRING", 8, b"xterm\0XTerm\0",
                        notype=True),
            b'WM_CLASS = "xterm", "XTerm"\n')

    def test_property_lookup_beats_type_lookup(self):
        # WM_NAME(STRING) resolves 8t via the property atom, not STRING's 8s
        # -> under a UTF-8 locale a latin-1 byte converts instead of escaping
        got = self.render(b"WM_NAME", "STRING", 8, b"caf\xe9",
                          utf8_locale=True)
        self.assertEqual(got, 'WM_NAME(STRING) = "café"\n'.encode("utf-8"))

    def test_last_mapping_wins(self):
        f = Formatter(atom_name=lambda a: None)
        f.setup_window_table()
        f.add_mapping(b"WM_CLASS", b"32x", None)
        out = bytearray(b"WM_CLASS")
        f.render_property(out, b"WM_CLASS", "STRING", 8, b"ab\0", None, None)
        self.assertEqual(
            bytes(out),
            b"WM_CLASS(STRING): Type mismatch: assumed size 32 bits, "
            b"actual size 8 bits.\n")

    # -- UTF-8 handling (8u) --------------------------------------------------

    def test_utf8_escaped_in_c_locale(self):
        got = self.render(b"MY_UTF8", "UTF8_STRING", 8,
                          "héllo wörld".encode("utf-8"))
        self.assertEqual(
            got, b'MY_UTF8(UTF8_STRING) = "h\\303\\251llo w\\303\\266rld"\n')

    def test_utf8_raw_in_utf8_locale(self):
        got = self.render(b"MY_UTF8", "UTF8_STRING", 8,
                          "héllo".encode("utf-8"), utf8_locale=True)
        self.assertEqual(got, 'MY_UTF8(UTF8_STRING) = "héllo"\n'
                         .encode("utf-8"))

    def test_invalid_utf8_diagnoses(self):
        cases = (
            (b"a\x80b", b"Tail too long"),
            (b"\xC3\x41", b"Tail too short"),   # ASCII where tail expected
            (b"\xC0\x80x", b"Overlong encoding"),
            (b"\xFF x", b"Forbidden value"),
        )
        for data, diag in cases:
            got = self.render(b"B", "UTF8_STRING", 8, data)
            self.assertIn(b"<Invalid UTF-8 string: " + diag + b"> ", got,
                          data)
        # a TRAILING incomplete sequence passes xprop's checker (the final
        # rem>0 state is never inspected) — oracle-verified
        got = self.render(b"B", "UTF8_STRING", 8, b"a\xC3")
        self.assertEqual(got, b'B(UTF8_STRING) = "a\\303"\n')

    # -- the 't' converter ----------------------------------------------------

    def test_t_c_locale_escapes_high_bytes(self):
        got = self.render(b"WM_NAME", "STRING", 8, b"caf\xe9")
        self.assertEqual(got, b'WM_NAME(STRING) = "caf\\351"\n')

    def test_t_does_not_use_s_escapes(self):
        # 't' escapes byte-wise octal — no \n or \" translation like 's'
        got = self.render(b"WM_NAME", "STRING", 8, b'a"b\nc')
        self.assertEqual(got, b'WM_NAME(STRING) = "a"b\\012c"\n')

    def test_t_embedded_nul_splits_thunks(self):
        # Extract_Len_String stops at the NUL (counting it), so the value
        # splits into two thunks; the first renders its NUL as a trailing
        # \000 item. Oracle-verified: `foo\0bar` -> "foo\000", "bar"
        got = self.render(b"WM_NAME", "STRING", 8, b"foo\0bar")
        self.assertEqual(got, b'WM_NAME(STRING) = "foo\\000", "bar"\n')

    def test_t_utf8_string_type_falls_back_to_quoted(self):
        # C locale has no UTF8_STRING converter: XConverterNotFound path
        got = self.render(b"WM_NAME", "UTF8_STRING", 8, b"caf\xc3\xa9",
                          fmt=b"8t")
        self.assertEqual(got, b'WM_NAME(UTF8_STRING) = "caf\\303\\251"\n')

    def test_t_compound_text_latin1(self):
        got = self.render(b"WM_NAME", "COMPOUND_TEXT", 8,
                          b"\x1b(Bcaf\x1b-A\xe9", fmt=b"8t")
        self.assertEqual(got, b'WM_NAME(COMPOUND_TEXT) = "caf\\351"\n')

    # -- _NET_WM_ICON ascii art ----------------------------------------------

    def test_icon_matches_oracle_bytes(self):
        got = self.render(b"_NET_WM_ICON", "CARDINAL", 32, _icon_wire())
        self.assertEqual(got, ICON_ORACLE)

    def test_icon_not_shown_when_too_wide(self):
        big = [80, 2] + [0xFF808080] * 160
        wire = struct.pack("<%dI" % len(big), *big)
        self.assertEqual(
            self.render(b"I", "CARDINAL", 32, wire, fmt=b"32o"),
            b"I(CARDINAL) = \tIcon (80 x 2):\n\t(not shown)\n\n")

    def test_icon_not_shown_when_too_tall(self):
        big = [2, 145] + [0] * 290
        wire = struct.pack("<%dI" % len(big), *big)
        self.assertEqual(
            self.render(b"I", "CARDINAL", 32, wire, fmt=b"32o"),
            b"I(CARDINAL) = \tIcon (2 x 145):\n\t(not shown)\n\n")

    def test_icon_truncated_data_stops_before_header(self):
        wire = struct.pack("<4I", 8, 8, 0, 0)  # promises 64 px, has 2
        self.assertEqual(self.render(b"I", "CARDINAL", 32, wire, fmt=b"32o"),
                         b"I(CARDINAL) = \n")

    def test_icon_utf8_palette(self):
        wire = struct.pack("<3I", 1, 1, 0xFF000000)  # opaque black
        got = self.render(b"I", "CARDINAL", 32, wire, fmt=b"32o",
                          utf8_locale=True)
        self.assertEqual(
            got, b"I(CARDINAL) = \tIcon (1 x 1):\n"
                 b"\t\342\226\210\342\226\210\n\n\n")

    # -- dformat interpreter edges -------------------------------------------

    def test_octal_escape_drops_first_digit(self):
        # \0101 is 'A' (octal read from the SECOND digit on); \101 is \001
        self.assertEqual(
            self.render(b"O", "STRING", 8, b"x", fmt=b"8s",
                        dfmt=b"\\0101\\n"),
            b"O(STRING)A\n")
        self.assertEqual(
            self.render(b"O", "STRING", 8, b"x", fmt=b"8s",
                        dfmt=b"\\101\\n"),
            b"O(STRING)\x01\n")

    def test_conditionals_and_negation(self):
        wire = struct.pack("<I", 5)
        self.assertEqual(
            self.render(b"C", "CARDINAL", 32, wire, fmt=b"32c",
                        dfmt=b"?$0=5(five)?!$0=5(notfive)\\n"),
            b"C(CARDINAL)five\n")

    def test_top_level_close_paren_skipped(self):
        self.assertEqual(
            self.render(b"C", "CARDINAL", 32, struct.pack("<I", 1),
                        fmt=b"32c", dfmt=b"a)b\\n"),
            b"C(CARDINAL)ab\n")

    def test_dollar_plus_empty_prints_nothing(self):
        self.assertEqual(
            self.render(b"C", "CARDINAL", 32, struct.pack("<I", 1),
                        fmt=b"32c", dfmt=b"$5+\\n"),
            b"C(CARDINAL)\n")

    def test_fatal_messages(self):
        cases = (
            (dict(fmt=b"33x"), "bad format: 33x"),
            (dict(fmt=b"32"), "bad format: "),
            (dict(fmt=b"32q"), "bad format character: q"),
            (dict(dfmt=b"$x"), "Bad number: x."),
            # a FALSE conditional must find its ')' (a true one just runs
            # its body and never looks for one)
            (dict(dfmt=b"?0(x"), "Missing ')'."),
            (dict(dfmt=b"?5x"), "Bad conditional: '(' expected: x."),
            (dict(dfmt=b"?z(x)"), "Bad term: z(x)."),
        )
        for kw, msg in cases:
            with self.assertRaises(FatalError, msg=kw) as cm:
                self.render(b"C", "CARDINAL", 32, struct.pack("<I", 1), **kw)
            self.assertEqual(str(cm.exception), msg, kw)

    def test_size_mismatch_for_string_formats(self):
        with self.assertRaises(FatalError) as cm:
            self.render(b"C", "CARDINAL", 32, struct.pack("<I", 1),
                        fmt=b"0s")
        self.assertEqual(str(cm.exception),
                         "can't use format character 's' with any size "
                         "except 8.")

    # -- classification helpers ----------------------------------------------

    def test_is_a_format(self):
        self.assertTrue(fmtmod.is_a_format("8s"))
        self.assertTrue(fmtmod.is_a_format("32x"))
        self.assertFalse(fmtmod.is_a_format("x8"))
        self.assertFalse(fmtmod.is_a_format(""))

    def test_is_a_dformat(self):
        self.assertTrue(fmtmod.is_a_dformat(" instance=$0\\n"))
        self.assertTrue(fmtmod.is_a_dformat("$0\\n"))
        self.assertTrue(fmtmod.is_a_dformat("?m0(x)"))
        self.assertFalse(fmtmod.is_a_dformat("-x"))
        self.assertFalse(fmtmod.is_a_dformat("atom"))
        self.assertFalse(fmtmod.is_a_dformat("_NET"))
        self.assertFalse(fmtmod.is_a_dformat(""))
        # digits classify as formats, and also pass Is_A_DFormat in C — the
        # format check runs first, so a leading digit is a format
        self.assertTrue(fmtmod.is_a_dformat("8s"))


class IconTruncationTest(unittest.TestCase):
    """wxprop-7: xprop's Format_Icons returns NULL when the byte budget
    cannot hold even one (width, height) pair, and glibc's printf renders
    that as "(null)" (verified against the oracle with `-len 4` on a real
    _NET_WM_ICON)."""

    def test_budget_too_small_for_a_header_is_null(self):
        f = Formatter()
        self.assertEqual(f.format_icons(b""), b"(null)")
        self.assertEqual(f.format_icons(b"\x02\0\0\0"), b"(null)")

    def test_a_whole_icon_still_renders(self):
        f = Formatter()
        data = struct.pack("<6Q", 2, 2, 0xFF000000, 0xFFFFFFFF,
                           0xFFFF0000, 0xFF00FF00)
        out = f.format_icons(data)
        self.assertTrue(out.startswith(b"\tIcon (2 x 2):\n"), out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
