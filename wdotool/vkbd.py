"""zwp_virtual_keyboard_v1: type by uploading a keymap of our own.

WHY THIS EXISTS. The uinput path injects *keycodes* at the kernel input
layer, so the compositor reads them through whatever XKB layout the session
has active -- which is why `xkbmap.py` has to look every character up
backwards through the compositor's own keymap. This protocol is the answer
built for that problem: the client uploads its OWN keymap and sends keycodes
against it. Measured on sway 1.11/wlroots, with the session set to `de` and
our US keymap uploaded, evdev 21/44/16 arrive as `y z q` -- while the same
keycodes through /dev/uinput arrive as `z y q`. The compositor sends the
focused client our keymap ahead of our first key and the session's keymap
back when the real keyboard is next used: the seat keymap follows the active
device, so ours is authoritative for our keycodes.

WHAT IT IS NOT. Four requests -- keymap, key, modifiers, destroy. No pointer,
no buttons, no scroll, no window control; `click` and `mousemove` stay on the
kernel device. Mutter does not implement it, and neither does KWin 6.6.6
(measured: the string appears nowhere in Plasma's libraries, and the same
globals are advertised to root and to the session user), so the reverse map
in `xkbmap.py` stays exactly where it is for GNOME and KDE.

THREE THINGS THE WIRE DOES DIFFERENTLY FROM uinput, all measured:

* **Modifiers are not derived from our keycodes.** wlroots does not run a
  virtual keyboard's keys through xkb state: evdev 42 down, `y`, 42 up types
  `y`, and a focused observer sees the 42 events with no `modifiers` event at
  all. The client has to send the `modifiers` request itself. That is what
  `_MOD_BITS` and `VirtualKeyboard.key()` are for -- pressing a modifier
  keycode through this sink updates the depressed mask and sends it, so
  `key ctrl+shift+t` and a shifted character keep working unchanged in the
  callers.
* **Modifier state is per device.** A modifier physically held on the user's
  own keyboard does not reach our keys (uinput's cannot avoid it), and ours
  does not reach theirs. `--clearmodifiers` is therefore honest here: we can
  release exactly what we hold and say so with `modifiers(0,0,0,0)`.
* **The compositor releases what we hold when we disconnect.** A `keydown`
  from a process that exits produces one keypress and no repeat. So the
  daemon keeps ONE connection and ONE zwp_virtual_keyboard_v1 object for the
  life of anything held down -- which is exactly the shape the daemon already
  has.

THE KEYMAP. `us_keymap.TEXT`, a captured keymap, uploaded verbatim. A keymap
we synthesise ourselves compiles and then delivers no key events (twice, on
wlroots, unexplained) -- so nothing here builds one, and the built-in US
character table in `keymap.py` is right by construction because the keymap we
upload is the one that table was written for. It binds more than that table's
ASCII: `key <I443> { [ 0x20ac ] }` is evdev 435 and really types `€`,
byte-exact into a focused `foot` on headless sway 1.11 [M goal2/recon/gaps.md
§3b], which is why `keymap.UPLOADED_EXTRA_KEYS` is read out of this file
rather than hand-listed.

A CHARACTER THE UPLOADED KEYMAP BINDS NOWHERE is **not yet** typeable here,
and X is the oracle that says it should be: the pinned xdotool 4.20260303.1 on
Xvfb with a plain `us` layout typed `é☃X` into an xterm byte for byte,
because X lets a client rebind a scratch keycode through
XChangeKeyboardMapping and xdotool does exactly that [M 2026-09-11, this box].
Our route to the same thing is rung 1 of AGENTS.md's ladder, the protocol we
already speak: put the wanted keysym on a spare keycode in a copy of
`us_keymap.TEXT` and press that keycode. The seam is already here --
`open(keymap_text=...)` takes the text, and `_upload()` (no arguments) sends
whatever `self.keymap_text` holds -- so the route is a second `VirtualKeyboard`
carrying the edited text, or a re-set of `keymap_text` and a second
`_upload()`, driven from `daemon.op_type`. The cost, which is why it is not
wired up yet: `open()` calls `_upload` once per object, so `op_type` -- the
only caller that knows a character was skipped -- has to drive the re-upload
and the re-press in the middle of a string; that swaps the keymap under the
focused client between two keystrokes and nothing has measured what each
compositor does with the swap; and the captured keymap may not be regenerated
(see `us_keymap.py`), so the edit has to be one added `key` line on an unused
keycode over the captured text, not a rebuild. Until then `type` names the
character it skipped and the layout it was looking at, and skips it.
"""

