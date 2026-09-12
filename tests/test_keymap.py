"""Unit tests for wdotool.keymap and the generated wdotool.keysyms."""

import os
import re
import string
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wdotool import keymap, us_keymap
from wdotool.keysyms import KEYSYM_TO_UNICODE, NAME_TO_KEYSYM

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")


class TestKeysyms(unittest.TestCase):
    def test_size(self):
        self.assertGreater(len(NAME_TO_KEYSYM), 2000)
        self.assertGreater(len(KEYSYM_TO_UNICODE), 1500)

    def test_known_values(self):
        self.assertEqual(NAME_TO_KEYSYM["space"], 0x20)
        self.assertEqual(NAME_TO_KEYSYM["dollar"], 0x24)
        self.assertEqual(NAME_TO_KEYSYM["a"], 0x61)
        self.assertEqual(NAME_TO_KEYSYM["A"], 0x41)
        self.assertEqual(NAME_TO_KEYSYM["Return"], 0xFF0D)
        self.assertEqual(NAME_TO_KEYSYM["BackSpace"], 0xFF08)
        self.assertEqual(NAME_TO_KEYSYM["Aacute"], 0xC1)
        self.assertEqual(NAME_TO_KEYSYM["F1"], 0xFFBE)
        self.assertEqual(NAME_TO_KEYSYM["F35"], 0xFFBE + 34)

    def test_unicode_mappings(self):
        self.assertEqual(KEYSYM_TO_UNICODE[0x20], 0x20)
        self.assertEqual(KEYSYM_TO_UNICODE[0xFF80], 0x20)  # KP_Space <U+0020>
        self.assertEqual(KEYSYM_TO_UNICODE[0xC1], 0xC1)  # Aacute

    def test_deprecated_aliases_present(self):
        # XStringToKeysym accepts deprecated names too
        self.assertIn("quoteleft", NAME_TO_KEYSYM)
        self.assertIn("L1", NAME_TO_KEYSYM)


class TestCharTable(unittest.TestCase):
    def test_all_printable_ascii_mapped(self):
        for ch in string.digits + string.ascii_letters + string.punctuation + " \t\n\r":
            self.assertIsNotNone(keymap.char_to_key(ch), f"unmapped char {ch!r}")

    def test_letters(self):
        self.assertEqual(keymap.char_to_key("a"), (30, False))
        self.assertEqual(keymap.char_to_key("A"), (30, True))
        self.assertEqual(keymap.char_to_key("z"), (44, False))
        self.assertEqual(keymap.char_to_key("Z"), (44, True))
        self.assertEqual(keymap.char_to_key("q"), (16, False))
        self.assertEqual(keymap.char_to_key("m"), (50, False))

    def test_digits_and_shifts(self):
        self.assertEqual(keymap.char_to_key("1"), (2, False))
        self.assertEqual(keymap.char_to_key("!"), (2, True))
        self.assertEqual(keymap.char_to_key("0"), (11, False))
        self.assertEqual(keymap.char_to_key(")"), (11, True))
        self.assertEqual(keymap.char_to_key("$"), (5, True))

    def test_punctuation(self):
        self.assertEqual(keymap.char_to_key("`"), (41, False))
        self.assertEqual(keymap.char_to_key("~"), (41, True))
        self.assertEqual(keymap.char_to_key("-"), (12, False))
        self.assertEqual(keymap.char_to_key("_"), (12, True))
        self.assertEqual(keymap.char_to_key("="), (13, False))
        self.assertEqual(keymap.char_to_key("+"), (13, True))
        self.assertEqual(keymap.char_to_key("["), (26, False))
        self.assertEqual(keymap.char_to_key("{"), (26, True))
        self.assertEqual(keymap.char_to_key("\\"), (43, False))
        self.assertEqual(keymap.char_to_key("|"), (43, True))
        self.assertEqual(keymap.char_to_key(";"), (39, False))
        self.assertEqual(keymap.char_to_key(":"), (39, True))
        self.assertEqual(keymap.char_to_key("'"), (40, False))
        self.assertEqual(keymap.char_to_key('"'), (40, True))
        self.assertEqual(keymap.char_to_key(","), (51, False))
        self.assertEqual(keymap.char_to_key("<"), (51, True))
        self.assertEqual(keymap.char_to_key("."), (52, False))
        self.assertEqual(keymap.char_to_key(">"), (52, True))
        self.assertEqual(keymap.char_to_key("/"), (53, False))
        self.assertEqual(keymap.char_to_key("?"), (53, True))

    def test_control_chars(self):
        self.assertEqual(keymap.char_to_key("\n"), (28, False))
        self.assertEqual(keymap.char_to_key("\r"), (28, False))
        self.assertEqual(keymap.char_to_key("\t"), (15, False))
        self.assertEqual(keymap.char_to_key(" "), (57, False))
        self.assertEqual(keymap.char_to_key("\b"), (14, False))
        self.assertEqual(keymap.char_to_key("\x1b"), (1, False))

    def test_unmapped(self):
        self.assertIsNone(keymap.char_to_key("é"))
        self.assertIsNone(keymap.char_to_key("\x00"))


