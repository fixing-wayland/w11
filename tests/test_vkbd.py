"""zwp_virtual_keyboard_v1: the wire, and the policy that selects it.

Everything here runs against a **real Wayland socket** served by a fake
compositor that speaks the wire format (`KeyboardCompositor` below): our client is
`w11common/wayland_mini.py` unmodified, the keymap really travels as an fd over
SCM_RIGHTS, and every assertion is about the bytes the compositor received. A
mock of our own client would have proved only that we can call our own
methods.

The selection policy is the part to get right, so it is tested in both
directions: with a kernel keyboard available nothing may reach the protocol
even where the compositor offers it, and with none available the protocol is
what types.
"""

import errno
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ...and the tests directory itself, for the bare `import support` /
# `import wl_fake` below: running this file by path puts it on sys.path
# for free, `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# every test file carries this itself: the suite is run file by file, where
# conftest.py never loads, and a tool that hands itself over would not be
# the code under test
os.environ["W11_PASSTHROUGH"] = "never"
# The injection assertions pin the *fixed US table* as the source of
# keycodes; without this a developer running the suite inside a German
# session would have the kernel-path daemon read that session's keymap.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")

import wl_fake
from support import RecorderDev
from w11common.wayland_mini import WlConn
from wdotool import cli, daemon, keymap, us_keymap, vkbd, xkbmap


def cosmic_globals(served):
    """cosmic-comp's own recorded registry, minus what this fake serves itself.

    tests/fixtures/registries/cosmic.txt is the 53 globals a COSMIC 1.7 session announced
    [M recon2/cosmic.md §4]. `zwp_virtual_keyboard_manager_v1` is one of them, which is the whole reason
    typing works there with no privilege at all."""
    return [g for g in wl_fake.registry_fixture("cosmic") if g[0] not in served]

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "keymaps")

# What a daemon that could not open /dev/uinput says today, and must keep
# saying when there is no protocol to fall back to either.
UINPUT_ERROR = ("cannot create uinput devices: [Errno 13] Permission denied: "
                "'/dev/uinput' (wdotool injects input via /dev/uinput; run it "
                "as root)")


# ---------------------------------------------------------------------------
# a compositor, on the wire


class KeyboardCompositor(wl_fake.Server):
    """Enough of a Wayland compositor to be a virtual keyboard's peer.

    On top of wl_fake.Server's wl_display/wl_registry: a wl_seat, a
    wl_output, and zwp_virtual_keyboard_manager_v1 +
    zwp_virtual_keyboard_v1, recording every request made on the keyboard.
    Knobs:

      manager_version  None -> the global is not advertised at all (Mutter,
                       KWin 6.6.6: measured, they do not implement it)
      refuse_create    answer create_virtual_keyboard with a protocol error
      refuse_keymap    answer keymap() with a protocol error
      extra_globals    more (interface, version) rows to advertise and bind
                       nothing for -- a recorded registry, so that "which
                       compositor is this" is a question about real bytes
    """

    MANAGER = vkbd.MANAGER
    PREFIX = "wdotool-vk-"

    def __init__(self, manager_version=1, refuse_create=False,
                 refuse_keymap=False, with_seat=True, extra_globals=()):
        self.refuse_create = refuse_create
        self.refuse_keymap = refuse_keymap
        self.with_seat = with_seat
        self.extra_globals = list(extra_globals)
        self.created = []        # vk object ids
        self.keymaps = []        # (format, bytes)
        self.keys = []           # (keycode, state)
        self.mods = []           # (depressed, latched, locked, group)
        super().__init__(manager_version)

    def advertise(self):
        return (([("wl_seat", 7)] if self.with_seat else [])
                + [("wl_output", 4)] + self.extra_globals)

    def new_state(self):
        return {"seat": None, "mgr": None, "vk": None}

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_seat":
            state["seat"] = new_id
            self._send(conn, new_id, 0, struct.pack("<I", 3))  # caps: kb|ptr
        elif iface == self.MANAGER:
            state["mgr"] = new_id

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid == state["mgr"] and opcode == 0:  # create_virtual_keyboard
            _seat, new_id = struct.unpack_from("<II", body)
            if self.refuse_create:
                self._error(conn, oid, 0, "no virtual keyboard for you")
                return
            state["vk"] = new_id
            self.created.append(new_id)
            self.events.append(("create", new_id))
            return
        if state["vk"] is not None and oid == state["vk"]:
            self._keyboard_request(conn, oid, opcode, body, fds)

    def _keyboard_request(self, conn, oid, opcode, body, fds):
        if opcode == vkbd._VK_KEYMAP:
            fmt, size = struct.unpack_from("<II", body)
            blob = b""
            if fds:
                fd = fds.pop(0)
                try:
                    blob = os.pread(fd, size, 0)
                finally:
                    os.close(fd)
            self.keymaps.append((fmt, blob))
            self.events.append(("keymap", fmt, len(blob)))
            if self.refuse_keymap:
                self._error(conn, oid, 0, "bad keymap")
            return
        if opcode == vkbd._VK_KEY:
            _t, code, st = struct.unpack_from("<III", body)
            self.keys.append((code, st))
            self.events.append(("key", code, st))
            return
        if opcode == vkbd._VK_MODIFIERS:
            d, la, lo, g = struct.unpack_from("<IIII", body)
            self.mods.append((d, la, lo, g))
            self.events.append(("mods", d, la, lo, g))
            return
        if opcode == vkbd._VK_DESTROY:
            self.destroyed += 1
            self.events.append(("destroy",))

    # -- readers used by the tests
    def key_codes(self):
        return [c for c, st in self.keys]

    def pressed(self):
        return [c for c, st in self.keys if st == 1]


# ---------------------------------------------------------------------------
# daemons


def daemon_with_uinput():
    d = daemon._Daemon()
    d.kb = RecorderDev()
    d.dev_error = None
    d._reader = None            # no key-state reads in a test
    return d


def daemon_without_uinput():
    """A daemon on a session where /dev/uinput cannot be opened -- the
    ordinary non-root desktop, where typing fails outright today."""
    d = daemon._Daemon()
    d.kb = None
    d.dev_error = UINPUT_ERROR
    d._reader = None
    d.create_devices = lambda: None     # the retry keeps failing
    return d


class VkbdTest(unittest.TestCase):
    """Base: a fake compositor, and the environment pointed at it."""

    manager_version = 1
    comp_kw: dict = {}

    def setUp(self):
        self.comp = KeyboardCompositor(manager_version=self.manager_version,
                                   **self.comp_kw)
        self.addCleanup(self.comp.close)
        self._env = {k: os.environ.get(k) for k in
                     ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "WDOTOOL_VKBD",
                      "WDOTOOL_LAYOUT", "WDOTOOL_XKB_KEYMAP")}
        self.addCleanup(self._restore)
        os.environ["XDG_RUNTIME_DIR"] = self.comp.dir
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(self.comp.path)
        os.environ.pop("WDOTOOL_VKBD", None)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def daemon(self, uinput=True):
        d = daemon_with_uinput() if uinput else daemon_without_uinput()
        self.addCleanup(d._drop_vkbd)
        return d


