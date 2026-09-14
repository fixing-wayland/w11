"""Input-injection daemon + client.

The daemon owns the uinput devices (device creation costs ~600ms of compositor hotplug latency — paid once),
tracks the injected pointer position, and serves one JSON object per line on a unix socket. `{"ok":true,...}` or
`{"ok":false,"error":"..."}`; a response may carry `"warnings":[...]` which the client prints to its stderr.

The daemon owns the pointer *model* (px, py): the position it last injected. It is only a model -- REL events, a
physical mouse, or another daemon move the compositor's pointer behind its back -- so clients that can ask the
compositor for the real position (the GNOME bridge's GetPointer) push it back with the `seed_pointer` op before
a relative move (see B1/B6 in docs/WDOTOOL.md). Neither injection path can ask: `zwlr_virtual_pointer_v1` has no
events and sway's IPC carries no cursor position, so a daemon that has moved nothing refuses the `pointer` op
instead of inventing an answer.

There are two of everything below. Keys go to /dev/uinput or to zwp_virtual_keyboard_v1 (`vkbd.py`), the pointer
to /dev/uinput or to zwlr_virtual_pointer_v1 (`vptr.py`), by one policy stated once under "which devices
inject".
"""

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback

from w11common.errors import CmdError
from hacks.input import keymap, keystate, layoutbox, uinput, vkbd, vptr, xkbmap

# Per-euid log path: /tmp is shared, and a root-owned log must not break (or
# leak into) another user's daemon spawn.
LOG_PATH = "/tmp/wdotool-daemon.log" if os.geteuid() == 0 else f"/tmp/wdotool-daemon-{os.geteuid()}.log"
FALLBACK_GEOMETRY = (0, 0, 1920, 1080)  # (min_x, min_y, width, height)

# Per-request sanity bounds: one malicious/buggy request must not hold the
# global injection lock for hours or overflow C int fields downstream.
MAX_REPEAT = 1_000_000
MAX_DELAY_MS = 300_000
_I32_MIN, _I32_MAX = -(2**31), 2**31 - 1
_MAX_REQUEST = 16 << 20  # bytes per request line

# Env the daemon keeps when it is spawned from a client (B10). Everything else -- the launcher's D-Bus address,
# DESKTOP_*, anything derived from the client's argv -- is dropped: an input daemon that outlives the command
# that started it must not pin that command's session state. WDOTOOL_* is kept as a prefix (uinput path,
# fake-uinput mode, WDOTOOL_REL_MODE).
#
# SWAYSOCK/I3SOCK are session state the daemon needs for itself: _rel_absolute() asks session.find_sway_socket()
# whether this is sway/i3, and answers "warp" for everything else (B1). Without them that question falls back to
# scanning the runtime dir, which finds sway's socket only because sway usually puts it there and never finds
# i3's, which lives under /tmp -- so a daemon spawned from a client warped where it had to send EV_REL.
_KEEP_ENV = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HOME", "PATH", "USER",
             "LOGNAME", "LANG", "LC_ALL", "SUDO_UID", "PKEXEC_UID",
             "SWAYSOCK", "I3SOCK")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _num(val, what: str, lo: int, hi: int) -> int:
    """Validate one numeric request field: JSON numbers only (bool excluded),
    truncated to int, bounds-checked. Raises RuntimeError -> {"ok":false}."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a number)")
    v = int(val)
    if not lo <= v <= hi:
        raise RuntimeError(f"{what} {v} out of range [{lo}, {hi}]")
    return v


def _text(val, what: str) -> str:
    if not isinstance(val, str):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a string)")
    return val


_LAYOUT_MODES = ("us", "fixed", "auto", "xkb")


def _mode_field(val, what: str, valid):
    """Validate one optional mode field. The CLI already screens these, but the socket is a trust boundary and
    the daemon lower-cases whatever it is given: an unknown name must be a rejected request, not a silent
    default."""
    if val is None:
        return None
    if not isinstance(val, str):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a string)")
    mode = val.strip().lower()
    if mode not in valid:
        raise RuntimeError(f"invalid {what}: {val!r} (valid: " + ", ".join(valid) + ")")
    return mode


def _layout_mode(val, what: str = "layout_mode"):
    return _mode_field(val, what, _LAYOUT_MODES)


def _xkb_group(val):
    """`xkb_group` on the wire: the client's own WDOTOOL_XKB_GROUP, carried per request because the daemon
    cannot see the client's environment and outlives it.

    Lenient in exactly the way the variable is (`xkbmap._pinned_group`): anything that is not a plain 1..4 is
    not a pin and the group gets inferred as usual, because a typo in a shell profile must not stop the tool
    typing. Junk cannot get further than this: the value only ever reaches `xkbmap.fetch(group=)`."""
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (str, int)):
        return None
    raw = str(val).strip()
    if not raw.isdigit():
        return None
    n = int(raw)
    return n if 1 <= n <= 4 else None


def _vkbd_mode(val, what: str = "vkbd_mode"):
    """`vkbd_mode` on the wire (VKBD_MODES, defined with the policy below). WDOTOOL_VKBD is read separately and
    stays lenient: a typo in a shell profile must not stop the tool typing, but a request is a request."""
    return _mode_field(val, what, VKBD_MODES)


def _mods(val, what: str) -> list:
    """Validate a request's modifier-keycode list: nothing but the eight
    modifier keycodes may ever be pressed on a client's say-so."""
    if val is None:
        return []
    if not isinstance(val, list):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a list)")
    out = []
    for v in val:
        if isinstance(v, bool) or not isinstance(v, int):
            raise RuntimeError(f"invalid {what} entry: {v!r} (expected a keycode)")
        if v not in keymap.MODIFIER_KEYCODES:
            raise RuntimeError(f"{what}: {v} is not a modifier keycode")
        out.append(v)
    return out


def socket_path() -> str:
    """Where the daemon listens and the client dials. session.runtime_dir() supplies the private directory (and
    the `.lock` beside the socket is out of reach with it); $XDG_RUNTIME_DIR is taken exactly as the environment
    gives it, unchecked, so a set-but-missing one stays the bind failure the user can see rather than a socket
    quietly moved somewhere else."""
    if os.geteuid() == 0:
        return "/run/wdotool.sock"
    from w11common import session

    rd = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(rd or session.runtime_dir(), "wdotool.sock")


# -- how long a daemon lives --------------------------------------------------
#
# The daemon deliberately outlives the command that spawned it: it pays for its uinput devices once (~600ms of
# compositor hotplug settle), and it keeps the compositor connections, the geometry and the keymap it has
# worked out, so the second command of a script costs a socket round trip instead of a second of setup. That
# is what it is for, and none of this is a timer on *using* wdotool. Two things end a daemon anyway, both
# checked once per _CHECK_SECONDS from the accept loop, both skipped while a client is connected:
#
# * **it can no longer be reached.** The socket is a name in $XDG_RUNTIME_DIR, and logging out clears that
#   directory; a daemon whose socket file is gone can never be dialled again, by anybody, and it neither
#   notices nor exits. Measured on the test rig: 161 of them alive at once, ~3GB between them, sockets in
#   per-test runtime directories that had been deleted hours before, the oldest 18 hours old. The check
#   compares the socket file's *inode*, not its name, so it also catches the other way to become unreachable:
#   a second daemon having replaced the socket at the same path.
# * **nobody has used it for _IDLE_SECONDS.** Fifteen minutes, chosen from what the daemon is for: the gap it
#   has to sit through is the gap between two commands of one piece of work -- a script, a keybinding pressed
#   twice, a person moving a window by hand -- and those are seconds apart, not minutes. A quarter of an hour
#   is an order of magnitude past the longest of them, and the cost of being wrong is one respawn (~0.6s, paid
#   once, by the command that comes late). Left running instead, that daemon costs ~19MB, two compositor
#   connections and three devices on the seat, for a session that has stopped using the tool.
#
# Both are overridable, and setting WDOTOOL_DAEMON_CHECK=0 restores the bare accept loop exactly.
IDLE_ENV = "WDOTOOL_DAEMON_IDLE"      # seconds with no client before exiting; 0 = never
CHECK_ENV = "WDOTOOL_DAEMON_CHECK"    # seconds between checks; 0 = no check runs at all
_IDLE_SECONDS = 900.0
_CHECK_SECONDS = 15.0


def _lifetime_seconds(name: str, default: float) -> float:
    """One lifetime knob out of the environment: a number of seconds, 0 for "never". Lenient like WDOTOOL_VKBD
    -- a typo in a shell profile must not hand the daemon a lifetime nobody asked for -- so anything that is
    not a non-negative number is the default."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


def _tick_seconds(idle: float, check: float) -> float:
    """How long accept() waits before it looks around: `check`, but never coarser than the idle period itself
    -- asking for five seconds and waiting fifteen would make the period mean nothing. 0 (no tick, no checks)
    stays 0, and the shipped pair is untouched: 15s against 900s."""
    return min(check, idle) if check and idle else check


def _socket_ident(path: str):
    """(st_dev, st_ino) of the socket file at `path`, or None when nothing is there.

    The identity of the *directory entry*, which is the thing a client dials -- not of the listening socket,
    whose own inode lives in sockfs and stays what it is however the name is unlinked or replaced. Only the
    entry can answer "is the path I bound still my socket?"."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _bbox_of(boxes) -> tuple[int, int, int, int]:
    """(min_x, min_y, width, height) of a list of (x, y, w, h) output boxes.
    Multi-output layouts can have non-zero or negative origins."""
    minx = min(x for x, _y, _w, _h in boxes)
    miny = min(y for _x, y, _w, _h in boxes)
    maxx = max(x + w for x, _y, w, _h in boxes)
    maxy = max(y + h for _x, y, _w, h in boxes)
    return (minx, miny, maxx - minx, maxy - miny)


