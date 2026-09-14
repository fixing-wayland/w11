"""`wdotool keys` -- watch what the keyboard really sends, or explain how to
type a character on the layout that is active right now.

Two modes, one command:

    wdotool keys watch      read the input devices and print one line per key
                            event, with the reproduction to paste
    wdotool keys explain X  the reverse, without touching the keyboard: what
                            to press to get X

Both halves already existed in pieces -- `keystate.py` reads the real input
devices and knows how to leave our own virtual ones alone, `xkbmap.py` turns
the compositor's keymap into "which key, with which modifiers" -- and the
hidden `wdotool __keymap --chars` diagnostic was most of `explain`. What is
new here is the presentation, and the ordering logic that tells a *chord*
(modifiers held down across another key press) from a *sequence* (two presses
in turn, which is what a dead-key accent is). Written down they look alike;
they are entirely different events, and only the press/release order says
which one happened.

Two things every line carries, deliberately:

  * the **replay** column, in evdev keycodes (as X keycodes, which is what
    `wdotool key` takes: X = evdev + 8). Exact, and meaningless without the
    layout that was active when it was recorded.
  * the **character** column, in characters and keysym names. Portable to any
    layout, and it is what you want in a script.

`watch` needs to read `/dev/input/event*`, which on every desktop measured is
`root:input` with no ACL. Nothing grants it: this project's udev rule tags
`/dev/uinput` `uaccess` (which is what lets *injection* run unprivileged, and
only once that rule is installed), and no rule anywhere tags the keyboards. So
watch mode needs root, and says so in one line instead of failing obscurely.
`explain` needs no privilege at all: the keymap arrives on
`wl_keyboard.keymap`, which every Wayland client is handed.

Not a chainable command: like `__daemon` and `__keymap` this is ours, routed
in `cli.main()` before the passthrough, and deliberately absent from the
command registry -- `help` is byte-compatible with the real xdotool's, which
has no `keys`.
"""

import errno
import os
import select
import shlex
import struct
import sys
import time
import unicodedata

from hacks.input import keymap, keystate, xkbmap
from hacks.input.keysyms import NAME_TO_KEYSYM
from hacks.input.uinput import EV_KEY, EV_MSC, EV_SYN

KEY_ESC = 1
KEY_A = 30
# input-event-codes.h: EV_KEY below BTN_MISC is a key, from BTN_MISC up it is a button (mouse, touchpad tool,
# joystick) -- a combined keyboard+mouse device sends both down one node. Buttons are not this command's
# business and have no X keycode to replay (X stops at 255), so the table skips them; `--raw` still shows every
# event.
BTN_MISC = 0x100

_EV_FMT = "llHHi"                      # struct input_event
_EV_SIZE = struct.calcsize(_EV_FMT)
_EV_TYPES = {EV_SYN: "EV_SYN", EV_KEY: "EV_KEY", 0x02: "EV_REL", 0x03: "EV_ABS",
             EV_MSC: "EV_MSC", 0x11: "EV_LED", 0x12: "EV_SND", 0x14: "EV_REP"}

# keysym -> the tag we print for a key held down. Read out of the *keymap*, so the third-level key is whichever
# key this layout puts it on: <RALT> on German, <CAPS> and <BKSL> on Neo, a dedicated <LVL3> elsewhere.
_MOD_TAGS = {
    0xFFE1: "shift", 0xFFE2: "shift",
    0xFFE3: "ctrl", 0xFFE4: "ctrl",
    0xFFE9: "alt", 0xFFEA: "alt", 0xFFE7: "alt", 0xFFE8: "alt",
    0xFFEB: "super", 0xFFEC: "super", 0xFFED: "super", 0xFFEE: "super",
    0xFE03: "level3", 0xFF7E: "level3",
    0xFE11: "level5",
    0xFFE5: "caps",
}
_TAG_MASK = {"shift": xkbmap.MOD_SHIFT, "level3": xkbmap.MOD_LEVEL3, "level5": xkbmap.MOD_LEVEL5}
_TAG_ORDER = ("ctrl", "alt", "super", "shift", "level3", "level5", "caps")
# The tags that pick a level (and so change the character) versus the ones
# that only decorate a key sequence.
_SEQ_TAGS = ("ctrl", "alt", "super")

_NAME_OF_KEYSYM = None


def keysym_name(ks):
    """The X name of a keysym ("at", "Return", "dead_acute"), or None."""
    global _NAME_OF_KEYSYM
    if ks is None:
        return None
    if _NAME_OF_KEYSYM is None:
        rev = {}
        for name, value in NAME_TO_KEYSYM.items():
            rev.setdefault(value, name)
        _NAME_OF_KEYSYM = rev
    return _NAME_OF_KEYSYM.get(ks)


def xk(code: int) -> int:
    """evdev keycode -> the X keycode `wdotool key` takes."""
    return code + 8


