"""XTEST: the requests xdotool types with, routed to the input daemon.

Design section 6. The proxy owns `FakeInput` because Xwayland's own XTEST
reaches X windows and nothing else: on sway, `xdotool type` into an *X* window
is byte-perfect, CJK included [recon/env.md 2.3], and into a focused `foot` it
writes an empty file [recon/env.md 2.7]. There is no graceful fallback to hand
a client either -- with XTEST answered `present=0` the tool prints
`Warning: XTEST extension unavailable on '(null)'`, sends **no** input request
at all and exits 0 for `key`/`type`, 1 for `click` [recon/tools.md 4.9] -- so
`QueryExtension("XTEST")` keeps upstream's answer and the requests carrying
that major become daemon operations here.

Four facts shape every line below, and all four are measured:

* **the pads are garbage.** `FakeInput` is 36 bytes and libXtst clears none of
  them: the three `key ctrl+a` presses carry `rootX=3 rootY=2` and the release
  of the same command carries `rootX=256 rootY=0`, both of them Xlib's leftover
  output buffer [M recon/tools/caps/raw.jsonl MARK 1; recon/wire.md 5.2, where
  a `WarpPointer`'s `dst_x/dst_y` turn up in the next FakeInput's pad]. Seven
  fields are read (`decode_fake_input`) and nothing else is validated.
* **a keycode is not a spec.** xdotool steals **keycode 8** to type a character
  outside the layout -- `ChangeKeyboardMapping(first=8, n=1, keysyms=[EuroSign
  as 0x010020ac])`, then `FakeInput KeyPress detail=8`, then two restores
  [M recon/tools/caps/plain.jsonl MARK 20-21; recon/seams.md 5.3's xtrace]. The
  daemon speaks the compositor's layout and `keymap.resolve_token("8")` is
  evdev code 0, a key that does not exist [wdotool/keymap.py:345-354]. So the
  keycode is resolved to a KEYSYM here, and the daemon is handed the keysym's
  NAME.
* **names, not hex.** `resolve_token("0xffe3", None)` answers "key '0xffe3' is
  not reachable on the US layout" and `resolve_token("Control_L", None)`
  answers `(29, False)` [M 2026-09-10, this box]: `KEYSYM_KEYS` has a row for
  every modifier and `_keysym_value_to_key` has none, so a hex spec would make
  every `ctrl+` chord unreachable (design section 6.2 step 4).
* **the daemon does not dedupe keys.** `key(spec, direction="down")` twice for
  `Control_L` writes `EV_KEY 29 value 1` twice to the uinput node, and two
  `up`s write value 0 twice [M 2026-09-10, a spawned daemon under
  `WDOTOOL_FAKE_UINPUT=1` with a file for the node]. `xdotool key ctrl+a` sends
  `KP 37, KP 37, KP 37, KP 38, KR 37, KR 37, KR 38` [recon/tools.md 4.5], and
  the compositor refcounts presses per seat, so the held set lives here
  (design section 6.3).

When there is no daemon route at all -- no `/dev/uinput`, no virtual-keyboard
protocol, a socket owned by another uid [recon/seams.md 5.1] -- `FakeInput` and
`WarpPointer` are **forwarded to Xwayland** and one line carries the sentence
and the route (design section 6.7). Refusing instead would take away the one
thing that works today: typing into an X window.
"""

import json
import struct
import time

from w11common.errors import CmdError
from wdotool import backend as backend_mod
from wdotool import daemon as daemon_mod
from wdotool import keysyms as keysyms_mod
from xw11 import policy, wire

#: `FakeInput`'s `type` field [recon/wire.md 5.2].
KEY_PRESS = 2
KEY_RELEASE = 3
BUTTON_PRESS = 4
BUTTON_RELEASE = 5
MOTION_NOTIFY = 6

#: XTEST minor opcodes.
GET_VERSION = 0
COMPARE_CURSOR = 1
FAKE_INPUT = 2
GRAB_CONTROL = 3

#: `ChangeKeyboardMapping`, the one core opcode this file needs that
#: `xw11/wire.py` does not name (its table stops at the requests the batches
#: before this one handled). 100 is fixed by the protocol, like every core
#: opcode [/usr/share/xcb/xproto.xml].
OP_CHANGE_KEYBOARD_MAPPING = 100

#: `MappingNotify`, which carries no window and is not in `xw11/wire.py`'s
#: event constants: the proxy's own connection gets one whenever any client
#: rewrites the map, and the table below is fed from it (design section 6.2
#: feed two). `request` is at 4 (0 Modifier, 1 Keyboard, 2 Pointer),
#: `first_keycode` at 5 and `count` at 6 [/usr/share/xcb/xproto.xml].
EV_MAPPING_NOTIFY = 34
MAPPING_KEYBOARD = 1

#: The whole keycode range a keyboard mapping covers: the setup reply's
#: min_keycode is 8 and its max is 255 on both servers measured
#: [recon/wire.md 1.2], and xdotool asks for `first=8 count=247` itself
#: [recon/tools.md 4.5].
KEYMAP_FIRST = 8
KEYMAP_COUNT = 248