def keymap_bindings():
    """Every (char -> {(evdev keycode, shifted)}) the keymap `vkbd.py` uploads binds in group 1 at levels 1
    and 2, scanned line by line out of `us_keymap.TEXT`.

    Deliberately NOT the scanner in keymap.py: this one walks the file a line at a time, keeps every key a
    character appears on instead of the first, and never consults CHAR_TO_KEY -- so it can disagree with the
    table under test. The keymap it reads is the shipped one, byte for byte the same file as
    tests/fixtures/keymaps/us.xkb (a real `us` session's own keymap)."""
    text = us_keymap.TEXT
    sym_at = text.index("xkb_symbols")
    codes = {}
    for line in text[:sym_at].splitlines():
        m = re.match(r"\s*<([^<>]+)>\s*=\s*(\d+)\s*;", line)
        if m:
            codes[m.group(1)] = int(m.group(2))
    out = {}
    name = None
    for line in text[sym_at:].splitlines():
        m = re.match(r"\s*key\s+<([^<>]+)>", line)
        if m:
            name = m.group(1)
        if name is None or "[" not in line:
            continue
        if "symbols[" in line and not re.search(r"symbols\[\s*1\s*\]", line):
            continue                      # group 2 and up: the active group here is 1
        levels = re.search(r"\[([^\]]*)\]", line.split("=", 1)[-1] if "symbols[" in line else line)
        if levels is None:
            continue
        for level, tok in enumerate(t.strip() for t in levels.group(1).split(",")):
            if level > 1 or not tok.startswith("0x"):
                continue
            cp = KEYSYM_TO_UNICODE.get(int(tok, 16))
            if cp is None and 0x20 <= int(tok, 16) <= 0xFF:
                cp = int(tok, 16)
            if cp is None or not 0 <= cp <= 0x10FFFF:
                continue
            out.setdefault(chr(cp), set()).add((codes[name] - 8, bool(level)))
    return out