# ---------------------------------------------------------------------------
# the keymap we upload


class TheKeymapWeUpload(unittest.TestCase):
    def test_it_is_what_the_fixed_us_table_assumes(self):
        """The whole reason this path needs no reverse lookup.

        `active_group_is_plain_us` is the repo's own bypass checker: it
        verifies, key by key, that every keycode the fixed US table emits
        carries exactly the keysyms that table assumes at levels 1 and 2. The
        kernel path runs it against the *session's* keymap and usually loses;
        here it runs against the keymap we are about to upload, and it has to
        win -- otherwise `keymap.CHAR_TO_KEY` would be typing through a
        keymap it was not written for."""
        self.assertTrue(
            xkbmap.active_group_is_plain_us(us_keymap.TEXT, 1),
            "the uploaded keymap must satisfy the fixed US table exactly")
        self.assertEqual(xkbmap.group_name(us_keymap.TEXT, 1), "English (US)")

    def test_the_modifier_bits_come_from_that_keymaps_own_modifier_map(self):
        """vkbd._MOD_BITS is not a guess about keyboards in general: every
        entry has to be in the uploaded keymap's own `modifier_map`."""
        import re
        text = xkbmap.strip_comments(us_keymap.TEXT)
        codes = re.findall(r"<([^<>\s]+)>\s*=\s*(\d+)\s*;", text)
        # XKB keycode = evdev code + 8, which is the offset the `key` request
        # does NOT use: it takes the evdev code, as wl_keyboard.key does.
        name_of = {int(v) - 8: k for k, v in codes}
        bit_of = {"Shift": vkbd.MOD_SHIFT, "Lock": vkbd.MOD_LOCK,
                  "Control": vkbd.MOD_CONTROL, "Mod1": vkbd.MOD_MOD1,
                  "Mod2": vkbd.MOD_MOD2, "Mod3": vkbd.MOD_MOD3,
                  "Mod4": vkbd.MOD_MOD4, "Mod5": vkbd.MOD_MOD5}
        declared = {}
        for mod, keys in re.findall(r"modifier_map\s+(\w+)\s*\{([^}]*)\}", text):
            for key in re.findall(r"<([^<>\s]+)>", keys):
                declared[key] = bit_of[mod]
        for code, bit in vkbd._MOD_BITS.items():
            key = name_of.get(code)
            self.assertIsNotNone(key, f"evdev {code} is not in the keymap")
            self.assertEqual(declared.get(key), bit,
                             f"evdev {code} (<{key}>) carries the wrong bit")

    def test_the_blob_is_nul_terminated(self):
        blob = vkbd.keymap_blob()
        self.assertTrue(blob.endswith(b"\0"))
        self.assertFalse(blob[:-1].endswith(b"\0"), "exactly one NUL")
        self.assertEqual(blob[:-1].decode(), us_keymap.TEXT)

    def test_the_extra_characters_it_binds_are_typeable_and_out_of_the_bypass_table(self):
        """B2. This keymap binds `key <I443> { [ 0x20ac ] }` -- evdev 435 -- and the built-in table had no
        row for it, so `type` skipped the character on the one path where this keymap is the authority
        (goal2/recon/gaps.md §3b). `keymap.UPLOADED_EXTRA_KEYS` is read out of this same text.

        The second half is why those rows are not IN `CHAR_TO_KEY`: `active_group_is_plain_us` is built from
        that table and reads no keycode above X 263, so a 435 in it would fail the check above for every
        keymap in the tree -- including this one, the one keymap that must pass it."""
        self.assertEqual(keymap.char_to_key("\u20ac"), (435, False))
        self.assertNotIn("\u20ac", keymap.CHAR_TO_KEY)
        self.assertTrue(xkbmap.active_group_is_plain_us(us_keymap.TEXT, 1))
        with open(os.path.join(FIXTURES, "us.xkb"), encoding="utf-8") as f:
            session_us = f.read().rstrip("\0")
        self.assertTrue(xkbmap.active_group_is_plain_us(session_us, 1),
                        "a real plain-us session keymap must still take the bypass")

    def test_it_is_captured_not_generated(self):
        # A keymap we synthesise compiles and then delivers no key events
        # (measured twice on wlroots). This is the guard against someone
        # "simplifying" the file into an include.
        self.assertNotIn("include", us_keymap.TEXT)
        self.assertIn("xkb_keycodes", us_keymap.TEXT)
        self.assertIn("xkb_symbols", us_keymap.TEXT)


# ---------------------------------------------------------------------------
# the wire


class TheUpload(VkbdTest):
    def test_it_creates_a_keyboard_and_uploads_the_keymap(self):
        vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(vk.close)
        vk.flush()
        self.assertEqual(len(self.comp.created), 1)
        self.assertEqual(len(self.comp.keymaps), 1)
        fmt, blob = self.comp.keymaps[0]
        self.assertEqual(fmt, 1, "XKB_V1 is the only format there is")
        self.assertEqual(blob, vkbd.keymap_blob())

    def test_the_keymap_is_uploaded_before_any_key(self):
        # `key` before `keymap` is a protocol error: "Cannot send a keypress
        # before defining a keymap".
        vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(vk.close)
        vk.key(21, True)
        vk.key(21, False)
        vk.flush()
        kinds = [e[0] for e in self.comp.events]
        self.assertEqual(kinds[0], "create")
        self.assertEqual(kinds[1], "keymap")
        self.assertIn("key", kinds)

    def test_it_binds_the_manager_at_version_one(self):
        vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(vk.close)
        self.assertEqual(vk.version, 1)
        self.assertIn((vkbd.MANAGER, 1), self.comp.binds)

    def test_destroy_on_close(self):
        vk = vkbd.VirtualKeyboard.open()
        vk.flush()
        vk.close()
        # the destroy request rides the same socket; give the server the
        # roundtrip it needs to have read it
        self.assertTrue(_eventually(lambda: self.comp.destroyed == 1))


class AVersionTwoCompositor(VkbdTest):
    manager_version = 2

    def test_we_still_bind_version_one(self):
        vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(vk.close)
        self.assertEqual(vk.version, 1)
        self.assertIn((vkbd.MANAGER, 1), self.comp.binds)


