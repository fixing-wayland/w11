"""Reverse keymap: which key produces a character under the ACTIVE layout.

We inject evdev keycodes through a virtual keyboard, and the compositor
interprets them through whatever XKB layout the session has active. The fixed
US-QWERTY table in `keymap.py` is therefore only right when the active layout
*is* US: under `de`, `fr` or Dvorak even plain ASCII comes out wrong. X11's
answer (rebind a spare keycode to the wanted keysym) is not on the table here
-- no compositor takes a keymap edit from a client, and Mutter does not
implement zwp_virtual_keyboard_v1 either -- so the route this file takes
instead is to reverse the lookup: read the compositor's keymap, find the
key + modifiers that produce the character, and press *that*. It costs
nothing at run time and it needs nothing installed, which is why it is the
one taken rather than a patched compositor.

Every Wayland client is handed the full keymap as a file descriptor on
`wl_keyboard.keymap`, so `fetch()` binds the seat, takes the keyboard and
reads the fd — no protocol extension, no privileges, nothing to install.

*Which* of that keymap's groups is active is the one thing the protocol keeps
from us: `wl_keyboard.modifiers` carries it and reaches only the window with
keyboard focus. sway sends it anyway, and so does COSMIC on a protocol of its
own (`zcosmic_keyboard_layout_manager_v1`, whose `group` event says in the XML
that it arrives with no focused window). The rest are asked outright: KWin on
the session bus (`KwinLayouts`), GNOME through the portal that serves its
setting (`GnomeInputSources`), Hyprland and Wayfire over the same IPC sockets
their window backends use (`HyprLayouts`, `WayfireLayouts`) and Cinnamon
through `org.Cinnamon.Eval` (`CinnamonInputSources`). Everywhere else the
group is inferred, and `Snapshot.group_known` says which of the two happened
-- a gap and not a policy: a compositor that is not in that list is one more
reader of its own bus or socket (AGENTS.md route 2), at the cost of a dialect
per desktop, which is what each name above already is.

    snap = fetch()                               # text + active group
    if not active_group_is_plain_us(snap.text, snap.group):
        rmap = build(snap.text, snap.group)      # else: keep the US table
        rmap.lookup_char("é")  -> [(13, 0), (18, 0)]   # dead_acute, then e

Layout of this module:
  * fetch()                      Wayland: keymap text + active group
  * kwin_group()                 KDE: the active group, off the session bus
  * gnome_group()                GNOME: the active group, from its setting
  * hypr_group()                 Hyprland: the active group, per input device
  * wayfire_group()              Wayfire: the active group, off its IPC
  * cinnamon_group()             Cinnamon: the active group, from its setting
  * desktop_group()              all five of those, tried in turn
  * parse()                      keymap text -> Keymap (keycodes/types/groups)
  * build()                      Keymap + group -> ReverseMap
  * ReverseMap.lookup_char()     char -> [(evdev keycode, modifier mask)]
  * active_group_is_plain_us()   the US bypass check -- deliberately its own
                                 scanner, sharing no code with the parser

An entry is (evdev keycode, modifier mask) where the mask is made of the
MOD_* bits below, NOT an X11 modifier mask: what the caller has to *press* is
a key, and which key carries level three is itself a property of the layout
(`ReverseMap.mod_keys`).

Env overrides (see README):
  WDOTOOL_LAYOUT=auto|us|xkb   force the fixed US table or the reverse map
  WDOTOOL_XKB_KEYMAP=<path>    read the keymap from a file, not the compositor
  WDOTOOL_XKB_GROUP=<n>        pin the active group (1-based)
"""

import json
import os
import re
import signal
import stat
import struct
import threading
import time
import unicodedata
import warnings

from wdotool.keysyms import KEYSYM_TO_UNICODE, NAME_TO_KEYSYM

# Modifier *bits* of an entry's mask. MOD_SHIFT is 1 so that the fixed US
# table's `shifted` boolean and a reverse-map mask are the same value.
MOD_SHIFT = 1
MOD_LEVEL3 = 2  # AltGr / ISO_Level3_Shift
MOD_LEVEL5 = 4  # ISO_Level5_Shift

MOD_BITS = (MOD_SHIFT, MOD_LEVEL3, MOD_LEVEL5)

# Fallback keycodes for the modifier bits, used only when the keymap does not
# say which key carries them (it always does in practice).
_DEFAULT_MOD_KEYS = {MOD_SHIFT: 42, MOD_LEVEL3: 100, MOD_LEVEL5: None}


class XkbError(Exception):
    """The keymap could not be read, parsed or reversed. Always caught by the
    caller, which then falls back to the fixed US table."""


class Snapshot:
    """One reading of the compositor's keyboard state."""

    __slots__ = ("text", "group", "source", "group_known", "mods_seen")

    def __init__(self, text: str, group: int, source: str, group_known: bool, mods_seen: bool = False):
        self.text = text
        self.group = group          # 1-based, as in the keymap's name[N]
        self.source = source        # for diagnostics
        self.group_known = group_known  # False: assumed, not read back
        # Did a wl_keyboard.modifiers event actually arrive? A compositor that does not send one to an unfocused
        # client never will, so the caller can stop paying `mods_wait` for it (B5).
        self.mods_seen = mods_seen


# ---------------------------------------------------------------------------
# which layout to type through


def layout_mode(forced: str | None = None) -> str:
    """"us", "xkb" or "auto" -- the layout the caller asked for, normalized.

    `forced` is a client's --layout, which outranks WDOTOOL_LAYOUT: a command line is the more specific
    statement of intent, and it is the only one that can reach a daemon spawned with a different environment.
    "fixed" is a spelling of "us", and anything unrecognized is "auto"."""
    mode = (forced or os.environ.get("WDOTOOL_LAYOUT") or "auto").strip().lower()
    if mode in ("us", "fixed"):
        return "us"
    return "xkb" if mode == "xkb" else "auto"


def decide(text: str, group: int, mode: str) -> bool:
    """THE BYPASS: is the fixed built-in US table the right answer here?

    "us" says so outright and nothing is read or parsed; "auto" says so when the active group is plain US, which
    is the common case and the fast one; "xkb" never does, even on a US keymap -- that is what asking for it
    means.
    """
    if mode == "us":
        return True
    return mode != "xkb" and active_group_is_plain_us(text, group)


# ---------------------------------------------------------------------------
# fetching


def fetch(timeout: float = 2.0, mods_wait: float = 0.08, keymap: str | None = None, group=None) -> Snapshot:
    """Read the active keymap + group. Raises XkbError, never hangs.

    `mods_wait` is how long to keep dispatching after the keymap arrives in the hope of a
    `wl_keyboard.modifiers` event carrying the active group. Mutter (and wlroots, and KWin) only send that event
    to the client that holds keyboard focus, which a headless injector never does -- so the wait usually expires
    and the group has to be inferred; see `choose_group`. On the five desktops `desktop_group` can ask
    (Hyprland, Wayfire, KWin, GNOME Shell, Cinnamon) it does not have to be inferred: where the inference
    would be a guess, the desktop is asked outright, and the answer is a group as known as the one sway --
    and COSMIC, over `zcosmic_keyboard_layout_v1` -- puts on the wire.

    `keymap` and `group` are what --keymap/--group pass; each falls back to WDOTOOL_XKB_KEYMAP /
    WDOTOOL_XKB_GROUP when the caller says nothing.
    """
    path = keymap if keymap is not None else os.environ.get("WDOTOOL_XKB_KEYMAP")
    forced = _pinned_group(group)
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise XkbError(f"cannot read {path}: {e}") from None
        group, known = (forced, True) if forced else (None, False)
        if group is None:
            group, known = choose_group(text, None)
        return Snapshot(text, group, f"file:{path}", known)
    # A pinned group answers the only question the wait exists for, so skip
    # the wait entirely rather than paying it and throwing the answer away.
    text, group, mods_seen = _fetch_wayland(timeout, 0.0 if forced else mods_wait)
    if forced:
        return Snapshot(text, forced, "wayland (group pinned)", True, mods_seen)
    if group is None:
        group, known = choose_group(text, None)
        if not known:
            # The keymap alone cannot say which of its groups is live. The
            # five desktops `desktop_group` asks each publish exactly that, so
            # ask before settling for group 1 -- and only here, so the sessions
            # that already know the answer never open a socket or a bus.
            told = desktop_group(text)
            if told is not None:
                # Four of the five desktops `desktop_group` asks LOCK the group they report (the applet
                # calls the compositor's own group-set), so their answer is the live group and known.
                # Cinnamon's is not: muffin 6.4 exposes `lock_layout_group`/`set_keymap` and NO getter
                # [M requests-batch-18.md 2, `get_keymap_layout_group` -> undefined over org.Cinnamon.Eval],
                # so `org.cinnamon.desktop.input-sources current` can name a group muffin is NOT decoding
                # under whenever `current` is written behind muffin's back -- silent garbage, the exact
                # failure the "assuming '<name>'" notice exists for. So Cinnamon's answer is a GUESS and the
                # notice fires. The true read is rung 5, Xwayland's XkbGetState `group` field (one
                # QueryExtension + XkbUseExtension + a 16-byte reply in x11_mini), which would serve GNOME
                # and KDE too; rung 2 is closed for reading (muffin has no getter on any Cinnamon bus).
                known = told[1] not in GROUP_ASSUMED_DESKTOPS
                return Snapshot(text, told[0], "wayland + " + told[1], known, mods_seen)
        return Snapshot(text, group, "wayland", known, mods_seen)
    return Snapshot(text, group, "wayland", True, mods_seen)


def _pinned_group(forced=None):
    """The caller's --group, else WDOTOOL_XKB_GROUP. Anything that is not a
    plain 1..4 is not a pin, and the group gets inferred as usual."""
    raw = str(forced) if forced is not None else (os.environ.get("WDOTOOL_XKB_GROUP") or "")
    raw = raw.strip()
    if not raw.isdigit():
        return None
    n = int(raw)
    return n if 1 <= n <= 4 else None


def _fetch_wayland(timeout: float, mods_wait: float):
    """(keymap text, active group or None, modifiers event seen?)."""
    from w11common import session
    from w11common.wayland_mini import WlConn

    hit = session.find_wayland_socket()
    if hit is None:
        raise XkbError("no wayland socket found")
    try:
        conn = WlConn(hit[2])
    except OSError as e:
        raise XkbError(f"cannot connect to the compositor: {e}") from None
    # A wedged compositor must not hang the daemon while it holds the lock.
    conn.sock.settimeout(timeout)
    state = {"caps": 0, "text": None, "group": None, "mods": False}
    try:
        found = conn.find_global("wl_seat")
        if found is None:
            raise XkbError("the compositor advertises no wl_seat")
        seat = conn.bind(found[0], "wl_seat", min(found[1], 7))

        def seat_handler(op, cur, fds):
            if op == 0:  # capabilities(capabilities)
                state["caps"] = cur.u32()

        conn.on(seat, seat_handler)
        conn.roundtrip()
        if not state["caps"] & 2:  # WL_SEAT_CAPABILITY_KEYBOARD
            raise XkbError("the seat has no keyboard capability")
        kb = conn.alloc()
        conn.send(seat, 1, [("u", kb)])  # wl_seat.get_keyboard

        def kb_handler(op, cur, fds):
            if op == 0:  # keymap(format, fd, size)
                fmt, size = cur.u32(), cur.u32()
                if not fds:
                    return
                fd = fds.pop(0)
                try:
                    if fmt != 1:  # XKB_V1 is the only format there is
                        return
                    state["text"] = _read_keymap_fd(fd, size)
                finally:
                    os.close(fd)
            elif op == 4:  # modifiers(serial, depressed, latched, locked, group)
                cur.u32(), cur.u32(), cur.u32(), cur.u32()
                state["mods"] = True
                state["group"] = cur.u32() + 1  # wire is 0-based, keymap 1-based

        conn.on(kb, kb_handler)
        conn.roundtrip()
        if state["text"] is None:
            raise XkbError("the compositor sent no keymap")
        # The group only matters when there is more than one to choose from.
        if state["group"] is None and group_count(state["text"]) > 1:
            # Guarded exactly as the modifiers wait below is, and for the same reason: the keymap is
            # already read, and a cosmic-comp that refuses `get_keyboard_layout` (a `wl_display.error`
            # on a keyboard it does not like, a version quibble, a socket that times out) must leave the
            # caller its guess rather than throw away an answer we have. Nothing about typing may depend
            # on this working (B13).
            try:
                _cosmic_group(conn, kb, state)
            except (OSError, RuntimeError, ValueError, IndexError, struct.error):
                pass
        if state["group"] is None and mods_wait > 0 and group_count(state["text"]) > 1:
            try:
                conn.dispatch(mods_wait)
            except (OSError, RuntimeError):
                pass
        return state["text"], state["group"], state["mods"]
    except (OSError, RuntimeError, ValueError, IndexError, struct.error) as e:
        raise XkbError(f"wayland keymap read failed: {e}") from None
    finally:
        conn.close()