def kc(code: int) -> str:
    """evdev keycode -> the `wdotool key` TOKEN for it.

    Not just `str(xk(code))`: a keysequence token is looked up as a keysym *name* first (both here and in the
    real xdotool, which calls XStringToKeysym before it considers a number), and "9" is the name of the digit
    nine. Escape is X keycode 9, so `wdotool key 9` types a nine -- a replay line that does not replay. A
    leading zero is not the name of anything and parses as the same number, so pad the tokens that collide."""
    tok = str(xk(code))
    return "0" + tok if tok in NAME_TO_KEYSYM else tok


def _q(ch: str) -> str:
    """Shell-quote a character, always visibly: `wdotool type '@'` is the line to paste, and `wdotool type @` --
    which is what shlex.quote alone gives back -- reads like a stray token."""
    return shlex.quote(ch) if "'" in ch or "\\" in ch else "'%s'" % ch


# ---------------------------------------------------------------------------
# the active layout, both ways round


class Layout:
    """What both modes need to know about the layout that is active now.

    Forward (`keysym`): keycode + modifier mask -> what it produces, which is watch mode's question. Backward
    (`lookup_char`): character -> keystrokes, which is explain mode's, and which is the *same* call the typing
    path makes, US bypass and all -- so what explain prints is what `type` sends.
    """

    def __init__(self, name, source, group, ngroups, group_known, rmap, km=None, note=None):
        self.name = name
        self.source = source
        self.group = group
        self.ngroups = ngroups
        self.group_known = group_known
        self.rmap = rmap            # None: the built-in US table is in use
        self.note = note            # why, when it is
        self.fwd = {}               # keycode -> {mask: keysym}
        self.names = {}             # keycode -> XKB key name ("AD01")
        if km is not None:
            self._from_keymap(km)
        else:
            self._from_fixed_table()

    # -- construction ------------------------------------------------------

    def _from_keymap(self, km):
        try:
            syms = km.resolved(self.group)
        except xkbmap.XkbError:
            return self._from_fixed_table()
        g = km.group(self.group)
        for key, levels in sorted(syms.items()):
            code = km.keycodes.get(key)
            if code is None or not 0 < code < 256:
                continue
            self.names.setdefault(code, key)
            masks = km.types.get(g.types.get(key, ""), None)
            for i, ks in enumerate(levels):
                if ks is None:
                    continue
                mask = masks.get(i + 1) if masks else xkbmap.LEVEL_MASK.get(i + 1)
                if mask is None:
                    continue
                self.fwd.setdefault(code, {}).setdefault(mask, ks)

    def _from_fixed_table(self):
        """No keymap to read: invert the built-in US tables instead, so watch
        mode still names keys on a box with no compositor to ask."""
        for ch, (code, shifted) in keymap.CHAR_TO_KEY.items():
            ks = NAME_TO_KEYSYM.get(ch) or (ord(ch) if ord(ch) < 0x100 else None)
            if ks is not None:
                self.fwd.setdefault(code, {}).setdefault(xkbmap.MOD_SHIFT if shifted else 0, ks)
        for name, (code, shifted) in keymap.KEYSYM_KEYS.items():
            ks = NAME_TO_KEYSYM.get(name)
            if ks is not None:
                self.fwd.setdefault(code, {}).setdefault(xkbmap.MOD_SHIFT if shifted else 0, ks)

    @classmethod
    def load(cls, keymap=None, group=None):
        """The layout, chosen by exactly the rules the typing path uses: `WDOTOOL_LAYOUT`, then the compositor's
        keymap, then the US bypass, and the built-in US table as the floor (see daemon._layout). That includes
        where the active group came from, so `explain` reports the group `type` would really use and names its
        source -- "wayland" is the keymap alone, "wayland + kwin" is KWin having been asked.

        `keymap`/`group` are --keymap/--group, passed rather than exported."""
        mode = xkbmap.layout_mode()
        if mode == "us":
            return cls.fixed("WDOTOOL_LAYOUT=us")
        try:
            snap = xkbmap.fetch(keymap=keymap, group=group)
        except xkbmap.XkbError as e:
            return cls.fixed("the compositor's keymap could not be read (%s)" % e)
        try:
            km = xkbmap.parse(snap.text)
        except xkbmap.XkbError as e:
            return cls.fixed("the keymap could not be parsed (%s)" % e)
        bypass = xkbmap.decide(snap.text, snap.group, mode)
        rmap = None
        note = None
        if bypass:
            note = ("this layout is plain US: wdotool uses its built-in table "
                    "and never reads the keymap")
        else:
            try:
                rmap = xkbmap.reverse(km, snap.group)
            except xkbmap.XkbError as e:
                note = "the keymap could not be reversed (%s)" % e
        name = (rmap.name if rmap is not None else xkbmap.group_name(snap.text, snap.group))
        return cls(name, snap.source, snap.group, len(km.groups), snap.group_known, rmap, km, note)

    @classmethod
    def fixed(cls, why):
        return cls("US (built-in table)", "built-in", 1, 1, True, None, None, why)

    # -- forward: what does this key produce? ------------------------------

    def keysym(self, code, mask=0):
        levels = self.fwd.get(code)
        if not levels:
            return None
        for m in (mask, mask & ~xkbmap.MOD_SHIFT, 0):
            if m in levels:
                return levels[m]
        return None

    def modtag(self, code):
        """'shift'/'ctrl'/'level3'/... when this key is a modifier, else None."""
        return _MOD_TAGS.get(self.keysym(code, 0))

    def keyname(self, code):
        name = self.names.get(code)
        return "<%s>" % name if name else "-"

    def produces(self, code, mask):
        """(text for the PRODUCES column, character or None)."""
        ks = self.keysym(code, mask)
        if ks is None:
            return "-", None
        ch = xkbmap.keysym_char(ks)
        if ch is not None:
            return _q(ch), ch
        return keysym_name(ks) or "0x%x" % ks, None

    def is_alpha(self, code):
        ch = xkbmap.keysym_char(self.keysym(code, 0))
        return bool(ch) and ch.isalpha() and ch.islower()

    # -- backward: what do I press for this? -------------------------------

    def lookup_char(self, ch):
        """[(keycode, mask), ...] or None -- daemon.op_type's own lookup."""
        if self.rmap is not None:
            return self.rmap.lookup_char(ch)
        hit = keymap.char_to_key(ch)
        return None if hit is None else [(hit[0], xkbmap.MOD_SHIFT if hit[1] else 0)]

    def lookup_keysym(self, name):
        """(keycode, mask) or None -- keymap.keysym_to_key, as `key` uses."""
        hit = keymap.keysym_to_key(name, self.rmap)
        return None if hit is None else (hit[0], int(hit[1]))

    def mod_key(self, bit):
        """The key wdotool would press for a level bit, or None."""
        if self.rmap is not None:
            return self.rmap.mod_keys.get(bit)
        return keymap.KEY_LEFTSHIFT if bit == xkbmap.MOD_SHIFT else None

    def where(self, code):
        name = self.names.get(code)
        return "key %d <%s>" % (code, name) if name else "key %d" % code

    def describe(self):
        lines = ["layout: %s -- group %d of %d%s, from %s"
                 % (self.name, self.group, self.ngroups,
                    "" if self.group_known else " (assumed)", self.source)]
        bits = []
        for bit, tag in ((xkbmap.MOD_SHIFT, "shift"), (xkbmap.MOD_LEVEL3, "level3"),
                         (xkbmap.MOD_LEVEL5, "level5")):
            code = self.mod_key(bit)
            bits.append("%s = %s" % (tag, self.where(code) if code else "none"))
        lines.append("level keys: " + "   ".join(bits) + "   (what wdotool presses)")
        if self.note:
            lines.append("note: " + self.note)
        return lines


