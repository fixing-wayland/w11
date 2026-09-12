"""The reverse keymap (B13), over keymaps recorded from real compositors.

Every fixture in tests/fixtures/keymaps is a byte-for-byte copy of what a
compositor handed a client on `wl_keyboard.keymap` (see the README there).
The assertions are keycodes and modifier masks: what wdotool would actually
inject.

The last two classes are the safety requirement: on a plain US layout the
reverse map is not merely unused, it is never built -- proven by making
every entry point into the machinery raise.
"""

import contextlib
import copy
import io
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# ...and the tests directory itself, for the bare `import support` and
# `from test_dbus_mini import MockBus` below: running this file by path
# puts it on sys.path for free, `python3 -m unittest tests/<file>.py`
# does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common import dbus_mini
from w11common import session as w11_session
from w11common.dbus_mini import ERR, Bus, DBusError, Variant
from support import FakeHypr, FakeWayfire, RecorderDev, env, fixture_json
from test_dbus_mini import MockBus
from wdotool import cli, daemon, keymap, keys_cmds, xkbmap

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

KEYMAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "keymaps")
FIXTURES = ("us", "de", "fr", "es", "gb", "dvorak", "us_de", "de_fr",
            "five_es", "noble_de", "sway_de", "us_swapescape",
            "us_grptoggle", "kde_us", "kde_de", "kde_gr", "kde_us_de",
            "kde_us_de_fr", "kde5_de")


def text(name: str) -> str:
    with open(os.path.join(KEYMAPS, name + ".xkb"), encoding="utf-8") as f:
        return f.read()


def rmap(name: str, group: int = 1) -> xkbmap.ReverseMap:
    return xkbmap.build(text(name), group)


def setUpModule():
    """No test in this file may open the *developer's* session bus, or the
    compositor this box is actually running. All five desktop readers are
    module-level objects made on first use (KwinLayouts, GnomeInputSources,
    HyprLayouts, WayfireLayouts, CinnamonInputSources); park ones that have
    already given up in their place, and let the classes below hand out
    readers pointed at a mock bus or a fake socket instead.

    The two socket readers matter as much as the bus ones here: this box has
    wayfire installed, so an unparked WayfireLayouts would scan the runtime
    dir of whoever is running the suite."""
    for name, make in (("_kwin", xkbmap.KwinLayouts), ("_gnome", xkbmap.GnomeInputSources),
                       ("_hypr", xkbmap.HyprLayouts), ("_wayfire", xkbmap.WayfireLayouts),
                       ("_cinnamon", xkbmap.CinnamonInputSources)):
        reader = make()
        reader.absent = True
        setattr(xkbmap, name, reader)


def make_daemon():
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = (0, 0, 1920, 1080)
    d._rel_abs = False
    return d


def taps(dev) -> list:
    """The (code, down) events, so a test reads like a keystroke list."""
    return [(e[1], e[2]) for e in dev.events if e[0] == "KEY"]


# ---------------------------------------------------------------------------


class TestParse(unittest.TestCase):
    def test_every_fixture_parses(self):
        for name in FIXTURES:
            km = xkbmap.parse(text(name))
            self.assertGreater(len(km.keycodes), 200, name)
            self.assertGreater(len(km.types), 5, name)
            self.assertGreaterEqual(len(km.groups), 1, name)

    def test_keycodes_are_evdev(self):
        km = xkbmap.parse(text("us"))
        # X keycode 24 (<AD01>) is evdev 16, the `q` key of a US board
        self.assertEqual(km.keycodes["AD01"], 16)
        self.assertEqual(km.keycodes["ESC"], 1)
        self.assertEqual(km.keycodes["SPCE"], 57)
        self.assertEqual(km.keycodes["RTRN"], 28)
        self.assertEqual(km.keycodes["RALT"], 100)

    def test_aliases_resolve(self):
        km = xkbmap.parse(text("de"))
        self.assertEqual(km.keycodes["ALGR"], km.keycodes["RALT"])

    def test_the_two_keysym_dialects_agree(self):
        """GNOME 50 writes every keysym as a hex number, GNOME 46 writes
        names ("symbols[Group1]= [ q, Q, at, ... ]"), and sway hands out a
        keymap with a single group. Same layout, same answers."""
        de, noble, sway = rmap("de"), rmap("noble_de"), rmap("sway_de")
        for ch in "zyäöüß@€é|":
            self.assertEqual(de.lookup_char(ch), noble.lookup_char(ch), ch)
            self.assertEqual(de.lookup_char(ch), sway.lookup_char(ch), ch)

    def test_a_single_group_keymap(self):
        """sway compiles exactly the configured layout: one group, and
        nothing to guess about which one is active."""
        self.assertEqual(xkbmap.group_count(text("sway_de")), 1)
        self.assertEqual(xkbmap.choose_group(text("sway_de")), (1, True))
        self.assertEqual(rmap("sway_de").name, "German")

    def test_group_names(self):
        self.assertEqual(xkbmap.parse(text("de")).group_names,
                         ["German", "English (US)"])
        self.assertEqual(xkbmap.parse(text("us_de")).group_names,
                         ["English (US)", "German", "English (US)"])
        self.assertEqual(xkbmap.parse(text("de_fr")).group_names,
                         ["German", "French", "English (US)"])

    def test_group_count_is_cheap_and_agrees(self):
        for name in FIXTURES:
            self.assertEqual(xkbmap.group_count(text(name)),
                             len(xkbmap.parse(text(name)).groups), name)

    def test_types_carry_level_masks(self):
        km = xkbmap.parse(text("de"))
        four = km.types["FOUR_LEVEL"]
        self.assertEqual(four[1], 0)
        self.assertEqual(four[2], xkbmap.MOD_SHIFT)
        self.assertEqual(four[3], xkbmap.MOD_LEVEL3)
        self.assertEqual(four[4], xkbmap.MOD_SHIFT | xkbmap.MOD_LEVEL3)
        # ALPHABETIC reaches level 2 with Shift or with Lock; Shift is the one
        # we can press, and it is the cheaper of the two anyway.
        self.assertEqual(km.types["ALPHABETIC"][2], xkbmap.MOD_SHIFT)

    def test_type_with_unpressable_modifier_is_dropped(self):
        km = xkbmap.parse(text("de"))
        # PC_CONTROL_LEVEL2's level 2 needs Control held: not a typing route.
        self.assertNotIn(2, km.types["PC_CONTROL_LEVEL2"])

    def test_group_wrapping(self):
        """A key that binds only group 1 still exists in group 2 (groupsWrap):
        <SPCE> and <RTRN> are never repeated per layout."""
        km = xkbmap.parse(text("us_de"))
        self.assertNotIn("SPCE", km.groups[1].syms)      # not written for group 2
        self.assertIn("SPCE", km.resolved(2))            # but present all the same
        self.assertEqual(km.resolved(2)["SPCE"], km.resolved(1)["SPCE"])
        # ... while a key the German group does rebind differs
        self.assertNotEqual(km.resolved(2)["AD06"], km.resolved(1)["AD06"])

    def test_rejects_rubbish(self):
        for bad in ("", "hello", "xkb_keymap { }", "xkb_symbols {"):
            with self.assertRaises(xkbmap.XkbError):
                xkbmap.parse(bad)


class TestKeysymTokens(unittest.TestCase):
    def test_values(self):
        self.assertEqual(xkbmap.keysym_value("0x61"), 0x61)
        self.assertEqual(xkbmap.keysym_value(" a "), 0x61)
        self.assertEqual(xkbmap.keysym_value("dead_acute"), 0xFE51)
        self.assertEqual(xkbmap.keysym_value("U0161"), 0x1000161)
        self.assertEqual(xkbmap.keysym_value("U00E4"), 0xE4)
        self.assertIsNone(xkbmap.keysym_value("NoSymbol"))
        self.assertIsNone(xkbmap.keysym_value(""))
        self.assertIsNone(xkbmap.keysym_value("not_a_keysym"))

    def test_chars(self):
        self.assertEqual(xkbmap.keysym_char(0x61), "a")
        self.assertEqual(xkbmap.keysym_char(0xE4), "ä")
        self.assertEqual(xkbmap.keysym_char(0x20AC), "€")   # XK_EuroSign
        self.assertEqual(xkbmap.keysym_char(0x1002032), "′")  # unicode keysym
        self.assertIsNone(xkbmap.keysym_char(0xFE51))       # dead_acute types nothing
        self.assertIsNone(xkbmap.keysym_char(0xFF0D))       # Return is a key
        self.assertIsNone(xkbmap.keysym_char(None))


class TestReverseMap(unittest.TestCase):
    """Recorded layout in, keycode + modifier mask out."""

    def check(self, name, group, expected):
        r = rmap(name, group)
        for ch, want in expected.items():
            self.assertEqual(r.lookup_char(ch), want,
                             f"{name} group {group}: {ch!r}")

    def test_us(self):
        S = xkbmap.MOD_SHIFT
        self.check("us", 1, {
            "a": [(30, 0)], "A": [(30, S)], "z": [(44, 0)], "1": [(2, 0)],
            "!": [(2, S)], "@": [(3, S)], " ": [(57, 0)], "\n": [(28, 0)],
            "\t": [(15, 0)], "\x1b": [(1, 0)], "\b": [(14, 0)],
        })

    def test_german_is_qwertz(self):
        S, L3 = xkbmap.MOD_SHIFT, xkbmap.MOD_LEVEL3
        self.check("de", 1, {
            "z": [(21, 0)], "y": [(44, 0)], "Z": [(21, S)],
            "ä": [(40, 0)], "Ä": [(40, S)], "ö": [(39, 0)], "ü": [(26, 0)],
            "ß": [(12, 0)], "@": [(16, L3)], "€": [(18, L3)],
            "µ": [(50, L3)], "|": [(86, L3)],
        })

    def test_french_is_azerty(self):
        L3 = xkbmap.MOD_LEVEL3
        self.check("fr", 1, {
            "a": [(16, 0)], "q": [(30, 0)], "z": [(17, 0)], "w": [(44, 0)],
            "m": [(39, 0)], "é": [(3, 0)], "è": [(8, 0)], "ç": [(10, 0)],
            "à": [(11, 0)], "ù": [(40, 0)], "@": [(11, L3)], "€": [(18, L3)],
        })

    def test_spanish(self):
        L3 = xkbmap.MOD_LEVEL3
        self.check("es", 1, {"ñ": [(39, 0)], "ç": [(43, 0)], "¡": [(13, 0)],
                             "@": [(3, L3)], "€": [(18, L3)]})

    def test_uk(self):
        S, L3 = xkbmap.MOD_SHIFT, xkbmap.MOD_LEVEL3
        self.check("gb", 1, {"#": [(43, 0)], "£": [(4, S)], "@": [(40, S)],
                             "\\": [(86, 0)], "|": [(86, S)], "€": [(5, L3)]})

    def test_dvorak(self):
        self.check("dvorak", 1, {
            "a": [(30, 0)], "q": [(45, 0)], "z": [(53, 0)], "x": [(48, 0)],
            "p": [(19, 0)], "e": [(32, 0)], ",": [(17, 0)], ".": [(18, 0)],
            "'": [(16, 0)],
        })

    def test_level_shift_keys_come_from_the_layout(self):
        # AltGr is <RALT> where the layout puts ISO_Level3_Shift on it ...
        self.assertEqual(rmap("de").mod_keys[xkbmap.MOD_LEVEL3], 100)
        self.assertEqual(rmap("fr").mod_keys[xkbmap.MOD_LEVEL3], 100)
        # ... and the synthetic <LVL3> keycode where it does not (plain US
        # and Dvorak leave <RALT> as Alt_R).
        self.assertEqual(rmap("dvorak").mod_keys[xkbmap.MOD_LEVEL3], 84)
        for name in FIXTURES:
            self.assertEqual(rmap(name).mod_keys[xkbmap.MOD_SHIFT], 42, name)

    def test_keypad_never_wins_a_character(self):
        # KP_1 types "1" (with NumLock); the number row must win anyway.
        for name in ("us", "de", "fr"):
            self.assertEqual(rmap(name).lookup_char("1")[0][0],
                             2 if name != "fr" else 2)

    def test_unreachable_characters(self):
        for name in ("us", "de", "fr", "dvorak"):
            r = rmap(name)
            self.assertIsNone(r.lookup_char("漢"), name)
            self.assertIsNone(r.lookup_char("́"), name)  # a bare combiner
        self.assertIsNone(rmap("us").lookup_char("€"))
        self.assertIsNone(rmap("dvorak").lookup_char("ß"))

    def test_keysym_entry_for_key_sequences(self):
        de = rmap("de")
        self.assertEqual(de.keysym_entry("z"), (21, 0))
        self.assertEqual(de.keysym_entry("at"), (16, xkbmap.MOD_LEVEL3))
        self.assertEqual(rmap("gb").keysym_entry("sterling"), (4, xkbmap.MOD_SHIFT))
        self.assertIsNone(de.keysym_entry("nosuchkeysym"))

    def test_modifier_keycodes(self):
        de = rmap("de")
        self.assertEqual(de.modifier_keycodes(0), [])
        self.assertEqual(de.modifier_keycodes(xkbmap.MOD_SHIFT), [42])
        self.assertEqual(de.modifier_keycodes(xkbmap.MOD_LEVEL3), [100])
        self.assertEqual(
            de.modifier_keycodes(xkbmap.MOD_SHIFT | xkbmap.MOD_LEVEL3), [42, 100])


class TestDeadKeys(unittest.TestCase):
    def test_german_acute(self):
        # <AE12> is dead_acute on a German board: é is two keystrokes.
        self.assertEqual(rmap("de").lookup_char("é"), [(13, 0), (18, 0)])
        self.assertEqual(rmap("de").lookup_char("è"), [(13, xkbmap.MOD_SHIFT),
                                                       (18, 0)])

    def test_french_circumflex(self):
        fr = rmap("fr")
        self.assertEqual(fr.lookup_char("ô"), [(26, 0), (24, 0)])
        self.assertEqual(fr.lookup_char("î"), [(26, 0), (23, 0)])
        # é and è are single keys on French, not dead-key sequences
        self.assertEqual(len(fr.lookup_char("é")), 1)

    def test_spanish_and_uk(self):
        es = rmap("es")
        self.assertEqual(es.lookup_char("á"), [(40, 0), (30, 0)])
        self.assertEqual(es.lookup_char("ü"), [(40, xkbmap.MOD_SHIFT), (22, 0)])
        self.assertEqual(rmap("gb").lookup_char("é"),
                         [(39, xkbmap.MOD_LEVEL3), (18, 0)])

    def test_bare_accent_is_dead_key_then_space(self):
        # <dead_grave> <space> is "`" in the Compose table, so this one is
        # still a space; the accents Compose spells differently are in
        # TestDeadKeyPlusSpace below (B3).
        de = rmap("de")
        self.assertEqual(de.lookup_char("`"), [(13, xkbmap.MOD_SHIFT), (57, 0)])
        self.assertEqual(de.lookup_char("^"), [(41, 0), (57, 0)])

    def test_dead_key_is_not_a_character(self):
        # dead_acute must never be handed out as a way to type something
        for name in ("de", "fr", "es", "gb"):
            r = rmap(name)
            for entry in r.chars.values():
                self.assertNotIn(entry, list(r.dead.values()) or [None],
                                 f"{name}: a dead key leaked into chars")

    def test_only_sequences_that_recompose(self):
        de = rmap("de")
        # ẃ decomposes to w + acute, and NFC puts it back: allowed.
        self.assertEqual(de.lookup_char("ẃ"), [(13, 0), (17, 0)])
        # A three-codepoint decomposition (ế) is not a two-keystroke sequence.
        self.assertIsNone(de.lookup_char("ế"))
        # The case the name is actually about, which neither line above
        # reaches: two codepoints, both typable, that do NOT recompose. Both
        # assertions above are satisfied by the `len(decomposed) != 2` check
        # one line earlier, so deleting the NFC test left the suite green.
        # U+0958 is a composition exclusion: NFD is U+0915 U+093C and NFC
        # leaves the pair as it is, so typing the two would produce a
        # different string from the one asked for.
        de.chars["\u0915"] = (99, 0)
        de.dead[0x93C] = (98, 0)
        self.assertIsNone(de.lookup_char("\u0958"))
        # the control: both entries really are reachable, so the None above
        # comes from the NFC test and from nothing else
        self.assertEqual(de.lookup_char("\u0915\u093c"), [(98, 0), (99, 0)])


class TestALevelWeCannotPress(unittest.TestCase):
    """A layout with four-level types whose level-3 key has been removed
    (lv3:none, a custom keymap): the levels behind AltGr do not exist for us
    and must not be offered. The guard used to test the backfilled fallback
    table rather than what the keymap said, so it could never fire for the
    case its own comment names."""

    @staticmethod
    def _no_level3():
        src = text("de")
        src = re.sub(r"0xfe03|0xff7e", "0xffea", src)      # -> Alt_R
        return src.replace("ISO_Level3_Shift", "Alt_R").replace(
            "Mode_switch", "Alt_R")

    def test_level_3_characters_are_dropped_not_mistyped(self):
        r = xkbmap.reverse(xkbmap.parse(self._no_level3()))
        offered = [c for c, e in r.chars.items() if e[1] & xkbmap.MOD_LEVEL3]
        self.assertEqual(offered, [], "AltGr levels on a layout with no AltGr")
        # '@' is AltGr+q on a German layout: with no level-3 key it is simply
        # not typable, which the caller reports rather than pressing Alt_R.
        self.assertIsNone(r.lookup_char("@"))

    def test_the_real_layout_is_untouched(self):
        r = rmap("de")
        self.assertEqual(r.lookup_char("@"), [(16, xkbmap.MOD_LEVEL3)])
        self.assertEqual(r.modifier_keycodes(xkbmap.MOD_LEVEL3), [100])


class TestGroups(unittest.TestCase):
    def test_second_group_is_a_different_layout(self):
        us_de = text("us_de")
        self.assertEqual(xkbmap.build(us_de, 1).lookup_char("z"), [(44, 0)])
        self.assertEqual(xkbmap.build(us_de, 2).lookup_char("z"), [(21, 0)])
        self.assertEqual(xkbmap.build(us_de, 2).name, "German")
        self.assertEqual(xkbmap.build(us_de, 3).name, "English (US)")

    def test_three_groups(self):
        de_fr = text("de_fr")
        self.assertEqual(xkbmap.build(de_fr, 1).lookup_char("a"), [(30, 0)])
        self.assertEqual(xkbmap.build(de_fr, 2).lookup_char("a"), [(16, 0)])

    def test_wrapped_keys_are_in_every_group(self):
        for g in (1, 2, 3):
            r = xkbmap.build(text("us_de"), g)
            self.assertEqual(r.lookup_char(" "), [(57, 0)], g)
            self.assertEqual(r.lookup_char("\n"), [(28, 0)], g)

    def test_no_such_group(self):
        with self.assertRaises(xkbmap.XkbError):
            xkbmap.build(text("de"), 5)

    def test_choose_group_when_all_groups_are_the_same(self):
        # GNOME compiles a single `us` source as the two groups "us,us":
        # which one is active cannot matter, so the answer is known.
        self.assertEqual(xkbmap.choose_group(text("us")), (1, True))

    def test_choosing_a_group_never_needs_the_parser(self):
        """It runs on a US layout, where the parser must not."""
        real = xkbmap.parse
        self.addCleanup(setattr, xkbmap, "parse", real)
        xkbmap.parse = lambda *a, **k: self.fail("parse() was reached")
        for name in FIXTURES:
            xkbmap.choose_group(text(name))

    def test_choose_group_when_they_differ(self):
        # ... but "de,us" (a single German source plus GNOME's appended
        # fallback) is a guess: group 1, flagged as assumed.
        for name in ("de", "fr", "es", "gb", "dvorak", "us_de", "de_fr"):
            self.assertEqual(xkbmap.choose_group(text(name)), (1, False), name)

    def test_modifiers_event_wins(self):
        self.assertEqual(xkbmap.choose_group(text("us_de"), 2), (2, True))