#: COSMIC's way of saying what wl_keyboard.modifiers will not say to an
#: unfocused client. cosmic-comp advertised it at version 1 among its 53
#: globals [M recon2/cosmic.md §4, globals.txt].
COSMIC_LAYOUT_MANAGER = "zcosmic_keyboard_layout_manager_v1"


def _cosmic_group(conn, kb: int, state: dict):
    """Take COSMIC's active group off the wire, into `state["group"]`. Silent when it is not offered.

    `zcosmic_keyboard_layout_manager_v1.get_keyboard_layout(new_id, wl_keyboard)` makes a
    `zcosmic_keyboard_layout_v1` whose `group` event carries the active index, 0-based as on
    `wl_keyboard.modifiers` -- and the protocol says in as many words that it is "received even when the
    client has no focused window", which is the whole difference and the reason COSMIC needs no bus, no
    portal and no reader below [R recon2/cosmic/cosmic-keyboard-layout-unstable-v1.xml; the plan's A 1.4].

    One extra round trip, on a session that advertises the interface and has more than one group to choose
    between; nothing to close (the objects live as long as the connection, which ends with this function's
    caller). Whether cosmic-comp really sends the event to a client that has never been focused is not
    measured, so a group that does not arrive leaves `state` alone and the caller guesses as before -- and
    so does a compositor that refuses the request outright, which is why the caller wraps this call: the
    keymap is already read by then, and a `wl_display.error` on this round trip may not cost us that.
    """
    found = conn.find_global(COSMIC_LAYOUT_MANAGER)   # the registry is already cached: no round trip
    if found is None:
        return
    mgr = conn.bind(found[0], COSMIC_LAYOUT_MANAGER, min(found[1], 1))
    layout = conn.alloc()

    def layout_handler(op, cur, fds):
        if op == 0:  # group(group)
            state["group"] = cur.u32() + 1  # wire is 0-based, keymap 1-based

    conn.on(layout, layout_handler)
    conn.send(mgr, 0, [("u", layout), ("u", kb)])  # get_keyboard_layout(id, keyboard)
    conn.roundtrip()


def _read_keymap_fd(fd: int, size: int) -> str:
    import mmap

    if size <= 0 or size > (16 << 20):
        raise XkbError(f"implausible keymap size {size}")
    try:
        m = mmap.mmap(fd, size, mmap.MAP_PRIVATE, mmap.PROT_READ)
    except (OSError, ValueError):
        data = os.pread(fd, size, 0)  # a plain file (or a compositor that
    else:                             # sent a non-mmapable fd) still works
        try:
            data = m[:size]
        finally:
            m.close()
    return data.rstrip(b"\0").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# parsing

_SECTION_RE = re.compile(r"\bxkb_(keycodes|types|compat\w*|symbols)\b[^{}]*\{")
_CODE_RE = re.compile(r"<([^<>\s]+)>\s*=\s*(\d+)\s*;")
_ALIAS_RE = re.compile(r"\balias\s+<([^<>\s]+)>\s*=\s*<([^<>\s]+)>")
_TYPE_RE = re.compile(r'\btype\s+"([^"]*)"\s*\{')
_MAP_RE = re.compile(r"\bmap\s*\[\s*([^\]]*?)\s*\]\s*=\s*(\d+)")
_GROUPNAME_RE = re.compile(r'\bname\s*\[\s*(?:Group)?(\d+)\s*\]\s*=\s*"([^"]*)"')
_KEY_RE = re.compile(r"\bkey\s+<([^<>\s]+)>\s*\{")
_FIELD_RE = re.compile(r"^(\w+)\s*(?:\[\s*(?:Group)?(\d+)\s*\])?\s*=\s*(.*)$", re.S)

# level -> mask, when the key's type says nothing else. This is the xkeyboard-config convention every layout
# follows: 3 is AltGr, 5 is level five. Public: keys_cmds.py builds the *forward* map (keycode + mask -> keysym)
# from the same convention, and a second copy of it would drift.
LEVEL_MASK = {
    1: 0,
    2: MOD_SHIFT,
    3: MOD_LEVEL3,
    4: MOD_SHIFT | MOD_LEVEL3,
    5: MOD_LEVEL5,
    6: MOD_SHIFT | MOD_LEVEL5,
    7: MOD_LEVEL3 | MOD_LEVEL5,
    8: MOD_SHIFT | MOD_LEVEL3 | MOD_LEVEL5,
}

# XKB modifier names we can actually press, real and virtual. Everything else (Lock, Control, Alt, NumLock,
# Mod1/2/4, Super, Meta, Hyper) makes a level unreachable for us: we will not hold Control down to type a
# character.
_MOD_NAME_BITS = {
    "shift": MOD_SHIFT,
    "levelthree": MOD_LEVEL3,
    "mod5": MOD_LEVEL3,
    "levelfive": MOD_LEVEL5,
    "mod3": MOD_LEVEL5,
}

# keysym -> the level-shift bit that key carries.
_LEVEL_KEYSYMS = {
    0xFFE1: MOD_SHIFT,   # Shift_L
    0xFFE2: MOD_SHIFT,   # Shift_R
    0xFE03: MOD_LEVEL3,  # ISO_Level3_Shift
    0xFF7E: MOD_LEVEL3,  # Mode_switch
    0xFE11: MOD_LEVEL5,  # ISO_Level5_Shift
}
# Preferred physical keys for each bit, best first: a real keyboard has AltGr on <RALT>, and <LVL3>/<LVL5> are
# the synthetic keycodes xkeyboard-config keeps for keyboards that have a dedicated key.
_MOD_KEY_ORDER = {
    MOD_SHIFT: ("LFSH", "RTSH"),
    MOD_LEVEL3: ("RALT", "ALGR", "LVL3", "MDSW"),
    MOD_LEVEL5: ("LVL5", "MDSW"),
}


class Group:
    """One layout inside the keymap (XKB calls them groups)."""

    def __init__(self, index: int):
        self.index = index          # 1-based
        self.name = ""
        self.syms: dict[str, list] = {}   # key name -> [keysym or None, ...]
        self.types: dict[str, str] = {}   # key name -> type name


class Keymap:
    def __init__(self):
        self.keycodes: dict[str, int] = {}     # key name -> evdev keycode
        self.types: dict[str, dict] = {}       # type name -> {level: mask|None}
        self.groups: list[Group] = []          # index 0 is group 1

    def group(self, n: int) -> Group:
        if not 1 <= n <= len(self.groups):
            raise XkbError(f"no group {n} in this keymap ({len(self.groups)})")
        return self.groups[n - 1]

    def resolved(self, n: int) -> dict:
        """{key name: [keysym, ...]} for group `n`, with XKB's group wrapping applied: a key that binds fewer
        groups than the keymap has repeats its own groups (the default groupsWrap). Most keys -- every key that
        is the same on every layout, <RTRN> and <SPCE> included -- bind only group 1, and would otherwise vanish
        from group 2."""
        self.group(n)
        out = {}
        for key, per_group in self._by_key().items():
            top = max(per_group)
            levels = per_group.get((n - 1) % top + 1) or per_group.get(1)
            if levels:
                out[key] = levels
        return out

    def _by_key(self) -> dict:
        by_key: dict = {}
        for g in self.groups:
            for key, levels in g.syms.items():
                by_key.setdefault(key, {})[g.index] = levels
        return by_key

    @property
    def group_names(self) -> list:
        return [g.name for g in self.groups]


# A string literal, or a comment. Matching the literal first is what keeps a
# `//` inside a group name from being taken for a comment.
_COMMENT_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)


def strip_comments(text: str) -> str:
    """XKB comments (`// ...`, `/* ... */`) removed, string literals kept.

    Pure text handling with no keymap knowledge, which is why the parser and the US bypass may share it: a `}`
    inside a comment would otherwise close a block early and silently halve the keymap (B4). No compositor emits
    comments, but `WDOTOOL_XKB_KEYMAP=<file>` is a documented input and hand-written keymaps are full of them.
    """
    if "//" not in text and "/*" not in text:
        return text  # the common case: not one byte copied
    return _COMMENT_RE.sub(lambda m: m.group(0) if m.group(0)[:1] == '"' else " ", text)


def group_name(text: str, n: int) -> str:
    """The name of group `n`, by regex: no parse, so the US bypass path can
    say which group it assumed without building anything (B1)."""
    for m in _GROUPNAME_RE.finditer(strip_comments(text)):
        if int(m.group(1)) == n:
            return m.group(2) or f"group {n}"
    return f"group {n}"


# libxkbcommon's XKB_MAX_GROUPS: no keymap can legitimately declare more. The group index is a number the
# compositor chose, not a length we measured, so `symbols[Group2000000000]` is eight bytes of keymap text that
# parse() would otherwise turn into two billion Group objects (a root daemon can be pointed at a planted Wayland
# socket, so the compositor is not always ours).
MAX_GROUPS = 4


def group_count(text: str) -> int:
    """How many groups the keymap declares, capped at MAX_GROUPS. Cheap: no
    full parse."""
    n = 0
    for m in _GROUPNAME_RE.finditer(text):
        n = max(n, int(m.group(1)))
    for m in re.finditer(r"\bsymbols\s*\[\s*(?:Group)?(\d+)\s*\]", text):
        n = max(n, int(m.group(1)))
    return min(n, MAX_GROUPS) or 1


def _brace_body(text: str, open_idx: int) -> str:
    """The text between the brace at open_idx and its match. String-aware."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    raise XkbError("unterminated block in the keymap")


def _sections(text: str) -> dict:
    out = {}
    for m in _SECTION_RE.finditer(text):
        kind = m.group(1)
        kind = "compat" if kind.startswith("compat") else kind
        if kind not in out:
            out[kind] = _brace_body(text, m.end() - 1)
    return out


def _split_fields(body: str) -> list:
    """Split a key block into its comma-separated fields, ignoring commas
    inside [ ... ] lists and strings."""
    fields = []
    depth = 0
    cur = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == '"':
            cur.append(c)
            i += 1
            while i < n and body[i] != '"':
                cur.append(body[i])
                i += 1
            cur.append('"')
        elif c in "[({":
            depth += 1
            cur.append(c)
        elif c in "])}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            fields.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        fields.append(tail)
    return [f for f in fields if f]


def keysym_value(tok: str):
    """One symbol-list token -> keysym number, or None for NoSymbol."""
    tok = tok.strip()
    if not tok or tok in ("NoSymbol", "VoidSymbol", "any"):
        return None
    if tok[:2] in ("0x", "0X"):
        try:
            return int(tok, 16)
        except ValueError:
            return None
    ks = NAME_TO_KEYSYM.get(tok)
    if ks is not None:
        return ks
    m = re.fullmatch(r"U([0-9A-Fa-f]{4,6})", tok)
    if m:
        cp = int(m.group(1), 16)
        return cp if cp < 0x100 else 0x01000000 + cp
    return None


def keysym_char(ks: int):
    """The character a keysym types, or None (dead keys included: they type
    nothing on their own)."""
    if ks is None:
        return None
    cp = KEYSYM_TO_UNICODE.get(ks)
    if cp is None:
        if 0x20 <= ks <= 0xFF:
            cp = ks
        elif ks & 0xFF000000 == 0x01000000:
            cp = ks & 0xFFFFFF
        else:
            return None
    if cp < 0x20 or cp == 0x7F:  # Return/Tab/BackSpace: named keys, not text
        return None
    try:
        return chr(cp)
    except ValueError:
        return None


def _type_level_masks(body: str) -> dict:
    """A type's map[] entries -> {level: cheapest mask we can press}."""
    out = {1: 0}
    for m in _MAP_RE.finditer(body):
        names = [n.strip().lower() for n in m.group(1).split("+") if n.strip()]
        level = int(m.group(2))
        mask = 0
        ok = True
        for name in names:
            if name == "none":
                continue
            bit = _MOD_NAME_BITS.get(name)
            if bit is None:
                ok = False
                break
            mask |= bit
        if not ok:
            continue
        prev = out.get(level)
        if prev is None or bin(mask).count("1") < bin(prev).count("1"):
            out[level] = mask
    return out