#: How long the daemon connection is kept after the last `FakeInput`, and how
#: long the proxy waits before dialling again after a refusal (design section
#: 6.4, 6.7). A cold spawn-and-connect is 52.3 ms and a warm round trip
#: 0.243 ms [recon/seams.md 5.1], so holding the connection is what makes a
#: second `xdotool type` in the same second cost a round trip and not a spawn;
#: the daemon's own idle rules resume the moment it is dropped, because its
#: 15-second checks are skipped while a client is connected.
DAEMON_LINGER = 60.0
DAEMON_RETRY = 5.0

#: The daemon's warning for a key the active layout cannot reach
#: [wdotool/keymap.py:333, 344]. Matched as a substring, because the sentence
#: names the token and the layout: "key '0x010020ac' is not reachable on the US
#: layout. Ignoring it."
UNREACHABLE = "is not reachable on the"

#: `NoSymbol`, which is what a keycode the map does not bind reads as.
NO_SYMBOL = 0

#: X modifier bits, and the keysym names that set them, for the `mask` field of
#: a `QueryPointer` the proxy answers itself (design section 6.6). Only what
#: this proxy is HOLDING is in it: a modifier the user is holding on a physical
#: keyboard reaches Xwayland's own XKB state and not this table, and on a
#: shadow there is no upstream answer to take it from -- a row, route 4 (evdev,
#: which the daemon already owns on the uinput path) at the cost of read access
#: to /dev/input/event*.
MOD_MASKS = {
    "Shift_L": 0x01, "Shift_R": 0x01, "Caps_Lock": 0x02,
    "Control_L": 0x04, "Control_R": 0x04,
    "Alt_L": 0x08, "Alt_R": 0x08, "Meta_L": 0x08, "Meta_R": 0x08,
    "Num_Lock": 0x10, "ISO_Level3_Shift": 0x80, "Mode_switch": 0x80,
    "Super_L": 0x40, "Super_R": 0x40, "Hyper_L": 0x40, "Hyper_R": 0x40,
}

#: Button `b` is `Button<b>Mask` = 1 << (7 + b), for 1..5 [recon/wire.md 4.1].
BUTTON1_MASK = 0x0100


def _keysym_names():
    """`keysym -> name`, the inverse of `wdotool.keysyms.NAME_TO_KEYSYM`, first
    name in that table's own order winning.

    2512 names collapse onto fewer keysyms -- the aliases are real (`Control_L`
    has one name, `dead_grave` shares a value with nothing, `XF86AudioRaiseVolume`
    is one of several) -- and which alias wins matters only in that the daemon
    must accept it: `keymap.resolve_token` looks a name up in `KEYSYM_KEYS`, in
    the active layout and finally in `NAME_TO_KEYSYM` itself, so every name in
    this table round-trips by construction."""
    out = {}
    for name, value in keysyms_mod.NAME_TO_KEYSYM.items():
        out.setdefault(value, name)
    return out


KEYSYM_NAMES = _keysym_names()


def spec_for(keysym: int) -> str:
    """The token the daemon takes for one keysym (design section 6.2 step 4).

    The NAME when there is one, because `resolve_token` sends a name through
    `keysym_to_key`, where `KEYSYM_KEYS` answers `Control_L`, `Return`, `F5`
    and `KP_7` on every layout [wdotool/keymap.py:304-320], and the raw
    `0x<hex>` path goes through `_keysym_value_to_key`, which has no row for
    any modifier keysym: measured on this box, `resolve_token("0xffe3", None)`
    is "not reachable" while `resolve_token("Control_L", None)` is `(29,
    False)`. Hex is the fallback for a keysym with no name -- xdotool's own
    `0x010020ac` for `€` is exactly that -- and it is what makes the typed
    fallback below fire rather than a silent nothing."""
    name = KEYSYM_NAMES.get(keysym)
    return name if name is not None else "0x%08x" % keysym


def codepoint(keysym: int):
    """The character a keysym names, or None. Latin-1 keysyms are their own
    codepoint and the `0x01000000 | codepoint` space is Unicode's
    [wdotool/keymap.py:268-302]; those two are the shapes xdotool plants into a
    spare keycode [recon/tools.md 4.6]."""
    if 0x20 <= keysym <= 0xFF:
        return keysym
    if keysym & 0xFF000000 == 0x01000000:
        cp = keysym & 0xFFFFFF
        return cp if cp <= 0x10FFFF else None
    return None


def decode_fake_input(buf: bytes):
    """The seven fields of a `FakeInput`, and nothing else (design section 6.1).

    `(type, detail, time, root, root_x, root_y, deviceid)`, or None for a frame
    that is not 36 bytes. Everything at 6-7, 16-23 and 28-34 is Xlib's leftover
    output buffer and is not read, not validated and not passed on: a proxy
    that checked those pads would reject real xdotool traffic
    [recon/wire.md 5.2].
    """
    if len(buf) < 36:
        return None
    kind, detail = buf[4], buf[5]
    delay, root = struct.unpack_from("<II", buf, 8)
    root_x, root_y = struct.unpack_from("<hh", buf, 24)
    return (kind, detail, delay, root, root_x, root_y, buf[35])