class TestKwinKeymaps(unittest.TestCase):
    """The five keymaps KWin hands its clients: Plasma 6.6 on Ubuntu 26.04
    (`kde_us`, `kde_de`, `kde_gr`, `kde_us_de`) and Plasma 5.27 on 24.04
    (`kde5_de`).

    Every keystroke asserted here was also sent into a real Kate window on
    `resolute-kde` and `noble-kde` and read back out of the saved file:
    repro/kde-keys-1-group-guess.sh.
    """

    def test_kwin_compiles_exactly_the_configured_layouts(self):
        """One configured layout is one group -- what sway does, and what
        mutter does not."""
        for name, want in (("kde_us", ["English (US)"]),
                           ("kde_de", ["German"]),
                           ("kde_gr", ["Greek"]),
                           ("kde5_de", ["German"]),
                           ("kde_us_de", ["English (US)", "German"])):
            self.assertEqual(xkbmap.parse(text(name)).group_names, want, name)
        # the same two sessions on GNOME, with its appended fallback group
        self.assertEqual(xkbmap.parse(text("us")).group_names,
                         ["English (US)", "English (US)"])
        self.assertEqual(xkbmap.parse(text("de")).group_names,
                         ["German", "English (US)"])

    def test_kwins_german_is_sways_byte_for_byte(self):
        """Nothing in a keymap is the compositor's own work: KWin and sway
        hand out the same libxkbcommon output for the same layout on the same
        release, down to the byte. What the KDE fixtures add is the *shape*
        the compositor asks for, not new bytes."""
        self.assertEqual(text("kde_de"), text("sway_de").rstrip("\x00"))

    def test_plasma_5_is_the_other_keysym_dialect(self):
        """24.04's libxkbcommon writes keysym names, 26.04's writes hex
        numbers. Same layout, same answers, on KDE as on GNOME."""
        self.assertIn("symbols[Group1]", text("kde5_de"))
        self.assertNotIn("symbols[Group1]", text("kde_de"))
        five, six = rmap("kde5_de"), rmap("kde_de")
        for ch in "yz\u00e4\u00f6\u00fc\u00df@\u20ac\u00e9|\u0142\u2014\u00e7":
            self.assertEqual(five.lookup_char(ch), six.lookup_char(ch), ch)

    def test_greek_is_the_first_non_latin_fixture(self):
        S, L3 = xkbmap.MOD_SHIFT, xkbmap.MOD_LEVEL3
        gr = rmap("kde_gr")
        self.assertEqual(gr.name, "Greek")
        for ch, want in (("\u03b1", [(30, 0)]), ("\u039a", [(37, S)]),
                         ("\u03bb", [(38, 0)]),
                         ("\u20ac", [(6, L3)]), ("\u00b2", [(3, S | L3)]),
                         # the tonos key is dead_acute, and shifted it is
                         # dead_diaeresis: both are two keystrokes
                         ("\u03ad", [(39, 0), (18, 0)]),
                         ("\u03ca", [(39, S), (23, 0)])):
            self.assertEqual(gr.lookup_char(ch), want, ch)

    def test_a_greek_only_layout_cannot_type_latin(self):
        """gr(basic) binds no Latin letter on any level, so `type` has to
        warn and skip them the way it skips any other unreachable
        character."""
        gr = rmap("kde_gr")
        for ch in "abzQ\u00e4":
            self.assertIsNone(gr.lookup_char(ch), ch)

    def test_one_group_means_nothing_to_guess(self):
        for name in ("kde_us", "kde_de", "kde_gr", "kde5_de"):
            self.assertEqual(xkbmap.group_count(text(name)), 1, name)
            self.assertEqual(xkbmap.choose_group(text(name)), (1, True), name)

    def test_three_configured_layouts_are_three_groups_in_order(self):
        """`us, de, fr`, captured with KWin reporting index 2. Three groups in
        the order System Settings lists them, so the bus index and the keymap
        group differ by exactly one past a pair too -- the length at which an
        off-by-one stops being visible as a swap and starts being a layout
        nobody configured."""
        self.assertEqual(xkbmap.parse(text("kde_us_de_fr")).group_names,
                         ["English (US)", "German", "French"])
        fr = xkbmap.build(text("kde_us_de_fr"), 3)
        self.assertEqual(fr.name, "French")
        self.assertEqual(fr.lookup_char("a"), [(16, 0)])   # azerty: US <AD01>

    def test_two_configured_layouts_are_a_guess_from_the_keymap_alone(self):
        """`us, de` in System Settings, switched to German with the layout
        switcher: KWin does not reorder the groups and does not tell an
        unfocused client which one is live, so from the keymap group 1 is a
        guess -- and on this one it is the wrong one. This is where KDE stops
        guessing and asks (TestTheActiveGroupFromKwin, below); the keymap
        text on its own still says exactly what it always said."""
        self.assertEqual(xkbmap.choose_group(text("kde_us_de")), (1, False))
        self.assertTrue(xkbmap.active_group_is_plain_us(text("kde_us_de"), 1))
        self.assertEqual(xkbmap.build(text("kde_us_de"), 2).name, "German")


class TestUsBypassDetection(unittest.TestCase):
    """`active_group_is_plain_us` decides whether any of this runs at all."""

    def test_true_only_for_a_us_group(self):
        cases = {
            ("us", 1): True, ("us", 2): True,
            ("de", 1): False, ("de", 2): True,       # 2 is GNOME's fallback
            ("fr", 1): False, ("fr", 2): True,
            ("es", 1): False, ("gb", 1): False,
            ("dvorak", 1): False, ("dvorak", 2): True,
            ("us_de", 1): True, ("us_de", 2): False, ("us_de", 3): True,
            ("de_fr", 1): False, ("de_fr", 2): False, ("de_fr", 3): True,
            ("noble_de", 1): False, ("noble_de", 2): True,
            ("sway_de", 1): False,
            # KWin's, which have no appended fallback group to be true of
            ("kde_us", 1): True, ("kde_de", 1): False, ("kde_gr", 1): False,
            ("kde5_de", 1): False,
            ("kde_us_de", 1): True, ("kde_us_de", 2): False,
        }
        for (name, group), want in cases.items():
            self.assertEqual(xkbmap.active_group_is_plain_us(text(name), group),
                             want, f"{name} group {group}")

    def test_one_changed_key_is_enough_to_refuse(self):
        """The name is not what is trusted: the keysyms are. A keymap still
        called "English (US)" whose Q key is not q must not be bypassed."""
        us = text("us")
        self.assertTrue(xkbmap.active_group_is_plain_us(us, 1))
        tampered = us.replace("""	key <AD01> {
		symbols[1]= [ 0x71, 0x51 ],""", """	key <AD01> {
		symbols[1]= [ 0x27, 0x22 ],""", 1)
        self.assertNotEqual(tampered, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(tampered, 1))

    def test_a_missing_key_is_enough_to_refuse(self):
        us = text("us")
        gone = us.replace("	key <SPCE> {	[ 0x20 ] };\n", "", 1)
        self.assertNotEqual(gone, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(gone, 1))

    def test_a_dead_key_on_level_one_is_enough_to_refuse(self):
        # us-intl's apostrophe key: same layout name, dead_acute on level 1.
        us = text("us")
        intl = us.replace("symbols[1]= [ 0x27, 0x22 ]",
                          "symbols[1]= [ 0xfe51, 0xfe57 ]", 1)
        self.assertNotEqual(intl, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(intl, 1))

    def test_a_type_whose_level_two_is_not_shift_is_enough_to_refuse(self):
        us = text("us")
        odd = us.replace("""	key <AD01> {
		symbols[1]= [ 0x71, 0x51 ],""", """	key <AD01> {
		type= "PC_ALT_LEVEL2",
		symbols[1]= [ 0x71, 0x51 ],""", 1)
        self.assertNotEqual(odd, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(odd, 1))

    def test_a_renamed_group_is_enough_to_refuse(self):
        us = text("us").replace('name[1]="English (US)"',
                                'name[1]="English (US, intl.)"', 1)
        self.assertFalse(xkbmap.active_group_is_plain_us(us, 1))

    def test_never_raises(self):
        for junk in ("", "{{{{", "xkb_keymap {", "\x00\xff" * 100,
                     text("us")[:5000]):
            self.assertFalse(xkbmap.active_group_is_plain_us(junk, 1))
        self.assertFalse(xkbmap.active_group_is_plain_us(text("us"), 99))


class TestFetchAndSnapshot(unittest.TestCase):
    def test_file_override(self):
        path = os.path.join(KEYMAPS, "de.xkb")
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP=None):
            snap = xkbmap.fetch()
        self.assertEqual(snap.group, 1)
        self.assertFalse(snap.group_known)   # de,us: group 1 is an assumption
        self.assertIn("de.xkb", snap.source)
        self.assertTrue(snap.text.startswith("xkb_keymap"))

    def test_group_override(self):
        path = os.path.join(KEYMAPS, "us_de.xkb")
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="2"):
            snap = xkbmap.fetch()
        self.assertEqual((snap.group, snap.group_known), (2, True))
        self.assertEqual(xkbmap.build(snap.text, snap.group).name, "German")

    def test_bad_group_override_is_ignored(self):
        path = os.path.join(KEYMAPS, "us_de.xkb")
        for bad in ("0", "9", "two", ""):
            with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP=bad):
                self.assertEqual(xkbmap.fetch().group, 1)

    def test_missing_file(self):
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/keymap.xkb"):
            with self.assertRaises(xkbmap.XkbError):
                xkbmap.fetch()

    def test_no_compositor(self):
        with env(WDOTOOL_XKB_KEYMAP=None, WAYLAND_DISPLAY=None,
                 XDG_RUNTIME_DIR="/nonexistent"):
            with self.assertRaises(xkbmap.XkbError):
                xkbmap.fetch()


# ---------------------------------------------------------------------------
# the daemon's typing path


class TestTypingThroughTheLayout(unittest.TestCase):
    def daemon_for(self, name, group=None, mode=None):
        d = make_daemon()
        self._env = env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                        WDOTOOL_XKB_GROUP=group, WDOTOOL_LAYOUT=mode)
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        return d

    def test_german_ascii_is_remapped(self):
        d = self.daemon_for("de")
        warns = d.op_type("yz", 0, False)
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0), (21, 1), (21, 0)])
        # the one-off "which group?" notice, and nothing else
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("German", warns[0])

    def test_german_umlaut_and_altgr(self):
        d = self.daemon_for("de")
        d.op_type("äÄ€", 0, False)
        self.assertEqual(taps(d.kb), [
            (40, 1), (40, 0),                             # ä
            (42, 1), (40, 1), (40, 0), (42, 0),           # shift+ä
            (100, 1), (18, 1), (18, 0), (100, 0),         # AltGr+e
        ])

    def test_dead_key_sequence_is_two_keystrokes(self):
        d = self.daemon_for("fr")
        d.op_type("ô", 0, False)
        self.assertEqual(taps(d.kb),
                         [(26, 1), (26, 0), (24, 1), (24, 0)])

    def test_unreachable_character_warns_and_the_rest_still_types(self):
        d = self.daemon_for("de")
        warns = d.op_type("a漢b", 0, False)
        self.assertEqual(taps(d.kb), [(30, 1), (30, 0), (48, 1), (48, 0)])
        self.assertEqual([w for w in warns if "漢" in w],
                         ["Can't type character '漢' (not on the German layout)."
                          " Skipping."])

    def test_key_sequence_uses_the_layout(self):
        d = self.daemon_for("de")
        d.op_key("ctrl+z", "press", 0, False)
        # xdo_send_keysequence_window releases in press order, not reversed
        self.assertEqual(taps(d.kb),
                         [(29, 1), (21, 1), (29, 0), (21, 0)])

    def test_key_sequence_keeps_position_keys_fixed(self):
        d = self.daemon_for("fr")
        d.op_key("ctrl+Return", "press", 0, False)
        self.assertEqual(taps(d.kb), [(29, 1), (28, 1), (29, 0), (28, 0)])

    def test_key_sequence_needing_altgr(self):
        d = self.daemon_for("de")
        d.op_key("at", "press", 0, False)
        self.assertEqual(taps(d.kb), [(100, 1), (16, 1), (100, 0), (16, 0)])

    def test_unreachable_key_names_the_layout(self):
        d = self.daemon_for("dvorak")
        warns = d.op_key("ssharp", "down", 0, False)
        self.assertEqual(taps(d.kb), [])
        self.assertIn("not reachable on the English (Dvorak) layout", warns[-1])

    def test_the_layout_notice_is_not_doubled_by_a_key_press(self):
        """`key` prints xdo's own diagnostics twice (B12); ours is not one
        of them."""
        d = self.daemon_for("de")
        warns = d.op_key("bogus", "press", 0, False)
        self.assertEqual(len([w for w in warns if "assuming" in w]), 1)
        self.assertEqual(len([w for w in warns if "No such key name" in w]), 2)

    def test_greek_types_and_skips_the_latin_it_cannot_reach(self):
        """Plasma 6.6 with one `gr` source. Measured in Kate: the Greek
        arrives, the Latin does not and says so."""
        d = self.daemon_for("kde_gr")
        warns = d.op_type("\u03b1a\u03b2", 0, False)
        self.assertEqual(taps(d.kb), [(30, 1), (30, 0), (48, 1), (48, 0)])
        self.assertEqual(warns,
                         ["Can't type character 'a' (not on the Greek layout)."
                          " Skipping."])

    def test_a_latin_chord_on_a_greek_layout_says_so_instead_of_guessing(self):
        """`key` used to fall back to the built-in US table's *position* for
        a character the layout cannot make, and say nothing: measured on a
        Greek-only Plasma session, `wdotool key ctrl+s` pressed <AC02>, Kate
        received Ctrl+sigma, nothing was saved, exit 0 and stderr empty --
        while `type s` warned and skipped and `keys explain ctrl+s` called
        the same 's' unreachable. The layout is the authority now, so this is
        the same warning `type` gives, and it is the caller who is told
        rather than the wrong key that is pressed."""
        d = self.daemon_for("kde_gr")
        warns = d.op_key("ctrl+s", "press", 0, False)
        self.assertEqual(taps(d.kb), [(29, 1), (29, 0)])
        self.assertIn("key 's' is not reachable on the Greek layout."
                      " Ignoring it.", warns)

    def test_the_same_chord_lands_when_a_latin_layout_is_configured_too(self):
        """`gr, us` is what a Greek user really has, and pinning the US group
        makes Ctrl+S the real Save again -- the answer to the warning above,
        the same one the group notice gives."""
        d = self.daemon_for("kde_gr")
        warns = d.op_key("ctrl+z", "press", 0, False)      # not on gr either
        self.assertEqual(taps(d.kb), [(29, 1), (29, 0)])
        self.assertIn("not reachable on the Greek layout", warns[-1])
        us = self.daemon_for("kde_us")
        us.op_key("ctrl+s", "press", 0, False)
        self.assertEqual(taps(us.kb), [(29, 1), (31, 1), (29, 0), (31, 0)])

    def test_a_unicode_keysym_still_finds_the_layouts_own_key(self):
        """The character table is what answers, not the keysym table, so
        `key 0x010020ac` (the Unicode spelling of EuroSign) finds the euro
        where the layout really has it -- AltGr+E on German, AltGr+5 on
        Greek -- rather than being called unreachable."""
        de, gr = rmap("kde_de"), rmap("kde_gr")
        self.assertEqual(keymap.resolve_token("0x010020ac", de),
                         de.lookup_char("\u20ac")[0])
        self.assertEqual(keymap.resolve_token("0x010020ac", gr),
                         gr.lookup_char("\u20ac")[0])

    def test_a_unicode_keysym_presses_the_layouts_key_not_the_us_one(self):
        """The keysym form of the same rule, all the way through the daemon:
        `wdotool key 0x01000079` is U+0079, the letter y, and on a German
        board that is <AB01> = 44 -- where a US board has z. 21 (<AB03>) is
        what the built-in table would have pressed, and it is the wrong
        letter, silently: xdotool's own `key 0x01000079` is a keysym lookup
        in the *active* layout too."""
        d = self.daemon_for("de")
        d.op_key("0x01000079", "press", 0, False)
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])
        self.assertNotIn(21, [c for c, _ in taps(d.kb)])

    def test_a_unicode_keysym_the_layout_cannot_reach_warns_and_presses_nothing(self):
        """`key 0x0100004c` is U+004C, capital L, and the Greek layout has no
        Latin letter on any level of any key. The named form (`key L`) has
        warned since the Greek session was measured; the numeric form is the
        same question and must not fall through to the US table's <AB09>,
        which on gr is lambda."""
        d = self.daemon_for("kde_gr")
        warns = d.op_key("0x0100004c", "press", 0, False)
        self.assertEqual(taps(d.kb), [])
        self.assertIn("key '0x0100004c' is not reachable on the Greek layout."
                      " Ignoring it.", warns)

    def test_a_layout_with_no_characters_at_all_falls_back_to_the_table(self):
        """The guard on that rule. `layout.chars` empty is not "this layout
        cannot type anything", it is a ReverseMap that carries no character
        table -- and refusing every keysym there would take the numeric form
        away from a session whose keymap parsed into nothing. So the built-in
        US table answers, exactly as it did before any of this existed."""
        empty = rmap("de")
        empty.chars = {}
        self.assertEqual(keymap.resolve_token("0x01000061", empty),
                         keymap.CHAR_TO_KEY["a"])
        self.assertEqual(keymap.resolve_token("0x01000041", empty), (30, True))

    def test_a_chord_on_kwins_german_moves_with_the_layout(self):
        """Ctrl+Z is the physical <AB01> on a US board and <AB03> on a German
        one; measured in Kate, this is the press that undoes."""
        d = self.daemon_for("kde_de")
        d.op_key("ctrl+z", "press", 0, False)
        self.assertEqual(taps(d.kb), [(29, 1), (21, 1), (29, 0), (21, 0)])

    def test_kwins_two_layout_session_types_us_until_the_group_is_pinned(self):
        """`us, de` switched to German in the layout switcher: group 1 is
        assumed, so 'y' and 'z' come out swapped and the umlauts are
        skipped. WDOTOOL_XKB_GROUP=2 is the documented way out, and it is the
        one that types what was asked for."""
        d = self.daemon_for("kde_us_de")
        warns = d.op_type("\u00fcyz", 0, False)
        self.assertEqual(taps(d.kb), [(21, 1), (21, 0), (44, 1), (44, 0)])
        self.assertIn("Can't type character '\u00fc' (not on the US layout)."
                      " Skipping.", warns)
        d2 = self.daemon_for("kde_us_de", group="2")
        d2.op_type("\u00fcyz", 0, False)
        self.assertEqual(taps(d2.kb), [(26, 1), (26, 0),
                                       (44, 1), (44, 0), (21, 1), (21, 0)])

    def test_group_two_of_a_two_layout_session(self):
        d = self.daemon_for("us_de", group="2")
        warns = d.op_type("z", 0, False)
        self.assertEqual(taps(d.kb), [(21, 1), (21, 0)])
        self.assertEqual(warns, [])  # the group was pinned: nothing to warn about

    def test_group_one_of_the_same_session_is_bypassed(self):
        d = self.daemon_for("us_de", group="1")
        self.assertEqual(taps(d.kb), [])
        d.op_type("z", 0, False)
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])  # US: z is <AB01>
        self.assertIsNone(d._layout_cache[1])             # the bypass took it


class TestCacheAndInvalidation(unittest.TestCase):
    def test_same_keymap_is_reused(self):
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            d = make_daemon()
            first = d._layout()
            self.assertIs(d._layout(), first)
            self.assertIsNotNone(first)

    def test_a_new_group_rebuilds(self):
        """The user switched layout without changing the keymap: same digest,
        different group, so the cached map must not be handed back."""
        path = os.path.join(KEYMAPS, "de_fr.xkb")
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="1",
                 WDOTOOL_LAYOUT=None):
            self.assertEqual(d._layout().name, "German")
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="2",
                 WDOTOOL_LAYOUT=None):
            self.assertEqual(d._layout().name, "French")
        # ... and the third group is plain US, so it bypasses instead
        with env(WDOTOOL_XKB_KEYMAP=path, WDOTOOL_XKB_GROUP="3",
                 WDOTOOL_LAYOUT=None):
            self.assertIsNone(d._layout())

    def test_a_new_keymap_rebuilds(self):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            self.assertEqual(d._layout().name, "German")
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "fr.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            self.assertEqual(d._layout().name, "French")