def parse(text: str) -> Keymap:
    """Parse a keymap in XKB_KEYMAP_FORMAT_TEXT_V1 (what every compositor
    hands out). Raises XkbError on anything it cannot make sense of."""
    text = strip_comments(text)
    if "xkb_symbols" not in text:
        raise XkbError("not an xkb keymap (no xkb_symbols section)")
    sec = _sections(text)
    km = Keymap()

    # -- keycodes: <AD01> = 24;  (X keycodes; evdev is 8 lower)
    codes = sec.get("keycodes", "")
    for m in _CODE_RE.finditer(codes):
        x = int(m.group(2))
        if 8 < x < 264:
            km.keycodes[m.group(1)] = x - 8
    for m in _ALIAS_RE.finditer(codes):
        target = km.keycodes.get(m.group(2))
        if target is not None:
            km.keycodes.setdefault(m.group(1), target)
    if not km.keycodes:
        raise XkbError("the keymap declares no keycodes")

    # -- types: which modifiers reach which level
    types_body = sec.get("types", "")
    for m in _TYPE_RE.finditer(types_body):
        km.types[m.group(1)] = _type_level_masks(_brace_body(types_body, m.end() - 1))

    # -- symbols: the groups themselves
    syms = sec.get("symbols")
    if syms is None:
        raise XkbError("the keymap has no xkb_symbols section")
    ngroups = max(group_count(syms), 1)
    km.groups = [Group(i + 1) for i in range(ngroups)]
    for m in _GROUPNAME_RE.finditer(syms):
        i = int(m.group(1))
        if 1 <= i <= ngroups:
            km.groups[i - 1].name = m.group(2)
    for m in _KEY_RE.finditer(syms):
        name = m.group(1)
        if name not in km.keycodes:
            continue
        body = _brace_body(syms, m.end() - 1)
        bare = 0
        for field in _split_fields(body):
            if field.startswith("["):
                bare += 1
                _add_symbols(km, name, bare, field)
                continue
            fm = _FIELD_RE.match(field)
            if not fm:
                continue
            what, idx, value = fm.group(1).lower(), fm.group(2), fm.group(3).strip()
            if what == "symbols" and value.startswith("["):
                _add_symbols(km, name, int(idx) if idx else 1, value)
            elif what == "type" and value.startswith('"'):
                tname = value.strip('"; ')
                for g in ([int(idx)] if idx else range(1, ngroups + 1)):
                    if 1 <= g <= ngroups:
                        km.groups[g - 1].types[name] = tname
    if not any(g.syms for g in km.groups):
        raise XkbError("the keymap binds no symbols")
    return km


def _add_symbols(km: Keymap, key: str, group: int, listtext: str):
    if not 1 <= group <= len(km.groups):
        return
    inner = listtext[listtext.index("[") + 1:]
    inner = inner[:inner.rindex("]")] if "]" in inner else inner
    km.groups[group - 1].syms[key] = [keysym_value(t) for t in inner.split(",")]


# ---------------------------------------------------------------------------
# dead keys

# dead keysym -> (combining codepoint, the character the dead key + space produces). NFD-decomposing a character
# gives the combining mark, which is how "é" becomes dead_acute + e without shipping a Compose table.
#
# The second element is not the spacing accent that shares the dead key's name: it is whatever <dead_x> <space>
# yields in the Compose table every toolkit implements (/usr/share/X11/locale/*/Compose), which for acute,
# diaeresis and abovering is ASCII ' " and °. Claiming U+00B4 there made `type ´` send dead_acute + space and
# land an apostrophe instead (B3).
DEAD_KEYSYMS = {
    0xFE50: (0x0300, 0x0060),  # dead_grave
    0xFE51: (0x0301, 0x0027),  # dead_acute      -> apostrophe, not U+00B4
    0xFE52: (0x0302, 0x005E),  # dead_circumflex
    0xFE53: (0x0303, 0x007E),  # dead_tilde
    0xFE54: (0x0304, 0x00AF),  # dead_macron
    0xFE55: (0x0306, 0x02D8),  # dead_breve
    0xFE56: (0x0307, 0x02D9),  # dead_abovedot
    0xFE57: (0x0308, 0x0022),  # dead_diaeresis  -> quotedbl, not U+00A8
    0xFE58: (0x030A, 0x00B0),  # dead_abovering  -> degree, not U+02DA
    0xFE59: (0x030B, 0x02DD),  # dead_doubleacute
    0xFE5A: (0x030C, 0x02C7),  # dead_caron
    0xFE5B: (0x0327, 0x00B8),  # dead_cedilla
    0xFE5C: (0x0328, 0x02DB),  # dead_ogonek
    0xFE5D: (0x0345, None),    # dead_iota
    0xFE5E: (0x3099, None),    # dead_voiced_sound
    0xFE5F: (0x309A, None),    # dead_semivoiced_sound
    0xFE60: (0x0323, None),    # dead_belowdot
    0xFE61: (0x0309, None),    # dead_hook
    0xFE62: (0x031B, None),    # dead_horn
    0xFE63: (0x0338, None),    # dead_stroke
    0xFE64: (0x0313, None),    # dead_abovecomma
    0xFE65: (0x0314, None),    # dead_abovereversedcomma
    0xFE66: (0x030F, None),    # dead_doublegrave
    0xFE67: (0x0325, None),    # dead_belowring
    0xFE68: (0x0331, None),    # dead_belowmacron
    0xFE69: (0x032D, None),    # dead_belowcircumflex
    0xFE6A: (0x0330, None),    # dead_belowtilde
    0xFE6B: (0x032E, None),    # dead_belowbreve
    0xFE6C: (0x0324, None),    # dead_belowdiaeresis
    0xFE6D: (0x0311, None),    # dead_invertedbreve
    0xFE6E: (0x0326, None),    # dead_belowcomma
}

# <dead_x> <dead_x> types the spacing accent itself -- the only way to type one on a layout that has it *only*
# as a dead key, now that <dead_x> <space> is known to type something else. The six the Compose table lists, no
# more.
DEAD_DOUBLE = {
    0xFE50: 0x0060,  # dead_grave      -> `
    0xFE51: 0x00B4,  # dead_acute      -> ´
    0xFE52: 0x005E,  # dead_circumflex -> ^
    0xFE53: 0x007E,  # dead_tilde      -> ~
    0xFE57: 0x00A8,  # dead_diaeresis  -> ¨
    0xFE58: 0x00B0,  # dead_abovering  -> °
}

# Characters that are keys, not text. xdotool's `type` sends these through the
# named key, exactly as the fixed US table does.
_CONTROL_KEYSYMS = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08, "\x1b": 0xFF1B, "\x7f": 0xFFFF}


class ReverseMap:
    """char/keysym -> the keystrokes that produce it under one group."""

    def __init__(self, group_index: int, group_name: str):
        self.group = group_index
        self.name = group_name or f"group {group_index}"
        self.chars: dict[str, tuple] = {}      # char -> (keycode, mask)
        self.keysyms: dict[int, tuple] = {}    # keysym -> (keycode, mask)
        self.dead: dict[int, tuple] = {}       # combining cp -> (keycode, mask)
        self.dead_space: dict[str, tuple] = {}  # dead+space char -> entry
        self.dead_double: dict[str, tuple] = {}  # dead+dead char -> entry
        self.mod_keys: dict[int, int] = {}     # MOD_* bit -> evdev keycode
        self._best: dict = {}                  # (table, key) -> cost so far

    # -- lookups ----------------------------------------------------------

    def lookup_char(self, ch: str):
        """The keystrokes that type `ch`: [(keycode, mask), ...], or None.

        More than one keystroke means a dead-key sequence: press the dead key, then the base character, and the
        *application* composes them (that is how a physical keyboard types é on a French layout too)."""
        ks = _CONTROL_KEYSYMS.get(ch)
        if ks is not None:
            hit = self.keysyms.get(ks)
            return [hit] if hit else None
        hit = self.chars.get(ch)
        if hit:
            return [hit]
        seq = self._dead_sequence(ch)
        if seq:
            return seq
        return None

    def _dead_sequence(self, ch: str):
        dead = self.dead_space.get(ch)
        if dead:  # dead key, then space
            space = self.chars.get(" ")
            if space:
                return [dead, space]
        dead = self.dead_double.get(ch)
        if dead:  # a spacing accent: the dead key twice
            return [dead, dead]
        decomposed = unicodedata.normalize("NFD", ch)
        if len(decomposed) != 2:
            return None
        base, mark = decomposed
        dead = self.dead.get(ord(mark))
        base_hit = self.chars.get(base)
        if dead and base_hit:
            # Only if it really recomposes: NFC(base+mark) == ch. Anything
            # else would type two characters where one was asked for.
            if unicodedata.normalize("NFC", base + mark) == ch:
                return [dead, base_hit]
        return None

    def keysym_entry(self, name: str):
        """(keycode, mask) for a keysym *name*, or None. Used by `key`."""
        ks = NAME_TO_KEYSYM.get(name)
        return self.keysyms.get(ks) if ks is not None else None

    def modifier_keycodes(self, mask: int) -> list:
        return [self.mod_keys[b] for b in MOD_BITS if mask & b and self.mod_keys.get(b)]

    # -- building ---------------------------------------------------------

    def _better(self, tag: str, key, entry, rank: int = 0):
        """Keep the cheapest keystroke for `key` in the table named `tag`:
        fewest modifiers first, and a main-block key over a keypad one."""
        table = getattr(self, tag)
        cost = (rank,) + _cost(entry)
        old = self._best.get((tag, key))
        if old is None or cost < old:
            self._best[(tag, key)] = cost
            table[key] = entry


def _cost(entry) -> tuple:
    code, mask = entry
    return (bin(mask).count("1"), mask, code)


def _keypad_rank(code: int, ks) -> int:
    """0 for a key in the main block, 1 for one on the keypad (B6).

    Excluding the keypad by key *name* (`<KP...>`) leaks: Mutter's keymaps put KP_Decimal on <I129> and the
    keypad parentheses on <I187>/<I188>, so `.` on a French layout and `(` on every non-US one used to resolve
    to a keypad keycode. A keypad entry is still recorded -- it really does produce the character -- but it can
    never beat a main-block key."""
    if ks is not None and 0xFF80 <= ks <= 0xFFBD:  # KP_Space .. KP_Equal
        return 1
    return 1 if code >= 128 else 0


def build(text: str, group: int = 1) -> ReverseMap:
    """Parse `text` and reverse its group `group` (1-based)."""
    return reverse(parse(text), group)