def mask_of(tags):
    mask = 0
    for t in tags:
        mask |= _TAG_MASK.get(t, 0)
    return mask


# ---------------------------------------------------------------------------
# watch: rendering an event stream

_COLUMNS = ("    TIME EV     CODE KEY      MODIFIERS       PRODUCES         "
            "REPLAY (keycodes)          CHARACTER (portable)")


def _summary_line(kind, replay, portable, why=None):
    """One run of held keys, as `= kind | keycodes | characters [| why]`.
    Pipe-separated, not column-aligned: a literal down/up sequence is longer
    than any sensible column, and a reason that runs into the command beside
    it is worse than a ragged line."""
    fields = ["%-11s" % kind, replay, portable]
    if why:
        fields.append(why)
    return " | ".join(fields)


class Watcher:
    """Event stream in, printable lines out. No I/O: the whole ordering
    story is testable over a recorded stream."""

    def __init__(self, layout):
        self.layout = layout
        self.t0 = None
        self.order = []        # keycodes held, in press order
        self.press = {}        # keycode -> (mask, keysym, mods held before it)
        self.owner = {}        # keycode -> the device node holding it down
        self.run = []          # this run's events, in order
        self.caps = False
        self.dead = None       # (combining mark, replay, chars) of the last run

    # -- one event ---------------------------------------------------------

    def key_event(self, t, code, value, path=None):
        if self.t0 is None:
            self.t0 = t
        rel = t - self.t0
        out = []
        if value:
            if self.layout.modtag(code) == "caps":
                self.caps = not self.caps
            mods = list(self.order)
            mask = self._mask(mods, code)
            ks = self.layout.keysym(code, mask)
            self.order.append(code)
            self.press[code] = (mask, ks, mods)
            self.owner[code] = path
            self.run.append(["down", code, mask, ks, mods])
            out.append(self._line(rel, "down", code, mask, ks, mods))
        else:
            mask, ks, _held = self.press.pop(code, (0, self.layout.keysym(code, 0), []))
            self.owner.pop(code, None)
            if code in self.order:
                self.order.remove(code)
            self.run.append(["up", code, mask, ks, list(self.order)])
            out.append(self._line(rel, "up", code, mask, ks, self.order))
            if not self.order:
                out.extend(self._summary())
                self.run = []
        return out

    def holding(self, path):
        """The keys that device is holding down, in press order."""
        return [c for c in self.order if self.owner.get(c) == path]

    def device_gone(self, t, path):
        """A keyboard that was holding keys was unplugged.

        The kernel releases a removed device's keys for everybody else (`input_dev_release_keys`), so watching
        has to do the same. Otherwise the run never closes -- no summary is printed again for the rest of the
        session -- and every later line reports a modifier that nobody is holding any more, which is a
        reproduction that does not reproduce."""
        return [line for code in self.holding(path) for line in self.key_event(t, code, 0, path)]

    def finish(self):
        """Flush a run that is still open (a key held when watching stops)."""
        if not self.run:
            return []
        out = self._summary(open_run=True)
        self.run = []
        return out

    # -- helpers -----------------------------------------------------------

    def _tags(self, mods):
        seen = []
        for c in mods:
            tag = self.layout.modtag(c)
            if tag and tag not in seen:
                seen.append(tag)
        if self.caps and "caps" not in seen:
            seen.append("caps")
        return [t for t in _TAG_ORDER if t in seen]

    def _mask(self, mods, code):
        mask = mask_of(self._tags(mods))
        if self.caps and self.layout.is_alpha(code):
            mask ^= xkbmap.MOD_SHIFT     # Caps Lock shifts a letter, and only a letter
        return mask

    def _line(self, rel, ev, code, mask, ks, mods):
        tags = self._tags(mods)
        produces, _ch = self.layout.produces(code, mask)
        if ev == "down" and self.layout.modtag(code):
            # A modifier going down is held, not struck: `keydown` is what
            # actually happened, and `key` would let go of it again.
            replay = "wdotool keydown %s" % kc(code)
            name = keysym_name(ks)
            portable = "wdotool keydown %s" % name if name else "-"
        elif ev == "down":
            replay = "wdotool key " + "+".join(kc(c) for c in list(mods) + [code])
            portable = self._portable_press(code, mask, ks, tags)
        else:
            replay = "wdotool keyup %s" % kc(code)
            name = keysym_name(self.layout.keysym(code, 0))
            portable = "wdotool keyup %s" % name if name else "-"
        return ("%8.3f %-4s %5d %-8s %-15s %-16s %-26s %s"
                % (rel, ev, code, self.layout.keyname(code),
                   "+".join(tags) or "-", produces, replay, portable))

    def _portable_press(self, code, mask, ks, tags):
        """The character-language reproduction of one press.

        Level modifiers are never spelled out: they are folded into the keysym they select, because the *name*
        of the level-three key is a property of this layout and would not travel. ctrl/alt/super do travel, and
        stay as tokens."""
        extra = [t for t in tags if t in _SEQ_TAGS]
        name = keysym_name(ks)
        ch = xkbmap.keysym_char(ks)
        if extra:
            return "wdotool key " + "+".join(extra + [name or "0x%x" % (ks or 0)])
        if ch is not None:
            return "wdotool type " + _q(ch)
        if name:
            return "wdotool key " + name
        return "-"

    # -- a whole run of held keys -----------------------------------------

    def _summary(self, open_run=False):
        downs = [i for i, e in enumerate(self.run) if e[0] == "down"]
        ups = [i for i, e in enumerate(self.run) if e[0] == "up"]
        mains = [i for i in downs if self.layout.modtag(self.run[i][1]) is None]
        why = None
        if open_run:
            why = "still held when watching stopped"
        elif not downs:
            # The key was already down when watching started (the Enter that ran the command is the usual one),
            # so its press is not ours to show. There is a release to reproduce and no chord to infer.
            why = "released without a press: held down before watching started"
        elif ups and downs and max(downs) > min(ups):
            why = "released out of order"
        elif len(mains) > 1:
            why = "%d keys held at once" % len(mains)
        elif mains and mains[0] != downs[-1]:
            why = "a modifier was pressed after the key"
        if why is None:
            return self._chord(downs, mains)
        seq = " ".join(("keydown %s" if e[0] == "down" else "keyup %s") % kc(e[1]) for e in self.run)
        names = []
        for e in self.run:
            name = keysym_name(self.layout.keysym(e[1], 0)) or kc(e[1])
            names.append(("keydown %s" if e[0] == "down" else "keyup %s") % name)
        self.dead = None
        return [_summary_line("= sequence", "wdotool " + seq, "wdotool " + " ".join(names), why)]

    def _chord(self, downs, mains):
        codes = [self.run[i][1] for i in downs]
        replay = "wdotool key " + "+".join(kc(c) for c in codes)
        i = mains[0] if mains else downs[-1]
        _kind, code, mask, ks, mods = self.run[i]
        tags = self._tags(mods)
        portable = self._portable_press(code, mask, ks, tags)
        out = [_summary_line("= chord", replay, portable)]
        pair = self._dead_pair(ks, replay, portable)
        if pair:
            out.append(pair)
        return out

    def _dead_pair(self, ks, replay, portable):
        """A dead key and then a base letter are two runs, not a chord. When the pair really composes, say what
        it typed -- that is the line the user wanted."""
        prev, self.dead = self.dead, None
        mark = xkbmap.DEAD_KEYSYMS.get(ks)
        if mark is not None:
            self.dead = (chr(mark[0]), replay, portable)
            return None
        if prev is None:
            return None
        base = xkbmap.keysym_char(ks)
        if not base:
            return None
        composed = unicodedata.normalize("NFC", base + prev[0])
        if len(composed) != 1:
            return None
        return _summary_line("= dead pair", prev[1] + replay[len("wdotool"):],
                             "wdotool type " + _q(composed),
                             "two presses in order, not a chord")