class TestFallbacks(unittest.TestCase):
    """Every failure lands on the fixed US table, with a warning, never a
    traceback."""

    def type_hello(self, **kw):
        d = make_daemon()
        with env(**kw):
            warns = d.op_type("aA", 0, False)
        return d, warns

    def test_no_compositor(self):
        d, warns = self.type_hello(WDOTOOL_XKB_KEYMAP=None, WAYLAND_DISPLAY=None,
                                   XDG_RUNTIME_DIR="/nonexistent",
                                   WDOTOOL_LAYOUT=None)
        self.assertEqual(taps(d.kb),
                         [(30, 1), (30, 0), (42, 1), (30, 1), (30, 0), (42, 0)])
        self.assertIn("cannot read the compositor's keymap", warns[0])
        self.assertIn("read", d._xkb_said)   # the daemon log says it once

    def test_unparsable_keymap(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".xkb", delete=False) as f:
            f.write("this is not a keymap\n")
        self.addCleanup(os.unlink, f.name)
        d, warns = self.type_hello(WDOTOOL_XKB_KEYMAP=f.name, WDOTOOL_LAYOUT=None)
        self.assertEqual(taps(d.kb),
                         [(30, 1), (30, 0), (42, 1), (30, 1), (30, 0), (42, 0)])
        self.assertIn("build", d._xkb_said)

    def test_a_crash_in_the_new_code_is_survivable(self):
        boom = lambda *a, **k: 1 / 0  # noqa: E731
        self.addCleanup(setattr, xkbmap, "build", xkbmap.build)
        xkbmap.build = boom
        d, warns = self.type_hello(
            WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb"), WDOTOOL_LAYOUT=None)
        self.assertEqual(taps(d.kb),
                         [(30, 1), (30, 0), (42, 1), (30, 1), (30, 0), (42, 0)])

    def test_failure_is_not_retried_on_every_keystroke(self):
        d = make_daemon()
        calls = []
        real = xkbmap.fetch
        self.addCleanup(setattr, xkbmap, "fetch", real)
        xkbmap.fetch = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/x.xkb", WDOTOOL_LAYOUT=None):
            for _ in range(5):
                d.op_type("a", 0, False)
        self.assertEqual(len(calls), 1)


class TestOverride(unittest.TestCase):
    def test_layout_us_never_reads_a_keymap(self):
        d = make_daemon()
        self.addCleanup(setattr, xkbmap, "fetch", xkbmap.fetch)
        xkbmap.fetch = lambda *a, **k: self.fail("fetch() must not be called")
        with env(WDOTOOL_LAYOUT="us",
                 WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb")):
            d.op_type("yz", 0, False)
        self.assertEqual(taps(d.kb), [(21, 1), (21, 0), (44, 1), (44, 0)])

    def test_layout_xkb_forces_the_reverse_map_on_a_us_keymap(self):
        d = make_daemon()
        with env(WDOTOOL_LAYOUT="xkb",
                 WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "us.xkb")):
            layout = d._layout()
        self.assertIsNotNone(layout)          # built, though the bypass applied
        self.assertEqual(layout.name, "English (US)")
        self.assertEqual(layout.lookup_char("a"), [(30, 0)])

    def test_unknown_mode_behaves_like_auto(self):
        d = make_daemon()
        with env(WDOTOOL_LAYOUT="banana",
                 WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "de.xkb")):
            self.assertIsNotNone(d._layout())


# ---------------------------------------------------------------------------
# the safety requirement


class TestUsLayoutNeverReachesTheNewCode(unittest.TestCase):
    """On a plain US layout the reverse map is not built, not consulted, and
    not even importable-in-anger: every entry point is replaced with a mine.

    The bypass check itself must still run -- something has to look at the
    keymap to say "this is US" -- so it is left alone and counted.
    """

    def setUp(self):
        self.calls = []
        self.mined = {}
        for name in ("parse", "reverse", "build"):
            self.mined[name] = getattr(xkbmap, name)

            def mine(*a, _n=name, **k):
                raise AssertionError(f"xkbmap.{_n}() was reached on a US layout")

            setattr(xkbmap, name, mine)
        real_check = xkbmap.active_group_is_plain_us
        self.mined["active_group_is_plain_us"] = real_check

        def counted(text, group=1):
            self.calls.append(group)
            return real_check(text, group)

        xkbmap.active_group_is_plain_us = counted

    def tearDown(self):
        for name, fn in self.mined.items():
            setattr(xkbmap, name, fn)

    def us_daemon(self, fixture="us"):
        d = make_daemon()
        self._e = env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, fixture + ".xkb"),
                      WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None)
        self._e.__enter__()
        self.addCleanup(self._e.__exit__, None, None, None)
        return d

    def test_type_is_byte_identical_to_the_fixed_table(self):
        d = self.us_daemon()
        warns = d.op_type("aA!\n", 0, False)
        self.assertEqual(warns, [])
        self.assertEqual(taps(d.kb), [
            (30, 1), (30, 0),
            (42, 1), (30, 1), (30, 0), (42, 0),
            (42, 1), (2, 1), (2, 0), (42, 0),
            (28, 1), (28, 0),
        ])
        self.assertEqual(self.calls, [1])   # checked once, then cached

    def test_the_unreachable_warning_is_the_old_one(self):
        d = self.us_daemon()
        warns = d.op_type("é", 0, False)
        self.assertEqual(
            warns, ["Can't type character 'é' (not on the US layout). Skipping."])

    def test_key_sequences_use_the_fixed_table(self):
        d = self.us_daemon()
        warns = d.op_key("ctrl+shift+t", "press", 0, False)
        self.assertEqual(warns, [])
        self.assertEqual(taps(d.kb), [(29, 1), (42, 1), (20, 1),
                                      (29, 0), (42, 0), (20, 0)])

    def test_a_two_layout_session_on_its_us_group_is_also_bypassed(self):
        d = self.us_daemon("us_de")
        warns = d.op_type("zZ", 0, False)
        self.assertEqual(taps(d.kb),
                         [(44, 1), (44, 0), (42, 1), (44, 1), (44, 0), (42, 0)])
        # ... and says which group that was (B1). Naming it is a regex scan
        # of the keymap text -- still not the parser, which is mined.
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("assuming 'English (US)'", warns[0])

    def test_the_mines_are_live(self):
        """This class only proves something if the mines would go off."""
        with self.assertRaises(AssertionError):
            xkbmap.build("anything")


class TestOneLayoutDecision(unittest.TestCase):
    """daemon._layout, keys_cmds.Layout.load and the __keymap diagnostic used
    to spell the same two questions three ways, and the diagnostic got one of
    them wrong. They ask xkbmap now."""

    def test_layout_mode_normalizes(self):
        with env(WDOTOOL_LAYOUT=None):
            self.assertEqual(xkbmap.layout_mode(), "auto")
            self.assertEqual(xkbmap.layout_mode("US"), "us")
            self.assertEqual(xkbmap.layout_mode(" fixed "), "us")
            self.assertEqual(xkbmap.layout_mode("XKB"), "xkb")
            self.assertEqual(xkbmap.layout_mode("nonsense"), "auto")

    def test_a_client_layout_flag_outranks_the_environment(self):
        with env(WDOTOOL_LAYOUT="xkb"):
            self.assertEqual(xkbmap.layout_mode(), "xkb")
            self.assertEqual(xkbmap.layout_mode("us"), "us")

    def test_decide_is_the_bypass(self):
        de, us = text("de"), text("us")
        self.assertTrue(xkbmap.decide(de, 1, "us"))    # asked for outright
        self.assertFalse(xkbmap.decide(de, 1, "auto"))  # German is not US
        self.assertTrue(xkbmap.decide(us, 1, "auto"))   # this one is
        self.assertFalse(xkbmap.decide(us, 1, "xkb"))   # asked against


class TestDiagnosticSubcommand(unittest.TestCase):
    def run_it(self, *args, mode=None):
        """`__keymap` takes --keymap/--group as arguments; the environment it
        reads as a fallback is set here and nowhere else."""
        out, err = io.StringIO(), io.StringIO()
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None,
                 WDOTOOL_LAYOUT=mode):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["wdotool", "__keymap", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_dump(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, text("de"))

    def test_info(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"),
                                 "--info")
        self.assertEqual(rc, 0)
        self.assertIn("group 1:      'German' <- active (assumed)", out)
        self.assertIn("us bypass:     no", out)
        self.assertIn("level3=key 100", out)

    def test_info_says_when_the_bypass_applies(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us.xkb"),
                                 "--info")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     yes", out)

    def test_chars(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"),
                                 "--chars", "zé€漢")
        self.assertEqual(rc, 0)
        self.assertIn("'z': key 21", out)
        self.assertIn("'é': key 13 then key 18", out)
        self.assertIn("'€': key 18+level3", out)
        self.assertIn("'漢': unreachable", out)

    def test_chars_on_a_bypassed_session_answers_from_the_fixed_table(self):
        """The diagnostic must not contradict the line above it: on a
        bypassed session wdotool sends the fixed table's keystrokes, so that
        is what --chars has to show (it used to print the reverse map's, and
        `(` came out as the keypad key)."""
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us.xkb"),
                                 "--info", "--chars", "(a")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     yes", out)
        self.assertIn("US bypass is in effect", out)
        self.assertIn("'(': key 10+shift", out)
        self.assertIn("'a': key 30", out)

    def test_forcing_the_reverse_map_is_shown_and_used(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us.xkb"),
                                 "--info", "--chars", "(", mode="xkb")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     no -- WDOTOOL_LAYOUT=xkb overrides it", out)
        self.assertNotIn("US bypass is in effect", out)
        self.assertIn("'(': key 10+shift", out)   # the same key, the long way

    def test_group_option(self):
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "us_de.xkb"),
                                 "--group", "2", "--chars", "z")
        self.assertEqual(rc, 0)
        self.assertIn("'z': key 21", out)

    def test_layout_us_makes_it_agree_with_what_type_sends(self):
        """WDOTOOL_LAYOUT=us is a promise that no layout code runs, and the
        diagnostic used to be the one place that ignored it: on a German
        keymap it named the reverse map's key 21 for 'z' while `type z` sent
        the fixed table's key 44. Both say 44 now."""
        de = os.path.join(KEYMAPS, "de.xkb")
        rc, out, _ = self.run_it("--keymap", de, "--info", "--chars", "z",
                                 mode="us")
        self.assertEqual(rc, 0)
        self.assertIn("us bypass:     yes -- WDOTOOL_LAYOUT=us asks for it", out)
        self.assertIn("US bypass is in effect", out)
        self.assertIn("'z': key 44", out)
        self.assertNotIn("level shifts", out)   # nothing was reversed at all
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=de, WDOTOOL_XKB_GROUP=None,
                 WDOTOOL_LAYOUT="us"):
            d.op_type("z", 0, False)
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])

    def test_the_options_are_arguments_not_exports(self):
        """--keymap/--group reach fetch() as arguments, so a process that
        runs the diagnostic does not find its environment rewritten."""
        out, err = io.StringIO(), io.StringIO()
        with env(WDOTOOL_XKB_KEYMAP="/somewhere/else", WDOTOOL_XKB_GROUP="3",
                 WDOTOOL_LAYOUT=None):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["wdotool", "__keymap", "--keymap",
                               os.path.join(KEYMAPS, "us_de.xkb"),
                               "--group", "2", "--chars", "z"])
            self.assertEqual((rc, os.environ["WDOTOOL_XKB_KEYMAP"],
                              os.environ["WDOTOOL_XKB_GROUP"]),
                             (0, "/somewhere/else", "3"))
        self.assertIn("'z': key 21", out.getvalue())

    def test_the_keymap_is_reversed_once_for_info_and_chars_together(self):
        real = xkbmap.reverse
        calls = []
        self.addCleanup(setattr, xkbmap, "reverse", real)
        xkbmap.reverse = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        rc, out, _ = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"),
                                 "--info", "--chars", "z")
        self.assertEqual((rc, len(calls)), (0, 1))
        self.assertIn("'z': key 21", out)

    def test_help_and_bad_option(self):
        rc, out, _ = self.run_it("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Usage: wdotool __keymap", out)
        rc, _, err = self.run_it("--bogus")
        self.assertEqual(rc, 1)
        self.assertIn("unknown option", err)

    def test_no_compositor_exits_two(self):
        with env(WDOTOOL_XKB_KEYMAP=None, WAYLAND_DISPLAY=None,
                 XDG_RUNTIME_DIR="/nonexistent"):
            rc, _, err = self.run_it()
        self.assertEqual(rc, 2)
        self.assertIn("wdotool:", err)

    def test_a_group_the_keymap_lacks_is_reported_not_raised(self):
        """--group 3 on a two-group keymap: reverse() raises XkbError and
        nothing caught it, so the diagnostic tracebacked -- where every
        other failure in it prints one line and exits 1. Both the --info
        and the --chars call sites."""
        de = os.path.join(KEYMAPS, "de.xkb")
        for extra in (["--info"], ["--chars", "z"],
                      ["--info", "--chars", "z"]):
            rc, out, err = self.run_it("--keymap", de, "--group", "3", *extra)
            self.assertEqual(rc, 1, extra)
            self.assertEqual(err, "wdotool: no group 3 in this keymap (2)\n",
                             extra)
            self.assertNotIn("Traceback", out)

    def test_a_group_the_keymap_has_still_works(self):
        rc, out, err = self.run_it("--keymap", os.path.join(KEYMAPS, "de.xkb"),
                                   "--group", "2", "--info")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("group 2:      'English (US)' <- active", out)

    def test_it_is_not_a_command(self):
        """__keymap is hidden: not in the registry, not in help."""
        from wdotool import commands

        self.assertFalse(commands.is_command("__keymap"))
        self.assertIsNone(commands.lookup("__keymap"))


# ---------------------------------------------------------------------------
# the review's findings, one regression test each


class TestThePinReachesARunningDaemon(unittest.TestCase):
    """The 0.4 retest, on GNOME `us,de`: the notice said "Set
    WDOTOOL_XKB_GROUP=<n> to pin one", and against a daemon that was already
    running that did nothing at all -- the daemon reads its own environment,
    which is the one it was spawned with. `WDOTOOL_XKB_GROUP=2 wdotool type
    yz` printed the notice again and typed `zy`; the same command with the
    daemon killed first printed nothing and typed `yz`. So the pin travels
    with the request, like --layout."""

    def typed(self, name, **extra):
        """One `type` request through handle(), with the *daemon's* own
        environment carrying no pin at all."""
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                 WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            resp = d.handle(dict(op="type", text="z", delay_ms=0, **extra))
        self.assertTrue(resp.get("ok"), resp)
        return d, [w for w in (resp.get("warnings") or []) if "assuming" in w]

    def test_without_a_pin_the_group_is_guessed_and_said(self):
        d, said = self.typed("us_de")
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])      # US z: <AB01>
        self.assertEqual(len(said), 1, said)

    def test_the_requests_pin_is_obeyed_and_silences_the_notice(self):
        for value in (2, "2", " 2 "):
            d, said = self.typed("us_de", xkb_group=value)
            self.assertEqual(taps(d.kb), [(21, 1), (21, 0)],   # German z: <AD11>
                             repr(value))
            self.assertEqual(said, [], repr(value))

    def test_junk_is_not_a_pin_and_types_anyway(self):
        """The variable is deliberately lenient (a typo in a shell profile
        must not stop the tool typing), and the socket is a trust boundary:
        anything that is not a plain 1..4 is simply not a pin."""
        for value in (0, 5, "banana", "", "2x", True, [2], 2.7):
            d, said = self.typed("us_de", xkb_group=value)
            self.assertEqual(taps(d.kb), [(44, 1), (44, 0)], repr(value))
            self.assertEqual(len(said), 1, repr(value))

    def test_one_daemon_serves_pinned_and_unpinned_clients(self):
        """The layout cache is keyed on the guess as well as on the group, or
        the second client gets the first one's answer and its notice."""
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "us_de.xkb"),
                 WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            first = d.handle({"op": "type", "text": "z", "delay_ms": 0})
            second = d.handle({"op": "type", "text": "z", "delay_ms": 0,
                               "xkb_group": 2})
            third = d.handle({"op": "type", "text": "z", "delay_ms": 0})
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0),
                                      (21, 1), (21, 0),
                                      (44, 1), (44, 0)])
        for resp, want in ((first, 1), (second, 0), (third, 1)):
            said = [w for w in (resp.get("warnings") or []) if "assuming" in w]
            self.assertEqual(len(said), want, resp)


class TestTheAssumedGroupIsAnnounced(unittest.TestCase):
    """B1: the "which layout is active?" notice lived on the reverse-map
    path only, so a `us,de` session -- the one case where the bypass is taken
    *and* the group is a guess -- typed US characters and said nothing."""

    def type_on(self, name, group=None, s="z"):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                 WDOTOOL_XKB_GROUP=group, WDOTOOL_LAYOUT=None):
            return d, d.op_type(s, 0, False)

    def test_the_bypass_path_says_which_group_it_assumed(self):
        d, warns = self.type_on("us_de")
        self.assertIsNone(d._layout_cache[1])              # bypassed, as before
        self.assertEqual(taps(d.kb), [(44, 1), (44, 0)])   # US z, as before
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("assuming 'English (US)'", warns[0])
        self.assertIn("WDOTOOL_XKB_GROUP", warns[0])

    def test_a_pinned_group_says_nothing(self):
        for group in ("1", "2"):
            _, warns = self.type_on("us_de", group=group)
            self.assertEqual(warns, [], group)

    def test_a_session_with_nothing_to_guess_says_nothing(self):
        for name in ("us", "sway_de", "us_swapescape"):
            _, warns = self.type_on(name, s="a")
            self.assertEqual([w for w in warns if "assuming" in w], [], name)

    def test_it_is_said_again_when_the_layout_changes(self):
        d = make_daemon()
        said = []
        for name in ("de", "fr", "de"):
            with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                     WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
                per_command = []
                for _ in range(2):        # two commands on the one daemon
                    w = []
                    d._layout(w)
                    per_command.append([x for x in w if "assuming" in x])
                said.append(per_command)
        self.assertEqual([[len(c) for c in cmds] for cmds in said],
                         [[1, 1], [1, 1], [1, 1]])
        self.assertIn("French", said[1][0][0])

    def test_every_command_is_told_though_the_log_says_it_once(self):
        """A guess that is wrong is wrong on every command, so every command
        says so -- the rule _xkb_warn_degraded already followed.

        Measured on Plasma 6.6 with `us, de` configured and German switched
        on: three identical `wdotool type` runs printed the notice once,
        because the second and third hit the layout cache and returned before
        reaching it. A script saw one warning and then typed the wrong
        characters in silence (repro/kde-keys-1-group-guess.sh). The daemon's
        own log is still one line per layout state: it is one fact about the
        session, not news each time."""
        d = make_daemon()
        err = io.StringIO()
        per_command = []
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "kde_us_de.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            with contextlib.redirect_stderr(err):
                for _ in range(3):
                    warns = d.op_type("z", 0, False)
                    per_command.append([w for w in warns if "assuming" in w])
        self.assertEqual([len(c) for c in per_command], [1, 1, 1])
        self.assertIn("assuming 'English (US)'", per_command[2][0])
        self.assertEqual(err.getvalue().count("assuming"), 1)

    def test_group_name_needs_no_parse(self):
        """The notice runs on the bypass path, so it must not drag the
        parser in with it."""
        self.addCleanup(setattr, xkbmap, "parse", xkbmap.parse)
        xkbmap.parse = lambda *a, **k: self.fail("parse() was reached")
        self.assertEqual(xkbmap.group_name(text("us_de"), 2), "German")
        self.assertEqual(xkbmap.group_name(text("de"), 1), "German")
        self.assertEqual(xkbmap.group_name(text("us_de"), 9), "group 9")