def _wayland_bbox(detail: bool = False):
    """Bounding box (min_x, min_y, w, h) of all outputs, queried over the
    Wayland wire. Prefers zxdg_output_manager_v1 logical size/position.

    With detail=True, returns (box, outs) -- the per-head wire state as well, which
    is what says whether the box is in an ambiguous pixel space (wdotool/layoutbox.py)."""
    from w11common import session
    from w11common.wayland_mini import WlConn

    hit = session.find_wayland_socket()
    if hit is None:
        raise RuntimeError("no wayland socket found")
    conn = WlConn(hit[2])
    # A wedged compositor must fall back to the warned default, not hang the
    # daemon forever while it holds the global lock.
    conn.sock.settimeout(3.0)
    try:
        outs = []
        for name, (iface, ver) in sorted(conn.get_registry().items()):
            if iface != "wl_output":
                continue
            o = {"x": 0, "y": 0, "w": 0, "h": 0, "scale": 1, "transform": 0,
                 "lx": None, "ly": None, "lw": None, "lh": None}
            o["oid"] = conn.bind(name, "wl_output", min(ver, 2))

            def handler(op, cur, fds, o=o):
                if op == 0:  # geometry(x, y, phys_w, phys_h, subpixel, make, model, transform)
                    o["x"], o["y"] = cur.i32(), cur.i32()
                    cur.i32(), cur.i32(), cur.i32()
                    cur.string(), cur.string()
                    o["transform"] = cur.i32()
                elif op == 1:  # mode(flags, width, height, refresh)
                    flags, w, h = cur.u32(), cur.i32(), cur.i32()
                    if flags & 1:  # current
                        o["w"], o["h"] = w, h
                elif op == 3:  # scale(factor)
                    o["scale"] = cur.i32()

            conn.on(o["oid"], handler)
            outs.append(o)
        conn.roundtrip()

        mgr = conn.find_global("zxdg_output_manager_v1")
        if mgr:
            mid = conn.bind(mgr[0], "zxdg_output_manager_v1", min(mgr[1], 3))
            for o in outs:
                xid = conn.alloc()
                conn.send(mid, 1, [("u", xid), ("u", o["oid"])])  # get_xdg_output

                def xdg_handler(op, cur, fds, o=o):
                    if op == 0:  # logical_position
                        o["lx"], o["ly"] = cur.i32(), cur.i32()
                    elif op == 1:  # logical_size
                        o["lw"], o["lh"] = cur.i32(), cur.i32()

                conn.on(xid, xdg_handler)
            conn.roundtrip()

        boxes = []
        for o in outs:
            if o["lw"] and o["lh"]:
                boxes.append((o["lx"] or 0, o["ly"] or 0, o["lw"], o["lh"]))
            elif o["w"] and o["h"]:
                w, h = o["w"], o["h"]
                if o["transform"] % 2:  # 90/270 (+flipped) swap
                    w, h = h, w
                s = max(o["scale"], 1)
                boxes.append((o["x"], o["y"], w // s, h // s))
        if not boxes:
            raise RuntimeError("no wl_output geometry advertised")
        box = _bbox_of(boxes)
        return (box, outs) if detail else box
    finally:
        conn.close()


_SHIFTS = (keymap.KEY_LEFTSHIFT, keymap.KEY_RIGHTSHIFT)

# A modifier held on a keyboard that is not ours cannot be cleared through uinput at all (see "modifiers around
# an injection" below), so the flag says so rather than look like it worked. Emitted once per client connection
# and only when the key state could be read -- without that access there is nothing to report and the behaviour
# is the same either way.
FOREIGN_MODS_WARNING = (
    "wdotool: --clearmodifiers cannot clear %s: held on a keyboard that is "
    "not ours, and a uinput device cannot release a key it does not hold -- "
    "the kernel drops that key-up, so nothing reaches the compositor. The "
    "injection goes ahead with the modifier still down; let go of it, or "
    "spell it out in the key sequence instead. Modifiers wdotool itself "
    "holds (keydown) are cleared and put back as usual.")

# Names for that warning, one per MODIFIER_KEYCODES entry.
_MOD_LABELS = {
    keymap.KEY_LEFTSHIFT: "shift", keymap.KEY_RIGHTSHIFT: "right shift",
    keymap.KEY_LEFTCTRL: "ctrl", keymap.KEY_RIGHTCTRL: "right ctrl",
    keymap.KEY_LEFTALT: "alt", keymap.KEY_RIGHTALT: "altgr",
    keymap.KEY_LEFTMETA: "super", keymap.KEY_RIGHTMETA: "right super",
}

# Skip the key-state read (the diagnostic) entirely; for testing.
NO_KEYSTATE_ENV = "WDOTOOL_NO_KEYSTATE"

# --vkbd / WDOTOOL_VKBD: which keyboard the typing ops inject through.
VKBD_ENV = "WDOTOOL_VKBD"
VKBD_MODES = ("auto", "on", "off")

# Said once per daemon when the policy picks a protocol: this is the case
# where wdotool now works for a user who could not inject at all before, so
# it is worth one line rather than a silent change of mechanism.
# (no "wdotool: " prefix -- _xkb_say adds one.)
VKBD_CHOSE_WARNING = (
    "%s -- typing through the compositor's zwp_virtual_keyboard_v1 instead, "
    "which needs no root and no device rule.")

VPTR_CHOSE_WARNING = (
    "%s -- moving and clicking through the compositor's "
    "zwlr_virtual_pointer_v1 instead, which needs no root and no device "
    "rule either.")

# The compositor we were typing through went away between two commands. Said only when we were holding
# something, because that is the only part the next command cannot simply redo.
VKBD_RESTART_WARNING = (
    "the compositor restarted; the keys wdotool was holding on its virtual "
    "keyboard were released with it")

# The compositor we were clicking through went away between two commands.
# Same rule as the keyboard's: only said when we were holding something.
VPTR_RESTART_WARNING = (
    "the compositor restarted; the mouse buttons wdotool was holding on its "
    "virtual pointer were released with it")

# The two sinks are separate devices with separate key state (see _own_sink).
SINK_SWITCH_WARNING = (
    "the keys wdotool was holding on the %s keyboard (%s) were released: "
    "this command types through the %s one, and only the device that pressed "
    "a key can release it")

# ...and the pointer's exact counterpart (see _own_pointer). A button, like a
# key, can only be released by the device that pressed it.
BTN_SWITCH_WARNING = (
    "the mouse buttons wdotool was holding on the %s pointer (%s) were "
    "released: this command injects through the %s one, and only the device "
    "that pressed a button can release it")

# The same two, for a command that NAMED a sink it then could not have. The command fails -- `--vkbd off` on a
# session with no /dev/uinput is asking for a device that is not there -- but the hold must not survive it.
# Measured on sway with /dev/uinput root-only: `--vkbd on mousedown 1` then `--vkbd off mouseup 1` reported the
# uinput error and left the LEFT BUTTON DOWN on the virtual pointer, which is a drag that outlives the command
# that failed. The release has to happen on the way out of the failure, not after it.
SINK_GONE_WARNING = (
    "the keys wdotool was holding on the %s keyboard (%s) were released: "
    "this command asked for the %s one, which cannot be used, and a key "
    "cannot be left down on a keyboard nothing is going to type through")
BTN_GONE_WARNING = (
    "the mouse buttons wdotool was holding on the %s pointer (%s) were "
    "released: this command asked for the %s one, which cannot be used, and "
    "a button cannot be left down on a pointer nothing is going to inject "
    "through")

# The tail on `--vkbd on`'s pointer refusal (_pick_pointer). It is deliberately about the FORCED mode and not
# about the compositor: the three that do not implement zwlr_virtual_pointer_manager_v1 -- Mutter, KWin and
# cosmic-comp -- are exactly where `--vkbd auto` reaches the kernel devices and clicks, which is rung 4 already
# in hand. What a user meets here is the sink they named, so the route is the sink they did not.
VPTR_FORCED_ROUTE = (
    "; not yet forced here, and the route is the kernel devices `--vkbd "
    "auto` takes on the same session (AGENTS.md route 4), at the cost of "
    "access to /dev/uinput")

# Names for that warning, one per _Daemon._BTN entry.
_BTN_LABELS = {uinput.BTN_LEFT: "left", uinput.BTN_MIDDLE: "middle",
               uinput.BTN_RIGHT: "right", uinput.BTN_SIDE: "side",
               uinput.BTN_EXTRA: "extra", uinput.BTN_FORWARD: "forward",
               uinput.BTN_BACK: "back", uinput.BTN_TASK: "task"}

# `getmouselocation` with nothing to report, on the virtual-pointer path. The protocol sends input and receives
# nothing at all -- zero events on the object, across motion, buttons and axes -- and sway's IPC has no cursor
# position either, so there is nothing to fall back to and no /dev/uinput error worth quoting: uinput is not
# what the user would have to fix.
#
# `xdotool getmouselocation` always answers, so a session where this one refuses is a gap of ours and owes a
# route. Two reach it and both are written up in wdotool/vptr.py's header, which is where the cost of each was
# worked out; the sentence carries the short form of both because the user reading it is the one who has to
# decide whether to wait for it.
POINTER_UNKNOWN = (
    "wdotool does not know where the pointer is: it has not moved it, and "
    "zwlr_virtual_pointer_v1 cannot be asked -- the protocol delivers no "
    "events, and neither sway's IPC nor Xwayland carries the cursor "
    "position. Move the pointer once (mousemove) and this answers exactly "
    "where it was put; on GNOME and KDE the compositor answers it directly. "
    "Answering it here is not yet done, and the routes are a wlr-layer-shell "
    "overlay whose wl_pointer.motion is the cursor (AGENTS.md route 1), at "
    "the cost of a surface that eats the events it reads and has to be "
    "unmapped before the user's next click, or evdev off /dev/input (route "
    "4), at the cost of read access to the devices and an anchor to count "
    "from, since the kernel says how far the pointer moved and never where "
    "it is.")

_UNSET = object()

CGROUP_ROOT = "/sys/fs/cgroup"
# Name of the cgroup the daemon moves itself into; a plain directory under
# the user manager's delegated subtree, not a systemd unit.
_ESCAPE_CGROUP = "wdotool-daemon"


def transient_scope_target(cgroup_line: str, euid: int) -> str | None:
    """Cgroup directory a freshly spawned daemon should move itself into, or None when it should stay where it
    is (B11).

    GNOME runs a custom keyboard shortcut inside a transient systemd scope (`app-gnome-<name>-<pid>.scope`). A
    scope stays *active* while any process remains in its cgroup, and neither fork nor setsid changes a cgroup
    -- so the double-forked daemon kept the launcher's scope (and the shell script that started it, as far as
    systemd was concerned) alive for as long as the daemon ran; observed at 10+ minutes on 24.04 / systemd 255.
    `systemd-run --user --scope` is the textbook migration but re-runs the command in an environment of
    systemd's choosing, which is exactly what B10 is trying to control. Writing our own pid into a sibling
    cgroup under the user manager's *delegated* subtree does the same job with one write, no new process and no
    environment surprises.

    Only transient app scopes are escaped. A `session-N.scope` (a normal login) or a system service is left
    alone: a daemon started there should still die with its session. Root is never in a user scope, so the root
    daemon keeps its cgroup too."""
    if euid == 0:
        return None
    path = (cgroup_line or "").strip()
    if path.startswith("0::"):
        path = path[3:]
    if not path.startswith("/"):
        return None
    leaf = path.rsplit("/", 1)[-1]
    if not (leaf.startswith("app-") and leaf.endswith(".scope")):
        return None
    marker = "/user@%d.service" % euid
    idx = path.find(marker + "/")
    if idx < 0:
        return None
    return CGROUP_ROOT + path[:idx + len(marker)] + "/" + _ESCAPE_CGROUP


def _escape_transient_scope():
    """Best effort; a failure is a documented limitation, never fatal."""
    try:
        with open("/proc/self/cgroup") as f:
            line = f.readline()
        target = transient_scope_target(line, os.geteuid())
        if target is None:
            return
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "cgroup.procs"), "w") as f:
            f.write("%d\n" % os.getpid())
    except OSError:
        pass


def clean_env(env=None) -> dict:
    """The environment a spawned daemon keeps (B10)."""
    src = os.environ if env is None else env
    out = {k: v for k, v in src.items() if k in _KEEP_ENV or k.startswith("WDOTOOL_")}
    if not out.get("PATH"):
        out["PATH"] = _DEFAULT_PATH
    return out


def _close_inherited_fds():
    """Close everything above stdio (B10): a daemon forked out of a running command inherits that command's open
    files -- most importantly the session D-Bus socket, which keeps a bus connection ESTABLISHED for the
    daemon's whole life and made the daemon look like a D-Bus client in `ss`."""
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = 4096
    if not isinstance(limit, int) or limit < 3 or limit > 65536:
        limit = 65536
    os.closerange(3, limit)


class _KernelPointer:
    """The uinput tablet and relative mouse behind the same four calls the virtual pointer answers to, so the
    pointer ops read the same on both paths and cannot drift apart.

    Nothing here is new. The ceiling axis map (B7), the unchanged-EV_ABS nudge (B2), the evdev button codes and
    the REL_WHEEL detents are the kernel contract the daemon tests pin, moved behind an interface and otherwise
    untouched. It holds the *daemon* rather than the two devices because those are replaced under it
    (create_devices, _drop_devices) and the last absolute report has to outlive any one pointer op.
    """

    def __init__(self, d):
        self.d = d

    def warp(self, x, y, gx, gy, w, h):
        """The absolute tablet. The compositor maps its axes across the FULL output layout, so scale the offset
        from the layout origin, not the raw (possibly negative) global coordinate."""
        d = self.d
        ax = d._axis(x - gx, w)
        ay = d._axis(y - gy, h)
        # B2: the kernel drops an EV_ABS whose value equals the axis' current value, so re-sending the
        # coordinates the tablet reported last time is a silent no-op -- and the pointer may well have moved
        # since, via REL events, a physical mouse, or another daemon. Nudge one axis by a single unit (1/32768
        # of the layout: sub-pixel on any real screen) first, so the second report is always a change and always
        # lands.
        if (ax, ay) == d._last_abs:
            nudge = ax + 1 if ax < 32767 else ax - 1
            d.tablet.emit(uinput.EV_ABS, uinput.ABS_X, nudge)
            d.tablet.syn()
        d.tablet.emit(uinput.EV_ABS, uinput.ABS_X, ax)
        d.tablet.emit(uinput.EV_ABS, uinput.ABS_Y, ay)
        d.tablet.syn()
        d._last_abs = (ax, ay)

    def move(self, dx, dy):
        d = self.d
        if dx:
            d.mouse.emit(uinput.EV_REL, uinput.REL_X, dx)
        if dy:
            d.mouse.emit(uinput.EV_REL, uinput.REL_Y, dy)
        d.mouse.syn()

    def button(self, code, down):
        self.d.mouse.key(code, down)

    def wheel(self, btn):
        """One detent of wdotool's wheel button 4/5/6/7, in evdev's sign -- REL_WHEEL positive is up, which is
        the opposite of Wayland's axis 0. vptr.WHEEL is the same four gestures in the other convention."""
        rel, value = _Daemon._WHEEL[btn]
        self.d.mouse.emit(uinput.EV_REL, rel, value)
        self.d.mouse.syn()