class TheKeysTheUploadedKeymapAdds(unittest.TestCase):
    """B2. `wdotool type` and the proxy's XTEST path both skipped EUR because the built-in table stopped at
    ASCII, while the keymap `vkbd.py` uploads has bound it to `key <I443>` all along -- and pressing evdev
    435 through that virtual keyboard put its three UTF-8 bytes into a focused `foot` on headless sway,
    byte-exact (goal2/recon/gaps.md §3b, reproduced end to end 2026-09-11: `wdotool --vkbd on type -- EUR+X`
    landed `b'\\xe2\\x82\\xacX\\n'`)."""

    EURO = "\u20ac"
    PLUSMINUS = "\u00b1"

    def test_the_euro_resolves_to_the_keycode_the_uploaded_keymap_binds_it_to(self):
        """(435, unshifted) -- and 435 is read back out of the keymap here, not repeated from the table, so a
        recapture that moved <I443> fails this instead of silently typing the wrong key."""
        x_keycode = int(re.search(r"<I443>\s*=\s*(\d+)\s*;", us_keymap.TEXT).group(1))
        self.assertRegex(us_keymap.TEXT, r"key <I443>\s*\{\s*\[ 0x20ac \] \};")
        self.assertEqual(keymap.char_to_key(self.EURO), (x_keycode - 8, False))
        self.assertEqual(keymap.char_to_key(self.EURO), (435, False))

    def test_every_character_that_keymap_binds_is_typeable_through_a_key_it_binds(self):
        """The whole claim of deriving the rows instead of hand-listing them: for each PRINTABLE character
        the uploaded keymap gives a key, `char_to_key` answers, and its answer is one of the keys THAT keymap
        puts the character on -- so our keycodes and the keymap that reads them cannot disagree."""
        bound = keymap_bindings()
        self.assertGreater(len(bound), 100, "the keymap scanner found nothing")
        for ch, keys in sorted(bound.items()):
            if ch < " " or ch == "\x7f":
                # The control characters are xdotool's own mapping and not the keymap's: `type` sends \n and
                # \r to Return (evdev 28) where this keymap has Linefeed on <LNFD> (101), and that is the
                # parity to keep. xkbmap._expected_us() leaves them out of the bypass check for the same
                # reason. The claim here is about characters a user types.
                continue
            hit = keymap.char_to_key(ch)
            self.assertIsNotNone(hit, "the uploaded keymap binds %r and nothing types it" % ch)
            self.assertIn(hit, keys, "%r resolves to %r, which that keymap does not bind it to" % (ch, hit))

    def test_it_adds_exactly_what_the_fixed_block_was_missing(self):
        """Two rows today: EUR on KEY_EURO (evdev 435) and PLUS-MINUS on KEY_KPPLUSMINUS (evdev 118).

        The second assertion is the derivation and carries the claim on its own; the literal pin is kept
        anyway, deliberately. A recapture of `us_keymap.TEXT` is the one thing that can add a third row, it
        moves the keycodes this whole table hands the daemon, and it should not be able to do that without a
        human reading the new list -- which is what a red here makes them do."""
        self.assertEqual(keymap.UPLOADED_EXTRA_KEYS,
                         {self.EURO: (435, False), self.PLUSMINUS: (118, False)})
        self.assertEqual(set(keymap.UPLOADED_EXTRA_KEYS),
                         set(keymap_bindings()) - set(keymap.CHAR_TO_KEY))

    def test_the_extra_rows_stay_out_of_the_table_the_us_bypass_is_built_from(self):
        """`xkbmap._expected_us()` turns CHAR_TO_KEY into the demands the plain-US bypass makes of a SESSION
        keymap, and `_plain_us` reads no keycode above X 263 -- so a 435 in CHAR_TO_KEY makes
        `active_group_is_plain_us` answer False for every keymap in the tree (measured: 20 failures in
        tests/test_xkbmap.py). CHAR_TO_KEY stays the US-QWERTY block; the extras ride alongside it."""
        self.assertNotIn(self.EURO, keymap.CHAR_TO_KEY)
        self.assertNotIn(self.PLUSMINUS, keymap.CHAR_TO_KEY)
        self.assertEqual(len(keymap.CHAR_TO_KEY), 101)
        self.assertIsNotNone(keymap.char_to_key(self.EURO))

    def test_the_keysym_name_and_the_unicode_keysym_reach_the_same_key(self):
        """The proxy's XTEST path (xw11/xtest.py `spec_for`) hands the daemon `0x010020ac` for a stolen
        keycode carrying EuroSign; `wdotool key EuroSign` and `wdotool key 0x20ac` are the other two spellings
        of the same key. All three went through `CHAR_TO_KEY.get` and answered "not reachable".

        The resolution is all this pins, and it is sink-independent -- the daemon resolves before it picks a
        device. Whether the keystroke then reaches a window is not: on the virtual-keyboard sink it does
        (measured live on sway, test_vkbd.py), and on the kernel device it does **not yet**, because
        `uinput.keyboard()` registers keybits 1..255 (wdotool/uinput.py:160) and the kernel drops 435 without
        a word. `--vkbd auto` picks that device wherever /dev/uinput is usable, so an XTEST EuroSign through
        the proxy is closed on the sway golden only once the uinput request in goal2/requests-batch-2.md
        lands and somebody runs it there."""
        for spelling in ("EuroSign", "0x20ac", "0x010020ac"):
            self.assertEqual(keymap.resolve_token(spelling), (435, False), spelling)
        self.assertEqual(keymap.keysym_to_key("EuroSign"), (435, False))

    def test_the_numeric_keycode_path_still_stops_at_x_keycode_263(self):
        """xdotool's own ceiling: X keycodes are 8..255 there, so `key 443` is a refusal and NOT a way in to
        evdev 435. char_to_key bypasses that ceiling, which is why the one table reaches the key and the
        numeric token still does not -- parity with the original, kept on purpose."""
        self.assertEqual(keymap.resolve_token("443"),
                         "key '443' is not reachable on the US layout. Ignoring it.")
        self.assertEqual(keymap.resolve_token("263"), (255, False))
        self.assertEqual(keymap.resolve_token("264"),
                         "key '264' is not reachable on the US layout. Ignoring it.")