class ACompositorWithoutTheProtocol(VkbdTest):
    manager_version = None      # Mutter; KWin 6.6.6 (both measured)

    def test_open_says_so_and_does_not_raise_anything_else(self):
        with self.assertRaises(vkbd.VkbdError) as cm:
            vkbd.VirtualKeyboard.open()
        self.assertIn("does not implement", str(cm.exception))
        self.assertEqual(self.comp.created, [])

    def test_the_sentence_names_muffin_on_both_sides_of_the_semicolon(self):
        """B2. The clause listing who DOES implement it left Muffin out, so a Cinnamon user was told nothing
        about their own desktop; vm/live-smoke.d/cinnamon-wayland.sh:376 pins the Muffin|Cinnamon half of it
        against a real muffin session, and this pins the bytes. Both halves are true and both are named:
        26.04's muffin 6.4 advertises 23 globals and none is this protocol, while master (the 6.6/6.8 line)
        ships virtual-keyboard-unstable-v1.xml and META_ZWP_VIRTUAL_KEYBOARD_MANAGER_V1_VERSION 1
        [M recon2/cinnamon.md §2.1]."""
        with self.assertRaises(vkbd.VkbdError) as cm:
            vkbd.VirtualKeyboard.open()
        said = str(cm.exception)
        # the step file's own two greps, in the order it runs them
        self.assertRegex(said, r"does not implement zwp_virtual_keyboard_manager_v1")
        self.assertRegex(said, r"Muffin|Cinnamon")
        do_not, _, do = said.partition(";")
        self.assertIn("Muffin 6.4", do_not)
        self.assertIn("Muffin 6.6+", do)
        for who in ("Mutter", "KWin"):
            self.assertIn(who, do_not)
        for who in ("sway", "Hyprland", "wlroots", "COSMIC"):
            self.assertIn(who, do)


class ACompositorThatRefuses(VkbdTest):
    comp_kw = {"refuse_create": True}

    def test_a_protocol_error_becomes_a_vkbderror(self):
        with self.assertRaises(vkbd.VkbdError) as cm:
            vkbd.VirtualKeyboard.open()
        self.assertIn("no virtual keyboard for you", str(cm.exception))


class ACompositorThatRefusesTheKeymap(VkbdTest):
    comp_kw = {"refuse_keymap": True}

    def test_the_upload_failure_is_reported(self):
        with self.assertRaises(vkbd.VkbdError) as cm:
            vkbd.VirtualKeyboard.open()
        self.assertIn("bad keymap", str(cm.exception))


class Modifiers(VkbdTest):
    """wlroots does not run a virtual keyboard's keys through xkb state, so
    the mask has to be sent explicitly. This is the single biggest difference
    from uinput."""

    def setUp(self):
        super().setUp()
        self.vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(self.vk.close)

    def test_shift_sets_the_bit_before_the_key_and_clears_it_after(self):
        self.vk.key(keymap.KEY_LEFTSHIFT, True)
        self.vk.key(21, True)
        self.vk.key(21, False)
        self.vk.key(keymap.KEY_LEFTSHIFT, False)
        self.vk.flush()
        wire = [e for e in self.comp.events if e[0] in ("key", "mods")]
        self.assertEqual(wire, [
            ("mods", vkbd.MOD_SHIFT, 0, 0, 0),
            ("key", keymap.KEY_LEFTSHIFT, 1),
            ("key", 21, 1),
            ("key", 21, 0),
            ("key", keymap.KEY_LEFTSHIFT, 0),
            ("mods", 0, 0, 0, 0),
        ])

    def test_two_shift_keys_do_not_clear_each_other(self):
        self.vk.key(keymap.KEY_LEFTSHIFT, True)
        self.vk.key(keymap.KEY_RIGHTSHIFT, True)
        self.vk.key(keymap.KEY_LEFTSHIFT, False)
        self.vk.flush()
        self.assertEqual([m[0] for m in self.comp.mods], [vkbd.MOD_SHIFT],
                         "Shift must stay down while the other one is held, "
                         "and an unchanged mask is not re-sent")

    def test_control_and_alt_and_super_carry_their_own_bits(self):
        for code, bit in ((keymap.KEY_LEFTCTRL, vkbd.MOD_CONTROL),
                          (keymap.KEY_LEFTALT, vkbd.MOD_MOD1),
                          (keymap.KEY_LEFTMETA, vkbd.MOD_MOD4)):
            self.comp.mods.clear()
            self.vk.key(code, True)
            self.vk.key(code, False)
            self.vk.flush()
            self.assertEqual([m[0] for m in self.comp.mods], [bit, 0])

    def test_caps_lock_toggles_the_locked_mask(self):
        self.vk.key(keymap.KEY_CAPSLOCK, True)
        self.vk.key(keymap.KEY_CAPSLOCK, False)
        self.vk.flush()
        self.assertEqual([(m[0], m[2]) for m in self.comp.mods],
                         [(vkbd.MOD_LOCK, vkbd.MOD_LOCK), (0, vkbd.MOD_LOCK)])

    def test_clear_modifiers_says_nothing_is_down(self):
        self.vk.key(keymap.KEY_LEFTCTRL, True)
        self.vk.flush()          # the press is on the wire before we look
        self.comp.mods.clear()
        self.vk.clear_modifiers()
        self.vk.flush()
        self.assertEqual(self.comp.mods, [(0, 0, 0, 0)])

    def test_a_plain_key_sends_no_modifiers_event(self):
        self.vk.key(21, True)
        self.vk.key(21, False)
        self.vk.flush()
        self.assertEqual(self.comp.mods, [])


class Lifetime(VkbdTest):
    def test_a_dead_compositor_is_a_clean_error(self):
        vk = vkbd.VirtualKeyboard.open()
        self.addCleanup(vk.close)
        vk.flush()
        self.comp.drop_clients()
        with self.assertRaises(vkbd.VkbdError):
            for _ in range(200):        # fill the socket buffer to notice
                vk.key(21, True)
                vk.key(21, False)


# ---------------------------------------------------------------------------
# the policy