# ---------------------------------------------------------------------------
# watch: the devices


class StreamEvdev(keystate.Evdev):
    """keystate's evdev seam plus the one call it deliberately does not make:
    reading the event stream itself."""

    def read(self, fd: int, n: int) -> bytes:
        return os.read(fd, n)


class Devices:
    """Every readable keyboard that is not ours, kept open while watching.

    Devices come and go (a USB keyboard, a Bluetooth one waking up), so the directory is re-scanned on a timer
    and a node that stops reading is dropped. Nothing is ever grabbed (`EVIOCGRAB`): the compositor keeps seeing
    every key, and stopping leaves the system exactly as it was."""

    RESCAN = 1.0

    def __init__(self, evdev=None, exclude_names=(keystate.OWN_NAME_PREFIX,)):
        self.evdev = evdev if evdev is not None else StreamEvdev()
        self.exclude_names = tuple(n for n in exclude_names if n)
        self.fds = {}          # fd -> (path, name)
        self.judged = set()    # paths already looked at
        self.denied = 0        # nodes we may not read
        self.ours = 0          # nodes that are our own virtual devices
        self.nodes = 0         # /dev/input/event* nodes seen at all
        self.notices = []
        self.gone = []         # paths dropped since the caller last looked
        self._poll = select.poll()
        self._next_scan = 0.0

    # -- opening -----------------------------------------------------------

    def scan(self):
        paths = self.evdev.paths()
        self.nodes = max(self.nodes, len(paths))
        live = set(paths)
        added = []
        for path in paths:
            if path in self.judged:
                continue
            self.judged.add(path)
            try:
                fd = self.evdev.open(path)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM):
                    self.denied += 1
                continue
            keep = False
            name = ""
            try:
                name = self.evdev.name(fd)
                if any(name.startswith(p) for p in self.exclude_names):
                    self.ours += 1        # our own injection, never a recording
                else:
                    caps = self.evdev.key_caps(fd)
                    keep = keystate.bit(caps, KEY_ESC) or keystate.bit(caps, KEY_A)
            except OSError:
                keep = False
            if not keep:
                try:
                    self.evdev.close(fd)
                except OSError:
                    pass
                continue
            self.fds[fd] = (path, name)
            try:
                self._poll.register(fd, select.POLLIN)
            except (OSError, ValueError):
                pass
            added.append((path, name))
        self.judged &= live | {p for p, _n in self.fds.values()}
        return added

    def _drop(self, fd, why):
        path, name = self.fds.pop(fd, ("?", "?"))
        try:
            self._poll.unregister(fd)
        except (OSError, KeyError, ValueError):
            pass
        try:
            self.evdev.close(fd)
        except OSError:
            pass
        self.judged.discard(path)
        self.gone.append(path)
        self.notices.append("- %s %r (%s)" % (path, name, why))

    def close_all(self):
        for fd in list(self.fds):
            try:
                self._poll.unregister(fd)
            except (OSError, KeyError, ValueError):
                pass
            try:
                self.evdev.close(fd)
            except OSError:
                pass
        self.fds.clear()

    # -- reading -----------------------------------------------------------

    def take_gone(self):
        """The devices dropped since the last call, and forget them."""
        out, self.gone = self.gone, []
        return out

    def now(self) -> float:
        """The clock the event timestamps are on, for the releases we have to invent when a device is unplugged.
        evdev stamps every event CLOCK_REALTIME, which is what time.time() reads, so a synthesised release lands
        on the same timeline as the real ones."""
        return time.time()

    def _wait(self, timeout):
        """Readable fds. Overridden by the test double, which has no real
        file descriptors to poll."""
        try:
            return [fd for fd, _ev in self._poll.poll(timeout * 1000)]
        except InterruptedError:
            return []

    def poll(self, timeout=0.5):
        now = time.monotonic()
        if now >= self._next_scan:
            self._next_scan = now + self.RESCAN
            for path, name in self.scan():
                self.notices.append("+ %s %r" % (path, name))
            live = set(self.evdev.paths())
            for fd, (path, _n) in list(self.fds.items()):
                if path not in live:
                    self._drop(fd, "disappeared")
        out = []
        for fd in self._wait(timeout):
            info = self.fds.get(fd)
            if info is None:
                continue
            try:
                data = self.evdev.read(fd, _EV_SIZE * 64)
            except (BlockingIOError, InterruptedError):
                continue                       # a spurious wakeup, not a loss
            except OSError:
                self._drop(fd, "disappeared")  # ENODEV: unplugged mid-read
                continue
            if not data:
                continue
            for off in range(0, len(data) - _EV_SIZE + 1, _EV_SIZE):
                sec, usec, etype, code, value = struct.unpack(_EV_FMT, data[off:off + _EV_SIZE])
                out.append((info[0], sec + usec / 1e6, etype, code, value))
        # One round drains each device in fd order, so two keyboards come out of it in blocks: the time column
        # would run backwards and a key held on one board across a press on the other would be rendered as two
        # separate runs. The kernel timestamps every event (CLOCK_REALTIME, so they are comparable between
        # devices); sort by them and the batch is the order things actually happened in. Stable: same-timestamp
        # events keep the order the device reported them.
        out.sort(key=lambda e: e[1])
        return out