class TestUsLayoutsWithOptions(unittest.TestCase):
    """B2: a plain `us` session with keyboard *options* set is still a plain
    US session. Refusing to bypass `caps:swapescape` ran the whole reverse
    map on exactly the setup the safety requirement is about."""

    def test_they_are_bypassed(self):
        for name in ("us_swapescape", "us_grptoggle"):
            self.assertTrue(xkbmap.active_group_is_plain_us(text(name), 1), name)

    def test_they_type_through_the_fixed_table(self):
        for name in ("us_swapescape", "us_grptoggle"):
            d = make_daemon()
            with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, name + ".xkb"),
                     WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
                warns = d.op_type("aA!", 0, False)
            self.assertIsNone(d._layout_cache[1], name)
            self.assertEqual(warns, [], name)
            self.assertEqual(taps(d.kb), [
                (30, 1), (30, 0),
                (42, 1), (30, 1), (30, 0), (42, 0),
                (42, 1), (2, 1), (2, 0), (42, 0)], name)

    def test_the_position_keys_are_not_part_of_the_check(self):
        """<CAPS> carrying Escape is what used to fail it. Escape, Return,
        Tab, BackSpace and Delete are in the same place on every layout and
        go through KEYSYM_KEYS, not through any layout table."""
        want = xkbmap._expected_us()
        for code in (1, 14, 15, 28, 111):   # Esc BackSpace Tab Return Delete
            self.assertNotIn(code, want)
        self.assertEqual(want[30], {1: ord("a"), 2: ord("A")})

    def test_a_type_is_still_checked_where_the_table_shifts_the_key(self):
        us = text("us")
        odd = us.replace('''\tkey <AD01> {
\t\tsymbols[1]= [ 0x71, 0x51 ],''', '''\tkey <AD01> {
\t\ttype= "PC_ALT_LEVEL2",
\t\tsymbols[1]= [ 0x71, 0x51 ],''', 1)
        self.assertNotEqual(odd, us)
        self.assertFalse(xkbmap.active_group_is_plain_us(odd, 1))

    def test_but_not_on_a_key_it_only_ever_presses_unshifted(self):
        """`grp:win_space_toggle` gives <SPCE> the type PC_SUPER_LEVEL2.
        The fixed table presses that key at level 1 and never shifts it, so
        its type is none of our business."""
        us = text("us")
        spce = us.replace("\tkey <SPCE> {\t[ 0x20 ] };",
                          '\tkey <SPCE> {\n\t\ttype= "PC_SUPER_LEVEL2",'
                          "\n\t\tsymbols[1]= [ 0x20 ]\n\t};", 1)
        self.assertNotEqual(spce, us)
        self.assertTrue(xkbmap.active_group_is_plain_us(spce, 1))

    def test_the_macintosh_group_name_is_accepted_but_still_verified(self):
        """A Macintosh keyboard model calls the same layout "USA"."""
        us = text("us").replace('name[1]="English (US)"', 'name[1]="USA"', 1)
        self.assertTrue(xkbmap.active_group_is_plain_us(us, 1))
        de = text("de").replace('name[1]="German"', 'name[1]="USA"', 1)
        self.assertNotEqual(de, text("de"))
        self.assertFalse(xkbmap.active_group_is_plain_us(de, 1))


class TestDeadKeyPlusSpace(unittest.TestCase):
    """B3: <dead_x> <space> types what the Compose table says it types, not
    the spacing accent that shares the dead key's name. `type ´` on a German
    layout used to land an apostrophe, silently."""

    # /usr/share/X11/locale/en_US.UTF-8/Compose, the table every toolkit
    # implements. These are the two rules it gives for a dead key that is
    # not followed by a letter.
    COMPOSE_SPACE = {
        0xFE50: "`", 0xFE51: "\u0027", 0xFE52: "^", 0xFE53: "~", 0xFE54: "\u00af",
        0xFE55: "\u02d8", 0xFE56: "\u02d9", 0xFE57: '"', 0xFE58: "\u00b0",
        0xFE59: "\u02dd", 0xFE5A: "\u02c7", 0xFE5B: "\u00b8", 0xFE5C: "\u02db",
    }
    COMPOSE_DOUBLE = {0xFE50: "`", 0xFE51: "\u00b4", 0xFE52: "^", 0xFE53: "~",
                      0xFE57: "\u00a8", 0xFE58: "\u00b0"}

    def test_the_tables_agree_with_compose(self):
        for ks, ch in self.COMPOSE_SPACE.items():
            self.assertEqual(chr(xkbmap.DEAD_KEYSYMS[ks][1]), ch, hex(ks))
        self.assertEqual({k: chr(v) for k, v in xkbmap.DEAD_DOUBLE.items()},
                         self.COMPOSE_DOUBLE)

    def test_a_spacing_accent_is_the_dead_key_twice(self):
        de = rmap("de")
        self.assertEqual(de.lookup_char("\u00b4"), [(13, 0), (13, 0)])
        self.assertEqual(de.lookup_char("\u00a8"),
                         [(26, xkbmap.MOD_LEVEL3)] * 2)

    def test_and_a_dead_key_plus_space_where_compose_says_space(self):
        self.assertEqual(rmap("de").lookup_char("^"), [(41, 0), (57, 0)])

    def test_a_character_that_is_on_a_key_is_still_one_keystroke(self):
        de = rmap("de")
        self.assertEqual(de.lookup_char("\u00b0"), [(41, xkbmap.MOD_SHIFT)])
        self.assertEqual(de.lookup_char("~"), [(27, xkbmap.MOD_LEVEL3)])
        self.assertEqual(de.lookup_char("'"), [(43, xkbmap.MOD_SHIFT)])


class TestCommentsInAKeymap(unittest.TestCase):
    """B4: comments are keymap syntax. A `}` inside one closed a block early
    and `build()` then *succeeded*, with a quarter of the characters."""

    def with_stray_brace(self, name, comment):
        t = text(name)
        marked = t.replace("\tkey <AD01> {", comment + "\n\tkey <AD01> {", 1)
        self.assertNotEqual(marked, t)
        return marked

    def test_a_brace_in_a_line_comment_no_longer_halves_the_keymap(self):
        clean = xkbmap.build(text("de"), 1)
        marked = xkbmap.build(self.with_stray_brace("de", "\t// stray } brace"), 1)
        self.assertEqual(len(marked.chars), len(clean.chars))
        self.assertEqual(marked.lookup_char("\u00e4"), [(40, 0)])
        self.assertEqual(marked.lookup_char("@"), [(16, xkbmap.MOD_LEVEL3)])

    def test_a_block_comment_too(self):
        marked = self.with_stray_brace("de", "\t/* } and\n\t   } again */")
        self.assertEqual(xkbmap.build(marked, 1).lookup_char("\u00e4"), [(40, 0)])

    def test_the_bypass_reads_them_as_comments_too(self):
        self.assertTrue(xkbmap.active_group_is_plain_us(
            self.with_stray_brace("us", "\t// } "), 1))

    def test_a_string_is_not_a_comment(self):
        kept = xkbmap.strip_comments('name[1]="a//b"; // dropped')
        self.assertIn('"a//b"', kept)
        self.assertNotIn("dropped", kept)
        untouched = "no comment characters here"
        self.assertIs(xkbmap.strip_comments(untouched), untouched)


class TestTheModifiersWait(unittest.TestCase):
    """B5: the 80 ms wait for a wl_keyboard.modifiers event Mutter never
    sends an unfocused client was paid on every command of a plain US GNOME
    session, because it was switched off by the wrong condition -- GNOME
    compiles a lone `us` source as two identical groups, which makes the
    group *known* and left the wait switched on for ever."""

    def waits(self, mods_seen, name="us"):
        d = make_daemon()
        seen = []
        body = text(name)

        def fake_fetch(timeout=2.0, mods_wait=0.08, keymap=None, group=None):
            seen.append(mods_wait)
            return xkbmap.Snapshot(body, 1, "test", True, mods_seen)

        self.addCleanup(setattr, xkbmap, "fetch", xkbmap.fetch)
        xkbmap.fetch = fake_fetch
        with env(WDOTOOL_LAYOUT=None, WDOTOOL_XKB_KEYMAP=None,
                 WDOTOOL_XKB_GROUP=None):
            d._layout()
            d._layout()
        return seen

    def test_a_compositor_that_sends_no_modifiers_event_is_waited_for_once(self):
        self.assertEqual(self.waits(False), [0.08, 0.0])

    def test_the_regression_itself(self):
        # us.xkb is two identical groups, so the group is "known" ...
        self.assertEqual(xkbmap.choose_group(text("us")), (1, True))
        # ... and the wait must be switched off all the same.
        self.assertEqual(self.waits(False, "us")[1], 0.0)

    def test_the_wait_stays_while_the_event_does_arrive(self):
        self.assertEqual(self.waits(True), [0.08, 0.08])

    def test_a_pinned_group_never_waits(self):
        seen = []

        def fake_wayland(timeout, mods_wait):
            seen.append(mods_wait)
            return text("us_de"), None, False

        self.addCleanup(setattr, xkbmap, "_fetch_wayland", xkbmap._fetch_wayland)
        xkbmap._fetch_wayland = fake_wayland
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP="2"):
            snap = xkbmap.fetch()
        self.assertEqual((snap.group, snap.group_known, seen), (2, True, [0.0]))
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None):
            xkbmap.fetch()
        self.assertEqual(seen[-1], 0.08)   # not pinned: the wait is paid


class TestTheKeypadIsNeverTheAnswer(unittest.TestCase):
    """B6: excluding the keypad by key *name* leaked. Mutter puts KP_Decimal
    on <I129> and the keypad parentheses on <I187>/<I188>, so `(` resolved to
    evdev 179 on every non-US layout and French `.` to 121 -- keys the layout
    does not intend for those characters."""

    KEYPAD_CODES = frozenset(
        {55, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 96, 98,
         117, 121, 179, 180})

    def test_no_character_of_any_fixture_resolves_to_a_keypad_key(self):
        for name in FIXTURES:
            body = text(name)
            for group in range(1, xkbmap.group_count(body) + 1):
                for ch, (code, _) in xkbmap.build(body, group).chars.items():
                    self.assertNotIn(code, self.KEYPAD_CODES,
                                     f"{name} group {group}: {ch!r}")
                    self.assertLess(code, 128, f"{name} group {group}: {ch!r}")

    def test_the_characters_the_leak_produced(self):
        S = xkbmap.MOD_SHIFT
        self.assertEqual(rmap("de").lookup_char("("), [(9, S)])
        self.assertEqual(rmap("de").lookup_char(")"), [(10, S)])
        self.assertEqual(rmap("es").lookup_char("("), [(9, S)])
        self.assertEqual(rmap("fr").lookup_char("("), [(6, 0)])
        self.assertEqual(rmap("fr").lookup_char("."), [(51, S)])
        self.assertEqual(rmap("de").lookup_char("."), [(52, 0)])

    def test_the_keypad_is_demoted_not_deleted(self):
        """`key KP_Add` must still find the keypad key."""
        self.assertEqual(rmap("de").keysym_entry("KP_Add"), (78, 0))
        self.assertEqual(rmap("fr").keysym_entry("KP_Decimal"), (121, 0))

    def test_the_rank_covers_both_shapes(self):
        self.assertEqual(xkbmap._keypad_rank(78, 0xFFAB), 1)   # KP_Add
        self.assertEqual(xkbmap._keypad_rank(121, 0xFFAE), 1)  # KP_Decimal
        self.assertEqual(xkbmap._keypad_rank(179, 0x28), 1)    # <I187>, parenleft
        self.assertEqual(xkbmap._keypad_rank(9, 0x28), 0)      # the 8 key


class TestADegradedSessionKeepsSayingSo(unittest.TestCase):
    """Falling back to the fixed US table under a non-US layout types the
    wrong characters. Telling only whichever client happened to ask first
    tells nobody: every command that types through the fallback says so."""

    def test_every_client_is_warned_while_the_keymap_cannot_be_read(self):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/x.xkb", WDOTOOL_LAYOUT=None):
            first = d.op_type("a", 0, False)
            second = d.op_type("a", 0, False)   # inside the 5 s backoff
        self.assertEqual(len(first), 1, first)
        self.assertIn("cannot read the compositor's keymap", first[0])
        self.assertEqual(first, second)

    def test_an_unusable_keymap_warns_every_client_too(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".xkb", delete=False) as f:
            f.write("this is not a keymap\n")
        self.addCleanup(os.unlink, f.name)
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP=f.name, WDOTOOL_LAYOUT=None):
            first = d.op_type("a", 0, False)
            second = d.op_type("a", 0, False)   # served from the cache
        self.assertIn("cannot use the compositor's keymap", first[0])
        self.assertEqual(first, second)

    def test_and_stops_when_the_keymap_can_be_read_again(self):
        d = make_daemon()
        with env(WDOTOOL_XKB_KEYMAP="/nonexistent/x.xkb", WDOTOOL_LAYOUT=None):
            self.assertTrue(d.op_type("a", 0, False))
        d._xkb_backoff = 0.0                     # the compositor came back
        with env(WDOTOOL_XKB_KEYMAP=os.path.join(KEYMAPS, "us.xkb"),
                 WDOTOOL_LAYOUT=None, WDOTOOL_XKB_GROUP=None):
            self.assertEqual(d.op_type("a", 0, False), [])



class GroupCountIsBounded(unittest.TestCase):
    """The group index is a number in the keymap, not a length we measured:
    `symbols[Group2000000000]` is eight bytes of text that used to become two
    billion Group objects (3.2 GB at five million). The compositor is not
    always ours -- a root daemon can be pointed at a planted Wayland socket."""

    def test_a_huge_group_index_is_clamped(self):
        base = "xkb_symbols { key <AE01> { [ a ] }; };"
        for n in (5, 1000, 5_000_000, 2_000_000_000):
            text = base.replace("[ a ]", "symbols[Group%d] = [ a ]" % n)
            self.assertEqual(xkbmap.group_count(text), xkbmap.MAX_GROUPS)
        self.assertEqual(xkbmap.MAX_GROUPS, 4)      # libxkbcommon's maximum

    def test_a_real_keymap_is_unchanged(self):
        self.assertEqual(xkbmap.group_count(text("sway_de")), 1)
        self.assertEqual(xkbmap.group_count(text("de")), 2)
        self.assertEqual(len(xkbmap.parse(text("us_de")).groups), 3)


# ---------------------------------------------------------------------------
# KDE: the active layout, asked of KWin


class _FakeService:
    """A service on the MockBus, on a thread of its own: own a well-known
    name, record every call that arrives, answer what the test says."""

    def __init__(self, address, name):
        self.bus = Bus(address)
        self.bus.serve_calls = True
        self.name = name
        self.calls = []            # (path, interface, member) as received
        assert self.bus.request_name(name) == 1
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def dispatch(self, m):
        raise DBusError(ERR + "UnknownObject", "no object at %s" % m.path)

    def _serve(self):
        try:
            for m in self.bus.messages(None):
                if m.type != dbus_mini.METHOD_CALL:
                    continue
                self.calls.append((m.path, m.interface, m.member))
                try:
                    sig, out = self.dispatch(m)
                except DBusError as e:
                    self.bus.error_reply(m, e.name, e.message)
                    continue
                if sig is None:
                    continue       # silence: the caller waits out its timeout
                self.bus.reply(m, sig, out)
        except Exception:          # noqa: BLE001 -- the socket, shut down by close()
            pass

    def close(self, mock=None):
        if self.bus.sock is None:
            return
        unique = self.bus.unique_name
        try:
            self.bus.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(3)
        self.bus.close()
        if mock is not None and not mock.wait_dropped(unique):
            raise AssertionError("the mock bus still holds %s" % unique)


class KwinLayoutsService(_FakeService):
    """KWin's `/Layouts`, as measured on Plasma 6.6.6 and 5.27.12: getLayout
    answers `u` with a 0-based index into getLayoutsList, whose long names are
    the keymap's group names in the keymap's order.

    `answer` bends that: "u" is KWin, "s" is an interface that answers with
    something else entirely, "none" is one that never answers at all."""

    def __init__(self, address, index=0, answer="u", error=None,
                 layouts=(("us", "", "English (US)"), ("de", "", "German"))):
        self.index = index
        self.answer = answer
        self.error = error
        self.layouts = [tuple(x) for x in layouts]
        self.answers = 0           # replies actually sent
        _FakeService.__init__(self, address, xkbmap.KWIN_BUS_NAME)

    def dispatch(self, m):
        if m.path != xkbmap.KWIN_LAYOUTS_PATH or m.interface != xkbmap.KWIN_LAYOUTS_IFACE:
            return _FakeService.dispatch(self, m)
        if m.member == "getLayout":
            if self.error:
                raise DBusError(self.error, "no")
            if self.answer == "none":
                return None, None
            self.answers += 1
            if self.answer == "s":
                return "s", ("the second one",)
            return "u", (self.index,)
        if m.member == "getLayoutsList":
            return "a(sss)", (self.layouts,)
        if m.member == "setLayout":
            self.index = m.args()[0]
            return "b", (True,)
        raise DBusError(ERR + "UnknownMethod", "no %s" % m.member)


class KdedLandmine(_FakeService):
    """`org.kde.kded6 /modules/keyboard` declares org.kde.KeyboardLayouts too,
    and getLayout on it CRASHES kded -- measured on both generations, every
    call answering NoReply with the bus name changing owner afterwards. It is
    on the bus in every test here so that a call to it would be recorded; the
    assertion is that nothing ever reaches it."""

    def __init__(self, address):
        _FakeService.__init__(self, address, "org.kde.kded6")


def fake_wayland(name, group=None, mods=False):
    """A stand-in for _fetch_wayland: the fixture's keymap, and the silence an
    unfocused client really gets from KWin (no group, no modifiers event)."""
    body = text(name)

    def _fetch(timeout, mods_wait):
        return body, group, mods

    return _fetch


# `wdotool type 'yz@'` -- the string from the 0.4 retest, whose three
# characters all move between the two layouts. Group 1 is `us` (the bypass
# takes it, so these are the built-in table's keycodes) and group 2 is `de`.
US_TAPS = [(21, 1), (21, 0), (44, 1), (44, 0), (42, 1), (3, 1), (3, 0), (42, 0)]
DE_TAPS = [(44, 1), (44, 0), (21, 1), (21, 0), (100, 1), (16, 1), (16, 0), (100, 0)]