class _Daemon:
    # Injection rate cap. Keystrokes injected faster than the compositor drains its per-open evdev buffer are
    # lost wholesale to the kernel's SYN_DROPPED — a zero-delay `type` of a few thousand characters silently
    # loses keys. Empirically ~600 keystrokes/s is drop-free on a headless wlroots compositor, so floor every
    # inter-keystroke gap at _MIN_GAP. Steady (deadline-scheduled) pacing survives where bursts do not: a burst
    # fills the buffer instantly. Explicit --delay values above the floor are honored unchanged.
    _MIN_GAP = 0.0018  # ~555 keystrokes/s

    def __init__(self):
        self.lock = threading.Lock()
        self.px = self.py = 0
        self.geom = None
        self.geom_warned = False
        self.kb = self.mouse = self.tablet = None
        self.dev_error = "uinput devices not initialized"
        self.down: set[int] = set()  # keycodes we injected as down
        # ...and which of the two sinks they are down ON. The kernel device and the virtual keyboard are
        # separate devices with separate key state; `self.down` describes exactly one of them at a time. See
        # _own_sink() for what happens when the sink changes under a held key.
        self._down_virtual = False
        # keycodes released by the op running inside _mods_cleared(); see
        # _mods_cleared() for why the restore has to subtract them
        self._released_mods: set[int] = set()
        self._next_ok = 0.0  # monotonic deadline for the next keystroke
        # Last (ABS_X, ABS_Y) written to the tablet; a fresh uinput device
        # starts at 0,0, which is what the kernel will compare against.
        self._last_abs = (0, 0)
        self._rel_abs = None      # relative moves as absolute warps? (B1)
        self._reader = _UNSET     # evdev key-state reader (None: degraded)
        self._keystate_logged = False
        self.pos_known = False    # has px/py ever been established? (B6)
        self.geom_fallback = False  # last geometry() used FALLBACK_GEOMETRY
        # Active-layout state (B13). `_layout_cache` is ((keymap digest, group, group known?), ReverseMap or
        # None); None means "the fixed US table is the answer", which is both the bypass and every failure mode.
        # A layout switch changes the group, which changes the key, so the cache follows the user's switcher --
        # on KDE, where xkbmap asks KWin which layout is live, that is on every command.
        self._layout_cache = None
        self._xkb_backoff = 0.0    # monotonic: don't re-try a failing read
        self._xkb_mods_wait = 0.08  # seconds to wait for a modifiers event
        self._xkb_said: set = set()  # one-shot diagnostics already emitted
        self._xkb_degraded = None  # set while the keymap cannot be used
        self._xkb_group_said = None  # layout state the group notice was for
        self._xkb_group_msg = None   # ...and the notice itself, for every later client
        # zwp_virtual_keyboard_v1 (see vkbd.py). ONE connection and ONE keyboard object for the daemon's life:
        # the compositor releases whatever a client was holding when it disconnects, so a keydown that has to
        # survive until the next command cannot be a per-command connection. `_vk_error` + `_vk_backoff` keep a
        # compositor that cannot be reached from being retried on every keystroke.
        self._vk = None
        self._vk_error = None
        self._vk_backoff = 0.0
        # zwlr_virtual_pointer_v1 (see vptr.py), the pointer's exact counterpart: ONE connection and ONE pointer
        # object for the daemon's life, for the same reason -- the compositor releases whatever a client was
        # holding when it disconnects, so a `mousedown` that has to survive until the matching `mouseup` cannot
        # be a per-command connection.
        self._vp = None
        self._vp_error = None
        self._vp_backoff = 0.0
        # Evdev button codes we injected as down, and which of the two pointers they are down ON -- the same
        # trap `down`/`_down_virtual` carry for keys: one set, two devices, and only the device that pressed a
        # button can release it (see _own_pointer).
        self.btns: set[int] = set()
        self._btns_virtual = False

    def _key_gap(self, delay: float):
        """Inter-keystroke pause. Sleeps `delay` seconds but never lets the keystroke rate exceed 1/_MIN_GAP,
        using an absolute deadline so the floor neither drifts nor accumulates syscall overhead."""
        now = time.monotonic()
        target = max(now + delay, self._next_ok)
        if target > now:
            time.sleep(target - now)
        self._next_ok = max(target, now) + self._MIN_GAP

    def create_devices(self):
        try:
            self.kb = uinput.keyboard()
            self.mouse = uinput.rel_mouse()
            self.tablet = uinput.abs_pointer()
            self._last_abs = (0, 0)  # fresh device: kernel axis state is 0,0
            self._reader = _UNSET    # rebuild it: our event nodes are new
            self.dev_error = None
            if os.environ.get("WDOTOOL_FAKE_UINPUT") != "1":
                time.sleep(0.6)  # compositor hotplug settle
        except OSError as e:
            import errno

            # A partial set (e.g. keyboard created, mouse raced EPERM) must not
            # leak devices: close what exists so a later retry starts clean.
            for dev in (self.kb, self.mouse, self.tablet):
                if dev is not None:
                    dev.close()
            self.kb = self.mouse = self.tablet = None
            hint = ""
            if e.errno in (errno.EACCES, errno.EPERM):
                hint = " (wdotool injects input via /dev/uinput; run it as root)"
            elif e.errno == errno.ENOENT:
                hint = " (/dev/uinput missing; is the uinput kernel module loaded?)"
            err = f"cannot create uinput devices: {e}{hint}"
            if err != self.dev_error:  # don't spam the log on every retry
                print(err, file=sys.stderr, flush=True)
            self.dev_error = err

    def _drop_devices(self, why: str):
        """Destroy the virtual devices and remember why, so the next request gets the reason instead of a silent
        no-op. create_devices() rebuilds them if the cause has gone away."""
        for dev in (self.kb, self.mouse, self.tablet):
            if dev is not None:
                dev.close()
        self.kb = self.mouse = self.tablet = None
        if not self._down_virtual:
            self.down.clear()  # the kernel released them with the device
        if not self._btns_virtual:
            self.btns.clear()  # ...and the buttons with it
        self._reader = _UNSET
        if why != self.dev_error:
            print(why, file=sys.stderr, flush=True)
        self.dev_error = why

    def _grant_gone(self) -> bool:
        """Did the /dev/uinput grant we opened the devices under go away?

        Only asked of devices that really came from the node (`fake` devices and the ones the tests install by
        hand answer no), and only ever answered yes by a permission error -- see uinput.access_ok()."""
        if self.kb is None or getattr(self.kb, "fake", True):
            return False
        return not uinput.access_ok()

    def _need_devices(self):
        if self._grant_gone():
            # The grant we opened /dev/uinput under is gone: logind moved the seat to another session (a VT
            # switch, a fast user switch). The devices we already hold would keep injecting into *their*
            # session, which is exactly what the uaccess tag exists to prevent, so let go of them until the seat
            # comes back.
            self._drop_devices(
                "/dev/uinput is no longer accessible to this user (this "
                "session is not the active one); wdotool stops injecting "
                "until it is")
            raise RuntimeError(self.dev_error)
        if self.dev_error:
            # Retry: the failure may be transient (uinput module loaded after boot, devices raced). Cheap when
            # it fails again — the 600ms hotplug settle is only paid on success.
            self.create_devices()
        if self.dev_error:
            raise RuntimeError(self.dev_error)

    # -- which devices inject (the kernel ones, or the protocols) ---------
    #
    # THE POLICY, in one sentence, and it is ONE policy for both halves: `key`/`keydown`/`keyup`/`type` go
    # through zwp_virtual_keyboard_v1, and `click`/`mousedown`/`mouseup`/`mousemove`/`mousemove_relative`
    # through zwlr_virtual_pointer_v1, when the matching kernel device cannot be opened and the compositor
    # implements that protocol -- and through /dev/uinput in every other case. `--vkbd on|off` (WDOTOOL_VKBD)
    # forces either, for both halves: it is one switch because it is one decision.
    #
    # It is deliberately that narrow. Where uinput works today it keeps working, byte for byte: the daemon tests
    # pin that event stream, and neither protocol is on GNOME or KDE at all, so most sessions could not use them
    # anyway. Typing through the protocol is not free either -- the compositor hands the focused application OUR
    # keymap ahead of our first key and the session's keymap back when the real keyboard is next touched, so
    # every injection makes that application recompile its keymap twice. What the protocols buy is the case
    # uinput cannot serve at all: a session where /dev/uinput is root-only (it is `crw------- root root` on
    # stock wlroots, with no uaccess ACL) and the user is not root. There, today, `wdotool type` and
    # `wdotool click` fail outright; through the protocols they work, with no privilege whatsoever. Turning a
    # hard failure into the right characters -- and the right clicks -- is the one change that is strictly
    # better, so it is the only one the default makes.
    #
    # The two halves are two connections and two objects, deliberately: a disconnect releases only what THAT
    # connection holds, so the keyboard's troubles cannot drop the pointer's held button or the other way round.

    def _vkbd_setting(self, forced=None) -> str:
        """`--vkbd` (per command) over WDOTOOL_VKBD (per daemon) over auto. An unknown value is `auto`: the flag
        is screened by cli.py and the request field by _vkbd_mode(), so what can still be wrong here is a typo
        in the environment, which must not stop the tool typing."""
        mode = (forced or os.environ.get(VKBD_ENV) or "auto").strip().lower()
        return mode if mode in VKBD_MODES else "auto"

    def _vk_alive(self) -> bool:
        """Is the connection we cached still there?

        One wl_display.sync round trip, paid once per command and only on this path. It buys the case a
        long-lived daemon cannot otherwise survive: the compositor restarted while nothing was being typed, and
        the first command afterwards would otherwise be spent discovering that. A slow compositor that misses
        the connection's 2s timeout is treated as gone and reconnected to, which costs one reconnect and never a
        wrong keystroke."""
        try:
            self._vk.flush()
        except vkbd.VkbdError:
            return False
        return True

    def _vkbd(self, warnings=None):
        """The live virtual keyboard, connecting on first use. Raises
        vkbd.VkbdError with the reason; the caller decides what that means."""
        if self._vk is not None:
            if self._vk_alive():
                return self._vk
            # Gone since the last command. The keys it was holding went with it -- that is the one thing
            # reconnecting cannot redo, so it is the one thing worth a line -- and then we reconnect rather than
            # spending a command on the discovery.
            lost = bool(self.down and self._down_virtual)
            self._drop_vkbd()
            if lost:
                self._xkb_print(VKBD_RESTART_WARNING, warnings)
        now = time.monotonic()
        if now < self._vk_backoff:
            raise vkbd.VkbdError(self._vk_error or "not available")
        try:
            self._vk = vkbd.VirtualKeyboard.open()
        except vkbd.VkbdError as e:
            self._vk_error = str(e)
            self._vk_backoff = now + 5.0
            raise
        except Exception as e:   # a bug here must not be a traceback either
            self._vk_error = repr(e)
            self._vk_backoff = now + 60.0
            raise vkbd.VkbdError(self._vk_error) from None
        self._vk_error = None
        return self._vk

    def _drop_vkbd(self):
        """Forget a virtual keyboard that failed. Nothing survives a compositor restart -- not the object, not
        the uploaded keymap, and not the keys it was holding -- so the keycodes we thought were down go with it;
        the next command reconnects and re-uploads."""
        vk, self._vk = self._vk, None
        if self._down_virtual:
            # Everything `self.down` named was down on THAT keyboard, so it is all released now -- including a
            # key whose key-up is what broke the connection in the first place, which `vk.held` no longer lists.
            # Trusting `vk.held` here left `shift` down in the daemon's model for the rest of its life, and
            # every later `type A` came out as `a`.
            self.down.clear()
            self._down_virtual = False
        if vk is not None:
            vk.close()

    def _keyboard(self, warnings=None, mode=None):
        """(device, is_virtual) for one typing op -- see THE POLICY above."""
        try:
            dev, virtual = self._pick_keyboard(warnings, mode)
        except Exception:
            # The sink this command named is unusable. Whatever we are holding is held on the OTHER one, and
            # _own_sink() -- the thing that releases it -- is below the raise. So it never ran, and
            # `--vkbd off keyup shift` on a session with no /dev/uinput left shift down on the virtual keyboard
            # for the daemon's life. Let go of it here, then report why the sink is unusable.
            self._release_named_sink(mode, warnings)
            raise
        self._own_sink(virtual, warnings)
        return dev, virtual

    def _named_sink(self, mode):
        """Which sink `--vkbd` names outright: True virtual, False kernel, None for `auto` (which names neither,
        and whose failure means neither sink exists -- so there is nothing left that could release a hold)."""
        return {"on": True, "off": False}.get(self._vkbd_setting(mode))

    def _release_named_sink(self, mode, warnings):
        """Let go of keys and buttons held on the sink a failed command was switching AWAY from. Both halves:
        `--vkbd` is one switch, so one failed command can strand one of each."""
        want = self._named_sink(mode)
        if want is None:
            return
        self._own_sink(want, warnings, gone=True)
        self._own_pointer(want, warnings, gone=True)

    def _pick_keyboard(self, warnings=None, mode=None):
        mode = self._vkbd_setting(mode)
        if mode == "off":
            self._need_devices()
            return self.kb, False
        if mode == "on":
            try:
                return self._vkbd(warnings), True
            except vkbd.VkbdError as e:
                # Forced and impossible: say exactly what was asked for and
                # what refused it, and do not quietly type through uinput.
                raise RuntimeError(f"--vkbd on: cannot use zwp_virtual_keyboard_v1: {e}") from None
        # auto: the kernel device unless it cannot be used at all.
        if self._vk is not None and self.down and self._down_virtual:
            # Never change sinks under a held key: a key held on the virtual keyboard can only be released there
            # (`--vkbd on wdotool keydown ctrl` then a plain `wdotool type c`), and a key-up sent to the other
            # device releases nothing at all. Asking _vkbd() rather than using the object directly is what makes
            # this safe when the compositor restarted in between: it notices, drops the hold it can no longer
            # honour, and says so -- and then there is no hold left to stay for, so the choice is made again
            # from scratch.
            try:
                vk = self._vkbd(warnings)
            except vkbd.VkbdError:
                vk = None
            if vk is not None and self.down and self._down_virtual:
                return vk, True
        try:
            self._need_devices()
            return self.kb, False
        except RuntimeError as e:
            why = str(e)
        try:
            dev = self._vkbd(warnings)
        except vkbd.VkbdError as e:
            # No kernel device and no protocol: the error the user gets is the kernel device's, unchanged --
            # that is still the thing they have to fix. The protocol's reason goes to the daemon log. Its own
            # tag: sharing one with the notice below meant a daemon that started before the compositor never
            # said it had switched.
            self._xkb_say("vkbd-none", f"no virtual-keyboard protocol either: {e}")
            raise RuntimeError(why) from None
        self._xkb_say("vkbd", VKBD_CHOSE_WARNING % why, warnings)
        return dev, True

    def _own_sink(self, virtual: bool, warnings=None, gone: bool = False):
        """Make `self.down` describe the sink that is about to type.

        The kernel device and the virtual keyboard are separate devices with separate key state, and `self.down`
        is one set. Letting it describe one device while the other types is how
        `wdotool --vkbd on keydown shift` followed by `wdotool --vkbd off type A` produced `a`: the kernel path
        found shift in `self.down`, believed it was already held, and pressed nothing. So when the sink changes
        with keys still down, they are released on the device that is actually holding them -- the only device
        that can -- and the user is told, every time, because it is a state change and not a fact about the
        environment."""
        if self.down and self._down_virtual != virtual:
            old = self._vk if self._down_virtual else self.kb
            codes = sorted(self.down)
            self.down.clear()
            for code in codes:
                if old is None:
                    break
                try:
                    old.key(code, False)
                except (OSError, vkbd.VkbdError):
                    break     # that device is gone; so are its keys
            self._flush_quietly(old)
            self._xkb_print((SINK_GONE_WARNING if gone
                             else SINK_SWITCH_WARNING) % (
                "virtual" if self._down_virtual else "kernel",
                ", ".join(_MOD_LABELS.get(c, "keycode %d" % c) for c in codes),
                "kernel" if self._down_virtual else "virtual"), warnings)
        self._down_virtual = virtual

    @contextlib.contextmanager
    def _vk_guard(self):
        """A virtual keyboard that fails mid-injection is gone -- the compositor restarted, or the socket died.
        Drop it, and with it the keys it was holding (the compositor released those when we disconnected, so
        believing we still hold them would leave the daemon lying about its own state), and report the reason as
        an ordinary error. The next command reconnects and re-uploads the keymap."""
        try:
            yield
        except vkbd.VkbdError as e:
            self._drop_vkbd()
            raise RuntimeError(str(e)) from None

    def _flush(self, dev):
        """End of an injection: let a sink that acknowledges say so. uinput
        writes are unacknowledged by construction and have no flush."""
        f = getattr(dev, "flush", None)
        if f is not None:
            f()

    def _flush_quietly(self, dev):
        """Round-trip a sink we are letting go of, so the releases we just sent it have reached the compositor
        before the other sink starts injecting -- the two are different connections, and nothing else orders
        them. A sink that is already gone took its keys and buttons with it, which is the outcome either way, so
        a failure here is not news."""
        try:
            self._flush(dev)
        except (OSError, vkbd.VkbdError, vptr.VptrError):
            pass

    # -- which pointer moves (the kernel devices, or the protocol) ---------
    #
    # Everything below mirrors the keyboard's half above, request for request, because it is the same policy and
    # the same lifetime; see vptr.py for what the wire does differently.

    def _kernel_pointer(self) -> "_KernelPointer":
        return _KernelPointer(self)

    def _vp_alive(self) -> bool:
        """Is the connection we cached still there? One wl_display.sync, paid once per pointer command and only
        on this path -- see _vk_alive() for why it is worth it."""
        try:
            self._vp.flush()
        except vptr.VptrError:
            return False
        return True

    def _vptr(self, warnings=None):
        """The live virtual pointer, connecting on first use. Raises
        vptr.VptrError with the reason; the caller decides what that means."""
        if self._vp is not None:
            if self._vp_alive():
                return self._vp
            # Gone since the last command. The buttons it was holding went with it -- the one thing reconnecting
            # cannot redo, so the one thing worth a line.
            lost = bool(self.btns and self._btns_virtual)
            self._drop_vptr()
            if lost:
                self._xkb_print(VPTR_RESTART_WARNING, warnings)
        now = time.monotonic()
        if now < self._vp_backoff:
            raise vptr.VptrError(self._vp_error or "not available")
        try:
            self._vp = vptr.VirtualPointer.open()
        except vptr.VptrError as e:
            self._vp_error = str(e)
            self._vp_backoff = now + 5.0
            raise
        except Exception as e:   # a bug here must not be a traceback either
            self._vp_error = repr(e)
            self._vp_backoff = now + 60.0
            raise vptr.VptrError(self._vp_error) from None
        self._vp_error = None
        return self._vp

    def _drop_vptr(self):
        """Forget a virtual pointer that failed. Nothing survives a compositor restart -- not the object and not
        the buttons it was holding -- so the codes we thought were down go with it.

        `vp.held` is deliberately NOT consulted, for the reason _drop_vkbd() spells out: the write that failed
        is the one that took the button out of `held`, so trusting it would leave the daemon believing it still
        holds a button nothing can ever release."""
        vp, self._vp = self._vp, None
        if self._btns_virtual:
            self.btns.clear()
            self._btns_virtual = False
        if vp is not None:
            vp.close()

    def _pointer_sink(self, warnings=None, mode=None):
        """(sink, is_virtual) for one pointer op -- see THE POLICY above."""
        try:
            sink, virtual = self._pick_pointer(warnings, mode)
        except Exception:
            self._release_named_sink(mode, warnings)   # see _keyboard()
            raise
        self._own_pointer(virtual, warnings)
        return sink, virtual

    def _pick_pointer(self, warnings=None, mode=None):
        mode = self._vkbd_setting(mode)
        if mode == "off":
            self._need_devices()
            return self._kernel_pointer(), False
        if mode == "on":
            try:
                return self._vptr(warnings), True
            except vptr.VptrError as e:
                # Forced and refused: say exactly what was asked for and what
                # refused it, and do not quietly click through uinput.
                #
                # The rung goes on the FORCED mode only. `--vkbd auto` on the
                # same session takes the route VPTR_FORCED_ROUTE names -- the
                # kernel devices -- and clicks, so the gap here is the one the
                # user opted into by naming a sink, and the sentence has to say
                # that rather than describe a compositor that is missing
                # nothing it could offer.
                raise RuntimeError("--vkbd on: cannot use zwlr_virtual_pointer_v1: %s%s"
                                   % (e, VPTR_FORCED_ROUTE)) from None
        # auto: the kernel devices unless they cannot be used at all.
        if self._vp is not None and self.btns and self._btns_virtual:
            # Never change sinks under a held button, for the same reason as under a held key: a button held on
            # the virtual pointer can only be released there. Asking _vptr() rather than using the object
            # directly is what makes this safe when the compositor restarted in between -- it notices, drops the
            # hold it can no longer honour, and says so, after which there is no hold left to stay for and the
            # choice is made again from scratch.
            try:
                vp = self._vptr(warnings)
            except vptr.VptrError:
                vp = None
            if vp is not None and self.btns and self._btns_virtual:
                return vp, True
        try:
            self._need_devices()
            return self._kernel_pointer(), False
        except RuntimeError as e:
            why = str(e)
        try:
            vp = self._vptr(warnings)
        except vptr.VptrError as e:
            # No kernel device and no protocol: the error the user gets is the kernel device's, unchanged --
            # that is still the thing they have to fix. The protocol's reason goes to the daemon log.
            self._xkb_say("vptr-none", f"no virtual-pointer protocol either: {e}")
            raise RuntimeError(why) from None
        self._xkb_say("vptr", VPTR_CHOSE_WARNING % why, warnings)
        return vp, True

    def _own_pointer(self, virtual: bool, warnings=None, gone: bool = False):
        """Make `self.btns` describe the pointer that is about to inject.

        The exact shape of _own_sink(), and it exists for the exact defect that one was written for: `self.btns`
        is one set and there are two devices behind it, so letting it describe one while the other clicks would
        leave `--vkbd on mousedown 1` followed by `--vkbd off mouseup 1` with a button held down for the
        daemon's life -- released by nobody, because only the device that pressed it can. So when the sink
        changes with buttons still down, they are released on the device that is actually holding them, and the
        user is told every time."""
        if self.btns and self._btns_virtual != virtual:
            old = self._vp if self._btns_virtual else (
                self._kernel_pointer() if self.mouse is not None else None)
            codes = sorted(self.btns)
            self.btns.clear()
            for code in codes:
                if old is None:
                    break
                try:
                    old.button(code, False)
                except (OSError, vptr.VptrError):
                    break     # that device is gone; so are its buttons
            self._flush_quietly(old)
            self._xkb_print((BTN_GONE_WARNING if gone
                             else BTN_SWITCH_WARNING) % (
                "virtual" if self._btns_virtual else "kernel",
                ", ".join(_BTN_LABELS.get(c, "button %d" % c) for c in codes),
                "kernel" if self._btns_virtual else "virtual"), warnings)
        self._btns_virtual = virtual

    @contextlib.contextmanager
    def _vp_guard(self):
        """_vk_guard for the pointer ops, and for the virtual keyboard they can still reach through
        --clearmodifiers: a protocol object that fails mid-injection is gone, so drop it -- and with it whatever
        it was holding, which the compositor released when we disconnected -- and report the reason as an
        ordinary error."""
        try:
            yield
        except vptr.VptrError as e:
            self._drop_vptr()
            raise RuntimeError(str(e)) from None
        except vkbd.VkbdError as e:
            self._drop_vkbd()
            raise RuntimeError(str(e)) from None

    # -- geometry / pointer ------------------------------------------------

    def geometry(self, warnings=None, quiet=False) -> tuple[int, int, int, int]:
        """Layout bounding box (min_x, min_y, w, h), re-read from the compositor on every call so output-layout
        changes (wxrandr, hotplug) are visible immediately; the last good reading serves as the fallback when
        the compositor can't be queried. The origin can be non-zero/negative on multi-output layouts; pointer
        coordinates are tracked in these global layout coordinates.

        `quiet` is a caller that is *asking* rather than moving a pointer: `fallback` in the reply tells it the
        numbers are the built-in guess, and it decides what to say. `getdisplaygeometry` is that caller, and it
        refuses to print a size nobody measured -- so the fallback notice below, which is true of the pointer
        map and of nothing else, contradicted the very next line the command printed (measured on both X11
        images: "assuming 1920x1080" above "not guessing a display size", exit 2)."""
        try:
            box, outs = _wayland_bbox(detail=True)
            # The box is the wire's, always -- it is the space the compositor maps
            # absolute motion across, measured even where it is stale. One GNOME state
            # advertises a layout it is not drawing, and there this says so once; on
            # every other session it opens nothing. See wdotool/layoutbox.py.
            self.geom = layoutbox.check(
                box, outs, warn=lambda tag, msg: self._xkb_say(tag, msg, warnings))
            self.geom_fallback = False
            return self.geom
        except Exception as e:
            if self.geom:
                self.geom_fallback = False
                return self.geom
            self.geom_fallback = True
            if not self.geom_warned and not quiet:
                self.geom_warned = True
                msg = (f"wdotool: cannot query Wayland output geometry ({e}); "
                       f"assuming {FALLBACK_GEOMETRY[2]}x{FALLBACK_GEOMETRY[3]}")
                print(msg, file=sys.stderr, flush=True)
                if warnings is not None:
                    warnings.append(msg)
            return FALLBACK_GEOMETRY

    @staticmethod
    def _axis(delta: int, span: int) -> int:
        """Layout offset -> tablet axis value (B7).

        libinput maps an absolute axis with scale_axis(): the pointer lands at `value * span / (max - min + 1)`
        = `v * span / 32768`, which the compositor then floors (or rounds) to a pixel. The forward map that
        round-trips through that inverse exactly is the CEILING of `delta * 32768 / span`, not a floor:
        floor(ceil(d*32768/S) * S/32768) == d for every d in [0, S) while S <= 32768. The old
        `d * 32767 // (S - 1)` floor map landed one pixel short wherever the division was inexact -- 257 of 301
        x values near the layout origin on a 5760px-wide three-head rig."""
        span = max(span, 1)
        return min(max(-((-delta * 32768) // span), 0), 32767)

    def _warp(self, x, y, gx, gy, w, h, sink=None):
        """Put the pointer at the global layout coordinate (x, y), and adopt that as the model. How the
        coordinate reaches the compositor is the sink's business: an absolute tablet report (B7's ceiling map,
        B2's nudge) on the kernel path, `motion_absolute` over the layout bounding box on the protocol path.
        Both land the coordinate asked for -- which is the whole point of having one call here."""
        (sink or self._kernel_pointer()).warp(x, y, gx, gy, w, h)
        self.px, self.py = x, y
        self.pos_known = True

    _REL_ENV = {"abs": True, "absolute": True, "warp": True, "rel": False, "relative": False}

    def _rel_absolute(self, virtual: bool = False) -> bool:
        """Should a relative move be emitted as an absolute warp? (B1)

        REL_X/REL_Y go through the compositor's pointer-acceleration curve, so `mousemove_relative 500 0` lands
        wherever libinput's profile puts it: on a stock GNOME (adaptive profile) 500 requested pixels moved the
        pointer 462 on 24.04 and 267 on 26.04, and only `accel-profile flat` was exact. xdotool's XWarpPointer
        is pixel-exact, so everywhere but sway/i3 the relative move is emitted as an absolute warp to (px+dx,
        py+dy) instead.

        sway/i3 keep the REL path: this repo's sway rig runs `pointer_accel 0` (REL is already exact there) and
        the sway/daemon tests pin the EV_REL contract. WDOTOOL_REL_MODE=rel|abs forces either, on any
        compositor.

        The virtual pointer never warps unless told to. It is not a libinput device -- sway lists it with an
        empty libinput configuration -- so no acceleration profile can apply to it on any wlroots compositor,
        and `motion` was measured exact for 1, 10, 100, 500 and 1000 pixels and for 500 separate one-pixel
        steps. B1's reason to warp does not exist here, and relative motion has the property a warp cannot have:
        it needs no position model, so it is right even on the first command of a daemon that has never been
        told where the cursor is."""
        forced = self._REL_ENV.get(os.environ.get("WDOTOOL_REL_MODE", "").strip().lower())
        if forced is not None:
            return forced
        if virtual:
            return False
        if self._rel_abs is None:
            from w11common import session
            self._rel_abs = not bool(session.find_sway_socket())
        return self._rel_abs

    def op_mousemove_abs(self, x, y, warnings, vkbd_mode=None):
        sink, _virtual = self._pointer_sink(warnings, vkbd_mode)
        gx, gy, w, h = self.geometry(warnings)
        x = min(max(x, gx), gx + w - 1)
        y = min(max(y, gy), gy + h - 1)
        self._warp(x, y, gx, gy, w, h, sink)
        self._flush(sink)

    def op_mousemove_rel(self, dx, dy, warnings, vkbd_mode=None):
        sink, virtual = self._pointer_sink(warnings, vkbd_mode)
        gx, gy, w, h = self.geometry(warnings)
        tx = min(max(self.px + dx, gx), gx + w - 1)
        ty = min(max(self.py + dy, gy), gy + h - 1)
        if self._rel_absolute(virtual):
            self._warp(tx, ty, gx, gy, w, h, sink)
            self._flush(sink)
            return
        self.px, self.py = tx, ty
        # NOT pos_known: a delta applied to a position we never knew is a guess, and B6's rule is that a guess
        # is never reported as known. (A warp above is a different matter -- it puts the pointer where it says
        # it does, so it may claim to know.) Where the position WAS known, relative motion keeps it: exact on
        # the protocol path, and the model the kernel path has always kept.
        sink.move(dx, dy)
        self._flush(sink)

    def _no_pointer_yet(self):
        """The `pointer` op with nothing to report (B6): what to say.

        A daemon that could not open /dev/uinput has injected nothing and knows nothing, and must fail with that
        reason rather than report the origin with rc 0. But on the virtual-pointer path /dev/uinput is not the
        thing the user would have to fix, and never will be: the protocol delivers no events at all, so wdotool
        cannot ask where the cursor is and must say so instead of guessing. (Where the kernel devices exist,
        nothing changes: a fresh tablet's own axis state really is 0,0, which is the model this reports, flagged
        `known: false`.)"""
        try:
            self._need_devices()
            return
        except RuntimeError as e:
            why = str(e)
        try:
            self._vptr()
        except vptr.VptrError:
            raise RuntimeError(why) from None
        raise RuntimeError(POINTER_UNKNOWN) from None

    def op_seed_pointer(self, x, y, warnings):
        """Adopt the compositor's real pointer position (B6). No injection: the client has just asked the
        compositor where the pointer is and is correcting the model before a relative move or a
        getmouselocation."""
        gx, gy, w, h = self.geometry(warnings)
        self.px = min(max(x, gx), gx + w - 1)
        self.py = min(max(y, gy), gy + h - 1)
        self.pos_known = True

    # -- buttons -----------------------------------------------------------

    # X11 button numbering: libinput/XWayland map BTN_SIDE..BTN_TASK to 8..12.
    _BTN = {1: uinput.BTN_LEFT, 2: uinput.BTN_MIDDLE, 3: uinput.BTN_RIGHT,
            8: uinput.BTN_SIDE, 9: uinput.BTN_EXTRA, 10: uinput.BTN_FORWARD,
            11: uinput.BTN_BACK, 12: uinput.BTN_TASK}
    _WHEEL = {4: (uinput.REL_WHEEL, 1), 5: (uinput.REL_WHEEL, -1),
              6: (uinput.REL_HWHEEL, -1), 7: (uinput.REL_HWHEEL, 1)}

    def op_button(self, btn, down, warnings=None, vkbd_mode=None):
        sink, _virtual = self._pointer_sink(warnings, vkbd_mode)
        self._button(sink, btn, down)
        self._flush(sink)

    def _button(self, sink, btn, down):
        """One button or wheel detent on an already-chosen sink.

        A press for a button we are already holding, and a release for one we are not, are dropped rather than
        sent -- on BOTH paths, so that both behave the same. The kernel drops such an event itself
        (input_handle_event compares against the device's own key state), but the compositor REFCOUNTS them per
        seat: press, press then release would leave the button down, and a `click` after a `mousedown` would
        deliver no press at all. Dropping them here is also what keeps `self.btns` an honest record of what we
        hold."""
        if btn in self._BTN:
            code = self._BTN[btn]
            if down == (code in self.btns):
                return
            sink.button(code, down)
            if down:
                self.btns.add(code)
            else:
                self.btns.discard(code)
        elif btn in self._WHEEL:
            if down:  # wheel "buttons" are one detent per press; release is a no-op
                sink.wheel(btn)
        else:
            raise RuntimeError(f"invalid mouse button {btn}")

    def op_click(self, btn, repeat, delay_ms, warnings=None, vkbd_mode=None):
        # xdo_click_window_multiple: 12ms between down/up, then `delay` after every click (including the last
        # one). The sink is chosen once for the whole run: a --repeat 1000 must not pay a policy decision (and,
        # on the protocol path, a round trip) per click.
        sink, _virtual = self._pointer_sink(warnings, vkbd_mode)
        for _ in range(repeat):
            self._button(sink, btn, True)
            time.sleep(0.012)
            self._button(sink, btn, False)
            time.sleep(delay_ms / 1000)
        self._flush(sink)

    # -- keyboard ----------------------------------------------------------

    # -- the active layout (B13) ------------------------------------------

    def _xkb_say(self, tag: str, msg: str, warnings=None):
        """One diagnostic per daemon per subject: the daemon log always, the
        answering client's stderr once."""
        if tag in self._xkb_said:
            return
        self._xkb_said.add(tag)
        self._xkb_print(msg, warnings)

    def _xkb_print(self, msg: str, warnings):
        print("wdotool: " + msg, file=sys.stderr, flush=True)
        if warnings is not None:
            warnings.append("wdotool: " + msg)

    def _xkb_say_group(self, state, msg: str, warnings):
        """The "which layout is active?" notice: on the daemon's own stderr once per layout state (and again
        whenever the state changes -- a layout switch is exactly when the guess is worth repeating), but in the
        answer to EVERY client, through _xkb_warn_group below.

        Measured on Plasma 6.6 with `us, de` and German switched on: three identical `wdotool type` runs
        printed it once, because the second and third hit the layout cache and returned before ever reaching
        here. A script therefore saw one warning and then typed the wrong characters in silence, which is the
        same defect _xkb_warn_degraded was written for -- a session typing the wrong thing on every command is
        not told by a single line to whoever happened to ask first."""
        self._xkb_group_msg = (state, "wdotool: " + msg)
        if self._xkb_group_said == state:
            if warnings is not None:
                warnings.append("wdotool: " + msg)
            return
        self._xkb_group_said = state
        self._xkb_print(msg, warnings)

    def _xkb_warn_group(self, state, warnings):
        """Tell *this* client that the active layout was a guess, whether or not an earlier client was told.
        Said from the cache-hit path, where the notice above is not reached at all."""
        if warnings is None or self._xkb_group_msg is None:
            return
        said_for, msg = self._xkb_group_msg
        if said_for == state:
            warnings.append(msg)

    def _xkb_warn_degraded(self, warnings):
        """Tell *this* client that the keymap could not be used, whether or not an earlier client was told. A
        session typing US characters because the read failed is typing the wrong thing on every command, and a
        single line to whoever happened to ask first tells nobody."""
        if self._xkb_degraded and warnings is not None:
            warnings.append(self._xkb_degraded)

    def _xkb_fell_back(self, why: str, tag: str, warnings):
        msg = why + "; typing with the built-in US layout table"
        self._xkb_degraded = "wdotool: " + msg
        self._xkb_say(tag, msg)          # the log says it once ...
        self._xkb_warn_degraded(warnings)  # ... the client, every time

    def _layout(self, warnings=None, forced=None, group=None):
        """The reverse map for the compositor's ACTIVE layout, or None when the fixed US table is the right
        answer (B13).

        `group` is the client's WDOTOOL_XKB_GROUP, carried on the request (see `_xkb_group`): it outranks the
        daemon's own environment for this one command, so the pin the group notice tells people to set works
        against a daemon that is already running rather than only against the next one.

        None is returned for the US bypass *and* for every failure: no compositor, an unreadable or unparsable
        keymap, a keymap with no typable character. The old path is the floor, never a traceback.
        """
        mode = xkbmap.layout_mode(forced)
        if mode == "us":
            # Nothing is read, nothing is parsed and the bypass check itself
            # is skipped: --layout us is a promise that no layout code runs.
            return None
        now = time.monotonic()
        if now < self._xkb_backoff:
            self._xkb_warn_degraded(warnings)
            return None
        try:
            snap = xkbmap.fetch(mods_wait=self._xkb_mods_wait, group=group)
        except xkbmap.XkbError as e:
            self._xkb_backoff = now + 5.0
            self._xkb_fell_back(f"cannot read the compositor's keymap ({e})", "read", warnings)
            return None
        except Exception as e:  # a bug in the new code must not break typing
            self._xkb_backoff = now + 60.0
            self._xkb_fell_back(f"keymap read failed ({e!r})", "read", warnings)
            return None
        if not snap.mods_seen:
            # This compositor does not send wl_keyboard.modifiers to an unfocused client, and never will: stop
            # paying for the wait. (Keying this off `group_known` instead left the wait switched on for ever on
            # the commonest GNOME session there is, whose two groups are the same `us` twice -- +87 ms on every
            # command, B5.)
            self._xkb_mods_wait = 0.0
        key = (hashlib.sha256(snap.text.encode("utf-8", "replace")).digest(),
               snap.group, snap.group_known)
        if self._layout_cache is not None and self._layout_cache[0] == key:
            self._xkb_warn_degraded(warnings)
            self._xkb_warn_group(key, warnings)
            return self._layout_cache[1]
        self._xkb_degraded = None
        rmap = None
        bypassed = False
        try:
            if xkbmap.decide(snap.text, snap.group, mode):
                bypassed = True  # THE BYPASS: nothing below this line runs
            else:
                rmap = xkbmap.build(snap.text, snap.group)
        except xkbmap.XkbError as e:
            rmap = None
            self._xkb_fell_back(f"cannot use the compositor's keymap ({e})", "build", warnings)
        except Exception as e:
            rmap = None
            self._xkb_fell_back(f"keymap conversion failed ({e!r})", "build", warnings)
        try:
            if not snap.group_known and (bypassed or rmap is not None):
                # Which layout is active was a guess. Say so on the bypass path too: a `us,de` session sitting
                # on its German group is precisely the one that types the wrong characters, and it is the bypass
                # that takes it (B1). Once per layout state, so a switch is announced again.
                #
                # It is a guess in ever fewer places: sway puts the group on the wire, KWin answers for it on
                # the session bus (xkbmap.kwin_group) and GNOME answers through the desktop portal
                # (xkbmap.gnome_group), so what is left here is any session that will neither send nor be
                # asked. Where wdotool knows, it says nothing.
                name = rmap.name if rmap is not None else xkbmap.group_name(snap.text, snap.group)
                self._xkb_say_group(
                    key,
                    "the compositor does not say which keyboard layout is "
                    "active (it sends that only to the focused window); "
                    f"assuming '{name}'. Set WDOTOOL_XKB_GROUP=<n> to "
                    "pin one.", warnings)
        except Exception:
            pass  # a notice must never be the thing that breaks typing
        self._layout_cache = (key, rmap)
        return rmap

    def _mod_keycodes(self, mask: int, layout) -> list:
        """The modifier keys to hold for one entry's mask."""
        if not mask:
            return []
        if layout is not None:
            return layout.modifier_keycodes(mask)
        return [keymap.KEY_LEFTSHIFT] if mask & xkbmap.MOD_SHIFT else []

    def _press(self, keys, delay, layout=None, dev=None):
        dev = self.kb if dev is None else dev
        for code, mask in keys:
            mask = int(mask)
            for mod in self._mod_keycodes(mask, layout):
                if mod in (keymap.KEY_LEFTSHIFT, keymap.KEY_RIGHTSHIFT):
                    if any(s in self.down for s in _SHIFTS):
                        continue
                elif mod in self.down:
                    continue
                dev.key(mod, True)
                self.down.add(mod)
            dev.key(code, True)
            self.down.add(code)
            self._key_gap(delay)

    def _release(self, keys, delay, layout=None, dev=None):
        dev = self.kb if dev is None else dev
        for code, mask in keys:
            mask = int(mask)
            for mod in self._mod_keycodes(mask, layout):
                if mod not in self.down:
                    continue
                dev.key(mod, False)
                self.down.discard(mod)
                self._released_mods.add(mod)
            dev.key(code, False)
            self.down.discard(code)
            self._released_mods.add(code)
            self._key_gap(delay)

    # -- modifiers around an injection (--clearmodifiers) ------------------
    #
    # xdotool's --clearmodifiers is "clear around the injection", not "clear
    # for good": X11 reads the modifier state, releases it, injects, and puts
    # back what was held. Through uinput that is possible for exactly the
    # modifiers *we* hold, and for no others. Two kernel facts decide it, both
    # measured on live GNOME, KDE and sway sessions:
    #
    #   * `input_handle_event()` drops an EV_KEY release for a code the
    #     emitting device does not have down. A key-up we send for a modifier
    #     the user holds on their own keyboard therefore produces no event at
    #     all -- not "the compositor ignored it": nothing reaches the wire. So
    #     a foreign modifier cannot be cleared from here.
    #   * a key-down we send *does* produce an event, and it stays ours until
    #     we release it. Mutter and KWin reference-count key state across the
    #     seat's devices, so the user letting go of the same modifier takes
    #     the count 2->1 and leaves it active -- for the rest of the session,
    #     because nothing else will ever send our release.
    #
    # Pressing back a modifier we did not hold is thus the worst of both: it
    # restores nothing (nothing was cleared) and it sticks. The restore set is
    # `self.down` -- what this daemon is holding -- sampled with no ioctl and
    # no re-read, so it cannot go stale: clear, inject and restore all happen
    # under one hold of the injection lock.
    #
    # The real keyboards are still read (`keystate.py`, EVIOCGKEY) for the
    # diagnostic: when that is permitted (root; logind's uaccess ACL covers
    # /dev/uinput, not the keyboards) and a foreign keyboard is holding a
    # modifier, say so once instead of letting a flag that cannot help look
    # like it did nothing. Clearing such a modifier for real would need the
    # device grabbed away from the compositor (EVIOCGRAB) -- a different tool.

    def _key_reader(self):
        """The evdev key-state reader used for the diagnostic, or None when we must not read: the
        WDOTOOL_NO_KEYSTATE override. Built once per set of uinput devices, so that our own event nodes -- which
        would answer with our own injection -- are known before the first read."""
        if self._reader is not _UNSET:
            return self._reader
        override = os.environ.get(NO_KEYSTATE_ENV, "")
        if override and override != "0":
            self._reader = None
        else:
            self._reader = keystate.Reader(exclude_paths=self._own_event_nodes())
        return self._reader

    def _own_event_nodes(self) -> set:
        """/dev/input paths of our own virtual devices (UI_GET_SYSNAME)."""
        out = set()
        for dev in (self.kb, self.mouse, self.tablet):
            name = ""
            if dev is not None:
                try:
                    name = dev.sysname()
                except AttributeError:   # a test double
                    name = ""
            if name:
                out.add(os.path.join(keystate.INPUT_DIR, name))
        return out

    def _clear_mods(self, warnings=None, session=None, dev=None, vkbd_path=False) -> set:
        """Release the modifier keys; return the ones to press back afterwards.

        That set is what *this daemon* holds. A modifier on another keyboard is neither released by the loop
        below (the kernel drops the key-up) nor safe to press back (it would stick), so it is reported and left
        alone. The loop still sends all eight: a key-up for a code we do not hold costs one write and is
        dropped, and spelling out "let go of every modifier" is what the flag means.

        On the virtual-keyboard path both halves of that are different, and better. The modifier state the
        compositor applies to our keys is the mask WE send and nothing else -- measured: with a real keyboard
        physically holding shift, uinput typed `Y` while the virtual keyboard typed `y` -- so there is nothing
        foreign to warn about, and one `modifiers(0,0,0,0)` says "no modifier is down" for certain. Only the
        keycodes we actually hold are released: with no kernel filter in front of it, a key-up for a key nobody
        pressed is a real event the compositor has to make sense of.
        """
        dev = self.kb if dev is None else dev
        ours = {c for c in keymap.MODIFIER_KEYCODES if c in self.down}
        if vkbd_path:
            for code in keymap.MODIFIER_KEYCODES:
                if code in self.down:
                    dev.key(code, False)
                    self.down.discard(code)
            dev.clear_modifiers()
            return ours
        self._warn_foreign_mods(warnings, session)
        for code in keymap.MODIFIER_KEYCODES:
            dev.key(code, False)
            self.down.discard(code)
        return ours

    def _restore_mods(self, held, dev=None):
        """Press back what _clear_mods() released -- the modifiers this daemon was already holding.

        They stay down afterwards, exactly as they were before the injection:
        `keydown ctrl; type --clearmodifiers x` ends with ctrl down, and the user's own `keyup ctrl` (or the
        daemon exiting, which destroys the device and releases its keys) still ends it. Nothing else is ever
        pressed here, which is half of what makes a stuck modifier impossible; the other half is in
        _mods_cleared(), which subtracts whatever the injection itself released (`keyup --clearmodifiers ctrl`
        asked for ctrl to be up, so it is not in the set we get)."""
        dev = self.kb if dev is None else dev
        for code in keymap.MODIFIER_KEYCODES:
            if code in held:
                dev.key(code, True)
                self.down.add(code)

    def _foreign_mods(self):
        """Modifiers held on a keyboard that is not ours, or None when the key state cannot be read at all (no
        permission -- the normal non-root case, in which nothing about the behaviour changes)."""
        reader = self._key_reader()
        if reader is None:
            return None
        try:
            return reader.held(keymap.MODIFIER_KEYCODES)
        except Exception:
            return None   # a diagnostic must never break an injection

    def _warn_foreign_mods(self, warnings, session):
        """One warning per client connection (`session`), so a chained
        command says it once and the next invocation says it again."""
        held = self._foreign_mods()
        if not held:
            return       # nothing held, or nothing readable: nothing to say
        msg = FOREIGN_MODS_WARNING % ", ".join(_MOD_LABELS[c] for c in keymap.MODIFIER_KEYCODES if c in held)
        if not self._keystate_logged:
            self._keystate_logged = True
            print(msg, file=sys.stderr, flush=True)
        if session is not None:
            if session.get("keystate_warned"):
                return
            session["keystate_warned"] = True
        if warnings is not None:
            warnings.append(msg)

    @contextlib.contextmanager
    def _mods_cleared(self, on, warnings, session, dev=None, vkbd_path=False, mode=None):
        """clear -> inject -> restore without letting go of the injection lock. The ops that carry `clearmods`
        themselves (`type`, `key`) do it inline; this is for the ones handle() wraps. Doing it as three requests
        instead would leave two gaps in which another wdotool process could inject with the modifiers down, or
        land its own injection between ours and the restore.

        `dev` is the sink the typing ops already chose. The pointer ops pass none and let the keyboard policy
        choose one, because the modifiers a click has to clear are wherever *we* are holding them and a key-up
        on one device releases nothing the other is holding. (Demanding /dev/uinput here, as this used to, would
        have made `click --clearmodifiers` the one pointer command that still needed root on a session where
        both protocols work.) A foreign modifier is still reported on that path: modifier state reaches the seat
        from the seat's keyboards, so a shift held on a real one rides our click whichever device sends it."""
        if not on:
            yield
            return
        ours = dev is None      # we chose the sink; we owe it a round trip
        if dev is None:
            try:
                dev, vkbd_path = self._keyboard(warnings, mode)
            except RuntimeError as e:
                # A pointer op on a session with a virtual pointer and no keyboard of either kind. There is
                # nothing to clear (a modifier we held on a kernel device went with the device, and one held on
                # a real keyboard was never ours to release), and a flag that can do nothing must not fail the
                # command it rides on.
                dev = None
                self._xkb_say(
                    "clearmods-nokbd",
                    "--clearmodifiers has no keyboard to release anything "
                    f"on ({e}); the pointer command goes ahead", warnings)
            else:
                if vkbd_path:
                    self._warn_foreign_mods(warnings, session)
        if dev is None:
            yield
            return
        held = self._clear_mods(warnings, session, dev, vkbd_path)
        self._released_mods = set()
        try:
            yield
        finally:
            # Whatever the op itself released stays released: `keyup --clearmodifiers ctrl` must not have ctrl
            # pressed back down afterwards. It would be stuck for the daemon's lifetime -- only the device
            # holding a key can release it, so the user's own keyboard could not clear it -- and the command
            # asked for the opposite of that.
            self._restore_mods(held - self._released_mods, dev)
            self._released_mods = set()
            if ours:
                # The typing ops flush after this block; the pointer ops do not inject on this sink at all, so
                # the clear/restore pair is the only thing that could fail on it and this is the only place that
                # would ever notice.
                self._flush(dev)

    def op_clear_modifiers(self, warnings=None, session=None) -> list:
        """The clear half on its own (DaemonClient.clear_modifiers, kept for the frozen API). Every wdotool
        command uses the `clearmods` flag on the injection op instead, which keeps the pair atomic.

        It goes through _keyboard() like the injection ops do, so it clears the modifiers on the device that is
        holding them and works wherever typing works at all -- demanding /dev/uinput for it would have left the
        frozen API as the one keyboard call that still needed root where the rest no longer does."""
        dev, vk = self._keyboard(warnings)
        held = sorted(self._clear_mods(warnings, session, dev, vk))
        self._flush(dev)   # a sink that acknowledges gets to say it failed
        return held

    def op_restore_modifiers(self, held):
        """The restore half; see op_clear_modifiers. `held` is validated to modifier keycodes by handle(), and
        _restore_mods presses nothing else, so a client cannot use this to hold down an arbitrary key."""
        dev, _vk = self._keyboard()
        self._restore_mods(held, dev)
        self._flush(dev)

    def _typing_layout(self, vkbd_path, warnings, layout_mode, xkb_group=None):
        """The character table for one typing op.

        On the virtual-keyboard path there is nothing to decide: the keymap that interprets our keycodes is the
        one we uploaded, so the built-in US table is right by construction and the compositor's keymap is
        neither read nor relevant -- the reverse map, the plain-US bypass and the group guess all belong to the
        kernel path and none of them runs here. `--layout us` asks for exactly what this path already does;
        `--layout xkb` asks for a table built from the *session's* keymap, which would type through the wrong
        one, so it is refused in one line rather than obeyed into garbage."""
        if not vkbd_path:
            return self._layout(warnings, layout_mode, xkb_group)
        if xkbmap.layout_mode(layout_mode) == "xkb":
            self._xkb_say(
                "vkbd-xkb",
                "layout 'xkb' does not apply to the virtual-keyboard path: "
                "the keymap that reads these keycodes is the one wdotool "
                "uploaded, not the session's. Typing with the built-in US "
                "table. Use --vkbd off to inject through /dev/uinput and the "
                "session's keymap instead.", warnings)
        return None

    def op_key(self, spec, direction, delay_ms, clearmods, session=None,
               layout_mode=None, vkbd_mode=None, warnings=None, xkb_group=None):
        # The caller's list when it passed one: these ops can raise after _keyboard() has already let go of a
        # hold (see handle()), and a warnings list local to this frame takes that line down with it.
        if warnings is None:
            warnings = []
        dev, vk = self._keyboard(warnings, vkbd_mode)
        # Outside the clear/restore window: reading the compositor's keymap is a query, and holding the
        # modifiers released across it buys nothing.
        layout = self._typing_layout(vk, warnings, layout_mode, xkb_group)
        # Restore even when the sequence is rejected or the injection fails:
        # the modifiers are already released by then.
        with self._mods_cleared(clearmods, warnings, session, dev, vk):
            # ValueError on a sequence xdo rejects outright
            keys, warns = keymap.parse_keyseq(spec, layout)
            d = delay_ms / 1000
            if direction == "press":
                # xdo_send_keysequence_window converts the sequence once per pass (press, then release), so
                # every "(symbol) No such key name" diagnostic is printed twice by the real xdotool (B12). Our
                # own one-shot layout notice is not one of xdo's and is not doubled.
                warns = warns * 2
                self._press(keys, d / 2, layout, dev)
                self._release(keys, d / 2, layout, dev)
            elif direction == "down":
                self._press(keys, d, layout, dev)
            elif direction == "up":
                self._release(keys, d, layout, dev)
            else:
                raise RuntimeError(f"invalid key direction {direction!r}")
        self._flush(dev)
        return warnings + warns

    def op_type(self, text, delay_ms, clearmods, session=None,
                layout_mode=None, vkbd_mode=None, warnings=None, xkb_group=None):
        # The caller's list when it passed one: these ops can raise after _keyboard() has already let go of a
        # hold (see handle()), and a warnings list local to this frame takes that line down with it.
        if warnings is None:
            warnings = []
        dev, vk = self._keyboard(warnings, vkbd_mode)
        layout = self._typing_layout(vk, warnings, layout_mode, xkb_group)  # see op_key
        lname = keymap.layout_name(layout)
        with self._mods_cleared(clearmods, warnings, session, dev, vk):
            # xdo_enter_text_window: delay split between down and up, down capped at 50ms
            down_d = min(delay_ms / 2, 50) / 1000
            up_d = delay_ms / 1000 - down_d
            for ch in text:
                if layout is None:
                    hit = keymap.char_to_key(ch)
                    seq = None if hit is None else [(hit[0], int(hit[1]))]
                else:
                    # One character can be two keystrokes: a dead key and then
                    # the base letter, which is how a French keyboard types "ô".
                    seq = layout.lookup_char(ch)
                if not seq:
                    warnings.append(f"Can't type character '{ch}' (not on the {lname} layout). Skipping.")
                    continue
                for code, mask in seq:
                    mods = [m for m in self._mod_keycodes(mask, layout)
                            if not (m in (keymap.KEY_LEFTSHIFT, keymap.KEY_RIGHTSHIFT)
                                    and any(s in self.down for s in _SHIFTS))
                            and m not in self.down]
                    for mod in mods:
                        dev.key(mod, True)
                    dev.key(code, True)
                    if down_d > 0:
                        time.sleep(down_d)
                    dev.key(code, False)
                    for mod in reversed(mods):
                        dev.key(mod, False)
                    self._key_gap(up_d)
        self._flush(dev)
        return warnings

    # -- protocol ----------------------------------------------------------

    def handle(self, req, session=None, warnings=None) -> dict:
        """One request. `session` is the per-connection scratch dict (a warning that must be said once per
        command belongs there, not in the daemon: the daemon outlives every client).

        `warnings` is the caller's own list, for the case where this RAISES: a command that fails can still have
        changed something the user has to know about -- letting go of a button or a key it was holding on a sink
        the command asked to switch away from (_release_named_sink) -- and an error reply that dropped those
        lines said nothing at all about it. serve_client() passes one in and puts what is in it on the error
        response."""
        if not isinstance(req, dict):
            return {"ok": False, "error": f"invalid request: {req!r} (expected an object)"}
        if session is None:
            session = {}
        op = req.get("op")
        if warnings is None:
            warnings = []
        with self.lock:
            if op == "type":
                with self._vk_guard():
                    warnings = self.op_type(
                        _text(req.get("text"), "text"),
                        _num(req.get("delay_ms", 12), "delay_ms", 0, MAX_DELAY_MS),
                        req.get("clearmods", False), session,
                        _layout_mode(req.get("layout_mode")),
                        _vkbd_mode(req.get("vkbd_mode")),
                        warnings=warnings,
                        xkb_group=_xkb_group(req.get("xkb_group")))
            elif op == "key":
                with self._vk_guard():
                    warnings = self.op_key(
                        _text(req.get("spec"), "spec"),
                        req.get("direction", "press"),
                        _num(req.get("delay_ms", 12), "delay_ms", 0, MAX_DELAY_MS),
                        req.get("clearmods", False), session,
                        _layout_mode(req.get("layout_mode")),
                        _vkbd_mode(req.get("vkbd_mode")),
                        warnings=warnings,
                        xkb_group=_xkb_group(req.get("xkb_group")))
            elif op == "clear_modifiers":
                with self._vk_guard():
                    held = self.op_clear_modifiers(warnings, session)
                return {"ok": True, "held": held, "warnings": warnings}
            elif op == "restore_modifiers":
                with self._vk_guard():
                    self.op_restore_modifiers(_mods(req.get("held"), "held"))
            elif op == "mousemove_abs":
                mode = _vkbd_mode(req.get("vkbd_mode"))
                with self._vp_guard(), self._mods_cleared(
                        req.get("clearmods", False), warnings, session,
                        mode=mode):
                    self.op_mousemove_abs(_num(req.get("x"), "x", _I32_MIN, _I32_MAX),
                                          _num(req.get("y"), "y", _I32_MIN, _I32_MAX),
                                          warnings, mode)
            elif op == "mousemove_rel":
                mode = _vkbd_mode(req.get("vkbd_mode"))
                with self._vp_guard(), self._mods_cleared(
                        req.get("clearmods", False), warnings, session,
                        mode=mode):
                    self.op_mousemove_rel(_num(req.get("dx"), "dx", _I32_MIN, _I32_MAX),
                                          _num(req.get("dy"), "dy", _I32_MIN, _I32_MAX),
                                          warnings, mode)
            elif op == "button":
                mode = _vkbd_mode(req.get("vkbd_mode"))
                with self._vp_guard(), self._mods_cleared(
                        req.get("clearmods", False), warnings, session,
                        mode=mode):
                    self.op_button(_num(req.get("btn"), "button", 0, 255),
                                   bool(req.get("down")), warnings, mode)
            elif op == "click":
                mode = _vkbd_mode(req.get("vkbd_mode"))
                with self._vp_guard(), self._mods_cleared(
                        req.get("clearmods", False), warnings, session,
                        mode=mode):
                    self.op_click(_num(req.get("btn"), "button", 0, 255),
                                  _num(req.get("repeat", 1), "repeat", 0, MAX_REPEAT),
                                  _num(req.get("delay_ms", 100), "delay_ms", 0, MAX_DELAY_MS),
                                  warnings, mode)
            elif op == "seed_pointer":
                self.op_seed_pointer(_num(req.get("x"), "x", _I32_MIN, _I32_MAX),
                                     _num(req.get("y"), "y", _I32_MIN, _I32_MAX),
                                     warnings)
            elif op == "pointer":
                # B6: never answer "0,0" for a daemon that has no pointer.
                if not self.pos_known:
                    self._no_pointer_yet()
                return {"ok": True, "x": self.px, "y": self.py, "known": self.pos_known}
            elif op == "geometry":
                # `quiet`: a query, not a pointer move -- see geometry().
                gx, gy, w, h = self.geometry(warnings, bool(req.get("quiet")))
                return {"ok": True, "x": gx, "y": gy, "w": w, "h": h,
                        "fallback": self.geom_fallback, "warnings": warnings}
            elif op == "ping":
                return {"ok": True, "pid": os.getpid()}
            else:
                return {"ok": False, "error": f"unknown op {op!r}"}
        return {"ok": True, "warnings": warnings}

    def serve_client(self, conn: socket.socket):
        # errors="replace": a request line that is not UTF-8 is a malformed request like any other, and gets the
        # malformed-request answer. A strict decode raised UnicodeDecodeError out of readline() below, where
        # only OSError is caught -- a traceback in the daemon log, the connection dropped, and EPIPE for the
        # client's next request.
        rfile = conn.makefile("r", encoding="utf-8", errors="replace")
        session: dict = {}   # per-connection state (see handle())
        try:
            while True:
                line = rfile.readline(_MAX_REQUEST + 1)
                if not line:
                    break
                if len(line) > _MAX_REQUEST and not line.endswith("\n"):
                    # Oversized request: drain the rest of the line so framing
                    # survives, then answer with an error.
                    while True:
                        chunk = rfile.readline(_MAX_REQUEST)
                        if not chunk or chunk.endswith("\n"):
                            break
                    resp = {"ok": False, "error": "request too large"}
                elif not line.strip():
                    continue
                else:
                    # Catch-all per-request boundary: a malformed request (bad JSON, wrong types, bare
                    # non-object) must produce an {"ok":false} reply, never kill the connection thread.
                    warnings: list[str] = []
                    try:
                        resp = self.handle(json.loads(line), session, warnings)
                    except Exception as e:
                        resp = {"ok": False, "error": str(e) or repr(e)}
                        # ...and whatever the failed command already changed (see handle()). Added only when
                        # there is something to say, so an ordinary error stays byte-identical.
                        if warnings:
                            resp["warnings"] = warnings
                conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass
        finally:
            try:
                rfile.close()
                conn.close()
            except OSError:
                pass


class _Clients:
    """Connected clients, counted, and when the last one left: what the idle timer reads.

    Its own lock, never the daemon's: every serve thread touches it and the accept loop reads it between
    accepts, and neither may end up waiting behind the injection lock -- one `type` of a long string holds that
    for as long as the typing takes, which is exactly when the accept loop has to stay responsive."""

    def __init__(self):
        self._lock = threading.Lock()
        self._live = 0
        self._left = time.monotonic()

    def opened(self):
        with self._lock:
            self._live += 1

    def closed(self):
        with self._lock:
            self._live -= 1
            self._left = time.monotonic()

    def quiet_for(self):
        """Seconds since the last client disconnected, or None while one is connected."""
        with self._lock:
            return None if self._live else time.monotonic() - self._left


def _serve(d: "_Daemon", conn: socket.socket, clients: _Clients):
    """One connection, plus the bookkeeping the idle timer reads. The count has to come down however
    serve_client() ends -- it catches OSError and nothing else, so an unexpected exception out of a handler
    would otherwise leave a daemon that can never be idle again."""
    try:
        d.serve_client(conn)
    finally:
        clients.closed()


def _exit_reason(path: str, ident, clients: _Clients, d: "_Daemon", idle: float):
    """Why this daemon should stop serving, or None to carry on. One stat(2) and a couple of comparisons, once
    per _CHECK_SECONDS.

    Neither check can end a daemon that is in use. A connected client -- one in the middle of a `type`, one
    that has not said anything yet -- is "in use" for both, so no exit ever cuts a request short or drops a
    connection somebody is holding; an unreachable daemon simply exits once its last client has gone. The
    liveness check goes first: a daemon nothing can dial is finished whatever its idle timer says.

    `d.down`/`d.btns` are read without the injection lock on purpose (see _Clients): a set's truthiness is one
    word, and the worst a lost race can cost is one more tick."""
    quiet = clients.quiet_for()
    if quiet is None:
        return None
    if ident is not None:
        now = _socket_ident(path)
        if now != ident:
            return ("%s is gone" % path if now is None
                    else "%s is a different socket now" % path)
    if idle and quiet >= idle and not d.down and not d.btns:
        # ...and not while holding something for the next command (`keydown ctrl`, `mousedown 1`): the exit
        # would release it, which is the one thing a later command cannot redo for itself.
        return "no client for %gs" % idle
    return None


def daemon_main() -> int:
    try:
        path = socket_path()
    except CmdError as e:
        print(f"wdotool daemon: {e}", file=sys.stderr, flush=True)
        return 1
    # Startup lock, held for the daemon's lifetime: losers of a concurrent spawn race must never unlink the
    # winner's freshly-bound socket. O_NOFOLLOW and a message rather than a traceback: the lock file lives
    # beside the socket, and a socket directory can be somebody else's.
    try:
        lock_fd = os.open(path + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        print(f"wdotool daemon: cannot open {path}.lock: {e}", file=sys.stderr, flush=True)
        return 1
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print(f"wdotool daemon already running/starting on {path}", file=sys.stderr, flush=True)
        return 0

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
        probe.close()
        os.close(lock_fd)
        print(f"wdotool daemon already running on {path}", file=sys.stderr, flush=True)
        return 0
    except OSError:
        probe.close()
        try:
            os.unlink(path)  # stale socket from a killed daemon
        except OSError:
            pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Owner-only socket regardless of the spawning client's umask: the root daemon serves root clients only (a
    # world-connectable root socket would be an input-injection privilege escalation). Non-root users get their
    # own per-user daemon, which fails with the clean "/dev/uinput ... run it as root" error. chmod after bind
    # closes the umask race belt-and-braces.
    old_umask = os.umask(0o177)
    try:
        srv.bind(path)
    except OSError as e:
        print(f"wdotool daemon: cannot bind {path}: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    srv.listen(16)
    print(f"wdotool daemon (pid {os.getpid()}) listening on {path}", flush=True)

    # What we bound, by inode: the socket file can be deleted or replaced under a running daemon, and the
    # accept loop has no other way to find out (see "how long a daemon lives").
    ident = _socket_ident(path)

    d = _Daemon()
    d.create_devices()
    clients = _Clients()
    idle = _lifetime_seconds(IDLE_ENV, _IDLE_SECONDS)
    check = _tick_seconds(idle, _lifetime_seconds(CHECK_ENV, _CHECK_SECONDS))
    # A timeout on the *listening* socket, not a second thread: accept() still blocks in the kernel between
    # ticks, so an idle daemon costs one wakeup per `check` seconds and no CPU in between. The accepted
    # connection comes back blocking regardless -- socket.accept() restores that when the listener has a
    # timeout -- so nothing about serving a client changes. check == 0: the bare blocking accept, as before.
    srv.settimeout(check or None)
    reason = None
    try:
        while True:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                reason = _exit_reason(path, ident, clients, d, idle)
                if reason:
                    break
                continue
            clients.opened()
            threading.Thread(target=_serve, args=(d, conn, clients), daemon=True).start()
    finally:
        # Our own socket, or nothing: by now the path may be a *second* daemon's (one of the two things the
        # liveness check catches), and the startup-lock rule above -- a loser never unlinks -- would be worth
        # little if the exit path took the winner's socket away instead.
        if _socket_ident(path) == ident:
            try:
                os.unlink(path)
            except OSError:
                pass
        for dev in (d.kb, d.mouse, d.tablet):
            if dev is not None:
                dev.close()
        if reason:
            print(f"wdotool daemon (pid {os.getpid()}) exiting: {reason}", flush=True)
    return 0


class DaemonClient:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._rfile = sock.makefile("r", encoding="utf-8")

    @classmethod
    def connect_or_spawn(cls) -> "DaemonClient":
        path = socket_path()
        sock = cls._try_connect(path)
        if sock is None:
            # A stale socket file is handled by the daemon itself (it unlinks under the startup lock); unlinking
            # here would race a daemon that just bound the path.
            cls._spawn()
            deadline = time.monotonic() + 2.0
            while sock is None and time.monotonic() < deadline:
                time.sleep(0.05)
                sock = cls._try_connect(path)
            if sock is None:
                raise CmdError(f"cannot start wdotool daemon (see {LOG_PATH})")
        return cls(sock)

    @staticmethod
    def _peer_uid(sock):
        """uid at the other end of a connected AF_UNIX socket, or None when
        the kernel will not say (never on Linux)."""
        try:
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", data)[1]
        except (OSError, AttributeError, struct.error):
            return None

    @staticmethod
    def _try_connect(path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            return None
        # Who answered? Only a process running as us may: a socket path in a shared directory can be pre-bound
        # by another local user, and what we send down it is the text of `type` and the geometry a later click
        # is computed from. Say so and stop, rather than typing a password into somebody else's process.
        uid = DaemonClient._peer_uid(sock)
        if uid is not None and uid != os.geteuid():
            sock.close()
            raise CmdError(
                f"{path} belongs to uid {uid}, not to us: refusing to send "
                "input requests to another user's process")
        return sock

    @staticmethod
    def _exec_plan() -> "list[tuple[str, list[str], dict]]":
        """(path, argv, env) candidates for re-execing as the daemon, best first (B10). Re-execing is what gives
        the daemon a clean argv (`ps` showed the *client's* command line before), a fresh address space and no
        inherited state; `wdotool __daemon` / `python -m wdotool __daemon` are both predictable and both match
        `pkill -f __daemon`. Paths are resolved before the caller chdir()s to /."""
        env = clean_env()
        plan = []
        exe = sys.argv[0] if sys.argv else ""
        if exe:
            exe = os.path.abspath(exe)
        if (os.path.basename(exe) in ("wdotool", "xdotool")
                and os.path.isfile(exe) and os.access(exe, os.X_OK)):
            plan.append((exe, [exe, "__daemon"], dict(env)))
        if sys.executable:
            menv = dict(env)
            try:
                import wdotool as _pkg
                # The parent of the package directory; for a zipapp this is
                # the .pyz itself, which is a valid PYTHONPATH entry too.
                parent = os.path.dirname(os.path.dirname(os.path.abspath(_pkg.__file__)))
            except Exception:  # diagnostics only
                parent = ""
            if parent:
                menv["PYTHONPATH"] = parent
            plan.append((sys.executable, [sys.executable, "-m", "wdotool", "__daemon"], menv))
        return plan

    @staticmethod
    def _spawn():
        """Daemonize: fork, setsid, fork; the grandchild redirects stdio to the daemon log, closes every
        inherited fd, leaves the launcher's transient scope, drops the client's environment and cwd, and
        re-execs as `wdotool __daemon` (or `python -m wdotool __daemon`); if no re-exec is possible it runs
        daemon_main() in-process with the same clean state."""
        pid = os.fork()
        if pid:
            os.waitpid(pid, 0)
            return
        code = 1
        try:
            os.setsid()
            if os.fork():
                os._exit(0)  # session leader exits; grandchild is the daemon
            sys.stdout.flush()
            sys.stderr.flush()
            plan = DaemonClient._exec_plan()  # resolves paths before chdir
            null = os.open(os.devnull, os.O_RDONLY)
            try:
                # O_NOFOLLOW: never append through a planted symlink in /tmp -- and never into a regular file
                # somebody else made there either (the log carries session diagnostics).
                log = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
                if os.fstat(log).st_uid != os.geteuid():
                    os.close(log)
                    raise OSError("log file is not ours")
            except OSError:
                log = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            os.close(null)
            os.close(log)
            _close_inherited_fds()
            _escape_transient_scope()
            try:
                os.chdir("/")  # never hold the client's cwd busy
            except OSError:
                pass
            for path, argv, env in plan:
                try:
                    os.execve(path, argv, env)
                except OSError:
                    continue  # try the next plan, else run in-process
            os.environ.clear()
            os.environ.update(clean_env(dict(plan[0][2]) if plan else None))
            code = daemon_main()
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(code)

    def close(self):
        try:
            self._rfile.close()
            self._sock.close()
        except OSError:
            pass

    def _rpc(self, **req):
        try:
            self._sock.sendall((json.dumps(req) + "\n").encode())
            line = self._rfile.readline()
        except OSError as e:
            raise CmdError(f"wdotool daemon connection lost: {e}") from None
        if not line:
            raise CmdError("wdotool daemon connection lost")
        try:
            resp = json.loads(line)
        except ValueError:
            raise CmdError("wdotool daemon sent an invalid reply") from None
        if not isinstance(resp, dict):
            raise CmdError("wdotool daemon sent an invalid reply")
        for warning in resp.get("warnings") or []:
            print(warning, file=sys.stderr)
        if not resp.get("ok"):
            raise CmdError(resp.get("error", "wdotool daemon error"))
        return resp

    @staticmethod
    def _pin() -> dict:
        """`xkb_group=` for a typing request: the client's own WDOTOOL_XKB_GROUP, sent only when it is set.

        The daemon keeps the environment it was *spawned* with and outlives the command that spawned it, so the
        pin the group notice asks for used to reach a fresh daemon and be ignored by a running one -- the
        command printed the notice again and typed the other layout's characters. Like `--layout`, the value
        travels with the request; an unset variable leaves the request byte-identical to what an older client
        sent."""
        raw = (os.environ.get("WDOTOOL_XKB_GROUP") or "").strip()
        return {"xkb_group": raw} if raw else {}

    @staticmethod
    def _modes(layout_mode, vkbd_mode) -> dict:
        """The optional mode fields, sent only when they were given: an absent flag must leave the request
        byte-identical to what an older client sent, and every test double keeps its signature."""
        extra = {}
        if layout_mode:
            extra["layout_mode"] = layout_mode
        if vkbd_mode:
            extra["vkbd_mode"] = vkbd_mode
        return extra

    def type_text(self, text: str, delay_ms: int, clearmods: bool = False,
                  layout_mode: str | None = None, vkbd_mode: str | None = None):
        self._rpc(op="type", text=text, delay_ms=delay_ms, clearmods=clearmods,
                  **self._modes(layout_mode, vkbd_mode), **self._pin())

    def key(self, spec: str, direction: str, delay_ms: int, clearmods: bool,
            layout_mode: str | None = None, vkbd_mode: str | None = None):
        self._rpc(op="key", spec=spec, direction=direction, delay_ms=delay_ms,
                  clearmods=clearmods, **self._modes(layout_mode, vkbd_mode),
                  **self._pin())

    def clear_modifiers(self) -> list:
        """Release the modifier keys and report which ones wdotool itself was holding, to be handed back to
        restore_modifiers() when the injection is done. Kept for the frozen API: every command passes
        `clearmods` to the injection op instead, so that the daemon can do clear, inject and restore under one
        lock -- as two extra round trips this pair leaves gaps another process can inject into."""
        return list(self._rpc(op="clear_modifiers").get("held") or [])

    def restore_modifiers(self, held):
        """Press back what clear_modifiers() released. No round trip when
        there is nothing to restore."""
        if not held:
            return
        self._rpc(op="restore_modifiers", held=list(held))

    # `clearmods` on these is the whole --clearmodifiers dance in one request: the daemon releases the
    # modifiers, injects and puts back what it was holding without letting go of the injection lock.
    #
    # `vkbd_mode` is --vkbd, which selects the pointer path as well as the keyboard one; sent only when the flag
    # was given, so an absent flag leaves the request byte-identical to what an older client sent.
    def mousemove_abs(self, x: int, y: int, clearmods: bool = False, vkbd_mode: str | None = None):
        self._rpc(op="mousemove_abs", x=x, y=y, clearmods=clearmods, **self._modes(None, vkbd_mode))

    def mousemove_rel(self, dx: int, dy: int, clearmods: bool = False, vkbd_mode: str | None = None):
        self._rpc(op="mousemove_rel", dx=dx, dy=dy, clearmods=clearmods, **self._modes(None, vkbd_mode))

    def button(self, btn: int, down: bool, clearmods: bool = False, vkbd_mode: str | None = None):
        self._rpc(op="button", btn=btn, down=down, clearmods=clearmods, **self._modes(None, vkbd_mode))

    def click(self, btn: int, repeat: int, delay_ms: int,
              clearmods: bool = False, vkbd_mode: str | None = None):
        self._rpc(op="click", btn=btn, repeat=repeat, delay_ms=delay_ms,
                  clearmods=clearmods, **self._modes(None, vkbd_mode))

    def pointer(self) -> tuple[int, int, bool]:
        """(x, y, known). `known` is False when the numbers are the tablet's own axis state rather than a
        position anything established -- a daemon that has opened /dev/uinput but injected no motion answers
        0,0 with known false, and the caller decides whether 0,0 is an answer. `getmouselocation` says it is
        not (docs/WDOTOOL.md: it "refuses with that reason rather than guessing when wdotool has not moved
        it"), while a relative move, which needs no origin, shrugs and moves anyway."""
        resp = self._rpc(op="pointer")
        return (resp["x"], resp["y"], bool(resp.get("known", True)))

    def seed_pointer(self, x: int, y: int):
        """Tell the daemon where the compositor's pointer really is (B6)."""
        self._rpc(op="seed_pointer", x=x, y=y)

    def geometry(self) -> tuple[int, int]:
        resp = self._rpc(op="geometry")
        return (resp["w"], resp["h"])

    def geometry_status(self) -> tuple[int, int, bool]:
        """(w, h, fallback): `fallback` is True when the compositor could not
        be asked and the numbers are the built-in guess (B5).

        `quiet=True`: this caller prints its own line about the guess and must
        not have the daemon's pointer-map one printed over it."""
        resp = self._rpc(op="geometry", quiet=True)
        return (resp["w"], resp["h"], bool(resp.get("fallback")))

    def geometry_full(self) -> tuple[int, int, int, int]:
        """(min_x, min_y, w, h) of the output layout — the origin matters on
        multi-output layouts with non-zero/negative origins."""
        resp = self._rpc(op="geometry")
        return (resp.get("x", 0), resp.get("y", 0), resp["w"], resp["h"])