_EXPLAIN_INSTEAD = ("  `wdotool keys explain` needs no privilege and answers "
                    "the same question\n  backwards.")


def _no_devices(dev) -> str:
    """Why there is nothing to watch, in the terms of this machine.

    "Needs root" is the usual answer and it is worth being exact about: the keyboards are root:input with no ACL
    and nothing tags them -- this project's udev rule tags /dev/uinput only, which is the injecting half. But it
    is the wrong answer when there are no input devices at all (a container) or when the only ones here are our
    own, and saying `sudo` to someone who is already root helps nobody."""
    lines = ["wdotool keys watch: no keyboard to read."]
    if dev.denied:
        lines += [
            "  %d /dev/input/event* node%s there, and this user may not read"
            % (dev.denied, " is" if dev.denied == 1 else "s are"),
            "  %s: they are root:input with no ACL, and nothing tags them --"
            % ("it" if dev.denied == 1 else "them"),
            "  this project's udev rule tags /dev/uinput (the injecting half)",
            "  and no keyboard. So watching does need root:",
            "      sudo wdotool keys watch",
        ]
    elif dev.ours:
        lines += [
            "  The only input device%s here %s wdotool's own (%d virtual"
            % ("" if dev.ours == 1 else "s", "is" if dev.ours == 1 else "are",
               dev.ours),
            "  device%s), and a recording of our own injection is not a recording"
            % ("" if dev.ours == 1 else "s"),
            "  of you typing. Plug in a keyboard, or watch where one is.",
        ]
    elif dev.nodes:
        lines += [
            "  %d /dev/input/event* node%s readable here, and none of them"
            % (dev.nodes, " is" if dev.nodes == 1 else "s are"),
            "  is a keyboard (no Esc, no A). A mouse is not a keyboard.",
        ]
    else:
        lines += [
            "  There is nothing under /dev/input at all -- no keyboard is",
            "  attached, or this is a container without the device nodes.",
        ]
    return "\n".join(lines + [_EXPLAIN_INSTEAD])