class TestTheActiveGroupFromKwin(unittest.TestCase):
    """KWin publishes what wl_keyboard will not tell an injector: which of the
    configured layouts is live (`org.kde.KWin` `/Layouts`
    `org.kde.KeyboardLayouts.getLayout`, a 0-based index into the configured
    list, which is the keymap's group order).

    Four things can happen to that call -- it answers, it is not there, it
    answers nonsense, it changes between two commands -- and every one of them
    but the first has to leave typing exactly as it was before this existed.
    """

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.addCleanup(setattr, xkbmap, "_kwin", xkbmap._kwin)
        self.addCleanup(setattr, xkbmap, "KWIN_TIMEOUT", xkbmap.KWIN_TIMEOUT)
        self.addCleanup(setattr, xkbmap, "_fetch_wayland", xkbmap._fetch_wayland)
        self.kded = KdedLandmine(self.mock.address)
        self.addCleanup(self.kded.close, self.mock)
        self.addCleanup(self.assertEqual, [], self.kded.calls,
                        "kded was called: getLayout crashes it")
        self.reader = xkbmap._kwin = xkbmap.KwinLayouts(self.mock.address)

    def kwin(self, **kw):
        svc = KwinLayoutsService(self.mock.address, **kw)
        self.addCleanup(svc.close, self.mock)
        return svc

    def snapshot(self, name):
        xkbmap._fetch_wayland = fake_wayland(name)
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            return xkbmap.fetch()

    def typed(self, name, daemon_=None, group=None):
        """`wdotool type 'yz@'` through the daemon's own layout path."""
        d = daemon_ if daemon_ is not None else make_daemon()
        xkbmap._fetch_wayland = fake_wayland(name)
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=group, WDOTOOL_LAYOUT=None):
            warns = d.op_type("yz@", 0, False)
        return d, warns

    # -- it answers

    def test_the_index_is_the_group(self):
        """0-based on the bus, 1-based in the keymap, and nothing else to
        translate: the configured list and the group list are the same list."""
        svc = self.kwin(index=0)
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 1)
        svc.index = 1
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 2)
        self.assertEqual(svc.answers, 2)

    def test_the_third_of_three_layouts(self):
        """The fixture is this session, byte for byte: `us, de, fr` with KWin
        answering 2. French `a` is on the key US calls `q`, so the keystroke
        says which group was really used."""
        self.kwin(index=2, layouts=(("us", "", "English (US)"),
                                    ("de", "", "German"),
                                    ("fr", "", "French")))
        snap = self.snapshot("kde_us_de_fr")
        self.assertEqual((snap.group, snap.group_known), (3, True))
        d = make_daemon()
        xkbmap._fetch_wayland = fake_wayland("kde_us_de_fr")
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            self.assertEqual(d.op_type("a", 0, False), [])
        self.assertEqual(taps(d.kb), [(16, 1), (16, 0)])

    def test_fetch_knows_the_group_and_says_where_it_came_from(self):
        self.kwin(index=1)
        snap = self.snapshot("kde_us_de")
        self.assertEqual((snap.group, snap.group_known, snap.source),
                         (2, True, "wayland + kwin"))
        self.assertEqual(xkbmap.build(snap.text, snap.group).name, "German")

    def test_explain_reports_the_real_group_rather_than_an_assumption(self):
        self.kwin(index=1)
        xkbmap._fetch_wayland = fake_wayland("kde_us_de")
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            line = keys_cmds.Layout.load().describe()[0]
        self.assertEqual(line, "layout: German -- group 2 of 2, from wayland + kwin")
        self.assertNotIn("assumed", line)

    def test_the_measured_defect(self):
        """The 0.4 retest, in one test: `us,de` switched to German, `wdotool
        type 'yz@'`. It arrived as `zy\"` because group 1 was assumed; with
        KWin asked, the keystrokes are the German ones -- and the notice that
        said which layout had been assumed says nothing, because nothing is."""
        svc = self.kwin(index=1)
        d, warns = self.typed("kde_us_de")
        self.assertEqual(taps(d.kb), DE_TAPS)
        self.assertEqual(warns, [])
        svc.index = 0                       # the user switches back to US
        d2, warns2 = self.typed("kde_us_de")
        self.assertEqual(taps(d2.kb), US_TAPS)
        self.assertEqual(warns2, [])

    def test_a_switch_between_two_commands_on_one_daemon(self):
        """The daemon caches the layout by (keymap, group), and the group is
        re-read on every command: a user switching layouts between two
        `wdotool type`s gets two different keymaps out of one daemon."""
        svc = self.kwin(index=0)
        d, _ = self.typed("kde_us_de")
        self.assertEqual(taps(d.kb), US_TAPS)
        d.kb.events.clear()
        svc.index = 1                       # Meta+Alt+K, as a user does it
        _, warns = self.typed("kde_us_de", daemon_=d)
        self.assertEqual(taps(d.kb), DE_TAPS)
        self.assertEqual(warns, [])

    # -- it is not there

    def test_no_kwin_on_the_bus_leaves_todays_behaviour_exactly(self):
        """A GNOME or wlroots session: nothing owns org.kde.KWin, so the group
        is the guess it always was, the notice is printed, and the negative is
        remembered rather than re-dialled on every keystroke."""
        snap = self.snapshot("kde_us_de")
        self.assertEqual((snap.group, snap.group_known, snap.source), (1, False, "wayland"))
        self.assertTrue(self.reader.absent)
        self.reader._connect = lambda: self.fail("dialled the bus a second time")
        d, warns = self.typed("kde_us_de")
        self.assertEqual(taps(d.kb), US_TAPS)
        self.assertEqual(len(warns), 1)
        self.assertIn("assuming 'English (US)'", warns[0])

    def test_an_old_kwin_without_the_object_is_not_asked_twice(self):
        """Plasma before /Layouts existed: the object is missing, which is a
        permanent no rather than a transient one."""
        svc = self.kwin(error=ERR + "UnknownObject")
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        self.assertEqual(len(svc.calls), 1)
        self.assertTrue(self.reader.absent)

    def test_a_kwin_that_will_not_answer_is_waited_for_once(self):
        xkbmap.KWIN_TIMEOUT = 0.2
        self.kwin(answer="none")
        t0 = time.monotonic()
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        first = time.monotonic() - t0
        self.assertGreaterEqual(first, 0.2)      # it really waited...
        t0 = time.monotonic()
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        self.assertLess(time.monotonic() - t0, 0.1)   # ...and then backed off
        self.assertFalse(self.reader.absent)     # a wedge is not an absence

    # -- it answers nonsense

    def test_an_index_the_keymap_cannot_hold_is_not_an_answer(self):
        """A layout list edited between the keymap read and the call, or a
        compositor that is not the one whose keymap we read: clamp to what the
        keymap really has, and where it does not fit, keep the guess."""
        svc = self.kwin(index=4)
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))    # 5 of 2
        svc.index = 1
        self.assertIsNone(xkbmap.kwin_group(text("kde_de")))       # 2 of 1
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 2)  # and it recovers

    def test_an_answer_that_is_not_a_number(self):
        self.kwin(answer="s")
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        self.assertEqual(self.snapshot("kde_us_de").group_known, False)

    def test_an_error_reply_is_a_shrug_not_a_traceback(self):
        self.kwin(error=ERR + "Failed")
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        self.assertFalse(self.reader.absent)     # transient: ask again later

    # -- it goes away and comes back

    def test_a_dead_connection_is_redialled_once(self):
        """KWin restarting takes our connection with it. The next command must
        reconnect rather than type the wrong characters for the rest of the
        session."""
        svc = self.kwin(index=1)
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 2)
        self.reader.bus.sock.close()             # the bus went away under us
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 2)
        self.assertEqual(svc.answers, 2)

    def test_kwin_restarting_and_taking_the_name_back(self):
        svc = self.kwin(index=1)
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 2)
        svc.close(self.mock)
        self.assertIsNone(xkbmap.kwin_group(text("kde_us_de")))
        self.assertFalse(self.reader.absent)     # gone for a moment, not missing
        self.reader.retry_at = 0.0               # (the backoff, not the point here)
        self.kwin(index=0)
        self.assertEqual(xkbmap.kwin_group(text("kde_us_de")), 1)

    # -- and the sessions that must never pay for any of it

    def test_the_bus_is_not_dialled_when_the_group_is_already_known(self):
        """A plain US session, a one-layout KDE session and GNOME's `us,us`
        all know their group from the keymap alone. The reader is not even
        connected for them -- which is also what keeps `--layout us` honest."""
        svc = self.kwin(index=1)
        self.reader._connect = lambda: self.fail("dialled the bus for a group we know")
        for name in ("us", "kde_us", "kde_de", "sway_de"):
            self.assertTrue(self.snapshot(name).group_known, name)
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP="2", WDOTOOL_LAYOUT=None):
            xkbmap._fetch_wayland = fake_wayland("kde_us_de")
            self.assertEqual(xkbmap.fetch().source, "wayland (group pinned)")
        self.assertEqual(svc.calls, [])
        self.assertIsNone(self.reader.bus)

    def test_a_pinned_group_still_beats_kwin(self):
        """WDOTOOL_XKB_GROUP is what people were told to set; a daemon they
        already pinned must not start disagreeing with them."""
        self.kwin(index=1)
        d, warns = self.typed("kde_us_de", group="1")
        self.assertEqual(taps(d.kb), US_TAPS)
        self.assertEqual(warns, [])



# ---------------------------------------------------------------------------
# GNOME: the active layout, read from the setting the shell keeps


class PortalService(_FakeService):
    """`org.freedesktop.portal.Desktop` answering ReadAll for one namespace,
    as measured on GNOME 46.0 and 50.1: a{sa{sv}} of the whole namespace.

    `settings` is the namespace's contents as plain Python; `answer` bends it
    -- "s" is a portal that replies with something else entirely, "none" one
    that never replies at all."""

    NS = xkbmap.GNOME_SCHEMA

    def __init__(self, address, sources=(("xkb", "us"), ("xkb", "de")),
                 mru=(), per_window=False, answer="ok", error=None):
        self.sources = [tuple(s) for s in sources]
        self.mru = [tuple(s) for s in mru]
        self.per_window = per_window
        self.answer = answer
        self.error = error
        self.answers = 0
        _FakeService.__init__(self, address, xkbmap.PORTAL_BUS_NAME)

    def switch_to(self, i):
        """What the shell writes when the user picks a layout: the chosen
        source moves to the head of mru-sources."""
        self.mru = [self.sources[i]] + [s for s in self.sources if s != self.sources[i]]

    def values(self):
        return {"sources": Variant("a(ss)", self.sources),
                "mru-sources": Variant("a(ss)", self.mru),
                "per-window": Variant("b", self.per_window),
                "current": Variant("u", 0),      # deprecated and ignored
                "xkb-options": Variant("as", [])}

    def dispatch(self, m):
        if m.path != xkbmap.PORTAL_PATH or m.interface != xkbmap.PORTAL_IFACE:
            return _FakeService.dispatch(self, m)
        if m.member != "ReadAll":
            raise DBusError(ERR + "UnknownMethod", "no %s" % m.member)
        if self.error:
            raise DBusError(self.error, "no")
        if self.answer == "none":
            return None, None
        self.answers += 1
        if self.answer == "s":
            return "s", ("a layout, probably",)
        return "a{sa{sv}}", ({self.NS: self.values()},)


def symbols_groups(body: str) -> list:
    """The layout codes an `xkb_symbols` section name lists, in group order.

    libxkbcommon writes the whole configured set into that one name:
    `pc_us_de_2_fr_3_gr_4_inet(evdev)` is us,de,fr,gr and
    `pc_ru_es_2_us_3_inet(evdev)` is ru,es,us -- the bare digits are the
    group numbers of everything after the first. `pc` and the trailing
    `inet(evdev)` are the model and the compat section, not layouts."""
    line = re.search(r'xkb_symbols\s+"([^"]+)"', body).group(1)
    return [tok for tok in line.split("_")
            if tok != "pc" and not tok.isdigit() and "(" not in tok]


class ShellName(_FakeService):
    """`org.gnome.Shell` owning its name and nothing else. The reader checks
    it before it asks the portal anything, so that a KDE or sway box is one
    round trip from a permanent no rather than a portal call per keystroke."""

    def __init__(self, address):
        _FakeService.__init__(self, address, xkbmap.GNOME_BUS_NAME)