class TestKeysymResolution(unittest.TestCase):
    def test_specials(self):
        self.assertEqual(keymap.keysym_to_key("Return"), (28, False))
        self.assertEqual(keymap.keysym_to_key("BackSpace"), (14, False))
        self.assertEqual(keymap.keysym_to_key("Escape"), (1, False))
        self.assertEqual(keymap.keysym_to_key("F1"), (59, False))
        self.assertEqual(keymap.keysym_to_key("F10"), (68, False))
        self.assertEqual(keymap.keysym_to_key("F11"), (87, False))
        self.assertEqual(keymap.keysym_to_key("F12"), (88, False))
        self.assertEqual(keymap.keysym_to_key("F13"), (183, False))
        self.assertEqual(keymap.keysym_to_key("F24"), (194, False))
        self.assertEqual(keymap.keysym_to_key("Left"), (105, False))
        self.assertEqual(keymap.keysym_to_key("Up"), (103, False))
        self.assertEqual(keymap.keysym_to_key("Right"), (106, False))
        self.assertEqual(keymap.keysym_to_key("Down"), (108, False))
        self.assertEqual(keymap.keysym_to_key("Prior"), (104, False))
        self.assertEqual(keymap.keysym_to_key("Page_Down"), (109, False))
        self.assertEqual(keymap.keysym_to_key("ISO_Left_Tab"), (15, True))

    def test_keypad(self):
        self.assertEqual(keymap.keysym_to_key("KP_0"), (82, False))
        self.assertEqual(keymap.keysym_to_key("KP_5"), (76, False))
        self.assertEqual(keymap.keysym_to_key("KP_9"), (73, False))
        self.assertEqual(keymap.keysym_to_key("KP_Enter"), (96, False))
        self.assertEqual(keymap.keysym_to_key("KP_Add"), (78, False))
        self.assertEqual(keymap.keysym_to_key("KP_Divide"), (98, False))

    def test_modifier_keysyms(self):
        self.assertEqual(keymap.keysym_to_key("Control_L"), (29, False))
        self.assertEqual(keymap.keysym_to_key("Control_R"), (97, False))
        self.assertEqual(keymap.keysym_to_key("Shift_L"), (42, False))
        self.assertEqual(keymap.keysym_to_key("Shift_R"), (54, False))
        self.assertEqual(keymap.keysym_to_key("Alt_L"), (56, False))
        self.assertEqual(keymap.keysym_to_key("Super_L"), (125, False))
        self.assertEqual(keymap.keysym_to_key("Caps_Lock"), (58, False))

    def test_via_unicode(self):
        self.assertEqual(keymap.keysym_to_key("dollar"), (5, True))
        self.assertEqual(keymap.keysym_to_key("exclam"), (2, True))
        self.assertEqual(keymap.keysym_to_key("asciitilde"), (41, True))
        self.assertEqual(keymap.keysym_to_key("space"), (57, False))
        self.assertEqual(keymap.keysym_to_key("a"), (30, False))
        self.assertEqual(keymap.keysym_to_key("A"), (30, True))

    def test_unreachable(self):
        self.assertIsNone(keymap.keysym_to_key("Aacute"))
        self.assertIsNone(keymap.keysym_to_key("nosuchname"))