class ThePolicy(VkbdTest):
    """One sentence: the protocol types when the kernel keyboard cannot be
    opened and the compositor implements it; /dev/uinput in every other case;
    --vkbd on|off forces either."""

    def test_with_uinput_the_kernel_device_types_even_here(self):
        d = self.daemon(uinput=True)
        d.op_type("hi", 0, False)
        self.assertEqual(d.kb.events,
                         [("KEY", 35, 1), ("KEY", 35, 0),
                          ("KEY", 23, 1), ("KEY", 23, 0)])
        self.assertEqual(self.comp.connections, 0,
                         "the compositor must not even be contacted")

    def test_without_uinput_the_protocol_types(self):
        d = self.daemon(uinput=False)
        warns = d.op_type("hi", 0, False)
        self.assertEqual(self.comp.pressed(), [35, 23])   # h, i
        self.assertEqual(len(self.comp.created), 1)
        self.assertTrue(any("zwp_virtual_keyboard_v1" in w for w in warns),
                        warns)
        self.assertTrue(any("no root and no device rule" in w for w in warns),
                        warns)

    def test_without_uinput_and_without_the_protocol_nothing_changes(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.op_type("hi", 0, False)
        self.assertEqual(str(cm.exception), UINPUT_ERROR,
                         "the error the user has to act on is still uinput's")

    def test_vkbd_off_keeps_the_kernel_path_and_its_error(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.op_type("hi", 0, False, None, None, "off")
        self.assertEqual(str(cm.exception), UINPUT_ERROR)
        self.assertEqual(self.comp.connections, 0)

    def test_vkbd_on_uses_the_protocol_although_uinput_works(self):
        d = self.daemon(uinput=True)
        d.op_type("hi", 0, False, None, None, "on")
        self.assertEqual(self.comp.pressed(), [35, 23])
        self.assertEqual(d.kb.events, [], "nothing may reach the kernel device")

    def test_vkbd_on_without_the_protocol_is_a_clean_refusal(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.op_type("hi", 0, False, None, None, "on")
        self.assertIn("--vkbd on", str(cm.exception))
        self.assertIn("does not implement", str(cm.exception))
        self.assertEqual(d.kb.events, [], "it must not silently use uinput")

    def test_the_environment_selects_it_too(self):
        os.environ["WDOTOOL_VKBD"] = "on"
        d = self.daemon(uinput=True)
        d.op_type("a", 0, False)
        self.assertEqual(self.comp.pressed(), [30])

    def test_the_flag_beats_the_environment(self):
        os.environ["WDOTOOL_VKBD"] = "on"
        d = self.daemon(uinput=True)
        d.op_type("a", 0, False, None, None, "off")
        self.assertEqual(self.comp.connections, 0)
        self.assertEqual(d.kb.events, [("KEY", 30, 1), ("KEY", 30, 0)])

    def test_a_nonsense_environment_value_is_auto_not_a_failure(self):
        os.environ["WDOTOOL_VKBD"] = "yes-please"
        d = self.daemon(uinput=True)
        d.op_type("a", 0, False)
        self.assertEqual(d.kb.events, [("KEY", 30, 1), ("KEY", 30, 0)])


class ACosmicShapedSession(VkbdTest):
    """The keyboard half on COSMIC, over cosmic-comp's own registry.

    Measured on a live COSMIC 1.7 with no `/dev/uinput` node on the box at all: `wdotool key a`,
    `wdotool type 'hello cosmic'` and `wdotool key Return` all exited 0 and the focused `foot` read back
    `ahello cosmichello cosmic` [M recon2/cosmic.md §3]. Its sibling class in tests/test_vptr.py is the
    other half of the same session, where clicking has nothing to fall back to."""

    comp_kw = {"extra_globals": cosmic_globals(
        ("wl_seat", "wl_output", vkbd.MANAGER))}

    def test_the_session_these_tests_talk_to_offers_the_keyboard_and_no_pointer(self):
        """Read off the socket the daemon uses rather than out of the fixture file: a `comp_kw` that
        subtracted the wrong globals would leave the recording right and this session wrong, and the whole
        point of the class is that COSMIC's two input halves come apart."""
        conn = WlConn(self.comp.path)
        self.addCleanup(conn.close)
        ifaces = {iface for iface, _ver in conn.get_registry().values()}
        self.assertIn(vkbd.MANAGER, ifaces)
        self.assertNotIn("zwlr_virtual_pointer_manager_v1", ifaces)

    def test_vkbd_on_type_types(self):
        d = self.daemon(uinput=True)
        d.op_type("hi", 0, False, None, None, "on")
        self.assertEqual(self.comp.pressed(), [35, 23])
        self.assertEqual(d.kb.events, [], "nothing may reach the kernel device")

    def test_auto_types_through_the_protocol_when_there_is_no_uinput(self):
        """Which is the COSMIC desktop as measured: no /dev/uinput node, and typing landed anyway."""
        d = self.daemon(uinput=False)
        warns = d.op_type("hi", 0, False)
        self.assertEqual(self.comp.pressed(), [35, 23])
        self.assertTrue(any("zwp_virtual_keyboard_v1" in w for w in warns), warns)

    def test_the_refusal_no_longer_puts_cosmic_on_the_wrong_side(self):
        """The sentence a session without the protocol gets, byte for byte. COSMIC has it, so COSMIC belongs
        in the second half of the list; naming it in the first half would send a user looking for a bug that
        is not there. Muffin joined both halves in B2 -- 6.4 has no such global, master carries it -- and the
        byte pin is here so a reword has to come back through this test and cinnamon-wayland.sh:376."""
        self.comp.manager_version = None
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.op_type("hi", 0, False, None, None, "on")
        said = str(cm.exception)
        self.assertIn("(Mutter, KWin and Cinnamon's Muffin 6.4 do not; sway, Hyprland, the wlroots "
                      "family, COSMIC and Muffin 6.6+ do)", said)


class TheGnomePathIsUntouched(VkbdTest):
    """GNOME (and KDE) have no such protocol, so nothing about the reverse
    map may change. The compositor here advertises none, exactly as Mutter
    does."""

    manager_version = None

    def setUp(self):
        super().setUp()
        os.environ["WDOTOOL_XKB_KEYMAP"] = os.path.join(FIXTURES, "de.xkb")
        os.environ["WDOTOOL_LAYOUT"] = "auto"

    def test_a_german_session_still_types_through_the_reverse_map(self):
        d = self.daemon(uinput=True)
        d.op_type("yz", 0, False)
        # German: `y` is on the US z key (44) and `z` on the US y key (21).
        self.assertEqual(d.kb.events, [("KEY", 44, 1), ("KEY", 44, 0),
                                       ("KEY", 21, 1), ("KEY", 21, 0)])

    def test_an_accented_character_still_works(self):
        d = self.daemon(uinput=True)
        warns = d.op_type("ü", 0, False)
        self.assertEqual([w for w in warns if "Can't type" in w], [], warns)
        self.assertTrue(d.kb.events)

    def test_the_layout_flag_still_reaches_the_reverse_map(self):
        d = self.daemon(uinput=True)
        d.op_type("y", 0, False, None, "us")
        self.assertEqual(d.kb.events, [("KEY", 21, 1), ("KEY", 21, 0)],
                         "--layout us must still force the fixed table")


class UnderANonUsSessionLayout(VkbdTest):
    """The measurement this whole path exists for: session set to German, our
    US keymap uploaded, and the keycodes are the US ones -- because the
    keymap that reads them is ours."""

    def setUp(self):
        super().setUp()
        os.environ["WDOTOOL_XKB_KEYMAP"] = os.path.join(FIXTURES, "de.xkb")
        os.environ["WDOTOOL_LAYOUT"] = "auto"

    def test_the_characters_are_the_fixed_us_tables(self):
        d = self.daemon(uinput=False)
        d.op_type("yzq", 0, False)
        self.assertEqual(self.comp.pressed(), [21, 44, 16])

    def test_the_kernel_path_on_the_same_session_reverses_them(self):
        # The control: same daemon state, same keymap, uinput available.
        d = self.daemon(uinput=True)
        d.op_type("yzq", 0, False)
        self.assertEqual([c for _t, c, v in d.kb.events if v == 1],
                         [44, 21, 16])

    def test_shifted_characters_go_through_the_modifiers_request(self):
        d = self.daemon(uinput=False)
        d.op_type("Y", 0, False)
        self.assertEqual([e for e in self.comp.events
                          if e[0] in ("key", "mods")],
                         [("mods", vkbd.MOD_SHIFT, 0, 0, 0),
                          ("key", keymap.KEY_LEFTSHIFT, 1),
                          ("key", 21, 1),
                          ("key", 21, 0),
                          ("key", keymap.KEY_LEFTSHIFT, 0),
                          ("mods", 0, 0, 0, 0)])

    def test_the_euro_sign_goes_out_as_the_keycode_the_uploaded_keymap_binds(self):
        """B2, the end of goal2/recon/gaps.md §3b: `wdotool type EUR+X` put only the X in a focused `foot`
        and warned "Can't type character". Now evdev 435 (KEY_EURO, `key <I443>`) and 45 (x) go out, in that
        order, with no Shift and no warning -- reproduced live 2026-09-11 on headless sway 1.11 + foot:
        `wdotool --vkbd on type -- EUR+X` landed `b'\\xe2\\x82\\xacX\\n'`."""
        d = self.daemon(uinput=False)
        warns = d.op_type("\u20acx", 0, False)
        self.assertEqual(self.comp.pressed(), [435, 45])
        self.assertEqual([w for w in warns if "Can't type character" in w], [])

    def test_a_character_the_us_keymap_cannot_type_is_still_skipped(self):
        d = self.daemon(uinput=False)
        warns = d.op_type("aüb", 0, False)
        self.assertEqual(self.comp.pressed(), [30, 48])
        self.assertEqual(len(warns), 2, warns)   # the notice + the skip
        self.assertTrue(any("Can't type character" in w for w in warns), warns)

    def test_forcing_xkb_says_it_does_not_apply_here(self):
        d = self.daemon(uinput=False)
        warns = d.op_type("y", 0, False, None, "xkb")
        self.assertEqual(self.comp.pressed(), [21], "the US table, not de")
        self.assertTrue(any("does not apply" in w for w in warns), warns)

    def test_a_key_sequence_uses_the_us_keycodes(self):
        d = self.daemon(uinput=False)
        d.op_key("ctrl+z", "press", 0, False)
        self.assertEqual([e for e in self.comp.events
                          if e[0] in ("key", "mods")],
                         [("mods", vkbd.MOD_CONTROL, 0, 0, 0),
                          ("key", keymap.KEY_LEFTCTRL, 1),
                          ("key", 44, 1),
                          ("key", keymap.KEY_LEFTCTRL, 0),
                          ("mods", 0, 0, 0, 0),
                          ("key", 44, 0)])


class HeldKeysAndClearModifiers(VkbdTest):
    def test_a_key_held_across_two_commands_keeps_one_keyboard(self):
        d = self.daemon(uinput=False)
        d.op_key("ctrl", "down", 0, False)
        d.op_type("c", 0, False)
        d.op_key("ctrl", "up", 0, False)
        self.assertEqual(len(self.comp.created), 1,
                         "one connection and one keyboard for the daemon's life")
        self.assertEqual(len(self.comp.keymaps), 1, "uploaded once")
        self.assertEqual(self.comp.keys[0], (keymap.KEY_LEFTCTRL, 1))
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTCTRL, 0))
        self.assertIn((46, 1), self.comp.keys)      # c, with ctrl still down
        self.assertEqual(self.comp.mods[0][0], vkbd.MOD_CONTROL)
        self.assertEqual(self.comp.mods[-1][0], 0)

    def test_auto_does_not_change_sinks_under_a_held_key(self):
        """`wdotool --vkbd on keydown ctrl` and then a plain `wdotool type c`:
        the second command must not switch to the kernel device, because a
        key held on one of them cannot be released on the other."""
        d = self.daemon(uinput=True)          # uinput works in this one
        d.op_key("ctrl", "down", 0, False, None, None, "on")
        d.op_type("c", 0, False)              # auto -- would prefer uinput
        d.op_key("ctrl", "up", 0, False)      # auto again
        self.assertEqual(d.kb.events, [],
                         "nothing may leak to the kernel device mid-hold")
        self.assertIn((46, 1), self.comp.keys)
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTCTRL, 0))
        self.assertNotIn(keymap.KEY_LEFTCTRL, d.down)

    def test_clearmodifiers_clears_and_restores_what_we_hold(self):
        d = self.daemon(uinput=False)
        d.op_key("ctrl", "down", 0, False)
        self.comp.events.clear()
        d.op_type("a", 0, True)
        kinds = [e for e in self.comp.events if e[0] in ("key", "mods")]
        self.assertEqual(kinds[0], ("key", keymap.KEY_LEFTCTRL, 0))
        self.assertEqual(kinds[1], ("mods", 0, 0, 0, 0))
        self.assertEqual(kinds[-1], ("key", keymap.KEY_LEFTCTRL, 1))
        self.assertIn(keymap.KEY_LEFTCTRL, d.down, "still held afterwards")

    def test_clearmodifiers_never_blames_a_foreign_keyboard(self):
        """On this path a modifier held on the user's own keyboard provably
        does not reach our keys (measured), so the uinput warning would be a
        lie -- and the key-state read that produces it never happens."""
        d = self.daemon(uinput=False)
        d._reader = _Reader({keymap.KEY_LEFTSHIFT})
        warns = d.op_type("a", 0, True)
        self.assertEqual([w for w in warns if "clearmodifiers" in w], [])

    def test_the_kernel_path_still_does_blame_it(self):
        d = self.daemon(uinput=True)
        d._reader = _Reader({keymap.KEY_LEFTSHIFT})
        warns = d.op_type("a", 0, True)
        self.assertTrue(any("clearmodifiers" in w for w in warns), warns)