class TestTheActiveGroupFromGnome(unittest.TestCase):
    """GNOME publishes what wl_keyboard will not tell an injector, as a
    setting rather than a method: `org.gnome.desktop.input-sources`, served
    over the session bus by xdg-desktop-portal. The head of `mru-sources` is
    the live source, and its index is the keymap group -- with Mutter's
    appended `us` group and its chunking beyond three sources both folded into
    that one rule.

    Everything that can go wrong with it has to leave typing exactly as it was
    before this existed."""

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.addCleanup(setattr, xkbmap, "_gnome", xkbmap._gnome)
        self.addCleanup(setattr, xkbmap, "_kwin", xkbmap._kwin)
        self.addCleanup(setattr, xkbmap, "GNOME_TIMEOUT", xkbmap.GNOME_TIMEOUT)
        self.addCleanup(setattr, xkbmap, "_fetch_wayland", xkbmap._fetch_wayland)
        # KDE's reader shares the one place fetch() asks from; park it.
        xkbmap._kwin = xkbmap.KwinLayouts()
        xkbmap._kwin.absent = True
        self.reader = xkbmap._gnome = xkbmap.GnomeInputSources(self.mock.address)

    def shell(self):
        svc = ShellName(self.mock.address)
        self.addCleanup(svc.close, self.mock)
        return svc

    def portal(self, **kw):
        self.shell()
        svc = PortalService(self.mock.address, **kw)
        self.addCleanup(svc.close, self.mock)
        return svc

    def snapshot(self, name):
        xkbmap._fetch_wayland = fake_wayland(name)
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            return xkbmap.fetch()

    def typed(self, name, daemon_=None, group=None, s="yz@"):
        d = daemon_ if daemon_ is not None else make_daemon()
        xkbmap._fetch_wayland = fake_wayland(name)
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=group, WDOTOOL_LAYOUT=None):
            warns = d.op_type(s, 0, False)
        return d, warns

    # -- the mapping, which is the part most likely to be subtly wrong ------

    def test_the_head_of_mru_sources_is_the_active_source(self):
        svc = self.portal()
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 1)   # never switched
        svc.switch_to(1)
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 2)
        svc.switch_to(0)
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 1)
        self.assertEqual(svc.answers, 3)

    def test_one_source_is_group_one_and_it_is_known(self):
        """The whole reason the notice used to fire on every command of every
        non-US GNOME desktop: Mutter appends its own `us` group, so `de,us`
        in the keymap is one German source, not two sources. Only the setting
        can tell those apart, and it does."""
        self.portal(sources=(("xkb", "de"),))
        snap = self.snapshot("de")
        self.assertEqual((snap.group, snap.group_known), (1, True))
        self.assertEqual(snap.source, "wayland + gnome input-sources")

    def test_beyond_three_sources_mutter_chunks_the_keymap(self):
        """XKB allows four groups and Mutter spends one of them on its own
        `us`, so five sources are compiled three at a time around the one in
        use. `five_es.xkb` is that session captured: `de,fr,gr,ru,es` with
        Spanish picked compiles `ru, es, us`, and Spanish -- index 4 -- is
        group 2. Before this, wdotool assumed group 1 there and typed
        nothing at all."""
        five = [("xkb", n) for n in ("de", "fr", "gr", "ru", "es")]
        svc = self.portal(sources=five)
        self.assertEqual(xkbmap.group_count(text("five_es")), 3)
        self.assertEqual(xkbmap.parse(text("five_es")).group_names,
                         ["Russian", "Spanish", "English (US)"])
        svc.switch_to(4)
        snap = self.snapshot("five_es")
        self.assertEqual((snap.group, snap.group_known), (2, True))
        self.assertEqual(xkbmap.build(snap.text, snap.group).name, "Spanish")

    def test_the_index_within_the_chunk_is_the_group(self):
        """The mapping on its own, over every index of a five-source list:
        0,1,2 are groups 1,2,3 of the chunk `de,fr,gr,us`, and 3,4 are groups
        1,2 of the chunk `ru,es,us`. One source is always group 1, and so is
        a list nothing has switched yet."""
        five = [("xkb", n) for n in ("de", "fr", "gr", "ru", "es")]
        for i, want in enumerate((1, 2, 3, 1, 2)):
            mru = [five[i]] + [s for s in five if s != five[i]]
            self.assertEqual(
                xkbmap._group_of_sources({"sources": five, "mru-sources": mru}),
                want, five[i])
        self.assertEqual(xkbmap._group_of_sources(
            {"sources": five, "mru-sources": []}), 1)
        self.assertEqual(xkbmap._group_of_sources(
            {"sources": [("xkb", "de")], "mru-sources": [("xkb", "de")]}), 1)

    # -- it answers

    def test_the_first_three_sources_agree_under_both_rules(self):
        """`four_us_de_fr_gr.xkb` (xkbcli compile-keymap --layout us,de,fr,gr,
        libxkbcommon 1.13.2) is the shape a four-source GNOME session
        compiles: four groups and no appended `us`, because the user already
        has one. For the first three sources the chunk arithmetic and the
        keymap's own `xkb_symbols` name give the same group, which is what
        the live measurement on 51.beta says must not change."""
        four = [("xkb", n) for n in ("us", "de", "fr", "gr")]
        svc = self.portal(sources=four)
        self.assertEqual(symbols_groups(text("four_us_de_fr_gr")),
                         ["us", "de", "fr", "gr"])
        for i, want in enumerate((1, 2, 3)):
            svc.switch_to(i)
            self.assertEqual(xkbmap.gnome_group(text("four_us_de_fr_gr")), want,
                             four[i])

    def test_five_sources_on_51_beta_still_follow_the_chunk_arithmetic(self):
        """The live refutation, pinned so that a symbols-name guard cannot
        quietly overturn it: measured on GNOME 51.beta with sources
        us,de,fr,gr,es and Spanish picked by Super+Space, `keys explain` said
        "group 2 of 3" and `type 'yz@'` arrived byte-exact.

        The two sessions are not the same five sources and it matters that
        they need not be. `five_es.xkb` was compiled from the session
        keymaps/README.md records for it, de,fr,gr,ru,es, which is what
        produced `xkb_symbols "pc_ru_es_2_us_3_inet(evdev)"` -- three groups,
        ru,es,us, around the one in use. The live 51.beta session was
        us,de,fr,gr,es. Spanish is index 4 in both, so both take the second
        chunk and 4 % 3 + 1 = 2 either way, and es really is group 2 of that
        keymap's three: the arithmetic and the symbols name agree here. That
        is the whole point -- the chunk rule stays the primary route and any
        name-based cross-check is a guard, not a replacement."""
        self.assertEqual(symbols_groups(text("five_es")), ["ru", "es", "us"])
        five = [("xkb", n) for n in ("de", "fr", "gr", "ru", "es")]
        svc = self.portal(sources=five)
        svc.switch_to(4)                                   # es
        self.assertEqual(xkbmap.gnome_group(text("five_es")), 2)
        self.assertEqual(symbols_groups(text("five_es"))[1], "es")

    @unittest.expectedFailure
    def test_the_fourth_source_is_the_fourth_group(self):
        """DEFERRED: fix 37 (xkbmap.py:1399 -- cross-check `i % 3 + 1`
        against the `xkb_symbols` section name's group list and prefer the
        name where it resolves the mru head to a different group), F3.0.

        Four sources fit one keymap, so Mutter does not chunk: `us,de,fr,gr`
        compiles as four groups in that order and Greek is group 4. The
        arithmetic says 3 % 3 + 1 = 1 -- English (US) -- and, because the
        setting answered, says it with `group_known` true and no notice. So
        `wdotool type a` on a Greek desktop presses <AC01> and types a Latin
        `a` that the layout cannot produce, silently. The right answer is
        group 4, where `a` is unreachable and says so."""
        four = [("xkb", n) for n in ("us", "de", "fr", "gr")]
        svc = self.portal(sources=four)
        svc.switch_to(3)                                   # gr
        self.assertEqual(xkbmap.gnome_group(text("four_us_de_fr_gr")), 4)
        d, warns = self.typed("four_us_de_fr_gr", s="a")
        self.assertEqual(taps(d.kb), [])
        self.assertIn("not on the Greek layout", " ".join(warns))

    @unittest.expectedFailure
    def test_a_five_source_chunk_that_starts_at_the_head(self):
        """DEFERRED: fix 37, F3.0. `us,de,fr,gr,ru` with Russian picked
        compiles `ru,us` -- two groups, Russian first -- so Russian is group
        1. The arithmetic says 4 % 3 + 1 = 2, which fits the keymap (it has
        two groups) and so is not clamped away: the answer is English (US)
        with `group_known` true, and nothing warns. The keymap's own
        `xkb_symbols "pc_ru_us_2_inet(evdev)"` names `ru` first and settles
        it."""
        five = [("xkb", n) for n in ("us", "de", "fr", "gr", "ru")]
        svc = self.portal(sources=five)
        svc.switch_to(4)                                   # ru
        self.assertEqual(symbols_groups(text("ru_us")), ["ru", "us"])
        self.assertEqual(xkbmap.gnome_group(text("ru_us")), 1)

    @unittest.expectedFailure
    def test_the_answer_never_names_a_layout_the_head_is_not(self):
        """DEFERRED: fix 37, F3.0. The guard the fix is worth having for,
        stated over every fixture whose `xkb_symbols` name lists its groups:
        whatever the arithmetic says, the group the reader hands back must be
        the position of the mru head's own layout in that list. It holds for
        every capture here today except the four-source one, which is the
        finding."""
        svc = self.portal()
        for name in ("us_de", "de_fr", "five_es", "ru_us", "four_us_de_fr_gr"):
            groups = symbols_groups(text(name))
            for i, layout in enumerate(groups):
                if layout == "us" and i == len(groups) - 1:
                    continue        # Mutter's appended fallback is not a source
                svc.sources = [("xkb", g) for g in groups]
                svc.switch_to(i)
                self.assertEqual(xkbmap.gnome_group(text(name)), i + 1,
                                 (name, layout))

    def test_the_measured_defect(self):
        """`us, de` switched to German with Super+Space, `wdotool type 'yz@'`.
        It arrived as `zy\"` on GNOME 46 and 50 because group 1 was assumed;
        with the setting read, the keystrokes are the German ones -- and the
        notice that named the layout it had assumed says nothing."""
        svc = self.portal()
        svc.switch_to(1)
        d, warns = self.typed("us_de")
        self.assertEqual(taps(d.kb), DE_TAPS)
        self.assertEqual(warns, [])
        svc.switch_to(0)                     # the user switches back
        d2, warns2 = self.typed("us_de")
        self.assertEqual(taps(d2.kb), US_TAPS)
        self.assertEqual(warns2, [])

    def test_the_layout_a_login_restored_is_the_first_commands_layout(self):
        """A session whose mru-sources begins with `de` comes back on German,
        so the first command of a fresh session was already typing the wrong
        characters -- before the user had touched anything."""
        self.portal(mru=(("xkb", "de"), ("xkb", "us")))
        d, warns = self.typed("us_de")
        self.assertEqual(taps(d.kb), DE_TAPS)
        self.assertEqual(warns, [])

    def test_explain_reports_the_real_group_rather_than_an_assumption(self):
        svc = self.portal()
        svc.switch_to(1)
        xkbmap._fetch_wayland = fake_wayland("us_de")
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            line = keys_cmds.Layout.load().describe()[0]
        self.assertEqual(line, "layout: German -- group 2 of 3, from wayland + gnome input-sources")
        self.assertNotIn("assumed", line)

    def test_a_switch_between_two_commands_on_one_daemon(self):
        svc = self.portal()
        d, _ = self.typed("us_de")
        self.assertEqual(taps(d.kb), US_TAPS)
        d.kb.events.clear()
        svc.switch_to(1)                     # Super+Space, as a user does it
        _, warns = self.typed("us_de", daemon_=d)
        self.assertEqual(taps(d.kb), DE_TAPS)
        self.assertEqual(warns, [])

    # -- it is not there

    def test_no_gnome_shell_on_the_bus_leaves_todays_behaviour_exactly(self):
        """A KDE or wlroots session: nothing owns org.gnome.Shell, so the
        group is the guess it always was, the notice is printed, and the
        negative is remembered rather than re-dialled on every keystroke."""
        snap = self.snapshot("us_de")
        self.assertEqual((snap.group, snap.group_known, snap.source), (1, False, "wayland"))
        self.assertTrue(self.reader.absent)
        self.reader._connect = lambda: self.fail("dialled the bus a second time")
        d, warns = self.typed("us_de")
        self.assertEqual(taps(d.kb), US_TAPS)
        self.assertEqual(len(warns), 1)
        self.assertIn("assuming 'English (US)'", warns[0])

    def test_a_gnome_with_no_portal_is_not_asked_twice(self):
        """The shell is there and nothing owns the portal name: a permanent
        no, not a transient one."""
        self.shell()
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertTrue(self.reader.absent)
        self.reader._connect = lambda: self.fail("dialled the bus a second time")
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))

    def test_a_portal_without_the_settings_interface(self):
        svc = self.portal(error=ERR + "UnknownMethod")
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertEqual(len(svc.calls), 1)
        self.assertTrue(self.reader.absent)

    def test_a_portal_that_will_not_answer_is_waited_for_once(self):
        xkbmap.GNOME_TIMEOUT = 0.2
        self.portal(answer="none")
        t0 = time.monotonic()
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)      # it really waited...
        t0 = time.monotonic()
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertLess(time.monotonic() - t0, 0.1)              # ...and then backed off
        self.assertFalse(self.reader.absent)     # a wedge is not an absence

    # -- it answers nonsense, or an answer that describes no one layout

    def test_the_setting_is_absent_or_nonsense(self):
        for d in (None, {}, {"sources": [("xkb", "de")]}, {"mru-sources": []},
                  "", 7, [1, 2],
                  {"sources": "de", "mru-sources": []},
                  {"sources": [], "mru-sources": []},
                  {"sources": [("xkb", "de", "extra")], "mru-sources": []},
                  {"sources": [["xkb"]], "mru-sources": []},
                  {"sources": [("xkb", "us"), ("xkb", "de")], "mru-sources": [7]}):
            self.assertIsNone(xkbmap._group_of_sources(d), d)

    def test_a_portal_answering_something_else_entirely(self):
        self.portal(answer="s")
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertEqual(self.snapshot("us_de").group_known, False)

    def test_a_stale_mru_head_is_not_an_answer(self):
        """The source list was edited under us and the head names a layout
        that is no longer in it."""
        self.portal(mru=(("xkb", "fr"),))
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertEqual(self.snapshot("us_de").group_known, False)

    def test_per_window_layouts_are_refused(self):
        """An ordinary GNOME setting under which the session-wide value
        describes no window in particular -- measured saying German while a
        newly opened window was on US."""
        svc = self.portal(per_window=True)
        svc.switch_to(1)
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        svc.per_window = False
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 2)   # and it recovers

    def test_a_source_that_is_not_an_xkb_layout_is_refused(self):
        """An IBus engine is the one shape here nothing measured, so it is
        left alone rather than guessed at."""
        self.portal(sources=(("xkb", "us"), ("ibus", "anthy")))
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))

    def test_an_index_the_keymap_cannot_hold_is_not_an_answer(self):
        """Between the keymap read and the setting read the user edited the
        list: clamp to what the keymap really has, and where it does not fit,
        keep the guess."""
        svc = self.portal(sources=[("xkb", n) for n in ("de", "fr", "gr")])
        svc.switch_to(2)
        self.assertIsNone(xkbmap.gnome_group(text("de")))        # 3 of 2
        self.assertEqual(xkbmap.gnome_group(text("de_fr")), 3)   # and it fits here

    # -- it goes away and comes back

    def test_a_dead_connection_is_redialled_once(self):
        svc = self.portal()
        svc.switch_to(1)
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 2)
        self.reader.bus.sock.close()             # the bus went away under us
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 2)
        self.assertEqual(svc.answers, 2)

    # -- and the sessions that must never pay for any of it

    def test_the_bus_is_not_dialled_when_the_group_is_already_known(self):
        """The commonest GNOME session there is -- one `us` source, compiled
        as the two groups `us,us` -- knows its group from the keymap, and so
        does a pinned one. Neither opens a bus."""
        svc = self.portal()
        self.reader._connect = lambda: self.fail("dialled the bus for a group we know")
        for name in ("us", "us_swapescape", "us_grptoggle", "sway_de"):
            self.assertTrue(self.snapshot(name).group_known, name)
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP="2", WDOTOOL_LAYOUT=None):
            xkbmap._fetch_wayland = fake_wayland("us_de")
            self.assertEqual(xkbmap.fetch().source, "wayland (group pinned)")
        self.assertEqual(svc.calls, [])
        self.assertIsNone(self.reader.bus)

    def test_a_pinned_group_still_beats_the_setting(self):
        svc = self.portal()
        svc.switch_to(1)
        d, warns = self.typed("us_de", group="1")
        self.assertEqual(taps(d.kb), US_TAPS)
        self.assertEqual(warns, [])

    # -- root, which is where the daemon usually is on GNOME

    def test_the_read_works_from_a_process_that_forks_and_drops(self):
        """GNOME typing always goes through /dev/uinput, so the daemon is root
        under sudo -- and the portal answers the session user only, because it
        identifies its caller by opening /proc/<pid>/root. The read therefore
        happens in a live forked child that has dropped to that user and put
        PR_SET_DUMPABLE back. Dropping to the uid we already are is a no-op,
        so the whole mechanism runs here without root: fork, connect, call,
        JSON home over a pipe."""
        svc = self.portal()
        svc.switch_to(1)
        got = xkbmap._read_all_as(os.geteuid(), self.mock.address, 5.0)
        self.assertEqual(xkbmap._group_of_sources(got), 2)
        self.assertEqual(svc.answers, 1)

    def test_a_child_that_cannot_read_reports_why_rather_than_hanging(self):
        with self.assertRaises(DBusError) as caught:
            xkbmap._read_all_as(os.geteuid(), "unix:path=/nonexistent/bus", 1.0)
        self.assertTrue(caught.exception.name.startswith(ERR))

    def test_a_uid_that_does_not_exist_is_an_error_not_a_wait(self):
        """The drop happens before the connect, so a session uid that has
        gone (a user removed while their session lingers, or a bad SUDO_UID)
        fails in the child and comes home over the pipe -- rather than
        leaving the parent to wait out GNOME_TIMEOUT plus the fork grace on
        every command."""
        self.portal()
        t0 = time.monotonic()
        with self.assertRaises(DBusError) as caught:
            xkbmap._read_all_as(4294967290, self.mock.address, 5.0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn("password database", caught.exception.message)

    def test_the_child_holds_its_pipe_and_nothing_else(self):
        """F3.1, fix 36. fork() hands the reader child the whole descriptor
        table of whatever forked it. On the daemon that is /dev/uinput, the
        Wayland socket, the listening control socket and -- when a command
        forked it -- that command's own session-bus connection.

        Measured in the test runner with the two closerange lines removed
        (`python3 tests/test_xkbmap.py TestTheActiveGroupFromGnome`, this
        test): /proc/self/fd held 12 entries, 9 of them above stdio -- six
        AF_UNIX sockets (the mock bus's listener and the connections of the
        three fake services), the /dev/null sentinel this test opens, the
        reader's own pipe `w`, and the descriptor os.listdir() opened to read
        the directory, which is already gone by the time readlink() reaches
        it. With the fix those 9 are 2: `w` and the listing's own.

        The child then setuid()s to the session user and hands itself to a
        bus that identifies its caller by opening /proc/<pid>, so those are
        root's descriptors reachable from a session-user process, and a dup
        of the listening socket keeps the daemon's socket alive past its
        death.

        Snapshotted where the child is fully set up (in `_set_dumpable`, the
        call between the drop and the connect): its own pipe, plus the
        directory descriptor the listing itself opens. Nothing else -- named
        here by two things the parent deliberately holds open, a /dev/null
        and a bound AF_UNIX socket, neither of which may appear."""
        import json as _json
        svc = self.portal()
        svc.switch_to(1)
        tmp = tempfile.mkdtemp(prefix="wdotool-fds-")
        self.addCleanup(shutil.rmtree, tmp, True)
        report = os.path.join(tmp, "fds.json")

        sentinel = os.open("/dev/null", os.O_RDONLY)
        self.addCleanup(os.close, sentinel)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(os.path.join(tmp, "sock"))
        listener.listen(1)
        # What those two look like through /proc/<pid>/fd, which is how the
        # child would be caught holding them however the numbers fell.
        null_link = os.readlink("/proc/self/fd/%d" % sentinel)
        sock_link = os.readlink("/proc/self/fd/%d" % listener.fileno())

        real = xkbmap._set_dumpable

        def record():
            got = {}
            for name in os.listdir("/proc/self/fd"):
                try:
                    got[name] = os.readlink("/proc/self/fd/" + name)
                except OSError:
                    got[name] = "(gone)"      # the listing's own descriptor
            with open(report, "w") as f:
                f.write(_json.dumps(got))
            real()

        with mock.patch.object(xkbmap, "_set_dumpable", record):
            got = xkbmap._read_all_as(os.geteuid(), self.mock.address, 5.0)
        self.assertEqual(xkbmap._group_of_sources(got), 2)   # and it still read it
        with open(report) as f:
            fds = _json.loads(f.read())

        # 0/1/2 are stdio, which the child keeps on purpose (a traceback out
        # of it has to reach the terminal) and which is why the two sentinels
        # are only looked for above them: a runner whose own stdin is
        # /dev/null would otherwise match on fd 0.
        left = {int(n): v for n, v in fds.items() if int(n) > 2}
        self.assertNotIn(null_link, left.values(), fds)
        self.assertNotIn(sock_link, left.values(), fds)
        # One pipe and one listing dirfd is the whole of the rest.
        self.assertLessEqual(len(left), 2, fds)
        self.assertTrue(any(v.startswith("pipe:") for v in left.values()), fds)

    def test_a_shell_restart_is_a_moment_not_an_absence(self):
        """`org.gnome.Shell` leaves the bus every time the shell restarts
        (Alt+F2 r, or a crash) and comes back seconds later. A reader that
        wrote that down as `absent` would stop asking for the rest of the
        daemon's life and go back to guessing group 1 on a German desktop --
        so the negative is only permanent for a reader that has never had an
        answer, and here it is the ten-second backoff that stands between the
        restart and the next read."""
        shell = self.shell()
        svc = PortalService(self.mock.address)
        self.addCleanup(svc.close, self.mock)
        svc.switch_to(1)
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 2)
        self.assertEqual(self.reader.asked, 1)

        shell.close(self.mock)          # the shell goes...
        self.reader._drop()             # ...and takes our connection with it
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))
        self.assertFalse(self.reader.absent)
        self.assertGreater(self.reader.retry_at, 0.0)
        self.assertEqual(svc.answers, 1)     # the portal was not even asked

        back = ShellName(self.mock.address)  # ...and comes back
        self.addCleanup(back.close, self.mock)
        self.assertIsNone(xkbmap.gnome_group(text("us_de")))   # still backed off
        self.reader.retry_at = 0.0
        self.assertEqual(xkbmap.gnome_group(text("us_de")), 2)
        self.assertEqual(svc.answers, 2)


class PeerCredBus(MockBus):
    """MockBus that records the SO_PEERCRED uid of every client that
    connects, which is how a real dbus-daemon (and the portal behind it)
    knows who is calling.

    Hooked on hello() rather than on _accept(): the credentials are pinned at
    connect() and stay readable for the life of the socket, Hello is the
    first method every dbus_mini client sends, and MockBus calls it on the
    connection's own serving thread while that socket is certainly open. The
    alternative -- a copy of MockBus._accept with a getsockopt inserted --
    would be a second copy of the accept loop in a class that only ever runs
    as root, so it could drift from batch 0's original for months without
    anyone running it."""

    def __init__(self, **kw):
        self.peer_uids = []
        MockBus.__init__(self, **kw)

    def hello(self, conn):
        try:
            _pid, uid, _gid = struct.unpack(
                "3i", conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                           struct.calcsize("3i")))
            self.peer_uids.append(uid)
        except OSError:
            pass
        return MockBus.hello(self, conn)


class TestPeerCredBusSeesTheCaller(unittest.TestCase):
    """PeerCredBus above is only ever driven by the root-only class below, so
    on an ordinary run nothing would notice it breaking -- if batch 0 renames
    MockBus.hello, or _Conn stops carrying `sock`, the drop test would go on
    "passing" as a skip until someone next ran the suite as root in the
    guest. Two lines here keep that from being a silent hole: connect an
    ordinary client and check the uid comes out. Unprivileged that uid is the
    runner's own, which is the whole reason the root cell exists."""

    def test_the_uid_of_an_ordinary_client_is_recorded(self):
        bus = PeerCredBus()
        self.addCleanup(bus.close)
        client = Bus(bus.address)
        self.addCleanup(client.close)
        self.assertRegex(client.unique_name, r"^:1\.\d+$")   # Hello happened
        self.assertEqual(bus.peer_uids, [os.geteuid()])


@unittest.skipUnless(os.geteuid() == 0,
                     "the root cell: run it as root (vm/vmctl ssh -- "
                     "python3 tests/test_xkbmap.py)")
class TestTheForkedReaderReallyDropsToTheSessionUid(unittest.TestCase):
    """The cell every other GNOME test here can only pretend at: the daemon
    is root (typing on GNOME goes through /dev/uinput, so it is under sudo or
    a unit), and the portal answers the *session* user only -- it identifies
    its caller by opening /proc/<pid>/root, which a root process is not.

    Dropping to the uid we already are is a no-op, so unprivileged runs of
    test_the_read_works_from_a_process_that_forks_and_drops above exercise
    the fork, the pipe and the JSON but not one line of the drop. This class
    is the drop: it asks for uid 1000 from uid 0 and checks what the bus saw.

    SO_PEERCRED is pinned at connect(), so the connection has to be made by a
    process that is already uid 1000 -- which is exactly why the child stays
    alive for the call instead of handing a socket back (see _read_all_as).
    """

    UID = 1000

    def setUp(self):
        # 0700 under root would stop uid 1000 at the directory: the socket
        # has to be reachable, which is what `subdir` is for.
        self.mock = PeerCredBus(subdir="reachable")
        self.addCleanup(self.mock.close)
        os.chmod(self.mock.dir, 0o755)
        os.chmod(os.path.dirname(self.mock.path), 0o755)
        os.chmod(self.mock.path, 0o666)
        self.addCleanup(setattr, xkbmap, "_gnome", xkbmap._gnome)
        svc = ShellName(self.mock.address)
        self.addCleanup(svc.close, self.mock)
        self.portal = PortalService(self.mock.address)
        self.addCleanup(self.portal.close, self.mock)

    def child_report(self):
        """Fork the reader with `_set_dumpable` recording what the child is
        by then: its uid, whether /proc/self is its own again, and its
        descriptors."""
        import json as _json
        tmp = tempfile.mkdtemp(prefix="wdotool-root-")
        os.chmod(tmp, 0o777)
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "child.json")
        real = xkbmap._set_dumpable

        def record():
            real()
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            out = {"uid": os.getuid(), "euid": os.geteuid(),
                   "proc_self_uid": os.stat("/proc/self").st_uid,
                   "dumpable": libc.prctl(3, 0, 0, 0, 0),   # PR_GET_DUMPABLE
                   "fds": sorted(int(n) for n in os.listdir("/proc/self/fd"))}
            with open(path, "w") as f:
                f.write(_json.dumps(out))

        with mock.patch.object(xkbmap, "_set_dumpable", record):
            got = xkbmap._read_all_as(self.UID, self.mock.address, 5.0)
        with open(path) as f:
            return got, _json.loads(f.read())

    def test_the_portal_sees_the_session_user_and_the_parent_stays_root(self):
        self.portal.switch_to(1)
        got, child = self.child_report()
        self.assertEqual(xkbmap._group_of_sources(got), 2)
        self.assertIn(self.UID, self.mock.peer_uids)
        self.assertEqual((child["uid"], child["euid"]), (self.UID, self.UID))
        self.assertEqual(os.geteuid(), 0, "the parent is still root")

    def test_the_child_is_dumpable_again_so_the_portal_can_read_it(self):
        """setuid() clears PR_SET_DUMPABLE, and a process that is not
        dumpable has a /proc/<pid> owned by root that only root may open --
        which is precisely what the portal is not. Putting it back is what
        makes the read work at all."""
        _got, child = self.child_report()
        self.assertEqual(child["dumpable"], 1)
        self.assertEqual(child["proc_self_uid"], self.UID)

    def test_the_child_carries_none_of_roots_descriptors(self):
        """F3.1, fix 36, in the cell it matters in: these are root's open
        files in a process that has just become the session user."""
        _got, child = self.child_report()
        self.assertLessEqual(len([fd for fd in child["fds"] if fd > 2]), 2,
                             child["fds"])


# ---------------------------------------------------------------------------
# The three desktops that answer over an IPC of their own, and COSMIC's wire


#: The recorded `hyprctl -j devices` [M recon2/hyprland.md §2], and one keyboard
#: row out of it to build variants from: `layout: "us,de"`,
#: `options: "grp:alt_shift_toggle"`, the shape every row in it has.
HYPR_DEVICES = fixture_json("hypr", "devices.json")
HYPR_KB = HYPR_DEVICES["keyboards"][1]


def hypr_devices(*rows):
    """A `j/devices` answer whose keyboards are these `(name, active_layout_index, main)`, each in the byte
    shape of the recorded physical keyboard's row."""
    kbs = []
    for name, idx, main in rows:
        kb = dict(HYPR_KB)
        kb.update(name=name, active_layout_index=idx, main=main,
                  active_keymap="German" if idx else "English (US)")
        kbs.append(kb)
    return dict(HYPR_DEVICES, keyboards=kbs)