import os

from w11common.wayland_mini import now_ms as _now_ms, roundtrip
from wdotool.us_keymap import TEXT as US_KEYMAP

MANAGER = "zwp_virtual_keyboard_manager_v1"
# The only version there is (sway 1.11 advertises v1). We bind min(advertised, MAX_VERSION) so a future v2
# compositor still gets a v1 client rather than an object whose events we would not understand.
MAX_VERSION = 1

# wire opcodes
_MGR_CREATE_VIRTUAL_KEYBOARD = 0
_VK_KEYMAP = 0
_VK_KEY = 1
_VK_MODIFIERS = 2
_VK_DESTROY = 3
_KEYMAP_FORMAT_XKB_V1 = 1

# wl_keyboard.key_state
_RELEASED, _PRESSED = 0, 1

# The "real" XKB modifier bits, in the order every keymap declares them.
MOD_SHIFT = 1 << 0
MOD_LOCK = 1 << 1
MOD_CONTROL = 1 << 2
MOD_MOD1 = 1 << 3
MOD_MOD2 = 1 << 4
MOD_MOD3 = 1 << 5
MOD_MOD4 = 1 << 6
MOD_MOD5 = 1 << 7

# evdev keycode -> the bit it carries IN THE KEYMAP WE UPLOAD. Not a guess about keyboards in general: these are
# that file's own `modifier_map` lines, and tests/test_vkbd.py reads them back out of the keymap text to prove
# this table still matches it. (<RALT> is Mod1 there -- plain `us` has no AltGr level-three key, which is the
# same reason the fixed US table needs no mask but Shift.)
_MOD_BITS = {
    42: MOD_SHIFT,    # KEY_LEFTSHIFT   <LFSH>
    54: MOD_SHIFT,    # KEY_RIGHTSHIFT  <RTSH>
    58: MOD_LOCK,     # KEY_CAPSLOCK    <CAPS>
    29: MOD_CONTROL,  # KEY_LEFTCTRL    <LCTL>
    97: MOD_CONTROL,  # KEY_RIGHTCTRL   <RCTL>
    56: MOD_MOD1,     # KEY_LEFTALT     <LALT>
    100: MOD_MOD1,    # KEY_RIGHTALT    <RALT>
    69: MOD_MOD2,     # KEY_NUMLOCK     <NMLK>
    125: MOD_MOD4,    # KEY_LEFTMETA    <LWIN>
    126: MOD_MOD4,    # KEY_RIGHTMETA   <RWIN>
}

# Keys whose bit *locks* rather than only being held (modifier_map Lock /
# Mod2 in the same file). Their locked bit toggles on each press.
_LOCKING = {58: MOD_LOCK, 69: MOD_MOD2}


class VkbdError(Exception):
    """The virtual-keyboard path is not usable. Always caught by the daemon, which says why and falls back to
    the kernel device (or, when there is no kernel device either, reports the kernel device's own reason)."""


def keymap_blob(text: str = US_KEYMAP) -> bytes:
    """The bytes to upload. `size` on the wire counts the trailing NUL, which
    is how every compositor hands a keymap out and how xkbcommon reads one."""
    return text.encode("utf-8").rstrip(b"\0") + b"\0"