class _Reader:
    """keystate.Reader's one method, scripted."""

    def __init__(self, held):
        self._held = held

    def held(self, codes):
        return {c for c in codes if c in self._held}


class Reconnecting(VkbdTest):
    def test_a_compositor_restart_costs_the_hold_and_not_the_command(self):
        """A restart between two commands used to spend the next command
        discovering it (one error, then a working one). The command can be
        served: the connection is checked before it is used, and a dead one
        is replaced. What cannot be served is the *hold* -- the compositor
        released those keys when we vanished from it -- so that, and only
        that, is what the user is told about."""
        d = self.daemon(uinput=False)
        d.op_key("ctrl", "down", 0, False)
        self.assertIn(keymap.KEY_LEFTCTRL, d.down)
        self.comp.drop_clients()
        resp = d.handle({"op": "type", "text": "a", "delay_ms": 0})
        self.assertTrue(resp.get("ok"), resp)
        self.assertTrue(any("keys wdotool was holding" in w
                            for w in resp["warnings"]), resp["warnings"])
        self.assertNotIn(keymap.KEY_LEFTCTRL, d.down,
                         "the compositor released what we held; so must we")
        self.assertEqual(len(self.comp.created), 2)
        self.assertEqual(len(self.comp.keymaps), 2, "re-uploaded on reconnect")
        self.assertEqual(self.comp.connections, 2)
        self.assertIn((30, 1), self.comp.keys)

    def test_a_restart_with_nothing_held_says_nothing(self):
        d = self.daemon(uinput=False)
        d.op_type("a", 0, False)
        self.comp.drop_clients()
        resp = d.handle({"op": "type", "text": "b", "delay_ms": 0})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp["warnings"], [])
        self.assertIn((48, 1), self.comp.keys)      # b

    def test_a_compositor_that_does_not_come_back_is_a_clean_error(self):
        d = self.daemon(uinput=False)
        d.op_type("a", 0, False)
        self.comp.close()                # socket gone, nothing to reconnect to
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "type", "text": "b", "delay_ms": 0})
        self.assertEqual(str(cm.exception), UINPUT_ERROR,
                         "the error is the kernel device's, unchanged")

    def test_a_restart_falls_back_to_uinput_when_that_is_what_works(self):
        """uinput available, a key held on the virtual keyboard, and the
        compositor gone: the hold cannot be honoured by anyone, so auto is
        free to choose again -- and chooses the kernel device rather than
        failing the command."""
        d = self.daemon(uinput=True)
        d.op_key("ctrl", "down", 0, False, None, None, "on")
        self.comp.drop_clients()
        self.comp.close()
        resp = d.handle({"op": "type", "text": "x", "delay_ms": 0})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(d.kb.events, [("KEY", 45, 1), ("KEY", 45, 0)])
        self.assertFalse(d.down)

    def test_the_protocol_withdrawn_mid_session_does_not_break_the_hold(self):
        """global_remove for the manager. A Wayland object outlives the
        global it came from, so the keyboard we already have keeps typing and
        our client must not choke on the event; only the next connection
        finds the protocol gone, and falls back cleanly."""
        d = self.daemon(uinput=False)
        d.op_key("ctrl", "down", 0, False)
        self.comp.withdraw_manager()
        d.op_type("c", 0, False)
        self.assertIn((46, 1), self.comp.keys)
        d.op_key("ctrl", "up", 0, False)
        self.assertEqual(len(self.comp.created), 1)
        # now really gone: the reconnect cannot find it
        self.comp.manager_version = None
        self.comp.drop_clients()
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "type", "text": "a", "delay_ms": 0})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