def reverse(km: Keymap, group: int = 1) -> ReverseMap:
    g = km.group(group)
    syms = km.resolved(group)
    rmap = ReverseMap(group, g.name)

    # Which key carries Shift / level three / level five *in this group*: on a
    # German layout <RALT> is ISO_Level3_Shift, on a US one it is Alt_R.
    ranks: dict = {}
    for key, levels in sorted(syms.items()):
        code = km.keycodes.get(key)
        if code is None or not levels or not 0 < code < 256:
            continue
        bit = _LEVEL_KEYSYMS.get(levels[0])
        if bit is None:
            continue
        order = _MOD_KEY_ORDER.get(bit, ())
        rank = order.index(key) if key in order else len(order)
        if rank < ranks.get(bit, 99):
            ranks[bit] = rank
            rmap.mod_keys[bit] = code
    # What the *keymap* said, before the fallbacks are filled in: the guard below has to test that, not the
    # backfilled table, or it can never fire for the case it names. Every fixture we have names all three keys,
    # so the fallbacks are belt and braces and this changes nothing for them.
    from_keymap = set(rmap.mod_keys)
    for bit, code in _DEFAULT_MOD_KEYS.items():
        if code and bit not in rmap.mod_keys:
            rmap.mod_keys[bit] = code

    for key, levels in sorted(syms.items()):
        code = km.keycodes.get(key)
        if code is None or not 0 < code < 256:
            continue
        masks = km.types.get(g.types.get(key, ""), None)
        for i, ks in enumerate(levels):
            if ks is None:
                continue
            level = i + 1
            mask = masks.get(level) if masks else LEVEL_MASK.get(level)
            if mask is None:
                continue
            # A level we cannot press (no AltGr key in this layout) is a level that does not exist for us.
            # Testing rmap.mod_keys here tested the fallback keycodes too, so on a layout with four-level types
            # and no level-3 key -- lv3:none, a custom keymap -- 77 German characters still resolved to "hold
            # keycode 100", which without ISO_Level3_Shift on it is a plain Alt_R: a menu accelerator on both
            # GNOME and KWin, i.e. a wrong action where the promise is a warning and a skip.
            if any(mask & b and b not in from_keymap for b in MOD_BITS):
                continue
            entry = (code, mask)
            rank = _keypad_rank(code, ks)
            rmap._better("keysyms", ks, entry, rank)
            dead = DEAD_KEYSYMS.get(ks)
            if dead is not None:
                rmap._better("dead", dead[0], entry, rank)
                if dead[1] is not None:
                    rmap._better("dead_space", chr(dead[1]), entry, rank)
                twice = DEAD_DOUBLE.get(ks)
                if twice is not None:
                    rmap._better("dead_double", chr(twice), entry, rank)
                continue
            ch = keysym_char(ks)
            if ch is not None:
                rmap._better("chars", ch, entry, rank)
    if not rmap.chars:
        raise XkbError("no typable character in group %d" % group)
    return rmap


# ---------------------------------------------------------------------------
# group selection

_US_KEY_RE = re.compile(r"\bkey\s+<([^<>\s]+)>\s*\{([^{}]*)\}")
_US_SYMS_RE = re.compile(r"\bsymbols\s*\[\s*(?:Group)?(\d+)\s*\]\s*=\s*\[([^\]]*)\]")


def choose_group(text: str, from_modifiers=None) -> tuple:
    """(group, known) -- which group to reverse, and whether we *know*.

    The compositor only sends wl_keyboard.modifiers -- the one event that
    carries the active group -- to the client holding keyboard focus, which
    an injector never is. What is left:

      * one group, or several that bind the same symbols: the choice cannot
        matter (GNOME compiles a single `us` source as the two groups
        "us,us", so this is the ordinary GNOME case);
      * otherwise group 1, flagged as *assumed*. It is the first configured
        source, which is the active one whenever the user configured exactly
        one -- the case this whole module exists for. GNOME appends its own
        `us` fallback group after the user's sources, so "de,us" is a session
        with one German source, and group 1 is right.

    "Assumed" is where `fetch()` goes and asks the desktop (`desktop_group`),
    so on the five desktops that reader can ask -- Hyprland, Wayfire, KWin,
    GNOME Shell, Cinnamon -- this function's second case is a fallback rather
    than the answer. It stays the answer everywhere else.

    Deliberately regex-only: choosing a group must not need the parser, or
    the parser would run on a plain US layout, which the bypass promises it
    does not (see `active_group_is_plain_us`).
    """
    if from_modifiers:
        return from_modifiers, True
    if group_count(text) <= 1 or _groups_agree(text):
        return 1, True
    return 1, False


def _groups_agree(text: str) -> bool:
    """True when no key binds different symbols to different groups."""
    for m in _US_KEY_RE.finditer(text):
        lists = [sm.group(2).split() for sm in _US_SYMS_RE.finditer(m.group(2))]
        if len(lists) > 1 and any(sym != lists[0] for sym in lists[1:]):
            return False
    return True


# ---------------------------------------------------------------------------
# KDE: which layout is active, asked of KWin itself
#
# The one thing the Wayland protocol will not tell an injector, KWin publishes
# on the session bus: `org.kde.KWin` `/Layouts` `org.kde.KeyboardLayouts`
# `getLayout()` answers with the 0-based index of the active layout in the
# list the user configured -- and that list IS the keymap's group order, name
# for name (`getLayoutsList()`'s long name is character-for-character the
# keymap's `name[GroupN]`). So the active group is that index plus one, with
# nothing to translate.
#
# Measured on Plasma 6.6.6 and 5.27.12: the same object, the same members and
# the same meaning on both, tracking every way a user switches (the applet,
# setLayout, switchToNext/PreviousLayout, the global shortcut, and the chord
# pressed through wdotool itself) and, under per-window layouts, reporting the
# focused window's layout -- which is where our keystrokes land, so that is
# the answer we want. KDE truncates the configured list at four, which is
# XKB's MAX_GROUPS, so the index can never legitimately outrun the keymap.
#
# Two rules, both measured rather than assumed:
#
#   * ask KWin, never kded. `org.kde.kded6 /modules/keyboard` and the kded5
#     one declare this same interface and CRASH on getLayout -- every call
#     answered NoReply and the bus name changed owner, on both generations.
#     There is no fallback to them and there must never be one.
#   * never send a method call to a name nobody owns: that asks the bus to
#     START the service, and a GNOME box with kwin installed must not have
#     one launched at it. NameHasOwner first; it activates nothing.
#
# Everything here is best effort, and the caller keeps its guess whenever the
# answer does not arrive: no session bus, no KWin, an older KWin without
# /Layouts, a KWin that has gone away, an index that does not fit the keymap
# we just read. A layout wdotool cannot ask about is exactly the layout it
# used to assume, notice and all.

KWIN_BUS_NAME = "org.kde.KWin"
KWIN_LAYOUTS_PATH = "/Layouts"
KWIN_LAYOUTS_IFACE = "org.kde.KeyboardLayouts"
KWIN_TIMEOUT = 2.0       # KWin answers in ~0.3 ms; this only bounds a wedge
KWIN_RETRY_AFTER = 10.0  # monotonic seconds before re-dialling a bus that failed

# Error names that describe the session rather than the moment: this KWin
# does not have this interface, or this bus will not let us at it. Asking
# again would be asking the same question. A name that is momentarily
# unowned (KWin restarting) is deliberately NOT in here.
_KWIN_FATAL = ("UnknownObject", "UnknownMethod", "UnknownInterface",
               "UnknownProperty", "AccessDenied", "NotSupported")