class TestParseKeyseq(unittest.TestCase):
    def test_simple(self):
        keys, warns = keymap.parse_keyseq("ctrl+shift+t")
        self.assertEqual(keys, [(29, False), (42, False), (20, False)])
        self.assertEqual(warns, [])

    def test_aliases_case_insensitive(self):
        for spec in ("CTRL+T", "Ctrl+t", "ctrl+t"):
            keys, _ = keymap.parse_keyseq(spec)
            self.assertEqual(keys[0], (29, False))
        self.assertEqual(keymap.parse_keyseq("super+x")[0][0], (125, False))
        self.assertEqual(keymap.parse_keyseq("win+x")[0][0], (125, False))
        self.assertEqual(keymap.parse_keyseq("meta+x")[0][0], (56, False))
        self.assertEqual(keymap.parse_keyseq("enter")[0], [(28, False)])
        self.assertEqual(keymap.parse_keyseq("Return")[0], [(28, False)])

    def test_keysym_names_case_sensitive(self):
        # aliases match case-insensitively ("RETURN" -> alias "return"), but
        # plain keysym names do not ("BACKSPACE" is not "BackSpace")
        keys, warns = keymap.parse_keyseq("RETURN")
        self.assertEqual((keys, warns), ([(28, False)], []))
        _, warns = keymap.parse_keyseq("BACKSPACE")
        self.assertEqual(warns, ["(symbol) No such key name 'BACKSPACE'. Ignoring it."])

    def test_numeric_x_keycode(self):
        keys, warns = keymap.parse_keyseq("38")  # X keycode 38 == evdev 30 == 'a'
        self.assertEqual(keys, [(30, False)])
        self.assertEqual(warns, [])

    def test_unknown_token_warns_and_skips(self):
        keys, warns = keymap.parse_keyseq("ctrl+bogus+t")
        self.assertEqual(keys, [(29, False), (20, False)])
        self.assertEqual(warns, ["(symbol) No such key name 'bogus'. Ignoring it."])

    def test_invalid_sequence_chars(self):
        for bad in ("ctrl-x", "a b", "x[1]", "a\\b", "p|q", "a.b"):
            with self.assertRaises(ValueError):
                keymap.parse_keyseq(bad)

    def test_shifted_keysym(self):
        keys, _ = keymap.parse_keyseq("ctrl+A")
        self.assertEqual(keys, [(29, False), (30, True)])