class AConnectionThatDiesMidInjection(VkbdTest):
    """The worst outcome this path could have: a key the daemon believes is
    held on a keyboard that no longer exists. Nothing can release it -- not a
    later keyup (it goes to a fresh keyboard), not --clearmodifiers (it sees
    the key as already down) -- and every later `type A` comes out as `a`."""

    def test_no_key_survives_the_drop_in_the_daemons_model(self):
        d = self.daemon(uinput=False)
        d.op_key("shift", "down", 0, False)
        self.assertIn(keymap.KEY_LEFTSHIFT, d.down)
        self.comp.drop_clients()
        # --clearmodifiers: the release of the very key we hold is the write
        # that fails, so the object's own `held` no longer lists it.
        try:
            d.handle({"op": "type", "text": "a", "delay_ms": 0,
                      "clearmods": True})
        except RuntimeError:
            pass
        self.assertEqual(d.down, set(), "nothing may stay down after a drop")

    def test_and_the_characters_are_right_afterwards(self):
        d = self.daemon(uinput=False)
        d.op_key("shift", "down", 0, False)
        self.comp.drop_clients()
        try:
            d.handle({"op": "key", "spec": "shift", "direction": "up",
                      "delay_ms": 0})
        except RuntimeError:
            pass
        self.comp.keys.clear()
        d.handle({"op": "type", "text": "A", "delay_ms": 0})
        self.assertEqual(self.comp.keys,
                         [(keymap.KEY_LEFTSHIFT, 1), (30, 1), (30, 0),
                          (keymap.KEY_LEFTSHIFT, 0)])


class SwitchingSinks(VkbdTest):
    """`self.down` is one set and there are two devices behind it. Whichever
    one is about to type has to own it, or the modifier bookkeeping describes
    a device that is not typing -- which produced the wrong characters."""

    def test_a_key_held_on_the_virtual_keyboard_is_released_when_forced_off(self):
        d = self.daemon(uinput=True)
        d.op_key("shift", "down", 0, False, None, None, "on")
        d.kb.events.clear()
        warns = d.op_type("A", 0, False, None, None, "off")
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTSHIFT, 0),
                         "released on the keyboard that was holding it")
        self.assertEqual(d.kb.events,
                         [("KEY", keymap.KEY_LEFTSHIFT, 1), ("KEY", 30, 1),
                          ("KEY", 30, 0), ("KEY", keymap.KEY_LEFTSHIFT, 0)],
                         "the kernel path presses its own shift for 'A'")
        self.assertTrue(any("were released" in w for w in warns), warns)

    def test_a_key_held_on_the_kernel_device_is_released_when_forced_on(self):
        d = self.daemon(uinput=True)
        d.op_key("shift", "down", 0, False, None, None, "off")
        d.kb.events.clear()
        warns = d.op_type("A", 0, False, None, None, "on")
        self.assertEqual(d.kb.events, [("KEY", keymap.KEY_LEFTSHIFT, 0)])
        self.assertEqual(self.comp.keys,
                         [(keymap.KEY_LEFTSHIFT, 1), (30, 1), (30, 0),
                          (keymap.KEY_LEFTSHIFT, 0)])
        self.assertTrue(any("were released" in w for w in warns), warns)

    def test_a_hold_does_not_survive_a_switch_that_cannot_happen(self):
        """The half of the switch that only shows up where the OTHER sink is
        missing, which is every session this path exists for. `--vkbd on
        keydown shift` then `--vkbd off keyup shift` on a box with no
        /dev/uinput used to report the uinput error and leave shift down on
        the virtual keyboard for the daemon's life: _own_sink() sits below
        the raise in _pick_keyboard() and never ran. Measured on sway 1.11."""
        d = self.daemon(uinput=False)
        d.op_key("shift", "down", 0, False, None, None, "on")
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTSHIFT, 1))
        self.assertEqual(d.down, {keymap.KEY_LEFTSHIFT})
        warnings = []
        with self.assertRaises(RuntimeError):
            d.op_key("shift", "up", 0, False, None, None, "off",
                     warnings=warnings)
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTSHIFT, 0),
                         "released on the keyboard that was holding it")
        self.assertEqual(d.down, set())
        self.assertTrue(any("were released" in w for w in warnings), warnings)

    def test_nothing_is_said_when_nothing_is_held(self):
        d = self.daemon(uinput=True)
        warns = d.op_type("a", 0, False, None, None, "on")
        self.assertEqual(warns, [])
        warns = d.op_type("a", 0, False, None, None, "off")
        self.assertEqual(warns, [])