class KeyMap:
    """keycode -> keysyms, for 8..255, with the three feeds of design section
    6.2.

    Level 1 is what a spec is built from: xdotool picks the keycode whose
    level-1 keysym it wants and sends the modifiers as their own keycodes --
    `key ctrl+a` is `KeyPress 37, KeyPress 38` with 37 = Control_L
    [recon/tools.md 4.5] -- so the table never has to know which level a
    modifier state would have selected.
    """

    def __init__(self):
        #: keycode -> tuple of keysyms, level 1 first
        self.rows = {}
        #: `keysyms_per_keycode` of the last reply, for the log
        self.per = 0

    def keysym(self, keycode: int) -> int:
        row = self.rows.get(keycode)
        return row[0] if row else NO_SYMBOL

    def feed_reply(self, pkt: bytes, first: int = KEYMAP_FIRST) -> int:
        """A `GetKeyboardMapping` reply: `keysyms_per_keycode` at byte 1 and
        `count * per` CARD32s from 32 [recon/wire.md 10]. Returns how many
        keycodes it carried, so the caller can log a reply that was empty."""
        if len(pkt) < 32 or pkt[0] != 1:
            return 0
        per = pkt[1]
        if per <= 0:
            return 0
        (words,) = struct.unpack_from("<I", pkt, 4)
        syms = struct.unpack_from("<%dI" % words, pkt, 32) if words else ()
        self.per = per
        n = len(syms) // per
        for i in range(n):
            self.rows[first + i] = tuple(syms[i * per:(i + 1) * per])
        return n

    def feed_change(self, frame: bytes) -> int:
        """A client's `ChangeKeyboardMapping`, read BEFORE the frame goes
        upstream: `keycode_count` at 1, `first_keycode` at 4,
        `keysyms_per_keycode` at 5, keysyms from 8 [recon/wire.md 10].

        The count is bounded by the frame's own length as well as by its
        header, because the header is a client's word: xdotool sends
        `keycode_count = 1, per = 1` and twelve bytes [M
        recon/tools/caps/plain.jsonl seq 33, decoded `{"first": 8, "n": 1}`],
        and a frame claiming more keysyms than it carries must not read past
        its end.
        """
        if len(frame) < 8:
            return 0
        count = frame[1]
        first, per = frame[4], frame[5]
        if per <= 0 or count <= 0:
            return 0
        have = (len(frame) - 8) // 4
        count = min(count, have // per)
        if count <= 0:
            return 0
        syms = struct.unpack_from("<%dI" % (count * per), frame, 8)
        for i in range(count):
            self.rows[first + i] = tuple(syms[i * per:(i + 1) * per])
        return count


class ProxyDaemon(daemon_mod.DaemonClient):
    """`DaemonClient` whose warnings come back instead of going to stderr.

    The base prints `resp["warnings"]` to the client's stderr
    [wdotool/daemon.py:2177] because its caller is a command with a terminal.
    This caller is a long-lived server whose diagnostics belong in its log --
    and one of those warnings is not a diagnostic at all but the fact the typed
    fallback of design section 6.2 turns on: "key '0x010020ac' is not reachable
    on the US layout." So `_rpc` is the base's protocol with the printing
    replaced by a return value; it speaks the same JSON-per-line to the same
    socket [recon/seams.md 5.1], which is what `tests/support.py:FakeDaemon`
    answers and what `tests/test_xw11_xtest.py` round-trips over a real one.
    """

    def __init__(self, sock, log=None):
        super().__init__(sock)
        self.log = log
        #: the warnings of the LAST call, in order
        self.warnings = []

    def _rpc(self, **req):
        try:
            self._sock.sendall((json.dumps(req) + "\n").encode())
            line = self._rfile.readline()
        except OSError as e:
            raise CmdError("wdotool daemon connection lost: %s" % e) from None
        if not line:
            raise CmdError("wdotool daemon connection lost")
        try:
            resp = json.loads(line)
        except ValueError:
            raise CmdError("wdotool daemon sent an invalid reply") from None
        if not isinstance(resp, dict):
            raise CmdError("wdotool daemon sent an invalid reply")
        self.warnings = list(resp.get("warnings") or [])
        if self.log is not None:
            for warning in self.warnings:
                self.log("the input daemon said: %s" % warning)
        if not resp.get("ok"):
            raise CmdError(resp.get("error", "wdotool daemon error"))
        return resp

    def key_checked(self, spec: str, direction: str) -> list:
        """One `key` op, answering the warnings it produced. The daemon returns
        them in the response and raises nothing for a key it could not reach --
        `parse_keyseq` warns and skips, which is xdotool's own behaviour
        [wdotool/keymap.py:360] -- so the warning IS the answer."""
        self.key(spec, direction, 0, False)
        return list(self.warnings)


def connect_daemon(log=None):
    """The daemon route: `connect_or_spawn`, which dials
    `$XDG_RUNTIME_DIR/wdotool.sock`, checks `SO_PEERCRED` and double-forks a
    daemon when nothing answers [recon/seams.md 5.1]. One function rather than
    one call, so that a test can put its own refusal behind it without a
    two-second spawn deadline in the way."""
    got = ProxyDaemon.connect_or_spawn()
    got.log = log
    return got


class Engine:
    """One proxy's XTEST state: the table, the route, the pointer model."""

    def __init__(self, server):
        self.server = server
        self.keymap = KeyMap()
        self.daemon = None
        #: monotonic, when the route may be tried again after a refusal
        self.retry_at = 0.0
        #: monotonic, when the last operation went through it
        self.last_used = 0.0
        #: what `seed_pointer` last told THIS connection, so a position the
        #: daemon already has is not sent again on every `QueryPointer`
        self.seeded = None
        #: keycodes whose press became a `type` and whose release is therefore
        #: consumed, per client connection (design section 6.2's fallback). Not
        #: on `ClientConn`, which is batch 1's file: `conn.held` is the set that
        #: file already carries and this is the second half of the same state.
        self.typed = {}
        #: `{id(conn): {keycode: the spec its press used}}`. The spec cannot be
        #: recomputed at release time: `xdotool type '€'` plants the keysym in
        #: keycode 8, presses it, RESTORES the keycode to NoSymbol and only
        #: then releases it [M tests/fixtures/xw11/remap-keycode8.hex, the
        #: capture's own order], so a release that re-read the table would send
        #: `key 0x00000000 up` and leave the real key down.
        self.pressed = {}
        #: one line per refusal, not one per request
        self._said = set()
        self._keymap_loaded = False

    # -- the log ---------------------------------------------------------------

    def say(self, text: str) -> None:
        self.server.say(text)

    def say_once(self, key: str, text: str) -> None:
        if key in self._said:
            return
        self._said.add(key)
        self.say(text)

    # -- the table -------------------------------------------------------------

    def load_keymap(self) -> bool:
        """The table from the own connection's `GetKeyboardMapping(8, 248)`,
        which batch 2 keeps raw [recon/seams.md, requests-batch-2.md item 11].
        Once per open; `reread_keymap` is the asynchronous refresh."""
        if self._keymap_loaded:
            return True
        own = self.server.own
        if own is None or own.keyboard_mapping is None:
            return False
        got = self.keymap.feed_reply(own.keyboard_mapping, KEYMAP_FIRST)
        self._keymap_loaded = got > 0
        return self._keymap_loaded

    def reread_keymap(self) -> bool:
        """One asynchronous `GetKeyboardMapping` on the proxy's own connection.

        The feed for a remap by a client that is not going through this proxy
        (design section 6.2 feed three): `MappingNotify` says the map moved and
        the re-read lands one round trip later. A client's OWN
        `ChangeKeyboardMapping` is not this path -- that one is applied before
        the request is forwarded, so the `FakeInput` behind it resolves against
        the map xdotool just planted."""
        own = self.server.own
        if own is None or not own.open:
            return False

        def took(pkt):
            n = self.keymap.feed_reply(pkt, KEYMAP_FIRST)
            self.server.debug_say("xtest: the keyboard map was re-read after a "
                                  "MappingNotify (%d keycodes)" % n)
        try:
            own.send(wire.OP_GET_KEYBOARD_MAPPING, 0,
                     struct.pack("<BB2x", KEYMAP_FIRST, KEYMAP_COUNT),
                     on_reply=took)
        except Exception as e:                    # UpstreamGone, and no more
            self.say("xtest: asking the upstream for the keyboard map after a "
                     "MappingNotify failed (%s); the table keeps what it had" % (e,))
            return False
        return True

    # -- the route -------------------------------------------------------------

    def route(self):
        """The daemon, or None. `None` is the case design section 6.7 forwards
        to Xwayland: it types into an X window there and into nothing at all
        when a native window has the focus [recon/env.md 2.3, 2.7], which is
        the measured baseline and strictly more than a `BadAccess` would be."""
        if self.daemon is not None:
            return self.daemon
        now = time.monotonic()
        if now < self.retry_at:
            return None
        try:
            self.daemon = connect_daemon(log=self.say)
        except CmdError as e:
            self.retry_at = now + DAEMON_RETRY
            self.say_once("no-route", self.no_route_line(e))
            return None
        self.seeded = None
        self.retry_at = 0.0
        self._said.discard("no-route")
        self.server.debug_say("xtest: the input daemon answered; FakeInput is "
                              "routed to it from here")
        return self.daemon

    @staticmethod
    def no_route_line(exc) -> str:
        """The clone's own sentence, and the route (design section 6.7).

        Not a refusal on the wire: the request is forwarded to Xwayland, whose
        XTEST still reaches every X window on the display. What is missing is
        the native half, and this line says how to get it."""
        return ("the input daemon has no route (%s): the XTEST request is "
                "forwarded to Xwayland, which reaches every X window on this "
                "display and no native one [recon/env.md 2.3, 2.7]. That half "
                "is not yet here on this box, and the route is the daemon's "
                "own -- /dev/uinput with the udev rule that grants the seat "
                "user access to it, or zwp_virtual_keyboard_v1 where the "
                "compositor speaks it (AGENTS.md rung 4, and rung 1 for the "
                "protocol); retrying in %gs" % (exc, DAEMON_RETRY))

    def drop(self, why: str) -> None:
        """Let the daemon go. Its own idle rules resume the moment we do: the
        900-second timer and the 15-second socket check are skipped while a
        client is connected [recon/seams.md 5.1]."""
        if self.daemon is None:
            return
        try:
            self.daemon.close()
        except OSError:
            pass
        self.daemon = None
        self.seeded = None
        self.server.debug_say("xtest: the daemon connection is closed (%s)" % why)

    def holding(self) -> bool:
        """Is any client holding a key or a button through this engine?

        A `xdotool keydown ctrl` is one operation and then silence, so the
        linger timer would drop the connection out from under a key that is
        still down on the seat: the release at `on_close` would then have no
        route, and the daemon's own `self.down` keeps the key pressed until its
        900-second idle exit [wdotool/daemon.py, the idle timer; its
        release-on-disconnect is the COMPOSITOR's, for the virtual devices, and
        not a per-client undo]. The connection stays while anything is held.
        """
        for conn in getattr(self.server, "conns", ()):
            if conn.held or conn.buttons:
                return True
        return False

    def tick(self, now=None) -> None:
        """Called from the loop: close the connection `DAEMON_LINGER` after the
        last operation went through it, and never while a key or a button is
        still down on it."""
        if self.daemon is None or not self.last_used or self.holding():
            return
        now = time.monotonic() if now is None else now
        if now - self.last_used >= DAEMON_LINGER:
            self.drop("no input for %gs" % DAEMON_LINGER)

    def next_deadline(self):
        """When the loop next has to wake for this engine, or None. Nothing to
        wake for while a key is held: the release itself is an operation, and
        it restarts the timer from there."""
        if self.daemon is None or not self.last_used or self.holding():
            return None
        return self.last_used + DAEMON_LINGER

    # -- the pointer model ------------------------------------------------------

    def moved(self, x: int, y: int) -> None:
        """A routed absolute motion or warp: the proxy's own last known
        position, which design section 5.6 also samples for Enter/Leave.

        The daemon's own model moved with it, so this is also the last thing it
        was told: `seeded` is set here and not only in `seed()`, which is what
        keeps `QueryPointer` after a `mousemove` from spending a round trip
        telling the daemon what it just did."""
        self.server.pointer_model = (int(x), int(y))
        self.seeded = (int(x), int(y))

    def position(self):
        """`(x, y)` from the first source that knows, or None (design section
        6.6): the compositor's own pointer -- GNOME's Meta pointer, Wayfire's
        `stipc/get-cursor` [recon/seams.md 2.3] -- then the last position this
        proxy routed. sway's IPC carries no cursor and the daemon refuses to
        guess, so the third answer is "nobody knows" and upstream's own number
        stands. `Server.pointer_position` is the same order design section 5.6
        samples crossings with; one function, so the two cannot drift."""
        return self.server.pointer_position()

    def seed(self, x: int, y: int) -> None:
        """Tell the daemon where the pointer really is, so a relative move
        starts from truth (design section 6.6). Once per position per daemon
        connection: `getmouselocation` is one `QueryPointer` and `mousemove`
        sends two [recon/tools.md 4.2, 4.5], and the daemon's model does not
        move between them."""
        if self.daemon is None or self.seeded == (x, y):
            return
        try:
            self.daemon.seed_pointer(int(x), int(y))
        except CmdError as e:
            self.say("xtest: seeding the daemon's pointer with (%d, %d) failed "
                     "(%s)" % (x, y, e))
            self.drop("the connection failed")
            return
        self.seeded = (x, y)

    def modifier_mask(self) -> int:
        """`mask` for a `QueryPointer` the proxy answers itself: what THIS
        proxy is holding, keys and buttons both.

        A modifier the user is holding on a real keyboard is not in it. That is
        not yet here and the route is evdev -- `/dev/input/event*`, which the
        daemon already opens on the uinput path -- at the cost of read access
        to the devices (AGENTS.md rung 4)."""
        mask = 0
        for conn in getattr(self.server, "conns", ()):
            for keycode in conn.held:
                mask |= MOD_MASKS.get(self.spec_held(conn, keycode), 0)
            for btn in conn.buttons:
                if 1 <= btn <= 5:
                    mask |= BUTTON1_MASK << (btn - 1)
        return mask

    # -- who gets a keystroke (design section 6.1, measured) ---------------------

    def x_owns_the_keyboard(self) -> bool:
        """Is the compositor's focused toplevel an X window?

        When it is, a `FakeInput` KeyPress goes to **Xwayland**, untouched, and
        this proxy stays out of the way. That is not a preference, it is the
        measured baseline: real xdotool through Xwayland types `ünï €ur ß λ 日`
        into an X window byte for byte [recon/env.md 2.3], because it plants
        the keysym it wants into a spare keycode of *Xwayland's own* map and
        fires that -- a map no compositor reads and no seat has. Routed to the
        input daemon instead, the same command loses every one of those
        characters: measured through this proxy on 2026-09-11, a headless sway
        with no /dev/uinput, `xdotool type 'ünï €ur ß λ 日'` into an xterm -u8
        wrote `n ur   ` and the daemon said "Can't type character 'ü' (not on
        the US layout). Skipping." six times. AGENTS.md's rule decides the
        rest: X did it, so we do it.

        The native half is untouched by this. A `foot` with the focus has no X
        keycode to plant anything in, Xwayland's XTEST reaches it with nothing
        at all [recon/env.md 2.7], and that is the case this engine exists for.

        Buttons and motion are NOT split this way and never ask this question:
        a click has to move the SEAT's pointer, because that is what raises and
        focuses a window and what Xwayland then sees -- and Xwayland's own warp
        moves only Xwayland's pointer (measured 2026-09-11: `WarpPointer` to
        321,123 straight at Xwayland moved its `QueryPointer` and the
        compositor's cursor not at all).

        "Nobody knows" answers True: with no registry, or nothing focused, the
        behaviour that stands is the one that works today.
        """
        shadows = self.server.shadows
        if shadows is None:
            return True
        shadows.snapshot()
        entry = shadows.focused_entry()
        if entry is None:
            return True
        return not entry.shadow

    def this_proxy_holds(self, conn, keycode) -> bool:
        """Did the press of this keycode go to the input daemon?

        A release goes where its press went, and that is not always where the
        focus is now: `xdotool keydown ctrl` (a foot focused, so the press is
        routed to the daemon), a click that moves the compositor's focus to an
        xterm, `xdotool keyup ctrl` -- with the focus alone deciding, the
        release went to Xwayland, which had never seen the press, and the
        daemon held Control down on the seat until the client disconnected. A
        keycode this proxy is holding therefore keeps its half, in both
        directions: a second press of it is the repeat `_key_op` drops, and its
        release is the `up` the seat is waiting for.
        """
        return keycode in conn.held or keycode in self.typed.get(id(conn), ())

    def pass_to_xwayland(self, kind, detail):
        """Forward one key `FakeInput`. Nothing is let go of here: this is
        reached only for a keycode neither half is holding (`this_proxy_holds`),
        so Xwayland owns the press and the release both."""
        self.server.debug_say("xtest: an X window has the compositor's focus, "
                              "so this %s of keycode %d goes to Xwayland, whose "
                              "own keymap is the one xdotool just rewrote "
                              "[recon/env.md 2.3]"
                              % ("KeyPress" if kind == KEY_PRESS
                                 else "KeyRelease", detail))
        return policy.FORWARD

    # -- one FakeInput ----------------------------------------------------------

    def fake_input(self, conn, frame: bytes):
        """One `FakeInput`, as the daemon operations it means, or `FORWARD`.

        Returns `policy.FORWARD` when there is no daemon to route to, which is
        the one case the frame goes to Xwayland untouched (design section 6.7);
        None otherwise, and the request is consumed.
        """
        got = decode_fake_input(frame)
        if got is None:
            # Not 36 bytes: nothing here can say what it meant. Upstream
            # answers `BadLength` itself, with the serial the tools print.
            return policy.FORWARD
        kind, detail, delay, _root, root_x, root_y, _dev = got
        if kind in (KEY_PRESS, KEY_RELEASE) \
                and not self.this_proxy_holds(conn, detail) \
                and self.x_owns_the_keyboard():
            return self.pass_to_xwayland(kind, detail)
        client = self.route()
        if client is None:
            return policy.FORWARD
        self.load_keymap()
        op = self._operation(conn, kind, detail, root_x, root_y)
        if op is None:
            return None
        if delay or conn.deferred:
            # Order per client is the one thing a delay may never change, so an
            # operation with no delay of its own still goes behind one that has
            # not run yet.
            self.defer(conn, delay, op)
            return None
        self.run(conn, op)
        return None

    def _operation(self, conn, kind, detail, root_x, root_y):
        """The callable one FakeInput becomes, or None when it means nothing at
        all -- a repeated press, a release of a key nobody holds, a keycode the
        map binds to `NoSymbol`. Deciding this BEFORE the delay queue is what
        makes the held set honest: `key ctrl+a`'s three `KeyPress 37`s are one
        press whether or not they carry a delay."""
        if kind in (KEY_PRESS, KEY_RELEASE):
            return self._key_op(conn, kind, detail)
        if kind in (BUTTON_PRESS, BUTTON_RELEASE):
            down = kind == BUTTON_PRESS
            if down:
                conn.buttons.add(detail)
            else:
                conn.buttons.discard(detail)
            return lambda: self.daemon.button(detail, down)
        if kind == MOTION_NOTIFY:
            if detail:
                return lambda: self.daemon.mousemove_rel(root_x, root_y)
            return lambda: self.move_abs(root_x, root_y)
        self.say("xtest: FakeInput type %d is not one of the five the protocol "
                 "has [recon/wire.md 5.2]: nothing was injected" % kind)
        return None

    def move_abs(self, x, y):
        """An absolute motion, and the model that follows it: every position
        this proxy routes is a source design section 5.6 samples for
        Enter/Leave on a shadow."""
        self.daemon.mousemove_abs(x, y)
        self.moved(x, y)

    def _key_op(self, conn, kind, keycode):
        """Design section 6.3's held set, and section 6.2's typed fallback."""
        typed = self.typed.setdefault(id(conn), set())
        if kind == KEY_RELEASE:
            if keycode in typed:
                # Its press became a `type`, which is a press AND a release:
                # sending an `up` for a key nothing is holding would be a
                # second release the compositor refcounts against the seat.
                typed.discard(keycode)
                return None
            if keycode not in conn.held:
                self.server.debug_say("xtest: KeyRelease of keycode %d, which "
                                      "this client is not holding: dropped"
                                      % keycode)
                return None
            conn.held.discard(keycode)
            spec = self.spec_held(conn, keycode)
            self.pressed.get(id(conn), {}).pop(keycode, None)
            return lambda: self.daemon.key(spec, "up", 0, False)
        if keycode in conn.held:
            self.server.debug_say("xtest: KeyPress of keycode %d, which this "
                                  "client already holds: dropped (xdotool "
                                  "sends three for one ctrl+a "
                                  "[recon/tools.md 4.5])" % keycode)
            return None
        keysym = self.keymap.keysym(keycode)
        if keysym == NO_SYMBOL:
            self.say("xtest: keycode %d is NoSymbol in this server's keyboard "
                     "map: nothing was injected" % keycode)
            return None
        conn.held.add(keycode)
        return lambda: self._press(conn, keycode, keysym)

    def _press(self, conn, keycode, keysym):
        """One press, with the fallback of design section 6.2 behind it: a
        character keysym the daemon cannot reach on the compositor's layout is
        TYPED instead, which is the route xdotool's own spare-keycode remap
        takes on X and cannot take here."""
        spec = spec_for(keysym)
        warnings = self.daemon.key_checked(spec, "down")
        if not any(UNREACHABLE in w for w in warnings):
            self.pressed.setdefault(id(conn), {})[keycode] = spec
            return
        cp = codepoint(keysym)
        if cp is None:
            self.say("xtest: keycode %d resolves to keysym %s, which the "
                     "daemon cannot reach and which names no character: "
                     "nothing was typed" % (keycode, spec))
            conn.held.discard(keycode)
            return
        self.daemon.type_text(chr(cp), 0)
        conn.held.discard(keycode)
        self.typed.setdefault(id(conn), set()).add(keycode)
        self.server.debug_say("xtest: keysym %s is not on the compositor's "
                              "layout, so %r was typed instead and the release "
                              "of keycode %d is consumed"
                              % (spec, chr(cp), keycode))

    # -- the delay queue ---------------------------------------------------------

    def defer(self, conn, delay_ms: int, op) -> None:
        """`FakeInput`'s `time` is a delay in milliseconds and xdotool always
        sends 0 [recon/tools.md 4.5]. A client that sends one queues the
        operation behind whatever that client already queued -- order per
        client is the one thing a delay may not change -- and the loop's own
        `select()` timeout is what runs it. No sleep runs in the loop."""
        due = time.monotonic() + delay_ms / 1000.0
        if conn.deferred:
            due = max(due, conn.deferred[-1][0])
        conn.deferred.append((due, lambda: self.run(conn, op)))

    def run(self, conn, op) -> None:
        """One operation against the daemon, with the refusal rule of design
        section 3.3: a `CmdError` is one line in the log and no packet at all,
        because the request is void on the wire either way."""
        if self.daemon is None:
            self.say("xtest: the input daemon went away before an operation "
                     "this client sent could run; nothing was injected")
            return
        try:
            op()
        except CmdError as e:
            self.say("the input daemon refused the request (%s): the XTEST "
                     "request is silence on the wire, which is what X gives a "
                     "client whose server dropped a fake event, and this line "
                     "is the diagnostic" % (e,))
            self.drop("the connection failed")
            return
        self.last_used = time.monotonic()

    # -- a client goes away -------------------------------------------------------

    def spec_held(self, conn, keycode) -> str:
        """What a held keycode was pressed with."""
        got = self.pressed.get(id(conn), {}).get(keycode)
        return got if got is not None else spec_for(self.keymap.keysym(keycode))

    def on_close(self, conn) -> None:
        """Every key this client is holding gets an `up` (design section 6.3),
        so a killed `xdotool keydown ctrl` cannot leave the seat with Control
        down. The buttons are the daemon's own bookkeeping and it drops a
        release for a button it is not holding [wdotool/daemon.py:1230], so
        they are released the same way."""
        self.typed.pop(id(conn), None)
        held, buttons = sorted(conn.held), sorted(conn.buttons)
        specs = {k: self.spec_held(conn, k) for k in held}
        self.pressed.pop(id(conn), None)
        conn.held.clear()
        conn.buttons.clear()
        conn.deferred.clear()
        if not held and not buttons:
            return
        if self.route() is None:
            # Dialling again rather than giving up: the linger timer keeps the
            # connection while anything is held, but a refusal earlier in this
            # client's life could still have dropped it, and an `up` that is
            # never sent leaves the key down on the seat.
            self.say("xtest: a client went away holding %d key(s) and %d "
                     "button(s) and there is no route to the input daemon to "
                     "release them on. Not yet here on this box, and the route "
                     "is the daemon's own -- /dev/uinput with the udev rule, or "
                     "zwp_virtual_keyboard_v1 where the compositor speaks it "
                     "(AGENTS.md rung 4, and rung 1 for the protocol)"
                     % (len(held), len(buttons)))
            return
        for keycode in held:
            spec = specs[keycode]
            self.run(conn, lambda spec=spec: self.daemon.key(spec, "up", 0, False))
        for btn in buttons:
            self.run(conn, lambda btn=btn: self.daemon.button(btn, False))
        self.say("xtest: a client went away holding %s; every one of them was "
                 "released" % ", ".join([specs[k] for k in held]
                                        + ["button %d" % b for b in buttons]))


# -- the handlers -----------------------------------------------------------------


def fake_input(server, conn, req):
    """XTEST `FakeInput`: BATCH, because the row has to be able to say both
    "consumed" and "forwarded" -- there is no route to the daemon on a box with
    no /dev/uinput and no virtual-keyboard protocol, and the frame goes to
    Xwayland then (design section 6.7). `policy.FORWARD` is the same three-way
    answer `SendEvent` uses."""
    return server.xtest.fake_input(conn, req.frame)


def grab_control(server, conn, req):
    """XTEST `GrabControl`: CONSUME. `impervious` asks the server to let this
    client's requests through a grab; there is no grab here to be impervious to
    and the daemon has no notion of one. Nothing measured sends it
    [recon/tools.md 3]."""
    if len(req.frame) >= 5 and req.frame[4]:
        server.debug_say("xtest: GrabControl(impervious=1) is recorded and "
                         "ignored -- the proxy holds no server grab")
    return None


def change_keyboard_mapping(server, conn, req):
    """`ChangeKeyboardMapping`: the table is fed BEFORE the frame goes upstream,
    and then the frame goes upstream (design section 3.2, 6.2 feed one).

    That order is the whole point. `xdotool type '€'` plants `0x010020ac` into
    keycode 8 and fires `FakeInput KeyPress 8` in the same breath
    [recon/tools.md 4.6], so a table fed from the reply -- there is none -- or
    from the `MappingNotify` behind it would resolve keycode 8 against the map
    xdotool had already restored.
    """
    got = server.xtest.keymap.feed_change(req.frame)
    if got:
        server.debug_say("xtest: a client rebound %d keycode(s) from %d; the "
                         "table follows it and the request still goes upstream"
                         % (got, req.frame[4] if len(req.frame) > 4 else 0))
    return None


def get_keyboard_mapping(server, conn, req):
    """`GetKeyboardMapping`: PASS, with the reply read on the way back (design
    section 6.2 feed two). xdotool sends one before every keystroke of a `type`
    [recon/tools.md 4.5], so this is the cheapest feed there is -- no request of
    the proxy's own, and the range is the one the client asked about."""
    if len(req.frame) < 6:
        return None
    first = req.frame[4]

    def edit(pkt):
        server.xtest.keymap.feed_reply(pkt, first)
        return pkt
    return edit


def _entry(server, xid):
    """The registry's entry for an id, read through the 20 ms TTL -- the rule
    every handler in this proxy shares (design section 4.8)."""
    if server.shadows is None:
        return None
    server.shadows.snapshot()
    return server.shadows.by_shadow.get(xid)


# -- QueryPointer's sources (design section 6.6), for xw11/req_read.py ------------


def pointer_for(server, conn):
    """`(x, y)` for a `QueryPointer` answer, or None when only upstream knows.

    Seeds the daemon with whatever it found, so a later relative move starts
    from truth (design section 6.6) -- and only when that is news to it:
    `seeded` already holds every position this proxy routed, so the round trip
    is paid for a compositor's own cursor and for nothing else."""
    engine = server.xtest
    where = engine.position()
    if where is None:
        return None
    x, y = where
    if engine.daemon is not None:
        engine.seed(x, y)
    return (x, y)


def child_under(server, x, y) -> int:
    """The shadow under a point, over the merged list -- `backend.hit_test`,
    the one rule every backend and `getmouselocation` already share
    (wdotool/backend.py:159). 0 when no native toplevel is there, which leaves
    an X window's own id in place on an EDIT."""
    shadows = server.shadows
    if shadows is None:
        return 0
    wins = [e.window for e in shadows.snapshot() if e.window is not None]
    return shadows.resolve(backend_mod.hit_test(wins, x, y))


#: `Server.handlers`, keyed by `client.Request.key`. `WarpPointer` is not here:
#: it is a core request and its handler lives with the other core writes, in
#: `xw11/req_write.py` (design section 1.1's table).
HANDLERS = {
    ("XTEST", FAKE_INPUT): fake_input,
    ("XTEST", GRAB_CONTROL): grab_control,
    OP_CHANGE_KEYBOARD_MAPPING: change_keyboard_mapping,
    wire.OP_GET_KEYBOARD_MAPPING: get_keyboard_mapping,
}


def install(server) -> None:
    """Put the XTEST handlers into a server's table and give it its engine.
    Called once, from `Server.__init__`; a key written twice is a collision two
    batches would both have to see."""
    server.xtest = Engine(server)
    server.handlers.update(HANDLERS)