class SwitchingHypr(FakeHypr):
    """`FakeHypr` that lets `switchxkblayout <device> <n>` move that device's group.

    Hyprland keeps XKB state per device and the request is per device, so a double whose `j/devices` never
    changed could not tell a switch that took from one that did not -- which is the whole of the fourth
    rule.  It is a request of its own and NOT a `dispatch`: measured over a raw socket on the arch-hypr
    golden (Hyprland 0.56.2, 2026-09-11), `dispatch switchxkblayout wdotool-virtual-keyboard 1` answers
    `Invalid dispatcher` and changes nothing, while the bare form answers `ok` and the new index is in the
    very next `j/devices`.  `stubborn=True` answers `ok` and moves nothing -- a request accepted and not
    acted on, which is the state the whole route-2 apply in wxrandr/hypr.py exists for and which this
    reader has to survive; whether a name Hyprland does not know answers that way or `Invalid dispatcher`
    was not measured, so the double does not claim it."""

    def __init__(self, mode="ok", stubborn=False, **kw):
        self.stubborn = stubborn
        self.switches = []
        super().__init__(mode, **kw)

    def reply_for(self, req: str) -> bytes:
        if req.startswith("switchxkblayout "):
            name, _, idx = req[len("switchxkblayout "):].strip().rpartition(" ")
            self.switches.append((name, int(idx)))
            if self.mode == "refuse":
                return self.INVALID.encode()   # what the `dispatch` spelling really answered
            if not self.stubborn:
                rows = copy.deepcopy(self.payloads["devices"])
                for kb in rows.get("keyboards", []):
                    if kb.get("name") == name:
                        kb["active_layout_index"] = int(idx)
                        kb["active_keymap"] = "German" if int(idx) else "English (US)"
                self.payloads["devices"] = rows
            return b"ok"
        return super().reply_for(req)


class TestTheFourthRuleOnHyprland(unittest.TestCase):
    """U39b: the group the process that HOLDS /dev/uinput encodes for.

    Hyprland keeps XKB state per device, so "which group is the session in" and "which group will the keys
    this process is about to inject be read in" are two questions, and until this rule they had one answer.
    Measured, resolute-hypr / Hyprland 0.53.3, 2026-09-09, `kb_layout = us,de`: with the physical
    `at-translated-set-2-keyboard` switched to index 1, `j/devices` still reported
    `wdotool-virtual-keyboard` at index 0, `keys explain` correctly said `German -- group 2 of 2` and
    `wdotool type` wrote `zyq Stra-e` for `zy@ Straße` -- `@` encoded as the German AltGr+Q and read in the
    injected device's US group as a plain `q` [M vm/live-smoke.d/hypr.sh layout_phase, xwant at :425; the
    same on 0.56.2 in goal2/recon/gaps.md §1b #13].

    The stand-in for /dev/uinput is `/dev/urandom`: a character device this process can hold open, which is
    what `_holds_uinput` actually tests (st_rdev of every fd in /proc/self/fd against the node's).  The
    negative case asserts the probe is False BEFORE the file is opened, so the two tests differ by the open
    file and nothing else."""

    NODE = "/dev/urandom"

    def setUp(self):
        if not os.path.exists(self.NODE):
            self.skipTest("no %s on this box" % self.NODE)
        self.addCleanup(setattr, xkbmap, "UINPUT_NODE", xkbmap.UINPUT_NODE)
        xkbmap.UINPUT_NODE = self.NODE
        self.assertFalse(xkbmap._holds_uinput(), "%s is already open in this process" % self.NODE)

    def hold(self):
        """This process becomes the injector."""
        fh = open(self.NODE, "rb")
        self.addCleanup(fh.close)
        self.assertTrue(xkbmap._holds_uinput())
        return fh

    def reader(self, mode="ok", **kw):
        srv = SwitchingHypr(mode, **kw)
        self.addCleanup(srv.close)
        self.srv = srv
        return xkbmap.HyprLayouts(srv.path)

    def devices(self, physical=1, ours=0):
        return hypr_devices(("at-translated-set-2-keyboard", physical, False),
                            ("wdotool-virtual-keyboard", ours, True))

    def test_a_process_that_does_not_inject_reads_the_session_and_sends_nothing(self):
        """`keys explain` and every other client: the session is in group 2 and that is the answer, with
        `j/devices` the whole conversation."""
        r = self.reader(payloads={"devices": self.devices()})
        self.assertEqual(r.group(text("kde_us_de")), 2)
        self.assertEqual(self.srv.switches, [])
        self.assertEqual(self.srv.requests, ["j/devices"])

    def test_the_injector_moves_its_own_device_into_the_sessions_group(self):
        """The keys are about to go through `wdotool-virtual-keyboard`, so that device is put in the group
        they are encoded for -- one request (`switchxkblayout`, which is not a dispatcher, see
        `SwitchingHypr`), on our device alone; the session's own keyboard keeps the group the user put it
        in, and nobody else types on ours."""
        self.hold()
        r = self.reader(payloads={"devices": self.devices()})
        self.assertEqual(r.group(text("kde_us_de")), 2)
        self.assertEqual(self.srv.switches, [("wdotool-virtual-keyboard", 1)])
        self.assertEqual(r.switched, 1)
        ours = xkbmap._hypr_our_keyboard(self.srv.payloads["devices"])
        self.assertEqual(ours["active_layout_index"], 1)

    def test_nothing_is_sent_when_the_two_devices_are_already_in_one_group(self):
        self.hold()
        r = self.reader(payloads={"devices": self.devices(physical=1, ours=1)})
        self.assertEqual(r.group(text("kde_us_de")), 2)
        self.assertEqual(self.srv.switches, [])
        self.assertEqual(r.switched, 0)

    def test_a_switch_that_did_not_take_answers_the_group_our_device_is_really_in(self):
        """A group that is really there beats one that is not: encoding for the session's German while the
        injected device sits in US is exactly what typed `zyq`, and US is what wdotool typed correctly with
        before any of these readers existed."""
        self.hold()
        r = self.reader(stubborn=True, payloads={"devices": self.devices()})
        self.assertEqual(r.group(text("kde_us_de")), 1)
        self.assertEqual(self.srv.switches, [("wdotool-virtual-keyboard", 1)])
        self.assertEqual(r.switched, 0)

    def test_a_refused_request_is_not_an_exception_and_not_the_sessions_group(self):
        """Nothing about typing may depend on this working: a Hyprland that refuses the request leaves the
        answer at our device's own index, with no traceback anywhere near `type`.  `Invalid dispatcher` is
        the double's refusal text because it is the one the WRONG spelling really got: `dispatch
        switchxkblayout ...` shipped here first and answered exactly that on the arch-hypr golden, which is
        how the bare form was found."""
        self.hold()
        r = self.reader("refuse", payloads={"devices": self.devices()})
        self.assertEqual(r.group(text("kde_us_de")), 1)
        self.assertEqual(self.srv.switches, [("wdotool-virtual-keyboard", 1)])

    def test_the_probe_follows_the_node_the_daemon_was_told_to_open(self):
        """`WDOTOOL_UINPUT_PATH` is what `wdotool/uinput.py:dev_path()` opens, so it is what this probe
        stats.  A daemon started with the override held a node whose st_rdev matched nothing under
        /dev/uinput, the fourth rule never fired, and the injected device stayed in whatever group it was
        in -- silently typing `zyq` again.  UINPUT_NODE is pointed at a path that is not there, so only the
        override can make this True."""
        xkbmap.UINPUT_NODE = "/dev/w11-no-such-uinput-node"
        self.addCleanup(os.environ.pop, xkbmap.UINPUT_PATH_ENV, None)
        self.assertFalse(xkbmap._holds_uinput())
        os.environ[xkbmap.UINPUT_PATH_ENV] = self.NODE
        self.assertFalse(xkbmap._holds_uinput(), "%s is already open in this process" % self.NODE)
        fh = open(self.NODE, "rb")
        self.addCleanup(fh.close)
        self.assertTrue(xkbmap._holds_uinput())

    def test_an_injector_with_no_device_of_ours_in_the_list_reads_the_session(self):
        """Before the daemon's device is announced -- and on the virtual-keyboard path, which uploads its
        own keymap and never consults this -- there is nothing of ours to move, so the session's group is
        the answer it always was."""
        self.hold()
        r = self.reader(payloads={"devices": hypr_devices(("at-translated-set-2-keyboard", 1, True))})
        self.assertEqual(r.group(text("kde_us_de")), 2)
        self.assertEqual(self.srv.switches, [])


class TestTheActiveGroupFromHyprland(unittest.TestCase):
    """U39: `HyprLayouts` over the recorded `j/devices`.

    Hyprland keeps XKB state per device, and the recording is the trap [M recon2/hyprland.md §2, §3]: four
    keyboards on one `us,de` session, `main: true` on wdotool's own `wdotool-virtual-keyboard` (index 0),
    index 1 on the physical `at-translated-set-2-keyboard`, and a `power-button` that is a keyboard to
    libinput and has never switched a layout. Reading the wrong row is not a cosmetic error: switching the
    *injected* device's layout is what broke typing outright in the VM (`wdotool type 'echo zy > /tmp/C'`
    created no file, the `>` and `/` having moved with the layout), and its index is the one this reader
    must never take.

    `kde_us_de` is the keymap throughout: no Hyprland keymap was recorded, and this is the two-group
    `us, de` in that order, which is what `pc_us_de_2_inet(evdev)` compiles to [M hyprland.md §3]."""

    def reader(self, mode="ok", **kw):
        srv = FakeHypr(mode, **kw)
        self.addCleanup(srv.close)
        self.srv = srv
        return xkbmap.HyprLayouts(srv.path)

    def test_the_physical_keyboards_index_is_the_answer(self):
        """The recorded devices.json, unedited: index 1 on `at-translated-set-2-keyboard` is group 2."""
        self.assertEqual(self.reader().group(text("kde_us_de")), 2)

    def test_the_injected_keyboard_is_never_the_answer(self):
        """It carries `main: true` in the recording, and its index is the one we set ourselves."""
        r = self.reader(payloads={"devices": hypr_devices(
            ("at-translated-set-2-keyboard", 0, False),
            ("wdotool-virtual-keyboard", 1, True),
            ("hl-virtual-keyboard-python3.14", 1, False))})
        self.assertEqual(r.group(text("kde_us_de")), 1)

    def test_main_true_wins_over_the_first_non_virtual_device(self):
        r = self.reader(payloads={"devices": hypr_devices(
            ("at-translated-set-2-keyboard", 0, False),
            ("usb-usb-keyboard", 1, True))})
        self.assertEqual(r.group(text("kde_us_de")), 2)

    def test_the_power_button_does_not_answer_for_the_keyboard_beside_it(self):
        """`power-button` is first in the recorded list and is a keyboard to libinput; it carries the
        session's `us,de` and its own index 0, which is not the session's answer."""
        r = self.reader(payloads={"devices": hypr_devices(
            ("power-button", 0, False), ("at-translated-set-2-keyboard", 1, False))})
        self.assertEqual(r.group(text("kde_us_de")), 2)
        self.assertEqual(xkbmap._hypr_keyboard(hypr_devices(("power-button", 0, False)))["name"],
                         "power-button", "with nothing better, the one row there is answers")

    def test_an_index_past_the_keymaps_last_group_is_not_an_answer(self):
        """Two groups in the keymap we just read and a third layout in the device's list is a layout list
        edited under us; the caller's guess is the better answer, exactly as it is for KWin."""
        r = self.reader(payloads={"devices": hypr_devices(("at-translated-set-2-keyboard", 2, False))})
        self.assertIsNone(r.group(text("kde_us_de")))
        self.assertEqual(r.group(text("kde_us_de_fr")), 3)   # and three groups take it

    def test_the_reader_only_ever_reads(self):
        """The landmine: `switchxkblayout` is a dispatcher this reader must never send -- reading the
        session's layout may not change it. `j/devices` is the whole conversation."""
        r = self.reader()
        r.group(text("kde_us_de"))
        r.group(text("kde_us_de"))
        self.assertEqual(self.srv.requests, ["j/devices", "j/devices"])

    def test_a_socket_that_is_not_there_is_remembered(self):
        calls = []

        def no_socket():
            calls.append(1)
            return None

        r = xkbmap.HyprLayouts()
        with mock.patch.object(w11_session, "find_hypr_socket", no_socket):
            self.assertIsNone(r.group(text("kde_us_de")))
            self.assertIsNone(r.group(text("kde_us_de")))
        self.assertTrue(r.absent)
        self.assertEqual(len(calls), 1, "the second command looked again")

    def test_a_hyprland_that_goes_away_mid_session_backs_off_rather_than_rescanning(self):
        """A socket that was there and is gone is a restart, not "no Hyprland here", so `absent` stays
        false -- and then only the backoff keeps every command of the next few seconds from walking
        $XDG_RUNTIME_DIR again."""
        r = self.reader()
        self.assertEqual(r.group(text("kde_us_de")), 2)
        r.sockpath = None
        with mock.patch.object(w11_session, "find_hypr_socket", lambda: None):
            self.assertIsNone(r.group(text("kde_us_de")))
        self.assertFalse(r.absent)
        self.assertGreater(r.retry_at, 0.0)

    def test_a_wedged_hyprland_is_bounded_by_this_modules_deadline_not_the_backends(self):
        """These readers run from `fetch()`, which the daemon calls holding its lock, so the deadline that
        matters is the one every `type` waits behind. hypr_ipc.IPC_TIMEOUT is 10 s -- the right number for
        a `dispatch` a user is waiting on -- and HYPR_TIMEOUT is the 2 s KWIN_TIMEOUT, GNOME_TIMEOUT and
        CINNAMON_TIMEOUT all carry. Driven at 0.3 s here so the test is not a wait."""
        r = self.reader("wedged")
        with mock.patch.object(xkbmap, "HYPR_TIMEOUT", 0.3):
            started = time.monotonic()
            self.assertIsNone(r.group(text("kde_us_de")))
            waited = time.monotonic() - started
        self.assertLess(waited, 3.0, "hypr_ipc's own 10 s default was used instead (%.1fs)" % waited)
        self.assertGreater(r.retry_at, 0.0)

    def test_a_compositor_that_answers_nothing_leaves_the_guess(self):
        for mode in ("gone", "badjson", "short"):
            r = self.reader(mode)
            self.assertIsNone(r.group(text("kde_us_de")), mode)
            self.assertFalse(r.absent, mode)          # not permanent: it is there, it misbehaved
            self.assertGreater(r.retry_at, 0.0, mode)



#: The recorded `wayfire/get-keyboard-state` of the one-layout session
#: (tests/fixtures/wayfire/), and the two shapes recon recorded around it
#: [M recon2/wayfire.md §2.7].
WF_US = fixture_json("wayfire", "wayfire_get-keyboard-state.json")
WF_US_DE = {"possible-layouts": ["English (US)", "German"],
            "layout": "English (US)", "layout-index": 0}
#: what `wayfire/set-keyboard-state {"layout-index":1}` left behind: the
#: selected layout DUPLICATED, the other one gone until restart
WF_CORRUPT = {"possible-layouts": ["English (US)", "English (US)"],
              "layout": "English (US)", "layout-index": 0}


class WayfireSetLandmine(FakeWayfire):
    """FakeWayfire that records any attempt to WRITE the keyboard state.

    `wayfire/set-keyboard-state` is not a thing this reader is allowed to try: one call recompiled the
    keymap as the selected layout duplicated and the German layout was gone until the compositor restarted
    [M recon2/wayfire.md §2.7]. The double answers it as the live socket would, so a reader that called it
    would pass every other assertion -- `self.forbidden` is what fails the test."""

    def __init__(self, *a, **kw):
        self.forbidden = []
        FakeWayfire.__init__(self, *a, **kw)

    def reply_for(self, method, data):
        if method.endswith("set-keyboard-state"):
            self.forbidden.append(data)
            return {"result": "ok"}
        return FakeWayfire.reply_for(self, method, data)


class WayfireQuotingTheSentence(FakeWayfire):
    """A handler error whose text happens to quote `No such method found!`.

    The point is which field the reader keys on. `backend_wayfire._error_line` sets `.no_method` only for
    the `{"error": "No such method found!", "method": ...}` reply, and a substring test against the
    message would read this one -- a live plugin that raised -- as "the method is not there" and stop
    asking for the rest of the process."""

    def reply_for(self, method, data):
        return {"error": 'Error during execution of the handler for method "%s": No such method found!'
                         % method}


class TestTheActiveGroupFromWayfire(unittest.TestCase):
    """U07: `WayfireLayouts` over the recorded `wayfire/get-keyboard-state`.

    The defect it closes is measured: on `[input] xkb_layout = us,de` Wayfire sends no
    `wl_keyboard.modifiers` before focus, so `choose_group` guessed group 1 -- harmless on the
    virtual-keyboard path (wdotool uploads its own keymap and `zyx` typed correctly) and wrong on the
    uinput path, which types through the compositor's keymap [M recon2/wayfire.md §2.7]."""

    def reader(self, answer=None, cls=WayfireSetLandmine, mode="ok"):
        answers = {xkbmap.WAYFIRE_STATE_METHOD: answer} if answer is not None else None
        srv = cls(mode, answers=answers)
        self.addCleanup(srv.close)
        self.srv = srv
        return xkbmap.WayfireLayouts(srv.path)

    def test_the_three_recorded_states_answer_one_two_one(self):
        # `us` is the two-identical-groups keymap `pc_us_us_2` compiles to, which is what the corrupted
        # session was left holding; `kde_us` is one group and `kde_us_de` is `us, de` in that order.
        cases = ((WF_US, "kde_us", 1),                                  # one layout, index 0
                 (dict(WF_US_DE, **{"layout-index": 1, "layout": "German"}), "kde_us_de", 2),
                 (WF_CORRUPT, "us", 1))                                 # the set-keyboard-state wreckage
        for state, keymap_name, want in cases:
            r = self.reader(state)
            self.assertEqual(r.group(text(keymap_name)), want, state)

    def test_the_recorded_us_de_session_before_any_switch_is_group_one(self):
        self.assertEqual(self.reader(WF_US_DE).group(text("kde_us_de")), 1)

    def test_the_reader_never_writes_the_keyboard_state(self):
        r = self.reader(WF_US_DE)
        for _ in range(3):
            r.group(text("kde_us_de"))
        self.assertEqual(self.srv.forbidden, [])
        self.assertEqual({m for m, _ in self.srv.calls}, {xkbmap.WAYFIRE_STATE_METHOD})

    def test_an_index_past_the_keymaps_last_group_is_not_an_answer(self):
        r = self.reader(dict(WF_US_DE, **{"layout-index": 2}))
        self.assertIsNone(r.group(text("kde_us_de")))

    def test_a_socket_that_is_not_there_is_remembered(self):
        calls = []

        def no_socket():
            calls.append(1)
            return None

        r = xkbmap.WayfireLayouts()
        with mock.patch.object(w11_session, "find_wayfire_socket", no_socket):
            self.assertIsNone(r.group(text("kde_us_de")))
            self.assertIsNone(r.group(text("kde_us_de")))
        self.assertTrue(r.absent)
        self.assertEqual(len(calls), 1, "the second command looked again")

    def test_a_wayfire_that_goes_away_mid_session_backs_off_rather_than_rescanning(self):
        """Same restart as Hyprland's: `absent` stays false, and the backoff is what keeps the next few
        seconds of commands out of $XDG_RUNTIME_DIR."""
        r = self.reader(WF_US_DE)
        self.assertEqual(r.group(text("kde_us_de")), 1)
        r.sockpath = None
        with mock.patch.object(w11_session, "find_wayfire_socket", lambda: None):
            self.assertIsNone(r.group(text("kde_us_de")))
        self.assertFalse(r.absent)
        self.assertGreater(r.retry_at, 0.0)

    def test_a_wedged_wayfire_is_bounded_by_this_modules_deadline_not_the_backends(self):
        """The same claim `test_a_wedged_hyprland_is_bounded_by_this_modules_deadline_not_the_backends`
        makes, for the reader that got the knob on 2026-09-09 (requests-batch-6.md, batch 10 -> batch 6):
        these run from `fetch()` while the daemon holds its lock, so a wedged Wayfire may not spend
        `backend_wayfire.IPC_TIMEOUT` (10.0 s) of every `type`. WAYFIRE_TIMEOUT is the 2.0 s every other
        reader in this module carries, driven at 0.3 s here so the test is not a wait."""
        self.assertEqual(xkbmap.WAYFIRE_TIMEOUT, 2.0)
        r = self.reader(cls=FakeWayfire, mode="silent")
        with mock.patch.object(xkbmap, "WAYFIRE_TIMEOUT", 0.3):
            started = time.monotonic()
            self.assertIsNone(r.group(text("kde_us_de")))
            waited = time.monotonic() - started
        self.assertLess(waited, 3.0, "backend_wayfire's own 10 s default was used instead (%.1fs)" % waited)
        self.assertGreater(r.retry_at, 0.0)
        # and it did ask: the silence is on get-keyboard-state, not on a socket nobody spoke to
        self.assertEqual([m for m, _d in self.srv.calls], [xkbmap.WAYFIRE_STATE_METHOD])

    def test_a_wayfire_without_the_method_is_remembered_too(self):
        """`No such method found!` is a fact about this session's `plugins` line, not about this moment:
        a Wayfire with no `ipc-rules` will not grow the method while it runs."""
        r = self.reader(cls=FakeWayfire, mode="nomethod")
        self.assertIsNone(r.group(text("kde_us_de")))
        self.assertTrue(r.absent)
        self.assertIsNone(r.group(text("kde_us_de")))
        self.assertEqual(len(self.srv.calls), 1)

    def test_an_error_reply_leaves_the_guess_and_is_retried(self):
        r = self.reader(cls=FakeWayfire, mode="handler-error")
        self.assertIsNone(r.group(text("kde_us_de")))
        self.assertFalse(r.absent)
        self.assertGreater(r.retry_at, 0.0)

    def test_a_handler_error_that_quotes_the_sentence_is_not_the_method_being_gone(self):
        """`.no_method` is the field, not the words: `_error_line` sets it for the one reply shape that
        means "the plugin behind that method is not loaded", and only that shape is permanent."""
        r = self.reader(cls=WayfireQuotingTheSentence)
        self.assertIsNone(r.group(text("kde_us_de")))
        self.assertFalse(r.absent, "a handler that raised is not a config line")
        self.assertGreater(r.retry_at, 0.0)