class ThePointerIsANeighbourNotAPassenger(VkbdTest):
    """The pointer has its own protocol (zwlr_virtual_pointer_v1,
    `tests/test_vptr.py`) and this compositor advertises only the keyboard
    one, which is exactly the case worth pinning here: the keyboard half
    switching to the protocol must not carry the pointer half with it, and a
    click on a compositor that offers no pointer protocol still says what it
    always said."""

    def test_a_click_still_needs_the_kernel_device_when_only_the_keyboard_has_one(self):
        d = self.daemon(uinput=False)
        d.op_type("a", 0, False)              # the protocol is live and used
        for req in ({"op": "button", "btn": 1, "down": True},
                    {"op": "mousemove_abs", "x": 3, "y": 4},
                    {"op": "mousemove_rel", "dx": 1, "dy": 1}):
            with self.assertRaises(RuntimeError) as cm:
                d.handle(req)
            self.assertEqual(str(cm.exception), UINPUT_ERROR, req["op"])
        self.assertEqual([c for c, st in self.comp.keys], [30, 30],
                         "nothing the pointer did reached the keyboard")

    def test_a_pointer_clearmodifiers_releases_what_the_protocol_holds(self):
        """uinput works, but the shift we are holding is on the virtual
        keyboard: a key-up on the kernel device would release nothing."""
        d = self.daemon(uinput=True)
        d.mouse = RecorderDev()
        d.op_key("shift", "down", 0, False, None, None, "on")
        self.comp.events.clear()
        d.handle({"op": "button", "btn": 1, "down": True, "clearmods": True})
        self.assertEqual(d.mouse.events, [("KEY", 0x110, 1)])
        kinds = [e for e in self.comp.events if e[0] in ("key", "mods")]
        self.assertEqual(kinds[0], ("key", keymap.KEY_LEFTSHIFT, 0))
        self.assertEqual(kinds[-1], ("key", keymap.KEY_LEFTSHIFT, 1))
        self.assertIn(keymap.KEY_LEFTSHIFT, d.down, "still held afterwards")

    def test_clear_modifiers_alone_works_without_a_kernel_device(self):
        """The frozen DaemonClient API. It is a keyboard operation, so it
        must work wherever typing does."""
        d = self.daemon(uinput=False)
        d.op_key("ctrl", "down", 0, False)
        held = d.handle({"op": "clear_modifiers"})["held"]
        self.assertEqual(held, [keymap.KEY_LEFTCTRL])
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTCTRL, 0))
        d.handle({"op": "restore_modifiers", "held": held})
        self.assertEqual(self.comp.keys[-1], (keymap.KEY_LEFTCTRL, 1))
        self.assertIn(keymap.KEY_LEFTCTRL, d.down)


class TheNoticeIsSaidWhenItHappens(VkbdTest):
    manager_version = None

    def test_a_daemon_that_started_before_the_compositor_still_says_it(self):
        """The two diagnostics used to share one one-shot tag, so a daemon
        that first met a compositor without the protocol never announced the
        switch when a later one had it -- the one line telling the user that
        typing works but clicking still does not."""
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError):
            d.op_type("a", 0, False)
        self.comp.manager_version = 1
        d._vk_backoff = 0.0
        warns = d.op_type("a", 0, False)
        self.assertTrue(any("zwp_virtual_keyboard_v1" in w for w in warns),
                        warns)
        self.assertEqual(d.op_type("a", 0, False), [], "and only once")


class LocksAreNotClearedByClearModifiers(VkbdTest):
    def test_caps_lock_survives_a_clearmodifiers(self):
        """`--clearmodifiers` releases held modifiers. CapsLock is not one:
        the kernel path does not touch it (it is not in
        keymap.MODIFIER_KEYCODES), so neither may this path -- one flag, one
        meaning."""
        d = self.daemon(uinput=False)
        d.op_key("Caps_Lock", "press", 0, False)
        self.assertEqual(self.comp.mods[-1][2], vkbd.MOD_LOCK)
        d.op_type("a", 0, True)
        self.assertEqual(self.comp.mods[-1][2], vkbd.MOD_LOCK,
                         "the locked mask is not a held modifier")

    def test_a_second_press_unlocks_it(self):
        d = self.daemon(uinput=False)
        d.op_key("Caps_Lock", "press", 0, False)
        d.op_key("Caps_Lock", "press", 0, False)
        self.assertEqual(self.comp.mods[-1][2], 0)


class TheWire(VkbdTest):
    def test_the_client_sends_the_mode_and_the_daemon_reads_it(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        c = FakeClient()
        c.type_text("hi", 12, clearmods=False, vkbd_mode="on")
        self.assertEqual(sent["vkbd_mode"], "on")
        c.key("a", "press", 12, False, layout_mode="us", vkbd_mode="off")
        self.assertEqual((sent["layout_mode"], sent["vkbd_mode"]), ("us", "off"))

    def test_an_absent_mode_is_not_in_the_request(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        FakeClient().type_text("hi", 12)
        self.assertNotIn("vkbd_mode", sent)
        self.assertNotIn("layout_mode", sent)

    def test_handle_forwards_both_modes(self):
        """The daemon used to accept `layout_mode` on the wire and drop it in
        handle(), so `--layout` reached the daemon and did nothing."""
        d = self.daemon(uinput=True)
        seen = {}
        d.op_type = lambda *a, **k: seen.setdefault("type", a) and None
        d.op_key = lambda *a, **k: seen.setdefault("key", a) and None
        d.handle({"op": "type", "text": "x", "delay_ms": 0,
                  "layout_mode": "us", "vkbd_mode": "off"})
        d.handle({"op": "key", "spec": "a", "delay_ms": 0,
                  "layout_mode": "xkb", "vkbd_mode": "on"})
        self.assertEqual(seen["type"][-2:], ("us", "off"))
        self.assertEqual(seen["key"][-2:], ("xkb", "on"))

    def test_a_non_string_mode_is_refused(self):
        # handle() raises; serve_client's per-request catch-all is what turns
        # it into {"ok": false} (tests/test_hardening.py pins that half).
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "type", "text": "x", "delay_ms": 0,
                      "vkbd_mode": 7})
        self.assertIn("vkbd_mode", str(cm.exception))


class TheFlag(unittest.TestCase):
    """--vkbd, parsed exactly where and how --layout is."""

    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["wdotool"] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_it_reaches_the_context(self):
        made = []
        from wdotool import ctx as ctxmod
        orig = ctxmod.Context.__init__

        def init(this):
            orig(this)
            made.append(this)

        ctxmod.Context.__init__ = init
        try:
            rc, _, _ = self.run_cli("--vkbd", "on", "sleep", "0")
            self.assertEqual(rc, 0)
            self.assertEqual(getattr(made[-1], "vkbd_mode", None), "on")
            self.run_cli("--vkbd=off", "sleep", "0")
            self.assertEqual(getattr(made[-1], "vkbd_mode", None), "off")
            self.run_cli("--vkbd", "OFF", "--layout", "us", "sleep", "0")
            self.assertEqual(getattr(made[-1], "vkbd_mode", None), "off")
            self.assertEqual(getattr(made[-1], "layout_mode", None), "us")
            self.run_cli("sleep", "0")
            self.assertIsNone(getattr(made[-1], "vkbd_mode", None))
        finally:
            ctxmod.Context.__init__ = orig

    def test_it_is_stripped_before_the_command_sees_it(self):
        rc, _, err = self.run_cli("--vkbd", "auto", "sleep", "0")
        self.assertEqual((rc, err), (0, ""))

    def test_a_bad_value(self):
        rc, _, err = self.run_cli("--vkbd", "maybe", "sleep", "0")
        self.assertEqual(rc, 1)
        self.assertIn("invalid argument", err)
        self.assertIn("auto, on, off", err)

    def test_a_missing_value(self):
        rc, _, err = self.run_cli("--vkbd")
        self.assertEqual(rc, 1)
        self.assertIn("requires an argument", err)

    def test_it_is_not_in_xdotools_help(self):
        _, out, _ = self.run_cli("--help")
        self.assertNotIn("--vkbd", out)

    def test_it_is_a_leading_option_only(self):
        """Same rule as --layout, and for the same reason: the scan stops at
        the first non-option, so a command's own arguments keep the words. A
        scan that walked the whole line would eat them -- and it runs before
        the X11 handover, so the real xdotool would be handed the hole."""
        import tempfile

        fd, path = tempfile.mkstemp(prefix="vkbd-args-")
        os.close(fd)
        words = ["sh", "-c", 'printf "%s\\n" "$@" > ' + path, "--"]
        try:
            rc, _o, err = self.run_cli("exec", "--sync", *words,
                                       "a", "--vkbd", "on", "b")
            self.assertEqual((rc, err), (0, ""))
            with open(path) as f:
                self.assertEqual(f.read(), "a\n--vkbd\non\nb\n")
        finally:
            os.unlink(path)