# ---------------------------------------------------------------------------
# watch: the command

_USAGE_WATCH = """Usage: wdotool keys watch [options]
Print one line per key event as you type: the raw keycode, the modifiers held
at that instant, what it produced, and both reproductions -- keycodes and
characters. When a run of held keys cannot be written as one chord, the
literal down/up sequence is printed instead.

--count N       stop after N key events (for scripting); default: until Ctrl-C
--raw           print unfiltered evdev event lines instead of the table
                (every event of every device, autorepeat, EV_SYN and the
                mouse buttons a combined keyboard sends, all included)
--group N       read group N of the keymap (1-based) instead of the active one
--keymap PATH   read the keymap from a file instead of the compositor
-h, --help      this text

Needs root: /dev/input/event* is not readable by the seat user.
Times are seconds since the first event. The table goes to stdout,
everything else (the layout, the devices, hotplug notices) to stderr.
"""

_USAGE_EXPLAIN = """Usage: wdotool keys explain [options] <string|keysym> ...
Say what to press to produce each character, under the layout that is active
right now. Needs no privilege and touches no device.

An argument that is a keysym name (Return, dead_acute, EuroSign) is taken as
one; anything else is taken character by character. --chars and --keysym say
which explicitly.

--chars STRING  explain STRING character by character
--keysym NAME   explain one keysym by name
--group N       read group N of the keymap (1-based) instead of the active one
--keymap PATH   read the keymap from a file instead of the compositor
-h, --help      this text
"""