class TypingUnderALiveGermanGroup(unittest.TestCase):
    """The four characters that came out wrong on resolute-cinnamon-wayland, and the keys that type them.

    Measured live on the golden 2026-09-12 (Cinnamon 6.4.13, muffin 6.4.1, Xwayland 24.1.13,
    `org.cinnamon.desktop.input-sources` = `[('xkb','us'),('xkb','de')]` with `current` 1, one xterm
    running `cat`), `wdotool keys explain --chars 'zy:@'` reading the compositor's own keymap off the
    wire -- `layout: German -- group 2 of 2, from wayland + cinnamon input-sources`:

        'z'  press key 21 <AD06>                                     -> 'z'
        'y'  press key 44 <AB01>                                     -> 'y'
        ':'  press key 52 <AB09> with shift (key 42 <LFSH>)          -> ':'
        '@'  press key 16 <AD01> with level3 (key 100 <RALT>)        -> '@'

    Those are German positions, not US ones, and the compensation is right: with muffin's own layout
    group locked to the German one (`org.Cinnamon.Eval` of `Meta.get_backend().lock_layout_group(1)`,
    AGENTS.md route 2) `wdotool type --delay 30 -- 'de: yz@'` arrived BYTE-EXACT through /dev/uinput on
    that session. The `de> zyñ` that batch 13 recorded is the same German keycodes read back under a US
    group -- muffin locked group 0 while Cinnamon's `current` said 1 -- and the three-way table is in
    `vm/live-smoke.d/cinnamon-wayland.sh`'s layout phase.

    The fixture is `tests/fixtures/keymaps/us_de.xkb`, a real `wl_keyboard.keymap` off a `us,de` session
    (group 1 English (US), group 2 German), so these are the keys a compositor's own map yields and not
    a table written here."""

    @classmethod
    def setUpClass(cls):
        from wdotool import xkbmap
        path = os.path.join(ROOT, "tests", "fixtures", "keymaps", "us_de.xkb")
        with open(path, encoding="utf-8") as f:
            cls.text = f.read()
        cls.xkbmap = xkbmap
        cls.de = xkbmap.build(cls.text, 2)
        cls.us = xkbmap.build(cls.text, 1)

    def test_the_four_characters_are_the_keys_the_live_session_named(self):
        self.assertEqual(self.de.name, "German")
        self.assertEqual(self.de.lookup_char("z"), [(21, 0)])
        self.assertEqual(self.de.lookup_char("y"), [(44, 0)])
        self.assertEqual(self.de.lookup_char(":"), [(52, self.xkbmap.MOD_SHIFT)])
        self.assertEqual(self.de.lookup_char("@"), [(16, self.xkbmap.MOD_LEVEL3)])

    def test_the_modifier_keycodes_are_the_ones_the_live_session_named(self):
        """`shift = key 42 <LFSH>   level3 = key 100 <RALT>` off the same run: the mask is nothing until
        it names real keys to press, and AltGr is the key `@` needs."""
        self.assertEqual(self.de.modifier_keycodes(self.xkbmap.MOD_SHIFT), [42])
        self.assertEqual(self.de.modifier_keycodes(self.xkbmap.MOD_LEVEL3), [100])

    def test_group_1_of_the_same_keymap_is_the_us_answer(self):
        """The guard on the whole thing: these are not German keys because the keymap only has German
        ones. Group 1 of the very same file types all four the US way -- y and z the other way round,
        `:` on 39 and `@` on 3 with shift -- which is what arrives when the group is wrong."""
        self.assertEqual(self.us.lookup_char("z"), [(44, 0)])
        self.assertEqual(self.us.lookup_char("y"), [(21, 0)])
        self.assertEqual(self.us.lookup_char(":"), [(39, self.xkbmap.MOD_SHIFT)])
        self.assertEqual(self.us.lookup_char("@"), [(3, self.xkbmap.MOD_SHIFT)])

    def test_the_us_group_agrees_with_the_built_in_table_it_replaces(self):
        """And group 1 agrees with `keymap.CHAR_TO_KEY`, which is why a one-group `us` session takes the
        bypass and never builds a map at all."""
        for ch in "zy:@":
            code, shifted = keymap.char_to_key(ch)
            want = [(code, self.xkbmap.MOD_SHIFT if shifted else 0)]
            self.assertEqual(self.us.lookup_char(ch), want, ch)


class TestModifierTable(unittest.TestCase):
    def test_eight_modifiers(self):
        self.assertEqual(len(keymap.MODIFIER_KEYCODES), 8)
        self.assertEqual(
            set(keymap.MODIFIER_KEYCODES), {42, 54, 29, 97, 56, 100, 125, 126}
        )


if __name__ == "__main__":
    unittest.main()