class TheWireValidatesIt(unittest.TestCase):
    """The flag is screened by cli.py, but the socket is a trust boundary --
    and the daemon lower-cases whatever it is handed."""

    def test_an_unknown_mode_is_a_rejected_request(self):
        d = daemon._Daemon()
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "type", "text": "a", "vkbd_mode": "maybe"})
        self.assertIn("invalid vkbd_mode", str(cm.exception))
        self.assertIn("auto, on, off", str(cm.exception))

    def test_a_non_string_mode_is_refused(self):
        d = daemon._Daemon()
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "type", "text": "a", "vkbd_mode": 7})
        self.assertIn("expected a string", str(cm.exception))

    def test_the_environment_stays_lenient(self):
        """A typo in a shell profile must not stop the tool typing, which is
        the opposite trade from a request: nobody can fix WDOTOOL_VKBD from
        inside the command that is failing because of it."""
        d = daemon._Daemon()
        old = os.environ.get(daemon.VKBD_ENV)
        os.environ[daemon.VKBD_ENV] = "maybe"
        try:
            self.assertEqual(d._vkbd_setting(), "auto")
        finally:
            if old is None:
                os.environ.pop(daemon.VKBD_ENV, None)
            else:
                os.environ[daemon.VKBD_ENV] = old


class ADeviceThatFailsMidRelease(VkbdTest):
    """T38: every held key is released on the device or named to the user.

    `_own_sink` walks the held keycodes on the device that is holding them.
    A write to a uinput device that has gone (the daemon respawned its
    devices, or the kernel dropped them) raises EIO in the middle of that
    walk, and daemon.py:_own_sink breaks out of the loop there -- the
    modifiers after the failing one are never written to the device at all.
    What must not happen is the daemon going on believing they are held --
    `self.down` would then hide a real shift from the next command, which is
    the defect `_own_sink` was written for in the first place -- and what
    must not happen either is the user not being told, because a key stuck
    down on a device wdotool is no longer typing through is something only
    they can clear.

    So the property is: released-on-the-device plus named-in-the-warning
    covers everything that was held, and nothing outside it is written. The
    exact split between the two halves is today's break-on-first-error
    implementation and is pinned separately, marked as such.

    One inaccuracy this pins as-is: SINK_SWITCH_WARNING (daemon.py:356) says
    the named keys "were released", and after an EIO that is true of the
    first one and a hope about the rest. The message is right about what the
    daemon no longer holds and wrong about what reached the device. Changing
    that wording is a product change outside this batch's allowed fixes, so
    the assertion below matches the shipped text; a follow-up should make it
    "released, or lost with the device" for the broken-walk case."""

    class FlakyDev(RecorderDev):
        """RecorderDev that stops taking releases after the `fail_at`th."""

        def __init__(self, fail_at=2):
            RecorderDev.__init__(self)
            self.fail_at = fail_at
            self.releases = 0

        def key(self, code, down):
            if not down:
                self.releases += 1
                if self.releases >= self.fail_at:
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
            RecorderDev.key(self, code, down)

    def test_every_held_modifier_is_released_or_named(self):
        d = self.daemon(uinput=True)
        d.kb = self.FlakyDev(fail_at=2)
        d.op_key("ctrl+shift+alt", "down", 0, False, None, None, "off")
        held = {keymap.KEY_LEFTCTRL, keymap.KEY_LEFTSHIFT, keymap.KEY_LEFTALT}
        self.assertEqual(d.down, held)          # 29, 42, 56 on the kernel one
        d.kb.events.clear()

        warns = d.op_type("a", 0, False, None, None, "on")
        released = {c for _k, c, v in
                    [e for e in d.kb.events if e[0] == "KEY"] if v == 0}
        switch = [w for w in warns if "were released" in w]
        self.assertEqual(len(switch), 1, warns)
        labels = {keymap.KEY_LEFTCTRL: "ctrl", keymap.KEY_LEFTSHIFT: "shift",
                  keymap.KEY_LEFTALT: "alt"}
        named = {c for c, label in labels.items() if label in switch[0]}
        # the property: no key it never held is written, and between the
        # device and the warning nothing held is left unaccounted for
        self.assertLessEqual(released, held)
        self.assertEqual(released | named, held, (released, named, switch[0]))
        # ...and today's split, which is break-on-first-error: 29 sorts
        # before 42 and 56, so the first release lands and the second raises
        self.assertEqual(released, {keymap.KEY_LEFTCTRL},
                         "the second write is the one that fails")
        # the point: nothing is left behind in the model, whatever the
        # device did with the writes
        self.assertEqual(d.down, set())
        self.assertEqual(self.comp.pressed(), [30])   # and the `a` still typed

    def test_the_switch_is_not_reported_twice_for_the_same_hold(self):
        """The next command has nothing held and nothing to say: the warning
        is a state change, not a fact about the environment."""
        d = self.daemon(uinput=True)
        d.kb = self.FlakyDev(fail_at=2)
        d.op_key("ctrl+shift+alt", "down", 0, False, None, None, "off")
        d.op_type("a", 0, False, None, None, "on")
        warns = d.op_type("b", 0, False, None, None, "on")
        self.assertEqual([w for w in warns if "were released" in w], [])


class ACompositorWithNoSeat(VkbdTest):
    comp_kw = {"with_seat": False}

    def test_open_says_which_global_is_missing_and_creates_nothing(self):
        """zwp_virtual_keyboard_manager_v1.create_virtual_keyboard takes the
        seat as a non-null argument -- unlike zwlr_virtual_pointer_v1, where
        it is allow-null and tests/test_vptr.py proves a NULL is accepted. So
        a compositor advertising the manager and no wl_seat is a refusal
        before anything is created, not a request with a zero in it."""
        with self.assertRaises(vkbd.VkbdError) as cm:
            vkbd.VirtualKeyboard.open()
        self.assertIn("wl_seat", str(cm.exception))
        self.assertEqual(self.comp.created, [])


def _eventually(pred, tries=100):
    import time

    for _ in range(tries):
        if pred():
            return True
        time.sleep(0.01)
    return pred()


if __name__ == "__main__":
    unittest.main(verbosity=1)