_USAGE = """Usage: wdotool keys <watch|explain> [options]
  watch            print what the keyboard sends, with the command to replay it
  explain STRING   print what to press to produce STRING

`wdotool keys watch --help` and `wdotool keys explain --help` have the detail.
"""


class _Args:
    """A tiny option reader. Hand-rolled on purpose: this command is not in
    the registry, so it must not go anywhere near xdotool's getopt parity."""

    def __init__(self, argv, flags, values):
        self.opts = {}
        self.rest = []
        self.error = None
        self.help = False
        args = list(argv)
        while args:
            a = args.pop(0)
            if a in ("-h", "--help"):
                self.help = True
            elif a in flags:
                self.opts[a.lstrip("-")] = True
            elif a in values:
                if not args:
                    self.error = "option %s needs an argument" % a
                    return
                self.opts.setdefault(a.lstrip("-"), []).append(args.pop(0))
            elif a.startswith("-") and a != "-":
                self.error = "unknown option '%s'" % a
                return
            else:
                self.rest.append(a)

    def one(self, name, default=None):
        got = self.opts.get(name)
        return got[-1] if got else default


def _load_layout(args):
    group = args.one("group")
    if group is not None and not str(group).isdigit():
        return None, "--group wants a number"
    # --keymap/--group are arguments to the read, not exports: the process
    # environment comes back untouched because it was never touched.
    return Layout.load(keymap=args.one("keymap"), group=group), None


class _Done(Exception):
    """--count reached: unwind out of the read loop, print nothing."""