class KwinLayouts:
    """KWin's answer to "which of the configured layouts is active?", over one
    connection kept for the life of the process.

    One instance is enough: the daemon asks from inside its own lock, a client
    asks once and exits, and the object serialises itself anyway. The bus is
    dialled lazily on the first question that needs an answer, so a plain-US
    session, a one-layout session and the ordinary GNOME `us,us` session never
    open one at all (`choose_group` already knows the group there).

    Failure is silence: `group()` returns None and never raises, and a bus
    that refuses is not dialled again for KWIN_RETRY_AFTER seconds. A call
    that fails once is retried on a fresh connection -- a KWin restart takes
    the old one down with it -- and then given up on.
    """

    def __init__(self, address=None):
        self.address = address   # None: the graphical session's bus
        self.bus = None
        self.absent = False      # nothing to talk to here; stop asking
        self.retry_at = 0.0      # monotonic deadline of the connect backoff
        self.asked = 0           # answers received, for the tests
        self._lock = threading.Lock()

    def group(self, text: str):
        """The active group (1-based) or None.

        `text` is the keymap the index has to fit: an index past its last
        group is not an answer but a race with a layout list the user has
        just edited, and the caller's own guess is the better one."""
        with self._lock:
            try:
                idx = self._ask()
                if idx is None:
                    return None
                n = int(idx) + 1
                return n if 1 <= n <= group_count(text) else None
            except Exception:
                # Nothing about typing may depend on this working (B13).
                return None

    def close(self):
        with self._lock:
            self._drop()

    # -- the bus

    def _drop(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def _connect(self):
        """The session bus, with KWin on it, or None. Root gets there the way
        every other tool here does -- dbus_mini retries a refused EXTERNAL
        auth through a forked child owning the socket (measured at 3 ms)."""
        if self.bus is not None:
            return self.bus
        from w11common.dbus_mini import Bus
        try:
            bus = Bus(self.address, timeout=KWIN_TIMEOUT)
        except Exception:
            return None          # no session bus (yet): the backoff catches it
        try:
            if not bus.name_has_owner(KWIN_BUS_NAME):
                bus.close()
                # Nobody owns it. On a GNOME or wlroots session that is
                # permanent, and worth remembering: the alternative is a
                # connect and a GetNameOwner on every command a non-US GNOME
                # session types. If KWin has answered us before, though, this
                # is a restart and it will be back -- the backoff covers it.
                self.absent = not self.asked
                return None
        except Exception:
            # Anything at all here -- the bus answering nonsense included --
            # and the connection is ours to close, or the next command opens
            # a second one on top of it.
            bus.close()
            return None
        self.bus = bus
        return bus

    def _ask(self):
        """getLayout(), or None. One reconnect: KWin restarting must not
        poison the rest of the session, and a stale socket looks exactly like
        a wedged one from here."""
        now = time.monotonic()
        if self.absent or now < self.retry_at:
            return None
        for attempt in (0, 1):
            bus = self._connect()
            if bus is None:
                self.retry_at = now + KWIN_RETRY_AFTER
                return None
            try:
                out = bus.call(KWIN_BUS_NAME, KWIN_LAYOUTS_PATH, KWIN_LAYOUTS_IFACE,
                               "getLayout", timeout=KWIN_TIMEOUT)
            except Exception as e:
                # DBusError carries the bus's own error name; anything else
                # (a socket, a body we cannot read) has none, and none of
                # those are permanent.
                self._drop()
                if any(getattr(e, "name", "").endswith(w) for w in _KWIN_FATAL):
                    self.absent = True
                    return None
                if attempt:
                    self.retry_at = now + KWIN_RETRY_AFTER
                    return None
                continue         # once more, on a new connection
            if len(out) != 1 or not isinstance(out[0], int) or isinstance(out[0], bool):
                return None      # not the `u` this interface promises
            self.asked += 1
            return out[0]
        return None


_kwin = None
_kwin_lock = threading.Lock()


def kwin_group(text: str):
    """The active group KWin reports, or None where nothing answers.

    The module-level connection is the point: the daemon is long-lived and
    asks on every command whose group it would otherwise have to guess."""
    global _kwin
    with _kwin_lock:
        if _kwin is None:
            _kwin = KwinLayouts()
        reader = _kwin
    return reader.group(text)


# ---------------------------------------------------------------------------
# GNOME: which layout is active, read from the setting the shell keeps
#
# Mutter tells an unfocused client no more than KWin does, but GNOME Shell
# keeps the answer in GSettings, and xdg-desktop-portal serves GSettings on
# the session bus -- `org.freedesktop.portal.Settings.ReadAll` with the
# namespace `org.gnome.desktop.input-sources`. Measured on GNOME 46.0 and
# 50.1, default installs, where `xdg-desktop-portal` and
# `xdg-desktop-portal-gnome` are both already there:
#
#   * `mru-sources` is the live truth: its head is the active source, written
#     on every switch by every means a user has -- the Super+Space shortcut,
#     the panel indicator's menu -- and it is what a login restores, so the
#     very first command of a fresh session can already be on the second
#     layout. It is `[]` only in a session that has never switched, and there
#     `sources[0]` is active.
#   * `current` is DEAD. `gsettings describe` ends "DEPRECATED: This key is
#     deprecated and ignored" on both generations, `dconf watch` across a
#     session of switching shows the shell writing only `mru-sources`, and
#     writing `current` switches nothing. Never read it.
#   * dconf itself is not readable over the bus at all: `ca.desrt.dconf`
#     publishes `Init`, `Change` and the `Notify` signal and no read method.
#     The portal is the only route, and it needs nothing installed and no new
#     dependency -- `w11common/dbus_mini.py` speaks it as it stands.
#
# Turning the source's index into a keymap group is one rule with one wrinkle.
# Mutter compiles the sources in order and ALWAYS appends its own `us` group
# after them, even when `us` is already a source (`us,de` -> `us, de, us`);
# that appended group is why a one-layout GNOME session looks exactly like a
# two-layout one from the keymap alone, and why the notice used to fire on
# every command of every non-US GNOME desktop. XKB allows four groups, so
# beyond three sources Mutter recompiles around whichever source is in use:
# with `de,fr,gr,ru,es` the keymap is `de, fr, gr, us` until Spanish is
# picked, and then it is `ru, es, us` with group 2 active. So the group is the
# source's index within its chunk of three, plus one -- `tests/fixtures/
# keymaps/five_es.xkb` is that keymap, captured.
#
# Two states are refused rather than answered, because there the setting
# describes no single live layout:
#
#   * `per-window` true (Settings > Keyboard > "Let each window use its own
#     layout"): measured with `mru-sources` saying German while a newly opened
#     window was on US.
#   * a `mru-sources` head that is no longer in `sources` -- the list was
#     edited under us.
#
# and so is a source that is not an `xkb` layout (an IBus engine), which is
# the one shape here that was never measured. Every refusal, and every failure
# of the read, returns None and leaves the caller with the guess and the
# notice it had before this existed.

GNOME_BUS_NAME = "org.gnome.Shell"
GNOME_SCHEMA = "org.gnome.desktop.input-sources"
PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Settings"
GNOME_TIMEOUT = 2.0        # the read is 1-3 ms; this only bounds a wedge
GNOME_RETRY_AFTER = 10.0   # monotonic seconds before re-dialling a bus that failed
GNOME_FORK_GRACE = 2.0     # extra seconds before a silent child is killed

# Mutter fits this many of the user's sources into one keymap (XKB's four
# groups, less the `us` it appends). Beyond that it compiles in chunks.
GNOME_SOURCES_PER_KEYMAP = 3

# The same rule as KWin's: an answer that describes the session rather than
# the moment. No portal on this bus at all, or one that will not answer us.
_GNOME_FATAL = ("ServiceUnknown", "UnknownObject", "UnknownMethod",
                "UnknownInterface", "AccessDenied", "NotSupported")


class GnomeInputSources:
    """GNOME's answer to "which of the configured layouts is active?", read
    through the portal over one connection kept for the life of the process.

    The same shape as `KwinLayouts`, and for the same reasons: dialled lazily
    from the one place the group would otherwise be a guess, so a plain US
    session and a session with one thing in its keymap never open a bus;
    `NameHasOwner` on GNOME Shell before anything else, so a KDE or sway box
    is one round trip away from a permanent no rather than a portal call on
    every keystroke; failure is silence, and a bus that refuses is not
    re-dialled for GNOME_RETRY_AFTER seconds.

    One thing is GNOME's own. Typing here always goes through `/dev/uinput`,
    so the daemon is the session user under the udev rule and **root** under
    `sudo` -- and the portal answers the session user and nobody else, because
    it identifies its caller by opening `/proc/<pid>/root`. So when we are not
    that user the ReadAll happens in a forked child that has dropped to them
    (`_read_all_as`); the bus this object keeps is still ours, and still does
    the name check, because that much root may ask.
    """

    def __init__(self, address=None, uid=None):
        # A caller that hands us an address is handing us one it can already
        # use as itself (the tests); otherwise both come from the session.
        self.address = address
        self.uid = os.geteuid() if (uid is None and address is not None) else uid
        self.bus = None
        self.absent = False      # no GNOME here; stop asking
        self.retry_at = 0.0      # monotonic deadline of the connect backoff
        self.asked = 0           # answers received, for the tests
        self._lock = threading.Lock()

    def group(self, text: str):
        """The active group (1-based) or None.

        `text` is the keymap the answer has to fit: an index past its last
        group describes a keymap we did not read -- a layout list edited a
        moment ago -- and the caller's own guess is the better one."""
        with self._lock:
            try:
                n = _group_of_sources(self._ask())
                return n if n is not None and 1 <= n <= group_count(text) else None
            except Exception:
                # Nothing about typing may depend on this working (B13).
                return None

    def close(self):
        with self._lock:
            self._drop()

    # -- the bus

    def _drop(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def _target(self):
        """(uid, bus address) of the graphical session, or None."""
        if self.address is not None:
            return self.uid, self.address
        from w11common import session
        hit = session.find_user_bus()
        if hit is None:
            return None
        uid, addr = hit
        self.uid, self.address = uid, addr
        return uid, addr

    def _connect(self):
        """The session bus, with GNOME Shell on it, or None."""
        if self.bus is not None:
            return self.bus
        hit = self._target()
        if hit is None:
            return None
        from w11common.dbus_mini import Bus
        try:
            bus = Bus(hit[1], timeout=GNOME_TIMEOUT)
        except Exception:
            return None          # no session bus (yet): the backoff catches it
        try:
            if not bus.name_has_owner(GNOME_BUS_NAME):
                bus.close()
                # Nobody owns it: this is not a GNOME session, which is
                # permanent and worth remembering. If the shell has answered
                # us before this is a restart, and the backoff covers it.
                self.absent = not self.asked
                return None
        except Exception:
            bus.close()          # ours to close, or the next command opens a second
            return None
        self.bus = bus
        return bus

    def _ask(self):
        """The `org.gnome.desktop.input-sources` namespace, or None. One
        reconnect, for the same reason KWin gets one: a shell restart takes
        our connection with it and a stale socket looks like a wedged one."""
        now = time.monotonic()
        if self.absent or now < self.retry_at:
            return None
        for attempt in (0, 1):
            bus = self._connect()
            if bus is None:
                self.retry_at = now + GNOME_RETRY_AFTER
                return None
            try:
                if self.uid is not None and os.geteuid() != self.uid:
                    out = _read_all_as(self.uid, self.address, GNOME_TIMEOUT)
                else:
                    out = _read_all(bus, GNOME_TIMEOUT)
            except Exception as e:
                self._drop()
                if any(getattr(e, "name", "").endswith(w) for w in _GNOME_FATAL):
                    self.absent = True
                    return None
                if attempt:
                    self.retry_at = now + GNOME_RETRY_AFTER
                    return None
                continue         # once more, on a new connection
            self.asked += 1
            return out
        return None


def _read_all(bus, timeout: float):
    """`org.freedesktop.portal.Settings.ReadAll` for our one namespace. The
    values arrive already unmarshalled: `sources` and `mru-sources` as lists
    of (type, id) pairs, `per-window` as a bool."""
    (all_,) = bus.call(PORTAL_BUS_NAME, PORTAL_PATH, PORTAL_IFACE, "ReadAll",
                       "as", ([GNOME_SCHEMA],), timeout=timeout)
    return all_.get(GNOME_SCHEMA)


def _read_all_as(uid: int, addr: str, timeout: float):
    """`_read_all` from a live process owned by `uid`, JSON back over a pipe.

    `Bus(as_uid=)` cannot serve here even though it exists: its child hands
    the socket back and exits, and the portal, which identifies its caller by
    opening `/proc/<pid>/root`, then has no process left to identify. So the
    connection AND the call both happen in a child that stays alive for them.
    The child also has to put PR_SET_DUMPABLE back: setuid() clears it, and a
    process that is not dumpable has a `/proc/self` only root may open --
    which is precisely what the portal is not."""
    import json
    from w11common.dbus_mini import Bus, DBusError, ERR
    r, w = os.pipe()
    with warnings.catch_warnings():
        # 3.12 warns about fork() in a threaded process; the child does socket
        # I/O and _exit()s, and never touches the interpreter's locks.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:                                      # child
        try:
            os.close(r)
            # Everything but `w`, before the drop. fork() hands this child the whole fd table of whatever was
            # running -- on the daemon that is the uinput devices, the Wayland socket, the listening control
            # socket and, when a command forked it, that command's session bus connection. Measured with these
            # two lines removed, in the test runner (`python3 tests/test_xkbmap.py
            # TestTheActiveGroupFromGnome.test_the_child_holds_its_pipe_and_nothing_else`): 12 entries in
            # /proc/self/fd, 9 of them above stdio, against the 2 it has with the fix. The child then
            # setuid()s to the session user and connects to a bus that identifies its caller by opening
            # /proc/<pid>: a process holding root's descriptors is exactly what should not be reachable from a
            # session-user process, and a dup of the listening socket would keep the daemon's socket alive
            # past its death. It needs its pipe and nothing else.
            os.closerange(3, w)
            os.closerange(w + 1, _fd_bound())
            from w11common.dbus_mini import _drop_privileges
            _drop_privileges(uid)
            _set_dumpable()
            with Bus(addr, timeout=timeout) as bus:
                out = {"ok": True, "settings": _read_all(bus, timeout)}
        except BaseException as e:
            out = {"ok": False, "name": getattr(e, "name", "") or (ERR + "Failed"),
                   "error": "%s: %s" % (type(e).__name__, e)}
        try:
            os.write(w, json.dumps(out).encode())
        except BaseException:
            pass
        os._exit(0)
    os.close(w)
    try:
        raw = _read_until_eof(r, pid, timeout + GNOME_FORK_GRACE)
    finally:
        os.close(r)
        _reap(pid)
    try:
        out = json.loads(raw) if raw else None
    except ValueError:
        out = None
    if not isinstance(out, dict):
        raise DBusError(ERR + "NoReply", "the reader for uid %d said nothing" % uid)
    if not out.get("ok"):
        raise DBusError(out.get("name") or (ERR + "Failed"), out.get("error") or "")
    return out.get("settings")


def _fd_bound() -> int:
    """One past the highest descriptor worth closing, the way daemon.py's
    _close_inherited_fds computes it -- and clamped for the same reason: a
    container can report SC_OPEN_MAX = 1073741816 and closerange() would walk
    every one of them."""
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = 4096
    if not isinstance(limit, int) or limit < 3 or limit > 65536:
        limit = 65536
    return limit


def _set_dumpable():
    """PR_SET_DUMPABLE = 1. Best effort: a libc without it leaves the portal
    unable to see us, which is a read that fails, which is the guess."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(4, 1, 0, 0, 0)
    except Exception:
        pass


def _read_until_eof(fd: int, pid: int, timeout: float) -> bytes:
    """Everything the child writes before it closes, or b"" if it outstays
    `timeout` -- it holds every fd we had, so it does not get to hang us."""
    import select
    deadline = time.monotonic() + timeout
    buf = b""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            _kill(pid)
            return b""
        try:
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
        except OSError:
            return buf
        if not chunk:
            return buf
        buf += chunk


def _kill(pid: int):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _reap(pid: int):
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _group_of_sources(d):
    """The active keymap group from the setting, or None where the setting
    describes no single one. Pure: this is the whole mapping, and the part
    most worth pinning in a test."""
    if not isinstance(d, dict):
        return None
    sources, mru = d.get("sources"), d.get("mru-sources")
    if not isinstance(sources, (list, tuple)) or not sources or mru is None:
        return None
    if d.get("per-window"):
        return None
    src = [tuple(s) for s in sources
           if isinstance(s, (list, tuple)) and len(s) == 2]
    if len(src) != len(sources) or any(t != "xkb" for t, _ in src):
        return None
    if len(src) == 1:
        i = 0            # one source: the keymap's second group is Mutter's
    elif not isinstance(mru, (list, tuple)) or not mru:
        i = 0            # nothing switched in this session: still the first
    else:
        try:
            i = src.index(tuple(mru[0]))
        except (ValueError, TypeError):
            return None  # a head that names a source the list no longer has
    return i % GNOME_SOURCES_PER_KEYMAP + 1


_gnome = None
_gnome_lock = threading.Lock()


def gnome_group(text: str):
    """The active group GNOME's setting names, or None where nothing answers.

    The module-level connection is the point, as it is for KWin: the daemon is
    long-lived and asks on every command whose group it would otherwise guess."""
    global _gnome
    with _gnome_lock:
        if _gnome is None:
            _gnome = GnomeInputSources()
        reader = _gnome
    return reader.group(text)


# ---------------------------------------------------------------------------
# The three desktops that answer over an IPC of their own
#
# KWin's reader above dials a bus and GNOME's dials a bus and forks; these
# three are cheaper than either, and none of them needs anything installed.
#
#   * Hyprland keeps XKB state PER DEVICE and publishes all of it on
#     `j/devices`: every keyboard row carries `layout`, `active_layout_index`
#     and `active_keymap` [M recon2/hyprland.md §2]. Which row to read is the
#     whole question -- see `_hypr_keyboard`.
#   * Wayfire answers `wayfire/get-keyboard-state` with
#     `{"possible-layouts": [...], "layout": ..., "layout-index": 0}`, the
#     same 0-based index into the configured list that KWin's getLayout gives
#     [M recon2/wayfire.md §2.7]. It is the reader that closes a measured
#     defect: with `xkb_layout = us,de` Wayfire sends no
#     `wl_keyboard.modifiers` before focus, so `choose_group` guessed group 1
#     -- harmless on the virtual-keyboard path (wdotool uploads its own
#     keymap and `zyx` typed correctly) and wrong on the uinput one, which is
#     the compositor's keymap.
#   * Cinnamon keeps the answer in `org.cinnamon.desktop.input-sources`, whose
#     keys are `sources`, `current`, `show-all-sources` and `xkb-options` and
#     which has NO `mru-sources` [M recon2/cinnamon.md §2.2, `gsettings
#     list-keys`]. So unlike GNOME the live index really is `current`, and the
#     route to it is `org.Cinnamon.Eval` reading Gio.Settings -- no portal, no
#     fork-and-drop (Eval identifies nobody), one read-only program.
#
# Every one of them is best effort in exactly the way the other two are: the
# answer has to fit the keymap that was just read or it is not an answer, a
# failure returns None and leaves the caller with its guess and its notice,
# and a desktop that is not here is remembered as absent so the process asks
# once and never again.

#: monotonic seconds before a reader that failed is asked again. The same ten
#: as KWIN_RETRY_AFTER: a compositor restarting takes about that long to be
#: back, and nothing here is worth retrying faster.
DESKTOP_RETRY_AFTER = 10.0


class _IpcLayouts:
    """What the three readers below share: ask, clamp against the keymap, never raise.

    `KwinLayouts`'s contract with the bus ceremony taken out. A subclass implements `_index()`, which
    returns the 0-based index of the active layout, or None; it sets `self.absent` itself when what it
    found was "there is no such desktop here", which is permanent, and raises for everything else, which
    is not. The lock is `KwinLayouts`'s: the daemon asks from inside its own lock, a client asks once and
    exits, and one connection or socket per process is the point of the module-level readers below.
    """

    def __init__(self):
        self.absent = False      # nothing to talk to here; stop asking
        self.retry_at = 0.0      # monotonic deadline of the failure backoff
        self.asked = 0           # answers received, for the tests
        self._lock = threading.Lock()

    def group(self, text: str):
        """The active group (1-based) or None.

        `text` is the keymap the index has to fit: an index past its last group is not an answer but a
        race with a layout list that was edited a moment ago, and the caller's own guess is the better
        one -- the same rule `KwinLayouts.group` states, for the same reason."""
        with self._lock:
            try:
                idx = self._ask()
                if idx is None:
                    return None
                n = int(idx) + 1
                return n if 1 <= n <= group_count(text) else None
            except Exception:
                # Nothing about typing may depend on this working (B13).
                return None

    def close(self):
        """Nothing is held open: the two socket readers connect per question (Hyprland's protocol is one
        connection per request anyway, and Wayfire's socket costs microseconds), and `CinnamonInputSources`
        overrides this to drop the bus it keeps."""

    # -- the backoff

    def _failed(self):
        self.retry_at = time.monotonic() + DESKTOP_RETRY_AFTER

    def _index(self):
        raise NotImplementedError

    def _ask(self):
        """The 0-based index the desktop reports, or None. Never raises."""
        if self.absent or time.monotonic() < self.retry_at:
            return None
        try:
            idx = self._index()
        except Exception:
            # A socket that went away, a compositor that answered nonsense, a bus that hung up: none of
            # them is permanent, and none of them may reach the caller.
            self._failed()
            return None
        if idx is None:
            return None
        self.asked += 1
        return idx


#: The keyboards Hyprland lists that are OURS, by the names it gives them: the
#: `zwp_virtual_keyboard_v1` device wdotool creates, and the one Hyprland names
#: after the creating client's comm (`hl-virtual-keyboard-python3.14` in the
#: recording). Their index is never the session's answer -- Hyprland keeps
#: layout state per device, so switching the physical keyboard to German left
#: the injected one on US and typing stayed correct, and switching the injected
#: one broke typing outright [M recon2/hyprland.md §3].
HYPR_INJECTED = ("wdotool-virtual-keyboard", "hl-virtual-keyboard")

#: The reader's own deadline on Hyprland's socket, and the same number
#: KWIN_TIMEOUT, GNOME_TIMEOUT and CINNAMON_TIMEOUT carry, for the same reason:
#: it bounds a wedge, not a round trip (`hyprctl -j devices` answers in
#: milliseconds). hypr_ipc's own IPC_TIMEOUT is 10.0, which is the right number
#: for a `dispatch` a user is waiting on and the wrong one inside `fetch()`,
#: where the daemon holds its lock and every `type` waits behind it.
HYPR_TIMEOUT = 2.0

#: A device name that means "a thing a user types on". `power-button` is a
#: keyboard to libinput and to `j/devices`, carries the session's `us,de` and
#: its own index 0, and never switches; the recorded session's real keyboard is
#: `at-translated-set-2-keyboard`, which is the row that was on index 1 after a
#: `switchxkblayout` [M recon2/hyprland.md §2, fixtures/hypr/devices.json].
_HYPR_TYPING_RE = re.compile(r"keyboard|keybd|kbd", re.I)


#: The kernel device the injecting half of wdotool writes through. A process that has it OPEN is the one
#: whose keys reach the compositor as a real input device -- the daemon, and nothing else in this tree
#: (`wdotool/daemon.py:625` opens it at startup and holds it for the daemon's life; `keys explain` and every
#: other client never does). That is the whole test the fourth rule needs, and it is a fact about THIS
#: process rather than about the session, which is what tells the two questions apart.
UINPUT_NODE = "/dev/uinput"

#: The override `wdotool/uinput.py:dev_path()` honours, and the reason this module reads the environment
#: rather than that constant alone: a daemon started with `WDOTOOL_UINPUT_PATH=/dev/uinput2` opens THAT
#: node, and a fourth rule comparing st_rdev against /dev/uinput would find no fd of its own and quietly
#: encode for the session's group instead of its device's -- the exact failure the rule exists to end.
#: The name is not imported from wdotool.uinput: that module opens the kernel device at import-adjacent
#: call sites and every reader here (`keys explain`, the passthrough probe) would carry it for one string.
UINPUT_PATH_ENV = "WDOTOOL_UINPUT_PATH"


def _uinput_node() -> str:
    """The node the injecting half of this process would have opened, override and all."""
    return os.environ.get(UINPUT_PATH_ENV) or UINPUT_NODE


def _holds_uinput(node: str = "") -> bool:
    """Does this process have the uinput node open?

    By device number off `/proc/self/fd`, not by the link's text: the path a fd was opened through is not
    the only name a device node has, and comparing `st_rdev` of a character device is the same test the
    kernel would make. Any failure is False -- a /proc that is not mounted, a node that is not there -- and
    False means the reader below answers about the session, which is what it did before the fourth rule."""
    try:
        want = os.stat(node or _uinput_node())
        if not stat.S_ISCHR(want.st_mode):
            return False
        names = os.listdir("/proc/self/fd")
    except OSError:
        return False
    for name in names:
        try:
            st = os.stat("/proc/self/fd/" + name)
        except OSError:
            continue          # the fd closed between listdir and stat: not ours to worry about
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == want.st_rdev:
            return True
    return False


def _hypr_injected(name) -> bool:
    """Is this one of the virtual keyboards wdotool itself put there?"""
    return any(str(name or "").startswith(p) for p in HYPR_INJECTED)


def _hypr_our_keyboard(devices):
    """The row for the keyboard WE injected (`HYPR_INJECTED`), or None.

    The uinput device is created as "wdotool virtual keyboard" (`wdotool/uinput.py:162`) and Hyprland lists
    it as `wdotool-virtual-keyboard` -- the name the live 0.53.3 session reported at `active_layout_index: 0`
    while the physical keyboard sat at 1 [M recon2/hyprland.md §3]."""
    kbs = devices.get("keyboards") if isinstance(devices, dict) else None
    if not isinstance(kbs, list):
        return None
    for k in kbs:
        if isinstance(k, dict) and _hypr_injected(k.get("name")):
            return k
    return None


def _hypr_index_of(dev) -> "int | None":
    """`active_layout_index` of one `j/devices` keyboard row, or None."""
    if not isinstance(dev, dict):
        return None
    idx = dev.get("active_layout_index")
    return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


def _hypr_keyboard(devices):
    """The keyboard row whose `active_layout_index` is the session's answer, or None.

    Three rules, in order, and the first two exist because of what the recorded `j/devices` holds: four
    keyboards, `main: true` on `wdotool-virtual-keyboard` and index 1 on the physical
    `at-translated-set-2-keyboard` [M recon2/hyprland.md §2].

      * never one of ours (`HYPR_INJECTED`): its index is the injected device's own, and reading it would
        be reading back the state we set;
      * `main: true` among what is left -- Hyprland's word for the keyboard it last took input from,
        which is not the same thing as "the real one": on the live resolute-hypr session of 2026-09-09
        `main` was on `wdotool-virtual-keyboard` before a `switchxkblayout` and on the physical
        `at-translated-set-2-keyboard` after it.  The rule above is what makes this one usable;
      * else a name that looks like a keyboard, and only then the first row, so that `power-button`
        (a keyboard to libinput, and one that has never switched a layout in its life) does not answer
        for the keyboard beside it.

    What comes back therefore describes the PHYSICAL keyboard. On the virtual-keyboard path that is
    exactly right and also moot (wdotool uploads its own keymap and never consults this).

    **On the /dev/uinput path it is right about the session and wrong about the device, and that is now
    measured.**  resolute-hypr, Hyprland 0.53.3, 2026-09-09, three keyboards and `kb_layout = us,de`:
    after `hyprctl switchxkblayout at-translated-set-2-keyboard 1`, `j/devices` reported the physical
    keyboard at `active_layout_index: 1` (`German`) and `wdotool-virtual-keyboard` still at `0`
    (`English (US)`).  `keys explain --chars z` correctly answered `group 2 of 2, from wayland + hyprland
    devices` -- and `wdotool type "zy@ Strasse"` into a `foot -e cat` wrote `zyq Strasse`: `@` was encoded
    as the German AltGr+Q and landed in the injected device's US group as a plain `q`.  Before this reader
    existed wdotool said it was guessing and typed CORRECTLY, because the guess (US) was what the injected
    device really was [recon2/hyprland.md §3].

    NOT YET, and the fourth rule is ours to write rather than a rung of AGENTS.md's ladder: the uinput
    encoder must ask for the group ITS OWN device is in, which is a different question from the one
    `keys explain` answers about the session, so this function stays right and the caller gains a second
    one.  Below that sits route 2 -- `hyprctl switchxkblayout wdotool-virtual-keyboard <n>` before the
    injection -- which moves the session's own state and would have to put it back.
    `vm/live-smoke.d/hypr.sh:layout_phase` carries it as the xwant that names this.
    """
    kbs = devices.get("keyboards") if isinstance(devices, dict) else None
    if not isinstance(kbs, list):
        return None
    real = [k for k in kbs if isinstance(k, dict) and not _hypr_injected(k.get("name"))]
    for k in real:
        if k.get("main") is True:
            return k
    for k in real:
        if _HYPR_TYPING_RE.search(str(k.get("name") or "")):
            return k
    return real[0] if real else None


class HyprLayouts(_IpcLayouts):
    """Hyprland's active layout, off `j/devices` on its own IPC socket.

    No bus and no protocol extension: the socket is `$XDG_RUNTIME_DIR/hypr/<sig>/.socket.sock` and the
    request is the one `hyprctl -j devices` sends. A session with no such socket is not Hyprland and never
    will be, which is the `absent` case; a socket that is there and does not answer is the backoff's.
    """

    def __init__(self, sockpath=None):
        self.sockpath = sockpath   # None: found the way the backend finds it
        #: how many times the fourth rule below moved our own device's group, for the tests
        self.switched = 0
        super().__init__()

    def _index(self):
        from w11common import session
        from wdotool.hypr_ipc import HyprIPC

        path = self.sockpath or session.find_hypr_socket()
        if not path:
            # No Hyprland here. If it has answered us before this is a restart mid-session, and then the
            # backoff is what covers it -- the same distinction KwinLayouts._connect draws about a bus
            # name, and it has to arm the backoff for the sentence to be true.
            self.absent = not self.asked
            self._failed()
            return None
        ipc = HyprIPC(path, timeout=HYPR_TIMEOUT)
        devices = ipc.json("devices")
        idx = _hypr_index_of(_hypr_keyboard(devices))
        if idx is None:
            return None
        return self._group_of_our_device(ipc, devices, idx)

    def _group_of_our_device(self, ipc, devices, idx: int) -> int:
        """THE FOURTH RULE: the group the keys we are about to inject will be READ in.

        Hyprland keeps XKB state per device, so the session's group and the injected device's group are two
        questions. `_hypr_keyboard` answers the first one, which is what `keys explain` and every other
        reader wants; the process that holds /dev/uinput open is about to send keycodes THROUGH a device of
        its own, and the only group that decides what they produce is that device's.

        Measured, resolute-hypr / Hyprland 0.53.3, 2026-09-09, `kb_layout = us,de`: after `hyprctl
        switchxkblayout at-translated-set-2-keyboard 1`, `j/devices` reported the physical keyboard at
        `active_layout_index: 1` (German) and `wdotool-virtual-keyboard` still at `0` (US) -- and
        `wdotool type` encoded for German while the device read US, so `zy@ Straße` came out `zyq Stra-e`:
        `@` went as the German AltGr+Q and landed in the US group as a plain `q`
        [M vm/live-smoke.d/hypr.sh layout_phase, and again on 0.56.2 in goal2/recon/gaps.md §1b #13].

        So the two are put back together, and the cheap way round: `switchxkblayout <our device> <n>` over
        the socket we are already holding (AGENTS.md route 2) moves OUR device only -- the session's own
        keyboard keeps the group the user put it in, and nobody else types on ours, so there is nothing to
        put back. What is typed after that is the session's layout, which is what a user who switched to
        German and ran `wdotool type` asked for; X is the oracle and XTEST types in the layout the server
        is in.

        When the switch does not take, the answer is our device's own index rather than the session's: a
        group that is really there beats one that is not, and it is what wdotool typed correctly with
        before this reader existed. Only the injector does any of this -- for every other process this
        returns `idx` untouched and nothing is sent."""
        if not _holds_uinput():
            return idx
        ours = _hypr_our_keyboard(devices)
        mine = _hypr_index_of(ours)
        if ours is None or mine is None or mine == idx:
            return idx
        try:
            # `switchxkblayout` is a request of its own and NOT a dispatcher: `dispatch switchxkblayout
            # wdotool-virtual-keyboard 1` answers `Invalid dispatcher` and changes nothing, while the bare
            # `switchxkblayout wdotool-virtual-keyboard 1` answers `ok` and the device's
            # `active_layout_index` is 1 in the very next `j/devices` -- both measured over a raw socket on
            # the arch-hypr golden, Hyprland 0.56.2, 2026-09-11, after the dispatcher form shipped here and
            # typed `yz@ Strae` (the ASCII right, the sharp s dropped, because the fallback below was what
            # answered).
            reply = ipc.request("switchxkblayout %s %d"
                                % (str(ours.get("name") or ""), idx)).decode("utf-8", "replace").strip()
            now = mine if reply != "ok" else _hypr_index_of(_hypr_our_keyboard(ipc.json("devices")))
        except Exception:
            # A request Hyprland refuses, a socket that went in the middle: the group our device is in is
            # still the honest answer, and none of this may reach the caller as an exception.
            now = mine
        if now == idx:
            self.switched += 1
            return idx
        return now if now is not None else mine


#: The one method this reader sends. Its sibling `wayfire/set-keyboard-state` is
#: NEVER sent: it recompiles the keymap as the selected layout duplicated --
#: after one call `possible-layouts` read `["English (US)", "English (US)"]` and
#: the German layout was gone until restart [M recon2/wayfire.md §2.7]. That is
#: also the shape this reader has to survive reading, and does: the index still
#: names a group of the keymap the compositor just handed us.
WAYFIRE_STATE_METHOD = "wayfire/get-keyboard-state"


WAYFIRE_TIMEOUT = 2.0      # the get-keyboard-state round trip is sub-millisecond; this only bounds a wedge


class WayfireLayouts(_IpcLayouts):
    """Wayfire's active layout, off `wayfire/get-keyboard-state` on its JSON IPC socket.

    The method needs `plugins = ipc ipc-rules` and not the window backend's whole set (measured on this
    box: that configuration answers 23 methods and this is one of them); a Wayfire with no `ipc` plugin at
    all has no socket, which is the `absent` case. A Wayfire that has the socket and not the method says
    `No such method found!`, which is a fact about a config line rather than about this moment, so it is
    remembered too.

    The deadline is WAYFIRE_TIMEOUT and not backend_wayfire.IPC_TIMEOUT (10.0 s): every reader in this
    module bounds a wedged desktop at 2.0 s, and these run from `fetch()` inside the daemon's lock, where the
    difference is eight seconds added to every `type`. `_WayfireIPC` took the knob on 2026-09-09
    (requests-batch-6.md, batch 10 -> batch 6), and it bounds the connect too: a compositor wedged inside its
    event loop still has a listening socket.
    """

    def __init__(self, sockpath=None):
        self.sockpath = sockpath
        super().__init__()

    def _index(self):
        from w11common import session
        from w11common.errors import CmdError
        from wdotool.backend_wayfire import _WayfireIPC

        path = self.sockpath or session.find_wayfire_socket()
        if not path:
            self.absent = not self.asked
            self._failed()       # a Wayfire that has answered before and is gone: back off, as KWin does
            return None
        ipc = _WayfireIPC(path, timeout=WAYFIRE_TIMEOUT)
        try:
            state = ipc.call(WAYFIRE_STATE_METHOD)
        except CmdError as e:
            # `.no_method` is what `_error_line` sets for exactly this reply, and what the backend's own
            # api gate reads; matching the sentence in the text would also match a handler error quoting it.
            if getattr(e, "no_method", False):
                self.absent = True
                return None
            raise
        finally:
            ipc.close()
        idx = state.get("layout-index") if isinstance(state, dict) else None
        return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


CINNAMON_BUS_NAME = "org.Cinnamon"
CINNAMON_PATH = "/org/Cinnamon"
CINNAMON_IFACE = "org.Cinnamon"
#: The schema the program below reads. Named here for the reader of this file
#: and for the tests; the program itself spells it out, because a constant
#: formatted into an `Eval` string is the shape this module may not have.
CINNAMON_SCHEMA = "org.cinnamon.desktop.input-sources"
CINNAMON_TIMEOUT = 2.0     # the Eval round trip is ~9 ms with a gdbus spawn in it [M cinnamon.md §2.2]

#: The whole program, and it is read-only: `current` (the live index, since this
#: schema has no `mru-sources`) and `sources` unpacked from its GVariant. There
#: is no interpolation and there never may be -- `org.Cinnamon.Eval` runs
#: whatever arrives, and a window title reaching a script would be arbitrary
#: code execution in the user's session (the rule wdotool/cinnamon_js.py states
#: at length). Nothing here writes: no `set_`, no `set_uint`, no `set_value`.
CINNAMON_SCRIPT = (
    "JSON.stringify((s => [s.get_uint('current'), s.get_value('sources').deep_unpack()])"
    "(new imports.gi.Gio.Settings({schema_id: 'org.cinnamon.desktop.input-sources'})))"
)

#: The same list KWin's reader keys on, and for the same reason: an error name
#: that describes the session rather than the moment.
_CINNAMON_FATAL = _KWIN_FATAL


def _cinnamon_index(current, sources):
    """The 0-based index of the active input source, or None where the setting names no keymap group.

    Two refusals, both because the setting then describes no single live layout: an index that is not in
    `sources` (the list was edited under us -- the same race GnomeInputSources refuses on an `mru-sources`
    head), and a source that is not an `xkb` one, since an IBus engine is not a group of the keymap we
    just read. Cinnamon's own list is `[('xkb', 'us'), ...]`, `current` a 0-based index into it
    [M recon2/cinnamon.md §2.2]."""
    if not isinstance(current, int) or isinstance(current, bool):
        return None
    if not isinstance(sources, (list, tuple)) or not 0 <= current < len(sources):
        return None
    src = sources[current]
    if not (isinstance(src, (list, tuple)) and len(src) == 2 and src[0] == "xkb"):
        return None
    return current


class CinnamonInputSources(_IpcLayouts):
    """Cinnamon's active layout, read out of GSettings through `org.Cinnamon.Eval`.

    Shaped like `KwinLayouts`: the bus is dialled lazily from the one place the group would otherwise be a
    guess, `NameHasOwner` on `org.Cinnamon` comes before anything else so that a GNOME or KDE box is one
    round trip away from a permanent no (and so that nothing is ever *started* by the ask), failure is
    silence, and a bus that refuses is not re-dialled for DESKTOP_RETRY_AFTER seconds.

    Unlike GNOME's reader there is no fork-and-drop-privileges dance: Eval identifies nobody, so a root
    daemon gets the same answer the session user does [M recon2/cinnamon.md §2.2].
    """

    def __init__(self, address=None):
        self.address = address   # None: the graphical session's bus
        self.bus = None
        super().__init__()

    def close(self):
        with self._lock:
            self._drop()

    def _drop(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

    def _connect(self):
        """The session bus, with Cinnamon on it, or None."""
        if self.bus is not None:
            return self.bus
        from w11common.dbus_mini import Bus
        try:
            bus = Bus(self.address, timeout=CINNAMON_TIMEOUT)
        except Exception:
            return None          # no session bus (yet): the backoff catches it
        try:
            if not bus.name_has_owner(CINNAMON_BUS_NAME):
                bus.close()
                self.absent = not self.asked
                return None
        except Exception:
            bus.close()          # ours to close, or the next command opens a second one on top of it
            return None
        self.bus = bus
        return bus

    def _index(self):
        from w11common.dbus_mini import DBusError

        bus = self._connect()
        if bus is None:
            self._failed()
            return None
        try:
            out = bus.call(CINNAMON_BUS_NAME, CINNAMON_PATH, CINNAMON_IFACE, "Eval", "s",
                           (CINNAMON_SCRIPT,), timeout=CINNAMON_TIMEOUT)
        except DBusError as e:
            self._drop()
            if any(getattr(e, "name", "").endswith(w) for w in _CINNAMON_FATAL):
                self.absent = True
                return None
            raise
        except Exception:
            self._drop()         # a socket that went away: the connection is not reusable
            raise
        if len(out) != 2 or not out[0]:
            return None          # `(false, text)`: the program threw, and the text is Cinnamon's own
        # Eval is `JSON.stringify(eval(code))` around a program that already answers a JSON string, so
        # what comes back is encoded twice -- backend_cinnamon._json decodes the same two layers.
        cur, sources = json.loads(json.loads(out[1]))
        return _cinnamon_index(cur, sources)


_hypr = None
_wayfire = None
_cinnamon = None
_ipc_readers_lock = threading.Lock()


def hypr_group(text: str):
    """The active group Hyprland's `j/devices` names, or None where nothing answers."""
    global _hypr
    with _ipc_readers_lock:
        if _hypr is None:
            _hypr = HyprLayouts()
        reader = _hypr
    return reader.group(text)


def wayfire_group(text: str):
    """The active group Wayfire's keyboard state names, or None where nothing answers."""
    global _wayfire
    with _ipc_readers_lock:
        if _wayfire is None:
            _wayfire = WayfireLayouts()
        reader = _wayfire
    return reader.group(text)


def cinnamon_group(text: str):
    """The active group Cinnamon's input-sources setting names, or None where nothing answers."""
    global _cinnamon
    with _ipc_readers_lock:
        if _cinnamon is None:
            _cinnamon = CinnamonInputSources()
        reader = _cinnamon
    return reader.group(text)


# ---------------------------------------------------------------------------
# the desktops, in turn


#: The `who` strings from `desktop_group` whose group is a GUESS, not a verified live read: the reader
#: names a group the compositor is not proven to be decoding under. Cinnamon is here because muffin 6.4 has
#: no group getter [M requests-batch-18.md 2]; the other four lock the group they report.
GROUP_ASSUMED_DESKTOPS = frozenset({"cinnamon input-sources"})


def desktop_group(text: str):
    """(group, who said so) -- the active group read from the desktop, or
    None where no desktop answers.

    Five readers, each its own gate: the first command of a GNOME session asks
    Hyprland and Wayfire for a socket that is not there, asks KWin for a bus
    name nobody owns, gets its answer from the portal, and never asks any of
    the other four again -- every reader remembers a permanent no. So the loop
    costs one probe per reader per process, once, and after that only the
    desktop that answers is spoken to.

    Sockets before buses, which is the cheap end first: `find_hypr_socket()`
    and `find_wayfire_socket()` are a scandir of $XDG_RUNTIME_DIR where a bus
    reader pays a connect, an EXTERNAL auth and a GetNameOwner. The three bus
    readers keep detection's own order (KWin, GNOME Shell, Cinnamon: see
    backend_detect's docstring for why none of the three can shadow another).
    COSMIC is not here at all -- its group arrives on the wire in
    `_fetch_wayland`, the way sway's does."""
    for ask, who in ((hypr_group, "hyprland devices"),
                     (wayfire_group, "wayfire"),
                     (kwin_group, "kwin"),
                     (gnome_group, "gnome input-sources"),
                     (cinnamon_group, "cinnamon input-sources")):
        told = ask(text)
        if told is not None:
            return told, who
    return None


# ---------------------------------------------------------------------------
# the US bypass
#
# Deliberately standalone: its own regexes, its own scan, no call into the
# parser or the reverse map above. When the active layout is plain US this
# function is the *only* new code that runs, so a bug in the machinery above
# cannot reach the most common setup.

_US_CODE_RE = re.compile(r"<([^<>\s]+)>\s*=\s*(\d+)\s*;")
_US_BARE_RE = re.compile(r"^\s*\[([^\]]*)\]\s*$")
_US_TYPE_RE = re.compile(r'\btype\s*(?:\[\s*(?:Group)?(\d+)\s*\])?\s*=\s*"([^"]*)"')

# The names xkeyboard-config gives the plain `us` layout, and nothing else. `USA` is what the same layout is
# called when the session picks a Macintosh keyboard model; the key-by-key check below is what actually decides.
US_GROUP_NAME = "English (US)"
US_GROUP_NAMES = (US_GROUP_NAME, "USA")

# Types whose level 2 is reached with Shift and level 1 with no modifier -- the only shapes under which the
# fixed table's (keycode, shifted) pair is right. An unknown type on a key we care about fails the check.
_US_OK_TYPES = frozenset({
    "ONE_LEVEL", "TWO_LEVEL", "ALPHABETIC", "KEYPAD", "FOUR_LEVEL",
    "FOUR_LEVEL_ALPHABETIC", "FOUR_LEVEL_SEMIALPHABETIC",
    "FOUR_LEVEL_MIXED_KEYPAD", "FOUR_LEVEL_X", "FOUR_LEVEL_PLUS_LOCK",
    "FOUR_LEVEL_KEYPAD", "EIGHT_LEVEL", "EIGHT_LEVEL_ALPHABETIC",
    "EIGHT_LEVEL_SEMIALPHABETIC", "EIGHT_LEVEL_LEVEL_FIVE_LOCK",
    "EIGHT_LEVEL_ALPHABETIC_LEVEL_FIVE_LOCK",
    "EIGHT_LEVEL_SEMIALPHABETIC_LEVEL_FIVE_LOCK", "CTRL+ALT",
    "SEPARATE_CAPS_AND_SHIFT_ALPHABETIC", "PC_SYSRQ", "PC_BREAK",
})


def _expected_us() -> dict:
    """{evdev keycode: {level: keysym}} that the fixed US table assumes.

    Only the *printable* characters: Return, Tab, BackSpace, Escape and Delete are position keys, in the same
    place on every layout, and they go through `KEYSYM_KEYS` rather than through any layout table. Demanding
    them here made `us` + `caps:swapescape` -- a plain US session by any honest reading -- fail the check and
    drag the whole reverse map in (B2).
    """
    from wdotool import keymap as _keymap

    out: dict = {}
    for ch, (code, shifted) in _keymap.CHAR_TO_KEY.items():
        if len(ch) != 1 or ch < " " or ch == "\x7f":
            continue
        out.setdefault(code, {})[2 if shifted else 1] = ord(ch)
    return out


def active_group_is_plain_us(text: str, group: int = 1) -> bool:
    """Is the fixed US table exactly right for this keymap's active group?

    Not "is it called us": the name is only the fast reject. Every keycode the fixed table would ever emit is
    checked against the keysyms the keymap binds to it at level 1 and 2 in the active group, and every one of
    them has to be present and identical. A layout that passes types the same characters as the fixed table by
    construction, so the bypass cannot type the wrong thing; anything unexpected -- a missing key, an extra dead
    key on level 1, a type whose level 2 is not Shift, a keymap this scanner cannot read -- answers False and
    the reverse map takes over.
    """
    try:
        return _plain_us(text, group)
    except Exception:
        return False


def _plain_us(text: str, group: int) -> bool:
    text = strip_comments(text)
    if not re.search(r'\bname\s*\[\s*(?:Group)?%d\s*\]\s*=\s*"(?:%s)"'
                     % (group, "|".join(re.escape(n) for n in US_GROUP_NAMES)),
                     text):
        return False
    codes = {}
    for m in _US_CODE_RE.finditer(text):
        x = int(m.group(2))
        if 8 < x < 264:
            codes[m.group(1)] = x - 8
    want = _expected_us()
    seen = set()
    for m in _US_KEY_RE.finditer(text):
        code = codes.get(m.group(1))
        if code is None or code not in want:
            continue
        body = m.group(2)
        # A stated type only matters where the fixed table uses level 2: it is the "level 2 is Shift" assumption
        # that is being checked. On a key the table only ever presses unshifted (<SPCE>, which
        # `grp:win_space_toggle` gives the type PC_SUPER_LEVEL2) the type is none of our business (B2).
        if 2 in want[code]:
            for tm in _US_TYPE_RE.finditer(body):
                if tm.group(1) in (None, str(group)):
                    if tm.group(2) not in _US_OK_TYPES:
                        return False
        # XKB group wrapping: a key that binds fewer groups than the keymap has repeats its own. <SPCE> binds
        # group 1 only and is still the space bar in group 2.
        per_group = {int(sm.group(1)): sm.group(2) for sm in _US_SYMS_RE.finditer(body)}
        if per_group:
            levels = per_group.get((group - 1) % max(per_group) + 1)
        else:
            bare = _US_BARE_RE.match(body.strip().rstrip(";").strip())
            levels = bare.group(1) if bare else None
        if levels is None:
            return False
        syms = [_us_keysym(t) for t in levels.split(",")]
        for level, ks in want[code].items():
            if len(syms) < level or syms[level - 1] != ks:
                return False
        seen.add(code)
    return seen == set(want)


def _us_keysym(tok: str):
    """Keysym number of one token, for the bypass check only (its own
    resolver: the check must not depend on the parser above)."""
    tok = tok.strip()
    if tok[:2] in ("0x", "0X"):
        try:
            return int(tok, 16)
        except ValueError:
            return None
    return NAME_TO_KEYSYM.get(tok)


# ---------------------------------------------------------------------------
# `wdotool __keymap`: the hidden diagnostic subcommand
#
# Not in the command registry and not in `help`: xdotool has no such command
# and wdotool's output is byte-compatible with its help. It exists because
# every question about this module ("what did the compositor actually send
# us", "which group is it using", "why is that character skipped") is one
# `wdotool __keymap` away, and because the test fixtures were captured with it.

_KEYMAP_USAGE = """Usage: wdotool __keymap [--info] [--chars STRING] [--group N] [--keymap PATH]
Diagnostic: dump the keymap the compositor hands its clients.

--info          summarise instead of dumping: source, groups, active group,
                whether the US bypass would take it, the level-shift keys
--chars STRING  show the keystrokes each character of STRING would need
--group N       reverse group N (1-based) instead of the active one
--keymap PATH   read the keymap from a file instead of the compositor
"""


def diagnostic_main(argv) -> int:
    import sys

    want_info = want_chars = keymap = None
    group = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            sys.stdout.write(_KEYMAP_USAGE)
            return 0
        elif a == "--info":
            want_info = True
        elif a == "--chars" and args:
            want_chars = args.pop(0)
        elif a == "--group" and args:
            group = args.pop(0)
            if not group.isdigit():
                sys.stderr.write("wdotool __keymap: --group wants a number\n")
                return 1
        elif a == "--keymap" and args:
            keymap = args.pop(0)
        else:
            sys.stderr.write(f"wdotool __keymap: unknown option '{a}'\n")
            sys.stderr.write(_KEYMAP_USAGE)
            return 1
    try:
        snap = fetch(keymap=keymap, group=group)
    except XkbError as e:
        sys.stderr.write(f"wdotool: {e}\n")
        return 2
    if not (want_info or want_chars):
        sys.stdout.write(snap.text)
        return 0
    try:
        km = parse(snap.text)
    except XkbError as e:
        sys.stderr.write(f"wdotool: {e}\n")
        return 1
    # The same decision the typing path makes, so the diagnostic cannot
    # describe a layout wdotool would not have used (B13).
    mode = layout_mode()
    bypass = decide(snap.text, snap.group, mode)
    rmap = None

    def reversed_map():
        """reverse() at most once, however many of --info and --chars ask."""
        nonlocal rmap
        if rmap is None:
            rmap = reverse(km, snap.group)
        return rmap

    if want_info:
        print(f"source:        {snap.source}")
        print(f"keymap:        {len(snap.text)} bytes, {len(km.keycodes)} keycodes, "
              f"{len(km.types)} types")
        for g in km.groups:
            mark = " <- active" + ("" if snap.group_known else " (assumed)")
            print(f"group {g.index}:      {g.name!r}" + (mark if g.index == snap.group else ""))
        if mode == "us":
            print("us bypass:     yes -- WDOTOOL_LAYOUT=us asks for it")
        elif bypass:
            print("us bypass:     yes -- the fixed US table is used")
        elif mode == "xkb" and active_group_is_plain_us(snap.text, snap.group):
            print("us bypass:     no -- WDOTOOL_LAYOUT=xkb overrides it")
        else:
            print("us bypass:     no")
        if not bypass:
            try:
                rmap = reversed_map()
            except XkbError as e:
                # --group 3 on a two-group keymap, a keymap with nothing typable in it: the diagnostic reports
                # what it found, like every other failure here, and never tracebacks.
                sys.stderr.write("wdotool: %s\n" % e)
                return 1
            names = {MOD_SHIFT: "shift", MOD_LEVEL3: "level3", MOD_LEVEL5: "level5"}
            mods = ", ".join(f"{names[b]}=key {rmap.mod_keys[b]}" for b in MOD_BITS if rmap.mod_keys.get(b))
            print(f"level shifts:  {mods}")
            print(f"reachable:     {len(rmap.chars)} characters, "
                  f"{len(rmap.dead)} dead keys")
    if want_chars:
        if bypass:
            # Answer from the table wdotool would actually use, or the
            # diagnostic contradicts the "us bypass: yes" line above it.
            from wdotool import keymap as _keymap

            print("(the US bypass is in effect: these are the built-in US "
                  "table's keystrokes, which is what wdotool sends)")

            def lookup(ch):
                hit = _keymap.char_to_key(ch)
                return None if hit is None else [(hit[0], MOD_SHIFT if hit[1] else 0)]
        else:
            try:
                lookup = reversed_map().lookup_char
            except XkbError as e:
                sys.stderr.write("wdotool: %s\n" % e)
                return 1
        for ch in want_chars:
            seq = lookup(ch)
            if seq is None:
                print(f"{ch!r}: unreachable")
            else:
                print(f"{ch!r}: " + " then ".join(
                    "key %d%s" % (c, "+" + "+".join(
                        n for b, n in ((MOD_SHIFT, "shift"), (MOD_LEVEL3, "level3"),
                                       (MOD_LEVEL5, "level5")) if m & b) if m else "")
                    for c, m in seq))
    return 0