class CinnamonEval(_FakeService):
    """`org.Cinnamon` answering `Eval` for the input-sources read, and nothing else.

    Cinnamon's Eval is `JSON.stringify(eval(code))` [M recon2/cinnamon.md §2.2, cinnamonDBus.js], so a
    program that answers its own JSON string comes back encoded twice -- which is what this replays, and
    what the reader has to decode. `answer`: "ok", "throw" (the `(false, <message and stack>)` shape a
    program that raised comes back as), or "notjson"."""

    def __init__(self, address, current=0, sources=(("xkb", "us"), ("xkb", "de")), answer="ok"):
        self.current = current
        self.sources = [list(s) for s in sources]
        self.answer = answer
        self.scripts = []          # every program that arrived, for the landmine
        _FakeService.__init__(self, address, xkbmap.CINNAMON_BUS_NAME)

    def dispatch(self, m):
        if m.path != xkbmap.CINNAMON_PATH or m.interface != xkbmap.CINNAMON_IFACE:
            return _FakeService.dispatch(self, m)
        if m.member != "Eval":
            raise DBusError(ERR + "UnknownMethod", "no %s" % m.member)
        import json as _json

        self.scripts.append(m.args()[0])
        if self.answer == "throw":
            return "bs", (False, "TypeError: s.get_uint is not a function\n@<input>:1:20")
        if self.answer == "notjson":
            return "bs", (True, "undefined")
        inner = _json.dumps([self.current, self.sources])
        return "bs", (True, _json.dumps(inner))


class TestTheActiveGroupFromCinnamon(unittest.TestCase):
    """U26: `CinnamonInputSources` over `org.Cinnamon.Eval`.

    Cinnamon's schema is `org.cinnamon.desktop.input-sources` with `sources`, `current`,
    `show-all-sources` and `xkb-options` and **no `mru-sources`** [M recon2/cinnamon.md §2.2,
    `gsettings list-keys`], so unlike GNOME the live index really is `current` and the whole mapping is
    `current + 1`. What is refused is what describes no single live layout."""

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.addCleanup(setattr, xkbmap, "_cinnamon", xkbmap._cinnamon)
        self.reader = xkbmap._cinnamon = xkbmap.CinnamonInputSources(self.mock.address)
        self.addCleanup(self.reader.close)

    def service(self, **kw):
        svc = CinnamonEval(self.mock.address, **kw)
        self.addCleanup(svc.close, self.mock)
        return svc

    def test_current_is_the_live_index(self):
        svc = self.service()
        self.assertEqual(self.reader.group(text("kde_us_de")), 1)
        svc.current = 1
        self.assertEqual(self.reader.group(text("kde_us_de")), 2)

    def test_an_index_past_the_end_of_sources_is_not_an_answer(self):
        """The list was edited under us: `current` names a source that is not there any more, and the
        caller's guess is the better answer -- GnomeInputSources refuses the same race."""
        self.service(current=3)
        self.assertIsNone(self.reader.group(text("kde_us_de")))

    def test_a_non_xkb_source_is_refused(self):
        """An IBus engine is not a group of the keymap we just read."""
        self.service(current=1, sources=(("xkb", "us"), ("ibus", "libpinyin")))
        self.assertIsNone(self.reader.group(text("kde_us_de")))
        self.assertEqual(xkbmap._cinnamon_index(0, [["xkb", "us"], ["ibus", "libpinyin"]]), 0)

    def test_an_index_past_the_keymaps_last_group_is_not_an_answer(self):
        self.service(current=2, sources=(("xkb", "us"), ("xkb", "de"), ("xkb", "fr")))
        self.assertIsNone(self.reader.group(text("kde_us_de")))
        self.assertEqual(self.reader.group(text("kde_us_de_fr")), 3)

    def test_a_program_that_threw_leaves_the_guess(self):
        self.service(answer="throw")
        self.assertIsNone(self.reader.group(text("kde_us_de")))

    def test_an_answer_that_is_not_json_leaves_the_guess_and_is_retried(self):
        """`(true, 'undefined')` is what Eval answers for a program whose value `JSON.stringify` cannot
        encode -- the shape a Cinnamon whose schema lost a key would return. The decode raises inside
        `_index`, which is not a fact about the session: the guess stands and the backoff is armed rather
        than `absent` being set."""
        self.service(answer="notjson")
        self.assertIsNone(self.reader.group(text("kde_us_de")))
        self.assertFalse(self.reader.absent)
        self.assertGreater(self.reader.retry_at, 0.0)

    def test_a_bus_with_no_cinnamon_on_it_is_remembered(self):
        """No `org.Cinnamon`: not a Cinnamon session, which is permanent. NameHasOwner and no Eval, so
        nothing is started by the asking either."""
        self.assertIsNone(self.reader.group(text("kde_us_de")))
        self.assertTrue(self.reader.absent)
        svc = self.service()                       # Cinnamon arrives afterwards; we do not go back
        self.assertIsNone(self.reader.group(text("kde_us_de")))
        self.assertEqual(svc.scripts, [])

    #: The program the plan spells out for this reader, written here so that this file pins the bytes
    #: that reach `org.Cinnamon.Eval` rather than reading them back out of the constant that sends them
    #: [the plan's A 1.3; M recon2/cinnamon.md §2.2 for the two keys it reads].
    PROGRAM = ("JSON.stringify((s => [s.get_uint('current'), s.get_value('sources').deep_unpack()])"
               "(new imports.gi.Gio.Settings({schema_id: 'org.cinnamon.desktop.input-sources'})))")

    def test_the_program_is_read_only_and_is_the_only_one_sent(self):
        """The interpolation rule, at the one place this module talks to an `eval()`: exactly one program
        goes out, it is the one written above, and it writes nothing. Reordering it, widening it to a
        second Eval or growing a `set_` all fail here."""
        svc = self.service()
        self.reader.group(text("kde_us_de"))
        self.assertEqual(svc.scripts, [self.PROGRAM])
        self.assertNotIn("set_", self.PROGRAM)
        self.assertNotIn("%", xkbmap.CINNAMON_SCRIPT, "the constant is a literal, not a format string")

    def test_the_snapshot_says_who_answered_and_that_the_group_is_a_guess(self):
        """The source names Cinnamon, the group is the one `current` indexes (2) -- and `group_known` is
        FALSE, so the "assuming '<name>'" notice fires (item 7, requests-batch-18.md 2). muffin 6.4 has no
        group getter, so `current` can name a group muffin is not decoding under whenever it is written
        behind muffin's back; the value is still our best guess and typing uses it, but it is a guess and
        `keys explain` marks it "(assumed)". The four other desktops `desktop_group` asks LOCK the group
        they report and stay known -- only Cinnamon is in `GROUP_ASSUMED_DESKTOPS`."""
        self.service(current=1)
        self.addCleanup(setattr, xkbmap, "_fetch_wayland", xkbmap._fetch_wayland)
        xkbmap._fetch_wayland = fake_wayland("kde_us_de")
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            snap = xkbmap.fetch()
            # end to end: the same guess reaches `keys explain`, whose layout line MUST carry "(assumed)"
            # and name Cinnamon -- the visible half of item 7. group_known=False is what puts it there.
            line = keys_cmds.Layout.load().describe()[0]
        self.assertEqual((snap.group, snap.group_known, snap.source),
                         (2, False, "wayland + cinnamon input-sources"))
        self.assertEqual(line, "layout: German -- group 2 of 2 (assumed), "
                               "from wayland + cinnamon input-sources")


class CosmicWithKeyboard(wl_fake.CosmicCompositor):
    """The shared COSMIC fake, plus the seat and the keymap descriptor a `fetch()` needs.

    `wl_fake.Server` answers binds and nothing else, so the two events every real compositor sends a fresh
    client are packed here by hand, like everything else in that module: `wl_seat.capabilities` with the
    keyboard bit, and `wl_keyboard.keymap(XKB_V1, fd, size)` with the keymap on a descriptor. The layout
    manager itself is the shared fake's -- it advertises `zcosmic_keyboard_layout_manager_v1` at version 1
    among cosmic-comp's recorded globals and sends `group` on `get_keyboard_layout`
    [M recon2/cosmic.md §4]. What is recorded here is the request's *arguments*, because the one thing a
    reader can get wrong on the wire is handing it the wl_seat where the protocol asks for the
    wl_keyboard [R recon2/cosmic/cosmic-keyboard-layout-unstable-v1.xml]."""

    PREFIX = "wdotool-cosmic-kbd-"
    #: WL_SEAT_CAPABILITY_KEYBOARD
    KEYBOARD = 2

    def __init__(self, keymap_path, **kw):
        self.keymap_path = keymap_path
        self.kb_oid = None
        self.layout_args = []      # (new_id, object) of every get_keyboard_layout
        super().__init__(**kw)

    def on_bind(self, conn, state, name, iface, version, new_id):
        super().on_bind(conn, state, name, iface, version, new_id)
        if iface == "wl_seat":
            self._send(conn, new_id, 0, struct.pack("<I", self.KEYBOARD))   # capabilities

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid == self.seat_oid and opcode == 1:            # wl_seat.get_keyboard(id)
            self.kb_oid = struct.unpack_from("<I", body)[0]
            self._send_keymap(conn, self.kb_oid)
            return
        if self.kbd_mgr is not None and oid == self.kbd_mgr and opcode == 0:
            self.layout_args.append(struct.unpack_from("<II", body))
        super().on_request(conn, state, oid, opcode, body, fds)

    def _send_keymap(self, conn, kb):
        fd = os.open(self.keymap_path, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            wire = wl_fake.msg(kb, 0, struct.pack("<II", 1, size))   # keymap(XKB_V1, fd, size)
            conn.sendmsg([wire], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))])
        except OSError:
            pass
        finally:
            os.close(fd)


class CosmicRefusingTheLayout(CosmicWithKeyboard):
    """cosmic-comp answering `get_keyboard_layout` with `wl_display.error`, then serving on.

    Not measured -- the nested rig never drove this request -- but it is the shape a compositor uses to
    say no to a request it dislikes (a `wl_keyboard` it does not recognise, a version quibble), and it is
    fatal to the connection: `WlConn` marks itself dead and every later call raises. The keymap has
    already been read by then, and losing it would take `type` on COSMIC from "types with the guess" to
    "refuses outright"."""

    def on_request(self, conn, state, oid, opcode, body, fds):
        if self.kbd_mgr is not None and oid == self.kbd_mgr and opcode == 0:
            self.layout_args.append(struct.unpack_from("<II", body))
            text = b"invalid keyboard\0"
            # wl_display.error(object_id, code, message)
            self._send(conn, 1, 0, struct.pack("<III", oid, 0, len(text))
                       + text + b"\0" * ((-len(text)) % 4))
            return
        super().on_request(conn, state, oid, opcode, body, fds)


class CosmicWedgedOnTheLayout(CosmicWithKeyboard):
    """cosmic-comp that stops answering at `get_keyboard_layout` -- the sync after it never comes back.

    The other half of the same claim: a round trip that runs into `conn.sock.settimeout(timeout)` instead
    of into an error. `_request` is what is overridden, not `on_request`: `wl_fake.Server` answers
    `wl_display.sync` itself, above the subclass hook, so a fake that only stops handling requests still
    completes every round trip and proves nothing. `fetch(timeout=...)` bounds the wait, and what is left
    of the fetch must still be the keymap."""

    wedged = False

    def _request(self, conn, state, fds, oid, opcode, body):
        if self.kbd_mgr is not None and oid == self.kbd_mgr and opcode == 0:
            self.layout_args.append(struct.unpack_from("<II", body))
            self.wedged = True
            return                 # and the wl_display.sync behind it goes unanswered too
        if self.wedged:
            return
        super()._request(conn, state, fds, oid, opcode, body)


class TestTheActiveGroupOnTheCosmicWire(unittest.TestCase):
    """U29: COSMIC's group arrives on the wire, the way sway's does -- no bus, no portal, no reader.

    cosmic-comp publishes `zcosmic_keyboard_layout_manager_v1`, whose `group` event the XML says is
    "received even when the client has no focused window" -- which is the single sentence that separates it
    from `wl_keyboard.modifiers` and from every desktop `desktop_group()` has to ask
    [R recon2/cosmic/cosmic-keyboard-layout-unstable-v1.xml, advertised at version 1 among the 53 recorded
    globals [M cosmic.md §4]]. No COSMIC keymap was recorded, so the keymap on the descriptor here is the
    two-group `us, de` from the fixtures; what is being proved is the wire, not the keymap."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="wdotool-cosmic-keymap-")
        cls.maps = {}
        for name in ("kde_us_de", "kde_us"):
            path = os.path.join(cls.dir, name + ".xkb")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text(name))
            cls.maps[name] = path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def fetch(self, keymap="kde_us_de", cls=CosmicWithKeyboard, timeout=2.0, **kw):
        comp = cls(self.maps[keymap], **kw)
        self.addCleanup(comp.close)
        with env(XDG_RUNTIME_DIR=comp.dir, WAYLAND_DISPLAY=os.path.basename(comp.path),
                 SWAYSOCK=None, I3SOCK=None, WDOTOOL_XKB_KEYMAP=None,
                 WDOTOOL_XKB_GROUP=None, WDOTOOL_LAYOUT=None):
            return comp, xkbmap.fetch(timeout=timeout, mods_wait=0.05)

    def test_the_group_event_is_the_known_group(self):
        comp, snap = self.fetch(layout_group=1)
        self.assertEqual((snap.group, snap.group_known), (2, True))
        self.assertEqual(snap.source, "wayland")        # it came off the wire, like sway's
        self.assertFalse(snap.mods_seen, "no wl_keyboard.modifiers reached an unfocused client")

    def test_the_layout_object_is_made_on_the_keyboard_not_the_seat(self):
        comp, _snap = self.fetch(layout_group=0)
        self.assertEqual(len(comp.layout_args), 1)
        new_id, obj = comp.layout_args[0]
        self.assertEqual(obj, comp.kb_oid)
        self.assertNotEqual(obj, comp.seat_oid)
        self.assertEqual(comp.kbd_layouts, [new_id])

    def test_a_group_that_never_arrives_leaves_the_guess(self):
        """Whether cosmic-comp sends the event to a client that has never held focus is not measured, so
        the reader is written for both: no event, and the guess and its notice stand."""
        _comp, snap = self.fetch(layout_group=None)
        self.assertEqual((snap.group, snap.group_known), (1, False))

    def test_a_compositor_that_refuses_the_request_still_yields_its_keymap(self):
        """The guard: `_cosmic_group` runs inside the same `try` that turns a wire failure into XkbError,
        so an unguarded refusal would throw away a keymap that was already read and make `type` and `key`
        fail outright on COSMIC, where the guess had been typing. Nothing about typing may depend on this
        working (B13)."""
        comp, snap = self.fetch(cls=CosmicRefusingTheLayout, layout_group=1)
        self.assertEqual((snap.group, snap.group_known), (1, False))
        self.assertEqual(snap.text, text("kde_us_de"))
        self.assertEqual(len(comp.layout_args), 1, "the request really was sent and really was refused")

    def test_a_compositor_that_never_answers_the_request_still_yields_its_keymap(self):
        """The same claim through the timeout rather than through an error."""
        started = time.monotonic()
        comp, snap = self.fetch(cls=CosmicWedgedOnTheLayout, layout_group=1, timeout=0.4)
        self.assertEqual((snap.group, snap.group_known), (1, False))
        self.assertEqual(snap.text, text("kde_us_de"))
        self.assertTrue(comp.wedged, "the request reached the compositor and was swallowed")
        self.assertLess(time.monotonic() - started, 5.0, "the socket deadline bounded it")

    def test_one_group_is_not_worth_a_round_trip(self):
        """The group only matters when there is more than one to choose from, which is the rule the
        modifiers wait already follows."""
        comp, snap = self.fetch(keymap="kde_us", layout_group=1)
        self.assertEqual((snap.group, snap.group_known), (1, True))
        self.assertEqual(comp.layout_args, [])
        self.assertNotIn((xkbmap.COSMIC_LAYOUT_MANAGER, 1), comp.binds)


class TestTheFiveReadersInTurn(unittest.TestCase):
    """`desktop_group()`: the sockets before the buses, and the first answer wins.

    The cost of asking is the whole reason for the order -- `find_hypr_socket()` and
    `find_wayfire_socket()` are a scandir of $XDG_RUNTIME_DIR where a bus reader pays a connect, an
    EXTERNAL auth and a GetNameOwner -- and each reader's own `absent` is what keeps the loop to one probe
    per reader per process."""

    NAMES = ("hypr_group", "wayfire_group", "kwin_group", "gnome_group", "cinnamon_group")

    def readers(self, *answers):
        seen = []
        for name, answer in zip(self.NAMES, answers):
            self.addCleanup(setattr, xkbmap, name, getattr(xkbmap, name))

            def ask(_text, name=name, answer=answer):
                seen.append(name)
                return answer

            setattr(xkbmap, name, ask)
        return seen

    def test_every_reader_is_asked_once_sockets_first(self):
        seen = self.readers(None, None, None, None, None)
        self.assertIsNone(xkbmap.desktop_group(text("kde_us_de")))
        self.assertEqual(seen, list(self.NAMES))

    def test_the_first_answer_wins_and_the_rest_are_never_asked(self):
        seen = self.readers(None, 2, 1, 1, 1)
        self.assertEqual(xkbmap.desktop_group(text("kde_us_de")), (2, "wayfire"))
        self.assertEqual(seen, ["hypr_group", "wayfire_group"])

    def test_each_desktop_names_itself(self):
        """The name goes into `Snapshot.source` as `wayland + <who>`, which is what
        `wdotool __keymap` prints and what a bug report has to be able to name."""
        want = ("hyprland devices", "wayfire", "kwin", "gnome input-sources", "cinnamon input-sources")
        for i, who in enumerate(want):
            with contextlib.ExitStack() as stack:
                for j, name in enumerate(self.NAMES):
                    stack.enter_context(mock.patch.object(
                        xkbmap, name, lambda _t, answer=(2 if i == j else None): answer))
                self.assertEqual(xkbmap.desktop_group(text("kde_us_de")), (2, who))


if __name__ == "__main__":
    unittest.main()