def watch_main(argv, devices=None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = _Args(argv, ("--raw",), ("--count", "--group", "--keymap"))
    if args.help:
        out.write(_USAGE_WATCH)
        return 0
    if args.error:
        err.write("wdotool keys watch: %s\n" % args.error)
        err.write(_USAGE_WATCH)
        return 1
    if args.rest:
        err.write("wdotool keys watch: unexpected argument '%s'\n" % args.rest[0])
        return 1
    limit = args.one("count")
    if limit is not None and not str(limit).isdigit():
        err.write("wdotool keys watch: --count wants a number\n")
        return 1
    limit = int(limit) if limit is not None else 0
    layout, problem = _load_layout(args)
    if problem:
        err.write("wdotool keys watch: %s\n" % problem)
        return 1

    dev = devices if devices is not None else Devices()
    dev.scan()
    if not dev.fds:
        dev.close_all()
        err.write(_no_devices(dev) + "\n")
        return 1
    for line in layout.describe():
        err.write(line + "\n")
    err.write("watching %d keyboard%s%s; codes are evdev keycodes, the replay "
              "column uses X keycodes (evdev+8). Ctrl-C to stop.\n"
              % (len(dev.fds), "" if len(dev.fds) == 1 else "s",
                 "" if not dev.ours else ", ignoring %d of our own" % dev.ours))
    for _fd, (path, name) in sorted(dev.fds.items()):
        err.write("  %s %r\n" % (path, name))
    if not args.opts.get("raw"):
        err.write(_COLUMNS + "\n")
    err.flush()

    watcher = Watcher(layout)
    seen = 0
    rc = 0
    t0 = None      # every time column is seconds since the first event
    raw = bool(args.opts.get("raw"))
    try:
        while True:
            batch = dev.poll(0.5)
            # A keyboard that went away before this round was read is holding nothing any more, and its keys
            # have to be let go of *before* the events that arrived after it left -- otherwise every one of them
            # is reported under a modifier nobody is holding. --raw is the unfiltered device stream and gets no
            # invented events.
            gone = dev.take_gone()
            if gone and not raw:
                when = batch[0][1] if batch else dev.now()
                for path in gone:
                    held = watcher.holding(path)
                    if held:
                        err.write("wdotool keys watch: %s went away holding %s;"
                                  " released, as the kernel does\n"
                                  % (path, "+".join(kc(c) for c in held)))
                        err.flush()
                    if t0 is None:
                        t0 = when
                    for line in watcher.device_gone(when, path):
                        out.write(line + "\n")
                out.flush()
            for path, t, etype, code, value in batch:
                if t0 is None:
                    t0 = t
                if raw:
                    out.write("%8.3f %-12s %-7s %5d %d\n"
                              % (t - t0, os.path.basename(path),
                                 _EV_TYPES.get(etype, "0x%02x" % etype), code, value))
                    seen += 1
                elif etype == EV_KEY and value in (0, 1) and 0 < code < BTN_MISC:
                    for line in watcher.key_event(t, code, value, path):
                        out.write(line + "\n")
                    seen += 1
                else:
                    continue
                out.flush()
                if limit and seen >= limit:
                    raise _Done()
            for notice in dev.notices:
                err.write("wdotool keys watch: %s\n" % notice)
            del dev.notices[:]
            err.flush()
            if not dev.fds:
                err.write("wdotool keys watch: every keyboard went away.\n")
                rc = 1
                break
    except _Done:
        pass
    except KeyboardInterrupt:
        err.write("\n")
    finally:
        for line in watcher.finish():
            out.write(line + "\n")
        out.flush()
        dev.close_all()
    return rc


# ---------------------------------------------------------------------------
# explain: the command


def _steps(layout, seq):
    """Render one lookup result as numbered presses."""
    lines = []
    for n, (code, mask) in enumerate(seq, 1):
        bits = []
        for bit, tag in ((xkbmap.MOD_SHIFT, "shift"), (xkbmap.MOD_LEVEL3, "level3"),
                         (xkbmap.MOD_LEVEL5, "level5")):
            if mask & bit:
                mcode = layout.mod_key(bit)
                mname = keysym_name(layout.keysym(mcode, 0)) if mcode else None
                bits.append("%s (%s%s)" % (tag, layout.where(mcode) if mcode else "?",
                                           ", " + mname if mname else ""))
        what, _ch = layout.produces(code, mask)
        prefix = "    " if len(seq) == 1 else "    %d. " % n
        lines.append("%spress %s%s -> %s"
                     % (prefix, layout.where(code),
                        " with " + " and ".join(bits) if bits else "", what))
    return lines


def explain_one(layout, label, seq, portable, kind="char"):
    lines = []
    if seq is None:
        lines.append("%s -- unreachable on %s: %s" % (
            label, layout.name,
            "no key produces it, alone or as a dead-key pair."
            if kind == "char" else "no key on this layout carries it."))
        return lines
    kind = ""
    if len(seq) > 1:
        kind = " (a dead-key pair: two presses in order, not a chord)"
    lines.append("%s -- %d press%s on %s%s"
                 % (label, len(seq), "" if len(seq) == 1 else "es",
                    layout.name, kind))
    lines.extend(_steps(layout, seq))
    replay = "wdotool " + " ".join(
        "key %s" % "+".join([kc(layout.mod_key(b)) for b in xkbmap.MOD_BITS
                             if mask & b and layout.mod_key(b)] + [kc(code)])
        for code, mask in seq)
    lines.append("    %-42s (keycodes)" % replay)
    if portable:
        lines.append("    %-42s (characters)" % portable)
    return lines


def explain_main(argv, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = _Args(argv, (), ("--chars", "--keysym", "--group", "--keymap"))
    if args.help:
        out.write(_USAGE_EXPLAIN)
        return 0
    if args.error:
        err.write("wdotool keys explain: %s\n" % args.error)
        err.write(_USAGE_EXPLAIN)
        return 1
    wants = []
    for s in args.opts.get("chars", []):
        wants.extend(("char", ch) for ch in s)
    for name in args.opts.get("keysym", []):
        wants.append(("keysym", name))
    for token in args.rest:
        if len(token) > 1 and token in NAME_TO_KEYSYM:
            wants.append(("keysym", token))
        else:
            wants.extend(("char", ch) for ch in token)
    if not wants:
        err.write("wdotool keys explain: nothing to explain\n")
        err.write(_USAGE_EXPLAIN)
        return 1
    layout, problem = _load_layout(args)
    if problem:
        err.write("wdotool keys explain: %s\n" % problem)
        return 1
    for line in layout.describe():
        out.write(line + "\n")
    missing = 0
    for kind, item in wants:
        if kind == "char":
            seq = layout.lookup_char(item)
            portable = "wdotool type " + _q(item)
            label = repr(item)
        else:
            if item not in NAME_TO_KEYSYM and item not in keymap.KEYSYM_KEYS:
                out.write("%s -- not a keysym name.\n" % item)
                missing += 1
                continue
            hit = layout.lookup_keysym(item)
            seq = [hit] if hit else None
            portable = "wdotool key " + item
            label = item
        if seq is None:
            missing += 1
            portable = None
        for line in explain_one(layout, label, seq, portable, kind):
            out.write(line + "\n")
    out.flush()
    return 1 if missing else 0


# ---------------------------------------------------------------------------
# the entry point cli.main routes to


def keys_main(argv, devices=None) -> int:
    """`wdotool keys ...`. Never raises: a diagnostic that dies with a
    traceback is worse than no diagnostic."""
    try:
        if not argv:
            sys.stderr.write(_USAGE)
            return 1
        if argv[0] in ("-h", "--help", "help"):
            sys.stdout.write(_USAGE)
            return 0
        mode, rest = argv[0], argv[1:]
        if mode == "watch":
            return watch_main(rest, devices=devices)
        if mode == "explain":
            return explain_main(rest)
        sys.stderr.write("wdotool keys: unknown mode '%s'\n" % mode)
        sys.stderr.write(_USAGE)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as e:                     # never a traceback
        if os.environ.get("DEBUG") is not None:
            raise
        sys.stderr.write("wdotool keys: %r\n" % (e,))
        return 1