def _keymap_fd(data: bytes) -> int:
    """A readable fd holding `data`. memfd where there is one (no file, no
    name, nothing to clean up), a deleted temp file otherwise."""
    try:
        fd = os.memfd_create("wdotool-keymap")
    except (AttributeError, OSError):
        import tempfile

        f = tempfile.TemporaryFile()
        fd = os.dup(f.fileno())
        f.close()
    try:
        os.write(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


class VirtualKeyboard:
    """One connection + one zwp_virtual_keyboard_v1, with our keymap on it.

    `key(code, down)` is deliberately the same call as `uinput.UinputDevice.key`, so the daemon's
    press/release/type loops inject through either without knowing which -- everything this path does
    differently happens inside it.
    """

    def __init__(self, conn, vk_id: int, version: int, keymap_text: str):
        self.conn = conn
        self.vk = vk_id
        self.version = version
        self.keymap_text = keymap_text
        self.closed = False
        self._depressed = 0
        self._locked = 0
        self._group = 0
        self._sent = None   # last (depressed, locked) actually put on the wire
        # Keycodes we have told the compositor are down. The daemon tracks the same thing for its own reasons;
        # this one exists so a reconnect knows the mask it must not carry over.
        self.held: set[int] = set()

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, socket_path: str | None = None, keymap_text: str = US_KEYMAP,
             timeout: float = 2.0) -> "VirtualKeyboard":
        """Connect, create the keyboard, upload the keymap. Raises VkbdError
        for every failure, including "this compositor does not have it"."""
        from w11common import session
        from w11common.wayland_mini import WlConn

        if socket_path is None:
            hit = session.find_wayland_socket()
            if hit is None:
                raise VkbdError("no wayland socket found")
            socket_path = hit[2]
        try:
            conn = WlConn(socket_path)
        except OSError as e:
            raise VkbdError(f"cannot connect to the compositor: {e}") from None
        # A wedged compositor must not hang the daemon while it holds the
        # injection lock.
        conn.sock.settimeout(timeout)
        try:
            found = conn.find_global(MANAGER)
            if found is None:
                # Not the same list as vptr.py's, and the difference is measured: cosmic-comp advertises
                # zwp_virtual_keyboard_manager_v1 (v1) and no virtual pointer at all, so this sentence is one
                # COSMIC never sees while the pointer's is the one it always sees [M recon2/cosmic.md §3:
                # `wdotool type 'hello cosmic'` landed byte-exact with no /dev/uinput node on the box].
                # Muffin is named on both sides of the semicolon because both halves are true of it and a
                # Cinnamon user meets whichever their release is: 26.04's muffin 6.4 advertises 23
                # globals and not one is this one, while master (the 6.6/6.8 line) carries the XML and
                # META_ZWP_VIRTUAL_KEYBOARD_MANAGER_V1_VERSION 1 -- and still no virtual pointer
                # [M recon2/cinnamon.md §2.1]. vm/live-smoke.d/cinnamon-wayland.sh:376 checks this clause.
                raise VkbdError(
                    f"this compositor does not implement {MANAGER} "
                    "(Mutter, KWin and Cinnamon's Muffin 6.4 do not; sway, Hyprland, the wlroots "
                    "family, COSMIC and Muffin 6.6+ do)")
            version = min(found[1], MAX_VERSION)
            seat = conn.find_global("wl_seat")
            if seat is None:
                raise VkbdError("the compositor advertises no wl_seat")
            # The seat's keyboard *capability* is deliberately not required: a seat with no keyboard is exactly
            # where a virtual one is worth creating, and the protocol asks for the seat, not the keyboard.
            seat_id = conn.bind(seat[0], "wl_seat", min(seat[1], 7))
            mgr = conn.bind(found[0], MANAGER, version)
            vk = conn.alloc()
            conn.send(mgr, _MGR_CREATE_VIRTUAL_KEYBOARD,
                      [("u", seat_id), ("u", vk)])
            _roundtrip(conn, "create_virtual_keyboard")
            self = cls(conn, vk, version, keymap_text)
            self._upload()
            return self
        except VkbdError:
            conn.close()
            raise
        except (OSError, RuntimeError, ValueError) as e:
            conn.close()
            raise VkbdError(f"virtual keyboard setup failed: {e}") from None

    def _upload(self):
        """keymap(format, fd, size). A `key` before this is a protocol error ("Cannot send a keypress before
        defining a keymap"), so it happens once, here, before the object is handed out -- and again on every
        reconnect, because nothing survives a compositor restart."""
        data = keymap_blob(self.keymap_text)
        fd = _keymap_fd(data)
        try:
            # An fd-typed argument occupies no payload bytes: format and size
            # are the whole message body.
            self.conn.send_fds(self.vk, _VK_KEYMAP,
                               [("u", _KEYMAP_FORMAT_XKB_V1), ("u", len(data))],
                               [fd])
        finally:
            os.close(fd)
        _roundtrip(self.conn, "keymap")

    # -- the injection sink (uinput.UinputDevice's interface) --------------

    def key(self, code: int, down: bool):
        """Press or release one evdev keycode.

        A modifier keycode also moves the mask and sends `modifiers` -- on press before the key, on release
        after it, which is the order a real keyboard's events reach a client in. wlroots will not do this for
        us: it does not run a virtual keyboard's keys through xkb state.
        """
        bit = _MOD_BITS.get(code, 0)
        if down:
            self.held.add(code)
            if bit:
                self._depressed |= bit
                lock = _LOCKING.get(code)
                if lock:
                    self._locked ^= lock
                self._send_modifiers()
            self._send_key(code, _PRESSED)
        else:
            self.held.discard(code)
            self._send_key(code, _RELEASED)
            if bit and not any(_MOD_BITS.get(c) == bit for c in self.held):
                # Only when no other key carries the same bit: releasing
                # right shift while left shift is down must not clear Shift.
                self._depressed &= ~bit
                self._send_modifiers()

    def clear_modifiers(self):
        """Say, once and authoritatively, that no modifier is down.

        Only meaningful on this path: the mask we send is ours alone, so unlike the uinput path there is nothing
        here we cannot clear and nothing that could be left stuck (see --clearmodifiers in daemon.py).
        """
        self._depressed = 0
        # The LOCKED mask is deliberately left alone: `--clearmodifiers`
        # releases held modifiers, and CapsLock/NumLock are not among them on
        # the kernel path either (keymap.MODIFIER_KEYCODES has neither), so
        # clearing them here would make the same flag mean two things.
        # force: "nothing is down" is a statement worth making even when we
        # believe we had nothing down -- that is the whole point of the flag.
        self._send_modifiers(force=True)

    def flush(self):
        """Round-trip once, so a protocol error the compositor raised over the keys we just sent is reported
        instead of noticed by nobody. Key events themselves are not acknowledged one by one: that would put a
        round trip in the middle of every keystroke of a `type`."""
        _roundtrip(self.conn, "key")

    def close(self):
        """Destroy the keyboard and drop the connection. Safe to call on a keyboard that already failed -- the
        socket is closed either way, so a long-lived daemon cannot leak one per compositor restart."""
        if not self.closed:
            self.closed = True
            try:
                self.conn.send(self.vk, _VK_DESTROY, [])
            except OSError:
                pass
        self.held.clear()
        try:
            self.conn.close()
        except OSError:
            pass

    # -- internals ---------------------------------------------------------

    def _send_key(self, code: int, state: int):
        self._send(_VK_KEY, [("u", _now_ms()), ("u", code), ("u", state)])

    def _send_modifiers(self, force=False):
        """Only when the mask really moved: pressing right shift while left shift is down changes no state, and
        a compositor that re-sends the seat's modifiers to the focused client on each one should not be made to
        do it for nothing."""
        state = (self._depressed, self._locked)
        if state == self._sent and not force:
            return
        self._sent = state
        self._send(_VK_MODIFIERS, [("u", self._depressed), ("u", 0),
                                   ("u", self._locked), ("u", self._group)])

    def _send(self, opcode: int, args):
        if self.closed:
            raise VkbdError("the virtual keyboard is closed")
        try:
            self.conn.send(self.vk, opcode, args)
        except OSError as e:
            self.closed = True
            # The compositor released every key this keyboard was holding when the connection went; `held` must
            # not go on claiming otherwise, or the daemon inherits a key it can never release.
            self.held.clear()
            raise VkbdError(
                f"the compositor closed the connection ({e}); the keys "
                "wdotool was holding were released with it") from None


def _roundtrip(conn, what: str):
    return roundtrip(conn, what, VkbdError)
