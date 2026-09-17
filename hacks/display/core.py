"""Layout model + pending-layout resolver + the two wlroots apply backends +
state file + query rendering. The shared half of wxrandr: `mutter.py` and
`kwin.py` are the other two backends and are built on what is here.

Byte-parity target: xrandr 1.5.4. wxrandr talks to the compositor directly, over
four backends that are one object shape (snapshot/predicted_dims/verify/apply/
close/name), of which two live in this file:

- sway/i3 IPC (GET_OUTPUTS for state, `output ...` commands for mutation, all
  mutations batched into single RUN_COMMAND messages),
- generic wlroots: zwlr_output_management_unstable_v1 over w11common.wayland_mini
  — one atomic configuration apply, which is exactly xrandr's model,
- and, in their own modules, Mutter's DisplayConfig (`mutter.py`) and KWin's
  kde_output_management_v2 (`kwin.py`).

`WXRANDR_BACKEND=sway|wlr|mutter|kwin|x11` picks one, `--backend` beats it, and
detection is the default: sway when an IPC socket exists, else whichever
protocol or bus name the session advertises.
- physical sizes / preferred-mode flags come from the zwlr head events on
  either wlroots backend (headless outputs report 0mm x 0mm, like the oracle).

Transform mapping (verified against XWayland's own RandR translation, which is
the interop ground truth — see tests/test_wxrandr_unit.py):
    sway 90  == RandR right   (90 degrees clockwise)
    sway 270 == RandR left
    sway 180 == RandR inverted
    sway flipped-N == RandR (rotation-of-N, reflect X)
RandR applies reflection first, then rotation; the 16 (rotation, reflection)
combinations collapse onto the 8 Wayland transforms below.

xrandr concepts with no Wayland analog (primary, user mode lines) persist in a
small state file keyed by compositor socket:
    $XDG_RUNTIME_DIR/wxrandr-state.json  (else, under sudo or cron, the same
    name in the private /tmp/wdotool-<uid> that session.runtime_dir() makes)
"""

import copy
import dataclasses
import fcntl
import json
import math
import os
import re
import socket
import stat
import struct
import sys
import time

from w11common import procs, session
from w11common.errors import CmdError

PROGRAM_VERSION = "1.5.4"

# Screen constants: adopt XWayland's advertised limits (oracle captures).
MIN_WIDTH = MIN_HEIGHT = 16
MAX_WIDTH = MAX_HEIGHT = 32767


class Fatal(Exception):
    """xrandr's fatal(): stderr `xrandr: <msg>`, exit 1."""


class ArgErr(Exception):
    """xrandr's argerr(): stderr `xrandr: <msg>` + Try-help line, exit 1."""


def warn(msg: str):
    """xrandr's warning(): stderr `xrandr: <msg>`, execution continues."""
    sys.stderr.write("xrandr: " + msg)


def warn_bare(msg: str):
    """The one warning xrandr prints without its prefix (xrandr.c:1849)."""
    sys.stderr.write(msg)


# -- transforms ---------------------------------------------------------------

ROTATIONS = ("normal", "left", "inverted", "right")
REFLECTIONS = ("normal", "x", "y", "xy")

# rotation word -> the sway transform that XWayland reports as that rotation
_ROT_TO_SWAY = {"normal": "normal", "right": "90", "inverted": "180", "left": "270"}
# rotation composed with reflect-X (RandR: reflect first, then rotate)
_ROT_X_TO_SWAY = {"normal": "flipped", "right": "flipped-90",
                  "inverted": "flipped-180", "left": "flipped-270"}
_ROT_ORDER = ("normal", "right", "inverted", "left")  # +90deg CW steps


def _rot_add(rot: str, quarter_turns: int) -> str:
    return _ROT_ORDER[(_ROT_ORDER.index(rot) + quarter_turns) % 4]


def sway_transform(rotation: str, reflection: str) -> str:
    """The sway/wl_output transform matching RandR (rotation, reflection)."""
    if reflection == "normal":
        return _ROT_TO_SWAY[rotation]
    if reflection == "x":
        return _ROT_X_TO_SWAY[rotation]
    if reflection == "y":  # reflectY == rotate180 . reflectX
        return _ROT_X_TO_SWAY[_rot_add(rotation, 2)]
    # xy == rotate180
    return _ROT_TO_SWAY[_rot_add(rotation, 2)]


# canonical RandR view of each sway transform — exactly what real xrandr shows
# through XWayland for the same compositor state (verified live).
RANDR_VIEW = {
    "normal": ("normal", "normal"), "90": ("right", "normal"),
    "180": ("inverted", "normal"), "270": ("left", "normal"),
    "flipped": ("normal", "x"), "flipped-90": ("right", "x"),
    "flipped-180": ("inverted", "x"), "flipped-270": ("left", "x"),
}

# wl_output.transform enum (for the wlr backend wire protocol)
WL_TRANSFORM = {"normal": 0, "90": 1, "180": 2, "270": 3, "flipped": 4,
                "flipped-90": 5, "flipped-180": 6, "flipped-270": 7}
WL_TRANSFORM_NAME = {v: k for k, v in WL_TRANSFORM.items()}

# The same enum read the way the spec's counter-clockwise 90 implies, which is how both Mutter and KWin number
# transforms: libkscreen's toKScreenRotation and Xwayland's wl_transform_to_xrandr agree that 1 is xrandr `left`
# and 3 is `right`, where the sway table above has "90" == `right`. So the two numberings differ by a 90<->270
# swap (1<->3, 5<->7). The words below are what real xrandr prints through Mutter's XWayland for each of the
# eight (all eight measured on GNOME 50).
WL_SPEC_RANDR_VIEW = {0: ("normal", "normal"), 1: ("left", "normal"),
                      2: ("inverted", "normal"), 3: ("right", "normal"),
                      4: ("normal", "x"), 5: ("left", "x"),
                      6: ("inverted", "x"), 7: ("right", "x")}
SWAY_FROM_WL_SPEC = {n: next(tf for tf, v in RANDR_VIEW.items() if v == view)
                     for n, view in WL_SPEC_RANDR_VIEW.items()}
WL_SPEC_FROM_SWAY = {tf: n for n, tf in SWAY_FROM_WL_SPEC.items()}


def to_wl_spec_transform(sway_tf: str) -> int:
    """sway transform name (what RANDR_VIEW uses) -> the spec's number."""
    return WL_SPEC_FROM_SWAY.get(sway_tf, 0)


def from_wl_spec_transform(n: int) -> str:
    """The spec's transform number -> the sway name the renderer speaks."""
    return SWAY_FROM_WL_SPEC.get(n, "normal")

REFLECTION_SUFFIX = {"x": " X axis", "y": " Y axis", "xy": " X and Y axis"}


def transform_swaps(sway_tf: str) -> bool:
    return sway_tf in ("90", "270", "flipped-90", "flipped-270")


# -- mm / dpi math ------------------------------------------------------------

def synth_mm(px: int) -> int:
    """RandR-1.5 monitor mm as XWayland synthesizes them for mm-less outputs
    (round-half-even at 96dpi: 1280->339, 720->190 — matches the oracle)."""
    return round(px * 25.4 / 96.0)


def screen_mm(px: int) -> int:
    """X screen mm (dix formula, truncating): 1280->338, 720->190."""
    return px * 254 // 960


# -- modeline math ------------------------------------------------------------

MODE_FLAGS = ("+hsync", "-hsync", "+vsync", "-vsync", "+csync", "-csync", "csync", "interlace", "doublescan")


def mode_refresh_hz(clock_mhz: float, htotal: int, vtotal: int, flags=()) -> float:
    """xrandr.c:554 — dotClock/(hTotal*vTotal); DoubleScan doubles vTotal,
    Interlace halves it."""
    if not htotal or not vtotal:
        return 0.0
    v = vtotal
    lflags = [f.lower() for f in flags]
    if "doublescan" in lflags:
        v *= 2
    if "interlace" in lflags:
        v /= 2
    return clock_mhz * 1e6 / (htotal * v)


# -- data model ---------------------------------------------------------------

@dataclasses.dataclass
class Mode:
    w: int
    h: int
    refresh_mhz: int = 0          # 0 = unknown (headless)
    preferred: bool = False
    custom: bool = False          # user mode from --newmode
    name: str | None = None       # custom-mode name; real modes print WxH
    clock_mhz: float = 0.0        # custom modes carry the full modeline
    timings: tuple = ()           # (hss, hse, htot, vss, vse, vtot)
    flags: tuple = ()
    mode_id: str = ""             # compositor's opaque id (Mutter); "" = none

    @property
    def display_name(self) -> str:
        return self.name if self.name else "%dx%d" % (self.w, self.h)

    @property
    def refresh_hz(self) -> float:
        if self.clock_mhz and self.timings:
            # exact modeline math — the mHz round-trip would lose the second
            # decimal (74.5MHz/1664x748 is 59.8554: xrandr prints 59.86)
            return mode_refresh_hz(self.clock_mhz, self.timings[2], self.timings[5], self.flags)
        return self.refresh_mhz / 1000.0


@dataclasses.dataclass
class OutputState:
    name: str
    active: bool
    x: int = 0
    y: int = 0
    w: int = 0                    # logical (transformed, scaled) — sway rect
    h: int = 0
    scale: float = 1.0
    transform: str = "normal"     # sway transform string
    modes: list = dataclasses.field(default_factory=list)
    current: Mode | None = None
    ident: int = 0                # compositor id (verbose Identifier)
    subpixel: str = "unknown"
    mm_w: int = 0
    mm_h: int = 0
    make: str = "Unknown"
    model: str = "Unknown"
    serial: str = "Unknown"
    non_desktop: bool = False
    wlr_head: int | None = None   # protocol object id (wlr backend apply)
    virtual_modes: bool = False   # no compositor mode list (headless): any
    #                               WxH is achievable via a custom mode
    primary: bool = False         # the compositor's own primary flag (Mutter)

    def rect(self) -> tuple[int, int, int, int]:
        """The logical rectangle as (x, y, w, h), layout coordinates."""
        return (self.x, self.y, self.w, self.h)

    def geom(self) -> str:
        """The same rectangle as X11 geometry, `WxH+X+Y` -- the form --query
        prints it in, and the one `--fb`, `--pos` and slurp all speak."""
        return "%dx%d+%d+%d" % (self.w, self.h, self.x, self.y)


def layout_box(outputs) -> tuple[int, int, int, int]:
    """(min_x, min_y, max_x, max_y) over enabled outputs; zeros when none."""
    act = [o for o in outputs if o.active]
    if not act:
        return (0, 0, 0, 0)
    return (min(o.x for o in act), min(o.y for o in act),
            max(o.x + o.w for o in act), max(o.y + o.h for o in act))


# -- wlroots scale arithmetic -------------------------------------------------
#
# Two single-precision steps, and both of them matter.  sway quantises any scale it is given to 120ths --
# fractional-scale-v1's unit -- in float32 (sway 1.9 output.c: `scale = round(scale * 120) / 120`), and
# wlr_output_effective_resolution then divides the pixel size by that float and truncates.  Modelling either
# step in double gets real layouts wrong.
SCALE_STEPS = 120
WL_FIXED_UNIT = 256


def f32(x: float) -> float:
    """`x` at the width wlroots keeps a scale and does its division at."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def round_half_away(x: float) -> int:
    """C round(): halves go away from zero (Python's round() is banker's)."""
    r = int(math.floor(abs(x) + 0.5))
    return r if x >= 0 else -r


def wlr_scale(scale: float, wire: str = "text") -> float:
    """The scale a wlroots compositor really ends up running.

    What it quantises depends on how the number reached it.  The sway IPC takes it as text
    (`output NAME scale 1.03`, printed with %g and read back with strtof); zwlr_output_management takes a
    wl_fixed, and wayland_mini's marshaller truncates to 256ths on the way out.  So `--scale 1.03` runs as
    1.0333 on the sway backend and as 1.025 on the wlr one, and a predicted logical size has to know which it is
    being asked about -- placing the neighbour of a fractionally scaled output against the wrong one leaves a
    gap or an overlap of 1-10 px that nobody asked for."""
    if wire == "fixed":
        scale = int(scale * WL_FIXED_UNIT) / float(WL_FIXED_UNIT)
    else:
        scale = float("%g" % scale)
    return f32(round_half_away(f32(scale) * SCALE_STEPS) / SCALE_STEPS)


def logical_size(px_w: int, px_h: int, sway_tf: str, scale: float) -> tuple[int, int]:
    """sway/wlroots logical dimensions: the transform swap, then wlr_output_effective_resolution --
    `*width /= output->scale` with an int on the left and a C float on the right, so a single-precision division
    truncated back to an int (observed: 1111/1.5->740, 1281/2->640, 1280/1.5->853, and 1920/1.6->1200 where a
    double division says 1199).

    `scale` is what the compositor RUNS, not what was asked for: a prediction passes it through wlr_scale()
    first."""
    if transform_swaps(sway_tf):
        px_w, px_h = px_h, px_w
    return (int(f32(f32(px_w) / f32(scale))), int(f32(f32(px_h) / f32(scale))))


# -- sway IPC -----------------------------------------------------------------

_MAGIC = b"i3-ipc"
RUN_COMMAND = 0
GET_OUTPUTS = 3
GET_VERSION = 7

#: i3 has no `output` command at all: every apply died with a 30-token parse error listing every command i3
#: does have, and nothing about the layout changed [M recon2/i3.md §1, §2b, fixture run_output_pos.json]. The
#: X server owns the layout on i3, which is what the handover already hands `xrandr` for.
I3_NO_APPLY = ("this is i3, which has no output command; the X server owns the layout "
               "here -- use xrandr (or drop --backend sway)\n")


class SwayIPC:
    def __init__(self, sockpath: str | None = None):
        self._version = None            # GET_VERSION reply, cached by version()
        self.sockpath = sockpath or session.find_sway_socket()
        if not self.sockpath:
            raise Fatal("cannot connect to the compositor " "(no sway/i3 IPC socket found)\n")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(10.0)
            self.sock.connect(self.sockpath)
        except OSError as e:
            self.sock.close()
            raise Fatal("cannot connect to %s: %s\n" % (self.sockpath, e))

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise Fatal("compositor IPC connection closed\n")
            buf += chunk
        return buf

    def msg(self, mtype: int, payload=""):
        data = payload.encode() if isinstance(payload, str) else payload
        self.sock.sendall(_MAGIC + struct.pack("<II", len(data), mtype) + data)
        while True:
            hdr = self._read_exact(14)
            if hdr[:6] != _MAGIC:
                raise Fatal("bad compositor IPC framing\n")
            length, rtype = struct.unpack("<II", hdr[6:])
            body = self._read_exact(length) if length else b"null"
            if rtype == mtype:  # skip stray event frames
                return json.loads(body.decode("utf-8", "replace"))

    def version(self) -> dict:
        """The GET_VERSION reply, asked once per connection and cached ({} when it is not an object)."""
        if self._version is None:
            v = self.msg(GET_VERSION)
            self._version = v if isinstance(v, dict) else {}
        return self._version

    def dialect(self) -> str:
        """"i3" or "sway". sway is 1.x; i3 has been 4.x since 2011 and the live 4.25.1 answered `major` 4
        [M recon2/i3.md §1], so `major >= 4` is the whole test."""
        major = self.version().get("major")
        return "i3" if isinstance(major, int) and major >= 4 else "sway"

    def compositor_label(self) -> str:
        """What `--print-backend --verbose` calls the other end: `i3 4.25.1 (2026-02-06)`, `sway 1.11`.

        It printed `compositor: sway 4.25.1 (2026-02-06)` on the live i3 -- the version was right and the name
        was not [M recon2/i3.md §2b]."""
        human = self.version().get("human_readable")
        name = self.dialect()
        return "%s %s" % (name, human) if human else name

    def get_outputs(self) -> list:
        return self.msg(GET_OUTPUTS)

    def run(self, command: str):
        """RUN_COMMAND; raises Fatal on the first failed sub-command."""
        results = self.msg(RUN_COMMAND, command)
        for r in results:
            if not r.get("success"):
                raise Fatal("compositor rejected `%s`: %s\n" % (command, r.get("error", "unknown error")))
        return results

    def run_collect(self, command: str):
        """RUN_COMMAND without raising: sway runs every ';'-joined subcommand regardless of individual failures,
        so the caller can act on the ones that succeeded (e.g. still position outputs a partial phase-1
        configured) before reporting the failure."""
        return self.msg(RUN_COMMAND, command)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# -- state file ---------------------------------------------------------------

#: How long `State.save()` waits for the state lock before writing without it.  One second, because the wait
#: is on a file another local user can own: with no `$XDG_RUNTIME_DIR` -- every `sudo` run, and cron -- the
#: state lives in the private directory `session.runtime_dir()` makes or, when even that cannot be made ours,
#: in shared /tmp under a guessable name (`_state_path`), and a plain blocking `flock` there ends when the
#: holder says so, not when we do.  The file is a cache that is never load-bearing (`_read_state`), so
#: `hacks/mirror/core.py`'s `state_lock` bounds its own wait exactly this way, with the sentence this one
#: keeps: a lock we cannot get is not a reason to refuse.
LOCK_TIMEOUT = 1.0


def _state_path() -> str:
    """The state file in session.runtime_dir(). A layout cache is never worth failing a command for, so a
    runtime dir we cannot have degrades to the 0.2 name in shared /tmp -- where _read_state's checks below are
    what stand between us and a planted file."""
    try:
        return os.path.join(session.runtime_dir(), "wxrandr-state.json")
    except CmdError:
        return "/tmp/wxrandr-state-%d.json" % os.getuid()


def _read_state(path: str) -> dict:
    """The state dict on disk, or `{}` when it is not ours to trust.

    This file decides what wxrandr does next: which pid `--brightness` sends SIGTERM to when it drops a gamma
    hold, the mode lines `--newmode` added, which output is primary, what mode a re-enabled output goes back to.
    With no XDG_RUNTIME_DIR -- which is every `sudo` run, and cron -- it lives in the private directory
    session.runtime_dir() makes, or, when even that cannot be made ours, in world-writable /tmp under a
    guessable name, where another local user can create it before we do and choose those answers, including the
    pid a root wxrandr signals. The state is a cache and never load-bearing, so anything we cannot prove is ours
    (a symlink, another user's file, a file others may write) is ignored rather than obeyed. Group-writable is
    left alone: that is the default umask on some distributions, and a group is not the open door /tmp is."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o002):
            os.close(fd)
            return {}
        f = os.fdopen(fd, "r")
    except OSError:
        os.close(fd)
        return {}
    try:
        loaded = json.load(f)
    except (OSError, ValueError):
        return {}
    finally:
        f.close()
    return loaded if isinstance(loaded, dict) else {}


def _merge3(base: dict, ours: dict, theirs: dict) -> dict:
    """Three-way merge of one state key across a concurrent writer: `base` is the value we loaded, `ours` our
    in-memory edits, `theirs` what is on disk now (possibly another wxrandr's write). We start from `theirs` so
    a sibling's changes survive, then replay only the entries WE actually touched (added / changed / deleted) —
    so two parallel --brightness runs on different outputs keep both gamma holder records instead of
    clobbering."""
    result = dict(theirs)
    for k in set(base) | set(ours):
        if k not in ours:                       # we removed it
            result.pop(k, None)
        elif k not in base:                     # we first-wrote it
            # even a key we added may already exist on disk as a dict (two
            # procs both first-touching "gamma"): merge into it, don't clobber
            if isinstance(ours[k], dict) and isinstance(result.get(k), dict):
                result[k] = _merge3({}, ours[k], result[k])
            else:
                result[k] = ours[k]
        elif ours[k] != base[k]:                # we changed it
            if (isinstance(ours[k], dict) and isinstance(base[k], dict) and isinstance(result.get(k), dict)):
                result[k] = _merge3(base[k], ours[k], result[k])
            else:
                result[k] = ours[k]
        # else: untouched by us — keep the on-disk (their) value
    return result


class State:
    """Per-compositor persisted oddments: primary output, user mode lines (--newmode), mode->output attachments
    (--addmode), gamma holder pids, last known mode of outputs wxrandr turned off."""

    def __init__(self, key: str, path: str | None = None):
        self.path = path or _state_path()
        self.key = key
        self._all = _read_state(self.path)
        d = self._all.get(key)
        self.d = d if isinstance(d, dict) else {}
        # snapshot of what we loaded, so save() can tell OUR edits apart from
        # a concurrent writer's when it re-reads the file under the lock
        self._orig = copy.deepcopy(self.d)

    def save(self):
        lockpath = self.path + ".lock"
        lock_fd = None
        try:
            lock_fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            st = os.fstat(lock_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
                # A lock file that is not ours is not a lock: the 0o600 above applies only to a file WE
                # create, and in the /tmp fallback the name is guessable, so this can be another user's file,
                # planted before ours -- the same thing `_read_state` refuses to believe about the state file
                # itself.  Waiting on it would hand them the length of the wait.
                os.close(lock_fd)
                lock_fd = None
            else:
                deadline = time.monotonic() + LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            break       # a lock we cannot get is not a reason to refuse
                        time.sleep(0.05)
        except OSError:
            lock_fd = None  # locking unavailable: proceed best-effort
        try:
            # re-read under the lock and merge, so a concurrent wxrandr's writes (other compositor keys, or
            # another output's gamma record under this key) are not lost by our snapshot-then-replace
            disk = _read_state(self.path)
            theirs = disk.get(self.key)
            theirs = theirs if isinstance(theirs, dict) else {}
            merged = _merge3(self._orig, self.d, theirs)
            disk[self.key] = merged
            self._all = disk
            self.d = merged
            self._orig = copy.deepcopy(merged)
            tmp = "%s.%d.tmp" % (self.path, os.getpid())
            try:
                # O_EXCL|O_NOFOLLOW: the default state path is under /tmp when there is no XDG_RUNTIME_DIR, so
                # the name is guessable and the directory is shared.  A symlink planted there must not be
                # written through, and a leftover from a crashed run of ours (the name carries our pid) is
                # unlinked, not opened.
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
                try:
                    fd = os.open(tmp, flags, 0o600)
                except FileExistsError:
                    os.unlink(tmp)      # removes a symlink, not its target
                    fd = os.open(tmp, flags, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(disk, f)
                os.replace(tmp, self.path)
            except OSError as e:
                warn("cannot persist state to %s: %s\n" % (self.path, e))
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)

    # primary ----------------------------------------------------------------
    @property
    def primary(self) -> str | None:
        return self.d.get("primary")

    @primary.setter
    def primary(self, name):
        if name is None:
            self.d.pop("primary", None)
        else:
            self.d["primary"] = name

    # custom modes -----------------------------------------------------------
    def _container(self, key: str) -> dict:
        """One of the store's sub-dicts, coerced.  The state file is a plain JSON file, meant to be hand-edited
        and shared by every wxrandr in the session: a value of the wrong type used to survive setdefault() and
        come back as a str, an int or a list, whose next [] or .get() raises a TypeError somewhere else
        entirely.  __init__ already does exactly this for the top level."""
        d = self.d.get(key)
        if not isinstance(d, dict):
            d = self.d[key] = {}
        return d

    def modes(self) -> dict:
        return self._container("modes")

    def addmodes(self) -> dict:
        return self._container("addmode")

    def custom_mode(self, name: str) -> Mode | None:
        m = self.modes().get(name)
        if not m:
            return None
        try:
            clock = m["clock"]
            w, hss, hse, htot = m["h"]
            h, vss, vse, vtot = m["v"]
            flags = tuple(m.get("flags", ()))
        except (KeyError, ValueError, TypeError):
            return None  # corrupt / hand-edited entry: ignore, never crash
        hz = mode_refresh_hz(clock, htot, vtot, flags)
        return Mode(w=w, h=h, refresh_mhz=round(hz * 1000), custom=True,
                    name=name, clock_mhz=clock,
                    timings=(hss, hse, htot, vss, vse, vtot), flags=flags)

    def modes_for_output(self, output: str) -> list:
        out = []
        for name in self.addmodes().get(output, []):
            m = self.custom_mode(name)
            if m:
                out.append(m)
        return out

    # gamma holders ----------------------------------------------------------
    def gamma(self) -> dict:
        return self._container("gamma")

    # last known pixel mode of outputs wxrandr disabled ----------------------
    def lastmodes(self) -> dict:
        return self._container("lastmode")

    # heads a wlroots compositor stopped announcing after --off ---------------
    def offheads(self) -> dict:
        """{output: what it was, for a compositor that drops a head it is asked to turn off}.

        X's `xrandr` keeps a disabled output in `--query` and `--auto` turns it back on, and X is the
        oracle; two wlroots compositors here do not.  Measured: river 0.4.8 -- `wxrandr --output Virtual-3
        --off` works, `wlr-randr` then still says `Enabled: no`, and our next process sees no such head, so
        `--auto` answered `warning: output Virtual-3 not found; ignoring` with exit 0 [M
        goal2/recon/flavors.md §2]; cosmic-comp 1.7.0 loses it the same way (arch-cosmic `--auto` brought
        back 0 of 3 where 1.6.0 has no such problem) [M goal2/recon/flavors.md §5].  This is the keeping,
        which is ours; the compositor taking the head back is its own and is a documented gap in the step
        files."""
        return self._container("offhead")


# -- wlr-output-management snapshot + atomic apply ----------------------------

class WlrOutputs:
    """zwlr_output_management_unstable_v1 client over wayland_mini.

    Serves two roles: enrich queries (physical mm, preferred flags, make/model/serial — data sway IPC lacks) and
    apply whole-layout configurations atomically (the generic-wlroots backend)."""

    name = "wlroots"

    def __init__(self, conn=None):
        from w11common.wayland_mini import WlConn
        self.conn = conn
        self._own_conn = conn is None
        if self.conn is None:
            hit = session.find_wayland_socket()
            if hit is None:
                raise Fatal("cannot connect to the compositor " "(no wayland socket found)\n")
            self.conn = WlConn(hit[2])
            self.conn.sock.settimeout(10.0)
        g = self.conn.find_global("zwlr_output_manager_v1")
        if g is None:
            raise Fatal("compositor does not advertise " "zwlr_output_manager_v1\n")
        self.version = min(g[1], 4)
        self.serial = None
        self.heads = []  # dicts, server announce order
        self._mgr = self.conn.bind(g[0], "zwlr_output_manager_v1", self.version)
        self.conn.on(self._mgr, self._on_manager)
        self.conn.roundtrip()

    # -- events --------------------------------------------------------------

    def _on_manager(self, op, cur, fds):
        if op == 0:  # head(new_id)
            hid = cur.u32()
            head = {"id": hid, "name": "", "description": "",
                    "mm_w": 0, "mm_h": 0, "modes": [], "enabled": False,
                    "current": None, "x": 0, "y": 0, "transform": 0,
                    "scale": 1.0, "make": "Unknown", "model": "Unknown",
                    "serial": "Unknown", "gone": False}
            self.heads.append(head)
            self.conn.on(hid, lambda op, cur, fds, h=head: self._on_head(h, op, cur))
        elif op == 1:  # done(serial)
            self.serial = cur.u32()

    def _on_head(self, h, op, cur):
        if op == 0:
            h["name"] = cur.string()
        elif op == 1:
            h["description"] = cur.string()
        elif op == 2:
            h["mm_w"], h["mm_h"] = cur.i32(), cur.i32()
        elif op == 3:  # mode(new_id)
            mid = cur.u32()
            mode = {"id": mid, "w": 0, "h": 0, "refresh": 0, "preferred": False}
            h["modes"].append(mode)
            self.conn.on(mid, lambda op, cur, fds, m=mode: self._on_mode(m, op, cur))
        elif op == 4:
            h["enabled"] = bool(cur.i32())
        elif op == 5:  # current_mode(object id)
            h["current"] = cur.u32()
        elif op == 6:
            h["x"], h["y"] = cur.i32(), cur.i32()
        elif op == 7:
            h["transform"] = cur.i32()
        elif op == 8:
            h["scale"] = cur.fixed()
        elif op == 9:
            h["gone"] = True
        elif op == 10:
            h["make"] = cur.string()
        elif op == 11:
            h["model"] = cur.string()
        elif op == 12:
            h["serial"] = cur.string()

    @staticmethod
    def _on_mode(m, op, cur):
        if op == 0:
            m["w"], m["h"] = cur.i32(), cur.i32()
        elif op == 1:
            m["refresh"] = cur.i32()
        elif op == 2:
            m["preferred"] = True

    def live_heads(self) -> list:
        return [h for h in self.heads if not h["gone"]]

    def by_name(self, name: str) -> dict | None:
        for h in self.live_heads():
            if h["name"] == name:
                return h
        return None

    # -- the wire ------------------------------------------------------------

    def send(self, targets: dict, positions: dict):
        """One zwlr_output_configuration_v1: enable/disable + mode + position
        + transform + scale, applied atomically; waits for succeeded/failed/
        cancelled. The protocol demands EVERY head be configured, so `targets`
        must cover all live heads (unnamed ones are pinned to current state
        by disable/enable alone)."""
        conf = self.conn.alloc()
        result = []
        self.conn.on(conf, lambda op, cur, fds: result.append(("succeeded", "failed", "cancelled")[op]))
        self.conn.send(self._mgr, 0, [("u", conf), ("u", self.serial)])
        for head in self.live_heads():
            name = head["name"]
            t = targets.get(name)
            if t is None:
                # not part of the plan: keep as-is (still must be configured)
                if head["enabled"]:
                    ch = self.conn.alloc()
                    self.conn.send(conf, 0, [("u", ch), ("u", head["id"])])
                else:
                    self.conn.send(conf, 1, [("u", head["id"])])
                continue
            if not t.enabled:
                self.conn.send(conf, 1, [("u", head["id"])])  # disable_head
                continue
            ch = self.conn.alloc()
            self.conn.send(conf, 0, [("u", ch), ("u", head["id"])])
            mode = t.mode
            if mode is not None:
                match = None
                if not mode.custom:
                    for m in head["modes"]:
                        if m["w"] == mode.w and m["h"] == mode.h and (
                                not mode.refresh_mhz
                                or m["refresh"] == mode.refresh_mhz):
                            match = m
                            break
                if match:
                    self.conn.send(ch, 0, [("u", match["id"])])  # set_mode
                else:  # custom (or unlisted) mode
                    self.conn.send(ch, 1, [("i", mode.w), ("i", mode.h), ("i", mode.refresh_mhz)])
            if name in positions:
                x, y = positions[name]
                self.conn.send(ch, 2, [("i", x), ("i", y)])
            self.conn.send(ch, 3, [("i", WL_TRANSFORM[t.sway_tf])])
            self.conn.send(ch, 4, [("f", t.scale)])
        self.conn.send(conf, 2, [])  # apply
        deadline = time.monotonic() + 10.0
        try:
            while not result and time.monotonic() < deadline:
                if not self.conn.dispatch(timeout=1.0):
                    continue
        finally:
            # whatever happens in here, the socket keeps a deadline: the post-apply re-read must not block
            # forever on a compositor that has gone quiet (kwin.py carries the same guard)
            try:
                self.conn.sock.settimeout(10.0)
            except OSError:
                pass
        try:
            self.conn.send(conf, 4, [])  # destroy
        except OSError:
            pass
        if not result:
            raise Fatal("timed out waiting for the compositor to apply the "
                        "output configuration%s\n" % _hypr_clause())
        if result[0] == "failed":
            raise Fatal("compositor rejected the output configuration\n")
        if result[0] == "cancelled":
            raise Fatal("output configuration cancelled by a concurrent " "change; try again\n")

    def close(self):
        if self._own_conn:
            self.conn.close()

    # -- the backend shape ---------------------------------------------------

    def snapshot(self, state: "State | None" = None) -> list:
        return snapshot_wlr(self, state)

    def predicted_dims(self, t: "Target", state: "State") -> tuple:
        """wlroots computes the logical size from the fixed-point scale on
        the wire and truncates, where sway rounds the decimal it was typed."""
        return predicted_dims(t, state, wire="fixed")

    def verify(self, state: "State", targets: list):
        """--dryrun: zwlr_output_management can only be asked by applying, and an apply is what a dryrun must
        not do, so there is nothing to send here."""

    def apply(self, state: "State", targets: list, persistent: bool = False) -> list:
        """Single atomic zwlr_output_configuration apply (positions resolved against predicted logical sizes —
        same math wlroots uses), then the fresh snapshot, then — when the snapshot disagrees with the layout
        that was just accepted — the same configuration once more. `persistent` is accepted for contract
        parity and ignored: wlroots stores no layout of its own.

        The second apply is labwc's, and it is measured rather than defensive.  On resolute-labwc (labwc
        0.9.3 / wlroots 0.19.2, three 1920x1080 heads, 2026-09-09) the first configuration of a session that
        re-enables a head into a position another head already occupies — which is what `--output X --off`
        followed by `--output X --auto` asks for, because xrandr brings an output back at 0,0 and X is the
        oracle — is answered `succeeded` and then IGNORED: labwc lays the three heads out itself, and from
        then on every configuration comes back with one head somewhere labwc chose.  Measured, per step,
        asked -> read back:

            --off              V-1 0,0   V-2 1920,0                            landed
            --auto             V-1 0,0   V-2 1920,0   V-3 0,0     -> V-3 0,0   V-1 1920,0  V-2 3840,0
            --right-of V-2     V-1 0,0   V-2 1920,0   V-3 3840,0  -> V-1 0,0   V-3 3840,0  V-2 5760,0
            --below V-2        V-1 0,0   V-2 5760,0   V-3 5760,1080 -> V-2 7680,1080
            --right-of V-2     V-1 0,0   V-2 7680,1080 V-3 9600,1080 -> V-2 11520,1080

        the stray head landing immediately to the right of the last one labwc did place.  `wlr-randr` 0.4.1,
        the reference client, produces the identical layout from the identical starting state, so this is not
        our wire: the same three set_position requests were read off ours (probe of WlrOutputs.send in the
        guest) and they were the ones the layout was asked for.  Re-sending that same configuration lands it
        exactly, on the first retry, every time it was tried.  So the cost of the fix is one extra apply on a
        compositor that has re-arranged, and nothing at all on one that has not: labwc 0.9.3 is the only
        compositor measured on THIS path that reaches the second send — sway 1.11 forced onto the wlr
        backend does not (tests/test_wxrandr_live.py test_41/test_42, and the one-apply counts the fake
        compositor keeps in tests/test_wxrandr_hostile.py).  Hyprland is not on this path at all: detection
        sends it to `hacks/display/hypr.py`, whose `_verify_applied` is this read-back's analogue there.  A
        compositor that ignores the retry too gets the numbers in a sentence instead of a layout nobody
        asked for.
        """
        dims = {}
        for t in targets:
            if t.enabled:
                dims[t.name] = predicted_dims(t, state, wire="fixed")
        pos = resolve_positions(targets, dims)
        plan = {t.name: t for t in targets}
        self._refuse_dropped(targets)
        self._remember_off(state, targets)
        self.send(plan, pos)
        fresh = self._reread(state)
        stray = _stray_head(pos, fresh)
        if stray is not None:
            self.send(plan, pos)
            fresh = self._reread(state)
            stray = _stray_head(pos, fresh)
        if stray is not None:
            name, want, got = stray
            raise Fatal("the compositor accepted the position %d,%d for %s twice and put it at %d,%d "
                        "both times\n" % (want[0], want[1], name, got[0], got[1]))
        self._verify_applied(targets, fresh)
        self._forget_live(state)
        return fresh

    def _refuse_dropped(self, targets: list):
        """The output this run is enabling is one the compositor has stopped announcing.

        It is remembered here (`State.offheads`) because X keeps a disabled output in `--query` and X is the
        oracle, and `--auto` on it must say what happened rather than warn it away with exit 0: there is no
        head object to enable, so nothing would go on the wire at all.  NOT YET, and the rung is 6, a
        compositor that announces a disabled head instead of dropping it -- river 0.4.8 has no IPC of its
        own left (riverctl is gone in 0.4) and the reference client fails identically, `wlr-randr --output
        Virtual-3 --on` -> `failed to apply configuration`, rc 1 [M goal2/recon/flavors.md §2]."""
        for t in targets:
            if t.changed and t.enabled and t.output.wlr_head is None and self.by_name(t.name) is None:
                raise Fatal("%s was turned off and the compositor stopped announcing it, so there is no "
                            "head left to turn back on; not yet here, and the route is a compositor that "
                            "keeps announcing a disabled head (AGENTS.md route 6) -- wlr-randr refuses the "
                            "same request on the same session\n" % t.name)

    def _remember_off(self, state: "State | None", targets: list):
        """Keep every head this run is turning OFF, before it goes: a compositor that drops it takes the
        modes, the millimetres and the make/model/serial with it, and `--query` still owes all of them."""
        if state is None:
            return
        for t in targets:
            if not (t.changed and not t.enabled):
                continue
            head = self.by_name(t.name)
            if head is not None:
                state.offheads()[t.name] = _offhead_record(head)

    def _forget_live(self, state: "State | None"):
        """Drop the record of any remembered head that is announced again (a re-plug, or a compositor that
        did take it back), so the listing never carries two of one output."""
        if state is None:
            return
        live = {h["name"] for h in self.live_heads()}
        for name in [n for n in state.offheads() if n in live]:
            state.offheads().pop(name, None)

    @staticmethod
    def _verify_applied(targets: list, fresh: list):
        """`succeeded` and nothing changed is not a success.

        `_stray_head` above answers for the POSITION, which is labwc's failure mode; this is the rest of the
        apply, and it is here because of a measured silence: on Hyprland, after a `keyword monitor` apply,
        the wlr path stopped timing out and started answering rc 0 in 0.64 s with empty stderr and the head
        exactly where it was -- asked for 1920x1080 while sitting at 1280x1024, three times
        [M vm/live-smoke.d/hypr.sh display phase, resolute-hypr 2026-09-09].  A silent success that changed
        nothing is worse for a script than the timeout it replaced, and `xrandr` on X says `Configure crtc
        failed` rather than nothing.  The sentences are `hacks/display/hypr.py:_first_mismatch`'s, with "the
        compositor" for the name this backend does not know."""
        by = {o.name: o for o in fresh}
        for t in targets:
            if not t.changed:
                continue
            o = by.get(t.name)
            if o is None:
                raise Fatal("the compositor accepted the configuration for %s and then stopped "
                            "listing it\n" % t.name)
            if o.active != t.enabled:
                raise Fatal("the compositor accepted %s %s and did not apply it (it is still %s)\n"
                            % (t.name, "on" if t.enabled else "off", "on" if o.active else "off"))
            if not t.enabled:
                continue
            cur = o.current
            if t.mode is not None and cur is not None and (cur.w, cur.h) != (t.mode.w, t.mode.h):
                raise Fatal("the compositor accepted the mode %dx%d for %s and did not apply it "
                            "(it reports %dx%d)\n" % (t.mode.w, t.mode.h, t.name, cur.w, cur.h))
            if o.transform != t.sway_tf:
                raise Fatal("the compositor accepted the transform %s for %s and did not apply it "
                            "(it reports %s)\n" % (t.sway_tf, t.name, o.transform))

    def _reread(self, state: "State | None"):
        """The post-apply snapshot. A compositor that accepted the configuration and then stopped answering
        gets a sentence rather than the socket's own `timed out`."""
        try:
            self.conn.roundtrip()
            return snapshot_wlr(self, state)
        except OSError:
            raise Fatal("the compositor applied the output configuration " "and then stopped responding\n")


def _offhead_record(h: dict) -> dict:
    """What a head has to leave behind to keep being an output in `--query` after the compositor drops it:
    the millimetres, the identity strings and the mode list -- `--auto` re-derives the preferred mode off
    that list, so a record without it would find the output and then have nothing to enable it at."""
    return {"mm_w": h["mm_w"], "mm_h": h["mm_h"], "make": h["make"], "model": h["model"],
            "serial": h["serial"],
            "modes": [[m["w"], m["h"], m["refresh"], bool(m["preferred"])] for m in h["modes"]]}


def _offhead_output(name: str, rec, ident: int) -> "OutputState | None":
    """One `State.offheads()` record as the inactive OutputState `--query` prints and `--auto` targets.

    The file is hand-editable and shared (`_read_state`'s whole premise), so a record of the wrong shape is
    dropped rather than obeyed -- the same rule `State.custom_mode` follows."""
    if not isinstance(rec, dict):
        return None
    st = OutputState(name=name, active=False, ident=ident,
                     mm_w=int(rec.get("mm_w") or 0), mm_h=int(rec.get("mm_h") or 0),
                     make=str(rec.get("make") or "Unknown"), model=str(rec.get("model") or "Unknown"),
                     serial=str(rec.get("serial") or "Unknown"))
    for row in rec.get("modes") or []:
        try:
            w, h, mhz, pref = int(row[0]), int(row[1]), int(row[2]), bool(row[3])
        except (IndexError, TypeError, ValueError):
            return None
        st.modes.append(Mode(w=w, h=h, refresh_mhz=mhz, preferred=pref))
    st.virtual_modes = not st.modes
    return st


def _stray_head(pos: dict, fresh: list) -> tuple | None:
    """The first enabled output in `fresh` that is not where `pos` put it, as (name, asked, read-back).

    Snapshot order, not dict order, so the sentence a caller builds from this names the same output on every
    run.  A head the compositor turned off is not a stray: `--off` is checked by the caller's own targets and
    an output that vanished has no position to disagree about."""
    for o in fresh:
        if not o.active:
            continue
        want = pos.get(o.name)
        if want is not None and (o.x, o.y) != (want[0], want[1]):
            return (o.name, (want[0], want[1]), (o.x, o.y))
    return None


def _hypr_clause() -> str:
    """The sentence the apply timeout carries on a Hyprland session, and nothing anywhere else.

    Hyprland advertises zwlr_output_manager_v1 v4 and takes exactly one apply per session through it: the
    second times out at 10 s with nothing changed and no `[COutputConfiguration] Applying configuration` in
    its own log, and with a second output present even the first one hangs. `wlr-randr`, the reference client,
    hangs for ever on the same request, so this is the compositor's protocol implementation and not ours
    [M recon2/hyprland.md §4, and the same on 0.56.2 in recon2/arch.md]. The generic message is true and
    useless there -- the fix is a different backend, and this says which. Detection sends Hyprland to `hypr`,
    so the only way to be here on Hyprland is to have asked for `--backend wlr`."""
    try:
        if session.find_hypr_socket():
            return (" (Hyprland answers only the first output-configuration apply of a session; "
                    "use --backend hypr)")
    except OSError:      # a runtime dir that vanished mid-scan: the plain message is still right
        pass
    return ""


def wlr_snapshot_safe():
    """WlrOutputs or None; queries degrade gracefully without it."""
    try:
        return WlrOutputs()
    except Exception:
        return None


# -- unified snapshot ---------------------------------------------------------

def finish_modes(st: OutputState, customs: list):
    """The last two steps of every backend's snapshot: xrandr always marks one mode preferred, so a compositor
    that flags none makes the first listed one preferred; then the state file's custom modes join the list (they
    are ours, no compositor knows them)."""
    if not any(m.preferred for m in st.modes) and st.modes:
        st.modes[0].preferred = True
    st.modes.extend(customs)


_SUBPIXEL = {"rgb": "horizontal rgb", "bgr": "horizontal bgr",
             "vrgb": "vertical rgb", "vbgr": "vertical bgr",
             "none": "none", "unknown": "unknown"}


def snapshot_sway(ipc: SwayIPC, state: State, wlr=None) -> list:
    """OutputState list from sway GET_OUTPUTS, enriched with wlr head data
    (physical mm, preferred flags) and custom modes from the state file."""
    outs = []
    i3 = ipc.dialect() == "i3"
    for i, o in enumerate(ipc.get_outputs()):
        name = o.get("name", "?")
        if i3 and not o.get("active") and not o.get("current_mode"):
            # i3 reports a pseudo-output `xroot-0` covering the whole X screen: `active: false`, no modes, no
            # current mode, and `wxrandr --query` listed it as a connected output with no geometry beside the
            # real one [M recon2/i3.md §1, §2b, fixture get_outputs.json]. Dropped on the i3 dialect only: on
            # sway an inactive output is a real head that xrandr has to keep listing as disconnected.
            continue
        head = wlr.by_name(name) if wlr else None
        st = OutputState(
            name=name,
            active=bool(o.get("active")),
            ident=o.get("id") or (i + 1),
            subpixel=_SUBPIXEL.get(o.get("subpixel_hinting") or "unknown",
                                   "unknown"),
            non_desktop=bool(o.get("non_desktop")),
            make=o.get("make") or "Unknown",
            model=o.get("model") or "Unknown",
            serial=o.get("serial") or "Unknown",
        )
        rect = o.get("rect") or {}
        if st.active:
            st.x, st.y = rect.get("x", 0), rect.get("y", 0)
            st.w, st.h = rect.get("width", 0), rect.get("height", 0)
            st.scale = float(o.get("scale") or 1.0)
            st.transform = o.get("transform") or "normal"
        if head:
            st.mm_w, st.mm_h = head["mm_w"], head["mm_h"]
            st.wlr_head = head["id"]
        preferred = {}
        if head:
            for m in head["modes"]:
                if m["preferred"]:
                    preferred[(m["w"], m["h"], m["refresh"])] = True
        st.virtual_modes = not (o.get("modes"))
        for m in o.get("modes") or []:
            st.modes.append(Mode(
                w=m.get("width", 0), h=m.get("height", 0),
                refresh_mhz=m.get("refresh", 0),
                preferred=(m.get("width"), m.get("height"),
                           m.get("refresh")) in preferred))
        customs = state.modes_for_output(name)
        cm = o.get("current_mode")
        if st.active and cm:
            st.current = Mode(w=cm.get("width", 0), h=cm.get("height", 0), refresh_mhz=cm.get("refresh", 0))
            for m in st.modes + customs:
                # a custom mode currently applied via `mode --custom` comes back nameless from sway; match it up
                # by w/h/refresh so the named row gets the `*`
                if (m.w, m.h) == (st.current.w, st.current.h) and abs(
                        m.refresh_mhz - st.current.refresh_mhz) <= 1:
                    st.current = m
                    break
            else:
                st.modes.insert(0, st.current)
        finish_modes(st, customs)
        outs.append(st)
    return outs


def snapshot_wlr(wlr: WlrOutputs, state: State | None = None) -> list:
    """OutputState list from zwlr head events alone (generic wlroots).

    `state` may be None for a caller that wants the live layout and nothing else: the state file only ever adds
    custom modes to the lists."""
    outs = []
    for i, h in enumerate(wlr.live_heads()):
        st = OutputState(
            name=h["name"], active=h["enabled"], ident=i + 1,
            mm_w=h["mm_w"], mm_h=h["mm_h"], make=h["make"],
            model=h["model"], serial=h["serial"], wlr_head=h["id"],
        )
        st.virtual_modes = not h["modes"]
        by_id = {}
        for m in h["modes"]:
            mode = Mode(w=m["w"], h=m["h"], refresh_mhz=m["refresh"], preferred=m["preferred"])
            by_id[m["id"]] = mode
            st.modes.append(mode)
        if st.active:
            st.transform = WL_TRANSFORM_NAME.get(h["transform"], "normal")
            st.scale = h["scale"] or 1.0
            st.x, st.y = h["x"], h["y"]
            st.current = by_id.get(h["current"])
            if st.current is None and st.modes:
                st.current = st.modes[0]
            if st.current:
                st.w, st.h = logical_size(st.current.w, st.current.h, st.transform, st.scale)
        customs = ([] if state is None else state.modes_for_output(h["name"]))
        if st.current is not None and not st.current.name:
            for m in customs:
                if (m.w, m.h) == (st.current.w, st.current.h) and abs(
                        m.refresh_mhz - st.current.refresh_mhz) <= 1:
                    if st.current in st.modes:
                        st.modes.remove(st.current)
                    st.current = m
                    break
        finish_modes(st, customs)
        outs.append(st)
    # Then the heads this backend turned off and the compositor stopped announcing (`State.offheads`).
    # xrandr keeps a disabled output in `--query` and `--auto` brings it back, so a listing that lost the
    # output the last command disabled is ours to fix, whatever the compositor does with the head itself.
    live = {h["name"] for h in wlr.live_heads()}
    if state is not None:
        for name, rec in sorted(state.offheads().items()):
            if name in live:
                continue
            st = _offhead_output(name, rec, len(outs) + 1)
            if st is None:
                continue
            finish_modes(st, state.modes_for_output(name))
            outs.append(st)
    return outs


# -- pending-layout resolver --------------------------------------------------

RELATIONS = ("left-of", "right-of", "above", "below", "same-as")


@dataclasses.dataclass
class Stanza:
    """One `--output NAME ...` block, as parsed."""
    name: str
    mode: str | None = None          # mode name / WxH / 0xid string
    rate: float | None = None
    auto: bool = False
    preferred: bool = False
    off: bool = False
    pos: tuple | None = None
    relation: tuple | None = None    # (kind, other-output-name)
    rotate: str | None = None
    reflect: str | None = None
    scale: tuple | None = None       # (sx, sy)
    scale_from: tuple | None = None  # (w, h)
    primary: bool = False
    brightness: float | None = None
    gamma: tuple | None = None       # (r, g, b)
    props: list = dataclasses.field(default_factory=list)  # --set pairs


@dataclasses.dataclass
class Target:
    """Resolved end state for one output."""
    output: OutputState
    stanza: Stanza | None
    enabled: bool = True
    mode: Mode | None = None         # None for keep-current
    sway_tf: str = "normal"
    scale: float = 1.0
    changed: bool = False            # anything to apply for this output

    @property
    def name(self):
        return self.output.name


def _find_mode_for(output: OutputState, spec: str | None, rate: float | None, preferred: bool) -> Mode:
    """xrandr find_mode(): match by name (WxH for compositor modes), nearest
    refresh when a rate is given (no threshold — xrandr.c find_mode)."""
    if preferred and spec is None:
        for m in output.modes:
            if m.preferred:
                return m
        if output.current:
            return output.current
        raise Fatal("cannot find preferred mode\n")
    if spec is None:
        base = output.current or next(iter(output.modes), None)
        if base is None:
            raise Fatal("cannot find preferred mode\n")
        spec = base.display_name
    cands = [m for m in output.modes if m.display_name == spec]
    if not cands:
        m = re.fullmatch(r"(\d+)x(\d+)", spec)
        if output.virtual_modes and m:
            # headless/virtual output: the compositor can drive any WxH, so
            # honor the request as an on-the-fly custom mode.
            return Mode(w=int(m.group(1)), h=int(m.group(2)),
                        refresh_mhz=round(rate * 1000) if rate else 0,
                        custom=True)
        raise Fatal("cannot find mode %s\n" % spec)
    if rate:
        return min(cands, key=lambda m: abs(m.refresh_hz - rate))
    for m in cands:
        if output.current and m is output.current:
            return m
    return cands[0]


def mode_interlaced(m: Mode) -> bool:
    """Whether a mode is interlaced, by the flag xrandr prints."""
    return any(f.lower() == "interlace" for f in m.flags)


def match_mode(modes, w: int, h: int, rate_hz: float | None = None,
               tolerance: float | None = None,
               interlaced: bool | None = False) -> Mode | None:
    """The real (mode-id bearing) mode of size w x h: nearest refresh when a rate is given (within `tolerance`
    Hz if set), else the first listed. `interlaced=None` leaves the flag out of the match, for a compositor
    whose mode list carries no interlace bit to compare against."""
    cands = [m for m in modes if m.mode_id and (m.w, m.h) == (w, h)
             and (interlaced is None or mode_interlaced(m) == interlaced)]
    if not cands:
        return None
    if rate_hz:
        best = min(cands, key=lambda m: abs(m.refresh_hz - rate_hz))
        if tolerance is not None and abs(best.refresh_hz - rate_hz) > tolerance:
            return None
        return best
    return cands[0]


def resolve_real_mode(t: Target, state: State, interlace_known: bool = True) -> Mode:
    """The real mode an enabled target will run: the stanza's, else the current one, else the mode wxrandr
    disabled it at (state file), else the preferred one. A custom (--newmode) mode is only applicable when a
    real mode of the same size and rate exists -- a compositor that hands out mode objects or ids cannot be
    given a modeline.

    `interlace_known` says whether this compositor's mode list carries the interlace flag. Mutter's does, so a
    custom interlaced mode may only resolve onto an interlaced real one; KWin's modes are flagless, and matching
    them against a flag none of them can carry would find nothing.
    """
    o = t.output
    want = False if interlace_known else None
    mode = t.mode
    if mode is None:
        mode = o.current
    if mode is None:
        last = state.lastmodes().get(t.name)
        if last:
            mode = match_mode(o.modes, last[0], last[1], (last[2] or 0) / 1000.0 or None, interlaced=want)
    if mode is None:
        mode = next((m for m in o.modes if m.preferred and m.mode_id), None)
    if mode is None:
        mode = next((m for m in o.modes if m.mode_id), None)
    if mode is None:
        raise Fatal("cannot find preferred mode\n")
    if mode.mode_id:
        return mode
    real = match_mode(o.modes, mode.w, mode.h, mode.refresh_hz or None,
                      tolerance=1.0,
                      interlaced=mode_interlaced(mode) if interlace_known
                      else None)
    if real is None:
        raise Fatal("cannot find mode %s\n" % mode.display_name)
    return real


def build_targets(outputs: list, stanzas: list, state: State, global_auto: bool = False) -> list:
    """Match stanzas to outputs and settle everything except positions. Unknown --output names warn
    (`warning: output %s not found; ignoring`, exit stays 0) exactly like xrandr; relatives naming unknown
    outputs are fatal later, in resolve_positions."""
    by_name = {o.name: o for o in outputs}
    targets = {}
    for o in outputs:
        t = Target(output=o, stanza=None, enabled=o.active,
                   sway_tf=o.transform if o.active else "normal",
                   scale=o.scale if o.active else 1.0)
        t.mode = o.current
        targets[o.name] = t
    for s in stanzas:
        o = by_name.get(s.name)
        if o is None:
            warn_bare("warning: output %s not found; ignoring\n" % s.name)
            continue
        t = targets[s.name]
        t.stanza = s
        t.changed = True
        if s.off:
            t.enabled = False
            continue
        if s.mode is not None or s.rate is not None or s.preferred:
            t.mode = _find_mode_for(o, s.mode, s.rate, s.preferred)
            t.enabled = True
        if s.auto:
            t.enabled = True
            # xrandr set_name_preferred (xrandr.c:1820): --auto on a connected output with no explicit mode
            # switches it to the PREFERRED mode, even when it is already active on another one — so re-derive
            # whenever the stanza carries no mode/rate/preferred (not only when t.mode happens to be unset).
            if s.mode is None and s.rate is None and not s.preferred:
                try:
                    t.mode = _find_mode_for(o, None, None, True)
                except Fatal:
                    t.mode = None  # sway re-enables with its remembered mode
        if s.rotate is not None or s.reflect is not None:
            cur_rot, cur_refl = RANDR_VIEW[t.sway_tf]
            rot = s.rotate if s.rotate is not None else cur_rot
            refl = s.reflect if s.reflect is not None else cur_refl
            t.sway_tf = sway_transform(rot, refl)
        if s.scale is not None:
            sx, sy = s.scale
            if sx != sy and sx == sx and sy == sy:   # a nan differs from itself
                warn("anisotropic scaling %gx%g is not done yet (no output-management protocol "
                     "carries a per-axis scale; the route is a patched compositor, AGENTS.md route 6); "
                     "using %g for both axes\n" % (sx, sy, sx))
            t.scale = sx
        if s.scale_from is not None:
            fw, fh = s.scale_from
            base = t.mode or o.current
            if base is not None and fw > 0 and fh > 0:
                sx, sy = base.w / fw, base.h / fh
                if abs(sx - sy) > 1e-6:
                    warn("anisotropic scaling %gx%g is not done yet (no output-management protocol "
                         "carries a per-axis scale; the route is a patched compositor, AGENTS.md "
                         "route 6); using %g for both axes\n" % (sx, sy, sx))
                t.scale = sx
    if global_auto:
        for t in targets.values():
            if not t.output.active and t.stanza is None:
                t.enabled = True
                t.changed = True
                # like per-output --auto, a globally re-enabled output comes
                # up at its preferred mode, not sway's remembered one
                try:
                    t.mode = _find_mode_for(t.output, None, None, True)
                except Fatal:
                    t.mode = None
    return [targets[o.name] for o in outputs]


def predicted_dims(t: Target, state: State, wire: str = "text") -> tuple[int, int]:
    """Pending logical size of an enabled target (for dryrun + wlr backend + relative math when we cannot
    re-read).  `wire` is how the scale will reach the compositor -- "text" over the sway IPC, "fixed" over
    zwlr_output_management -- because that decides which 120th it lands on."""
    mode = t.mode
    if mode is None:
        last = state.lastmodes().get(t.name)
        if last:
            mode = Mode(w=last[0], h=last[1], refresh_mhz=last[2])
        elif t.output.modes:
            mode = t.output.modes[0]
        else:
            mode = Mode(w=1280, h=720)  # sway headless default
    return logical_size(mode.w, mode.h, t.sway_tf, wlr_scale(t.scale, wire))


def resolve_positions(targets: list, dims: dict) -> dict:
    """xrandr set_positions() (xrandr.c:1964) against PENDING geometry: iterative resolution so chains within
    one invocation work, fatal on circular relations, then the whole layout is normalized so min x = min y = 0.
    `dims` maps name -> (w, h) pending logical size. Returns {name: (x, y)} for every enabled output."""
    by_name = {t.name: t for t in targets}
    pos = {}
    pending = set()
    for t in targets:
        if not t.enabled:
            continue
        s = t.stanza
        if s is not None and s.relation is not None:
            pending.add(t.name)
        elif s is not None and s.pos is not None:
            pos[t.name] = s.pos
        else:
            pos[t.name] = (t.output.x, t.output.y)
    while pending:
        progressed = False
        for name in sorted(pending):
            t = by_name[name]
            kind, other = t.stanza.relation
            rel = by_name.get(other)
            if rel is None:
                raise Fatal('cannot find output "%s"\n' % other)
            if not rel.enabled:
                pos[name] = (0, 0)  # xrandr: relative-to-off lands at 0,0
                pending.discard(name)
                progressed = True
                break
            if rel.name in pending:
                continue
            rx, ry = pos[rel.name]
            w, h = dims[name]
            rw, rh = dims[rel.name]
            if kind == "left-of":
                pos[name] = (rx - w, ry)
            elif kind == "right-of":
                pos[name] = (rx + rw, ry)
            elif kind == "above":
                pos[name] = (rx, ry - h)
            elif kind == "below":
                pos[name] = (rx, ry + rh)
            else:  # same-as
                pos[name] = (rx, ry)
            pending.discard(name)
            progressed = True
            break
        if not progressed:
            raise Fatal("loop in relative position specifications\n")
    if pos:
        min_x = min(p[0] for p in pos.values())
        min_y = min(p[1] for p in pos.values())
        if min_x or min_y:
            pos = {n: (x - min_x, y - min_y) for n, (x, y) in pos.items()}
    return pos


# -- sway apply ---------------------------------------------------------------

def _fmt_refresh(mhz: int) -> str:
    return "%.3f" % (mhz / 1000.0)


def _mode_cmd(name: str, mode: Mode) -> str:
    if mode.custom:
        if not mode.refresh_mhz:
            return "output %s mode --custom %dx%d" % (name, mode.w, mode.h)
        return "output %s mode --custom %dx%d@%sHz" % (name, mode.w, mode.h, _fmt_refresh(mode.refresh_mhz))
    if mode.refresh_mhz:
        return "output %s mode %dx%d@%sHz" % (name, mode.w, mode.h, _fmt_refresh(mode.refresh_mhz))
    return "output %s mode %dx%d" % (name, mode.w, mode.h)


def phase1_commands(targets: list) -> list:
    """Mode/scale/transform/enable/disable for every touched output."""
    cmds = []
    for t in targets:
        if not t.changed:
            continue
        name = t.name
        if not t.enabled:
            if t.output.active:
                cmds.append("output %s disable" % name)
            continue
        if t.mode is not None and (
                t.output.current is None
                or (t.mode.w, t.mode.h, t.mode.refresh_mhz, t.mode.custom)
                != (t.output.current.w, t.output.current.h,
                    t.output.current.refresh_mhz, False)):
            cmds.append(_mode_cmd(name, t.mode))
        if t.sway_tf != (t.output.transform if t.output.active else "normal"):
            cmds.append("output %s transform %s" % (name, t.sway_tf))
        if abs(t.scale - (t.output.scale if t.output.active else 1.0)) > 1e-9:
            cmds.append("output %s scale %g" % (name, t.scale))
        if not t.output.active:
            cmds.append("output %s enable" % name)
    return cmds


def position_commands(targets: list, pos: dict) -> list:
    """Pin every enabled output to its resolved position (also re-pins untouched outputs: normalization can move
    the whole layout, and pinning stops sway's auto-arranger from second-guessing the plan)."""
    cmds = []
    for t in targets:
        if t.enabled and t.name in pos:
            x, y = pos[t.name]
            cmds.append("output %s position %d %d" % (t.name, x, y))
    return cmds


def _settle_modes(ipc: SwayIPC, state: State, targets: list):
    """Wait (bounded ~1s) until the compositor's re-read logical sizes match what we asked for, so
    resolve_positions packs against fresh geometry. Replaces a fixed settle sleep that raced under load; on a
    mismatch that never converges (a rounding quirk) it simply times out and the caller falls back to whatever
    sway reports — no worse than the old sleep."""
    want = {t.name: predicted_dims(t, state) for t in targets if t.changed and t.enabled}
    if not want:
        return
    deadline = time.monotonic() + 1.0
    while True:
        time.sleep(0.03)
        fresh = {o.name: o for o in snapshot_sway(ipc, state)}
        done = all(fresh.get(n) is not None and fresh[n].active
                   and (fresh[n].w, fresh[n].h) == wh
                   for n, wh in want.items())
        if done or time.monotonic() >= deadline:
            return


def record_lastmodes(state: State, targets: list):
    """Remember the mode of every output this run switches off, so that a later --auto can bring it back at the
    one it was running: a disabled output has no current mode left to ask the compositor for."""
    for t in targets:
        if t.changed and not t.enabled and t.output.active:
            cur = t.output.current
            if cur:
                state.lastmodes()[t.name] = [cur.w, cur.h, cur.refresh_mhz]


def apply_sway(ipc: SwayIPC, state: State, targets: list) -> list:
    """Two-phase apply: (1) modes/transforms/scales/enable/disable in one RUN_COMMAND, (2) re-read actual
    logical sizes, resolve positions against them, pin all positions in a second RUN_COMMAND. Returns the
    re-read OutputState list. Wayland has no fb concept, so unlike xrandr there is no screen-resize step in
    between.

    Phase 1 is not raise-on-first-failure: sway runs every ';'-joined subcommand regardless, so a mid-batch
    rejection would otherwise leave the survivors re-moded but un-positioned for sway's auto-arranger to
    scramble. We collect the results, still position everything that IS enabled, then re-raise the first
    failure."""
    record_lastmodes(state, targets)
    p1 = phase1_commands(targets)
    p1_err = None
    if p1:
        results = ipc.run_collect("; ".join(p1))
        for idx, r in enumerate(results):
            if not r.get("success"):
                p1_err = Fatal("compositor rejected `%s`: %s\n" % (
                    p1[idx] if idx < len(p1) else "; ".join(p1),
                    r.get("error", "unknown error")))
                break
        _settle_modes(ipc, state, targets)
    fresh = {o.name: o for o in snapshot_sway(ipc, state)}
    dims = {}
    for t in targets:
        if not t.enabled:
            continue
        f = fresh.get(t.name)
        if f is not None and f.active and f.w and f.h:
            dims[t.name] = (f.w, f.h)
            t.output = f  # positions of untouched outputs come from reality
        else:
            dims[t.name] = predicted_dims(t, state)
    pos = resolve_positions(targets, dims)
    p2 = position_commands(targets, pos)
    if p2:
        try:
            ipc.run("; ".join(p2))
        except Fatal:
            if p1_err is None:
                raise
    if p1_err is not None:
        raise p1_err
    return snapshot_sway(ipc, state)


# -- sway --persistent: the layout in a file sway's config sources ------------
#
# xrandr has no --persistent; it is wxrandr's own flag, so there is no oracle for WHERE a sway layout is kept,
# only the AGENTS.md rule that a layout X can save is one we save too.  The live apply above has already landed
# over the IPC -- --persistent is only about the NEXT session -- and sway keeps nothing of it on disk: the two
# ways a layout sticks across a sway restart are `~/.config/sway/config` and a file it `include`s (M
# docs/WXRANDR.md state-restoration table: "nothing on disk; only ~/.config/sway/config makes a layout stick").
# So the route is a file of our own (`SWAY_CONF_NAME`) carrying one `output ...` line per output this run
# touched, and an `include` line in sway's config that pulls it in at startup -- AGENTS.md route 2, at the cost
# of owning a file the user hand-edits.

#: The w11-owned file SwayBackend.apply writes `output` lines into on --persistent, beside sway's config.
SWAY_CONF_NAME = "w11-outputs.conf"

#: Rewritten in full every time, so whoever opens it knows who wrote it and what removing it does.
SWAY_CONF_HEADER = (
    "# Written by wxrandr (w11) -- one `output NAME ...` line per output --persistent applied.\n"
    "# sway does not watch this file: the layout is already live (it landed over the IPC when the\n"
    "# command ran), and this file only takes effect at the NEXT sway start or on `swaymsg reload`.\n"
    "# Delete this file, and the `include` line the sway config carries for it, to be rid of the layout.\n"
)

#: One `output NAME ...` line in the config, matching `include` and the parser's own spacing.
_SWAY_OUTPUT_RE = re.compile(r"^\s*output\s+(\S+)\s+.*$")
#: An `include` line, for finding the one that already points at our file.
_SWAY_INCLUDE_RE = re.compile(r"^\s*include\s+(.+?)\s*$")

#: What --persistent costs, said out loud: sway does NOT watch its config files, so nothing here is a reload of
#: the running session -- the layout is already on the screen from the live apply, and this only buys the next
#: session.  No `swaymsg reload` is sent, and that is deliberate: a reload re-runs every `exec_always`, puts
#: every output that has no config `output` line back to its preferred mode, and re-applies the config layout
#: in sway's enumeration order (the rig's own layout is an `exec`, which a reload does NOT re-run, so a reload
#: there leaves the heads in reverse-enumeration order) -- more than this one file's worth of change, for no
#: gain the live apply did not already give.
SWAY_PERSIST_NOTE = ("--persistent: the layout is in %s for the next session too; the running session already "
                     "has it from the live apply, so no `swaymsg reload` is sent (a reload re-runs every "
                     "`exec_always` and resets runtime-only settings)\n")

SWAY_INCLUDE_NOTE = "added an `include %s` line to %s\n"
#: The user config sway would read did not exist, so we created $XDG_CONFIG_HOME/sway/config: it `include`s the
#: system config first (so the next session keeps every default binding) and our layout file last.
SWAY_CREATED_NOTE = ("there was no user sway config, so %s was created; it `include`s %s (the system config, so "
                     "the next session keeps its defaults) first and our layout file last\n")
#: Same, but there was no system config to seed from either, so the created file carries only our `include`.
SWAY_CREATED_BARE_NOTE = ("there was no user sway config and no %s either, so %s was created holding only our "
                          "layout `include`; add your own sway settings there too\n")


#: `--persistent`'s file half, said out loud on a session that is not the caller's: the layout is on the
#: screen (the IPC apply crossed the uid boundary and landed) and the file is the half that did not happen.
SWAY_PERSIST_OTHER_USER_NOTE = (
    "--persistent: the layout was applied live and its file half was skipped: this sway session belongs to "
    "uid %d and this command runs as uid %d, so the file would go into %s inside their home, which this "
    "process does not write as root (a symlink planted there would be written through). Not yet; the route "
    "is writing that half as the seated user (fork, setgid/setuid to uid %d, then the same write), at the "
    "cost of a root-shell measurement on the rig. Until then run `wxrandr --persistent` as that user\n")


def _socket_owner(path: "str | None") -> "int | None":
    """`stat(2)`'s `st_uid` for a compositor IPC socket, or None when it cannot be asked.

    Its own function so the two callers below share one answer and a test can patch it: a unit test's socket
    lives in the test's own temp directory and is therefore owned by the test, which is precisely the uid the
    cases about somebody ELSE's session need to move."""
    if not path:
        return None
    try:
        return os.stat(path).st_uid
    except OSError:
        return None


def foreign_session_uid(sockpath: "str | None" = None) -> "int | None":
    """The graphical session's uid when it is somebody ELSE's, else None.

    Root over ssh and `sudo` are documented ways to drive these tools (docs/Technical.md section 12), and the
    session scan in `w11common/session.py` finds the seated user's sway or Hyprland socket across uids -- so
    the live apply lands on their session while `$HOME`, and with it `sway_config_dir()`, still names the
    caller's.  The GNOME half of the same flag answers that by resolving the session owner's home
    (`monitors_xml.default_path`, which takes a uid for this reason); the wlroots halves do not follow it
    yet, because a plain write into another account's `~/.config` as root is written through whatever symlink
    is waiting there -- the hazard `monitors_xml.keep_backup` had to close for the one such write we do make.
    So this is the test that tells the two cases apart, and the callers say which half they skipped.

    `sockpath` is the IPC socket the backend actually connected to, and when it is given it is the answer:
    the owner of THAT file is by definition the session being driven.  `session.session_uid()` answers a
    different question: it is `find_wayland_socket()` (w11common/session.py:392 and :218), the first
    `wayland-*` in `runtime_dir_candidates()` order, while `find_sway_socket()` (:248) and
    `find_hypr_socket()` (:290) walk those same candidates for a different file -- so the two can stop in
    different uids' runtime dirs.  With uid 1000 seated on GNOME and uid 1001 running a headless sway, the
    note would name 1000 and `~1000/.config/sway/w11-outputs.conf` for an apply landing on 1001's sway; the
    mirror case, our own sway on a box somebody else is seated at, would skip a file half the caller was
    entitled to write.
    `session.session_uid()` stays as the fallback for when the socket cannot be stat'ed at all (it was
    unlinked between connect and here, or a caller passed nothing), where a seated-session guess is still
    better than none: the skip direction costs a note, never a cross-uid write."""
    uid = _socket_owner(sockpath)
    if uid is None:
        uid = session.session_uid()
    return uid if uid is not None and uid != os.geteuid() else None


def _seated_config_path(uid: int, *parts) -> str:
    """`~<uid>/.config/<parts>` -- the file the notes above NAME and do not write.

    The home comes from the passwd database, not from the environment, which is the caller's; when there is
    no passwd entry to ask (a minimal container) the note carries a `~<uid>` placeholder rather than a path
    that would be wrong."""
    from hacks.display.monitors_xml import home_of       # at call time: monitors_xml imports this module
    return os.path.join(home_of(uid) or "~%d" % uid, ".config", *parts)


def sway_config_dir() -> str:
    """Where sway looks for the user config: `$XDG_CONFIG_HOME/sway`, else `~/.config/sway`."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "sway")


def _sway_config_path() -> str:
    return os.path.join(sway_config_dir(), "config")


#: The system config sway falls back to when no user config exists (sway(5)).  A module constant so a test can
#: point it at a temp file; seeded into a config we have to create so the next session keeps its default
#: bindings instead of losing the whole session to our near-empty file.
SWAY_SYSTEM_CONFIG = "/etc/sway/config"


def _sway_user_configs() -> list:
    """sway's own user-config search order (sway(5)): the FIRST of these that exists is the file sway reads,
    so the `include` has to go into that one -- not always `$XDG_CONFIG_HOME/sway/config`, or a user on the
    legacy `~/.sway/config` would be told their layout is saved into a file sway never opens."""
    home = os.path.expanduser("~")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [
        os.path.join(home, ".sway", "config"),
        os.path.join(base, "sway", "config"),
        os.path.join(home, ".i3", "config"),
        os.path.join(base, "i3", "config"),
    ]


def _sway_rules_path() -> str:
    return os.path.join(sway_config_dir(), SWAY_CONF_NAME)


def _sway_persist_line(o: OutputState) -> str:
    """The applied end state of one output as a single sway config `output` line.

    Built from the fresh post-apply snapshot, not the request, so it carries exactly what landed -- the mode
    sway settled on, the position after layout normalization, the transform and any non-unit scale.  sway's
    `output` command takes all of these on one line (sway-output(5))."""
    if not o.active:
        return "output %s disable" % o.name
    parts = ["output", o.name]
    if o.current is not None:
        m = o.current
        # A --newmode/--addmode mode comes back nameless from sway and matches the state file's custom Mode
        # (snapshot_sway); sway only accepts it back with a `mode --custom` word, exactly as `_mode_cmd` sends
        # it, or it refuses the line at the next start ("mode WxH@RHz not in the output's list").
        custom = "--custom " if m.custom else ""
        if m.refresh_mhz:
            parts.append("mode %s%dx%d@%sHz" % (custom, m.w, m.h, _fmt_refresh(m.refresh_mhz)))
        else:
            parts.append("mode %s%dx%d" % (custom, m.w, m.h))
    parts += ["position", str(o.x), str(o.y)]
    if o.transform != "normal":
        parts += ["transform", o.transform]
    if abs(o.scale - 1.0) > 1e-9:
        parts += ["scale", "%g" % o.scale]
    return " ".join(parts)


def _read_sway_rules(path: str) -> dict:
    """{output: full `output ...` line} from our file, or `{}` when it is not there.

    Read so a second `wxrandr --persistent` run for a DIFFERENT output does not take the first output's line
    out with it -- the file is merged, exactly as `hacks/display/hypr.py` merges its `monitor` rules."""
    rules = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return rules
    for ln in text.splitlines():
        m = _SWAY_OUTPUT_RE.match(ln)
        if m:
            rules[m.group(1)] = ln.strip()
    return rules


def _write_sway_rules(lines: dict) -> str:
    """Merge `lines` ({output: line}) into our file and return its path, renamed into place so a `swaymsg
    reload` racing the write cannot read half of it."""
    path = _sway_rules_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rules = _read_sway_rules(path)
    rules.update(lines)
    body = SWAY_CONF_HEADER + "".join("%s\n" % rules[k] for k in rules)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, path)
    return path


_SWAY_INCLUDE_TAG = "\n# w11: wxrandr writes the layout it applies here (AGENTS.md route 2).\n"


def _config_includes_us(text: str) -> bool:
    """True when `text` already `include`s our rules file.

    Matched by BASENAME, not full path: a user who wrote the include with `~`, a relative path, or after
    moving the file elsewhere already has us, and a second `include` would only be noise -- if they moved the
    file, that placement is theirs to own."""
    for ln in text.splitlines():
        m = _SWAY_INCLUDE_RE.match(ln)
        if m and os.path.basename(m.group(1).strip().strip('"')) == SWAY_CONF_NAME:
            return True
    return False


def ensure_sway_include(rules_path: str) -> tuple:
    """Make the sway config that sway ACTUALLY reads `include` our file, returning `(how, conf)` where how is
    `"present"`, `"added"`, `"created"` or `"created_bare"` and conf is the config file touched.

    The include is appended to the first config in sway's own search order that exists (`_sway_user_configs`),
    so a user on the legacy `~/.sway/config` is not told the layout is saved into a `~/.config` file sway never
    opens, and so our `output` lines are the LAST ones sway reads (they win over the rig's own `output` lines
    and the layout `exec`).  When none of those exist sway would fall back to /etc/sway/config; rather than
    shadow that wholesale with a near-empty file -- which would cost the next session its keybindings, bar and
    every default -- we create `$XDG_CONFIG_HOME/sway/config` that `include`s the system config FIRST and our
    layout file LAST (`SWAY_CREATED_NOTE`), so the next session keeps every default and still gets our layout.
    Unlike Hyprland (which writes a whole default config on first run, so `hacks/display/hypr.py` refuses to
    create one), sway ships no per-user config, so creating one is the only place the `include` can live."""
    line = "include %s\n" % rules_path
    for conf in _sway_user_configs():
        try:
            with open(conf, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if _config_includes_us(text):
            return "present", conf
        with open(conf, "a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write(_SWAY_INCLUDE_TAG + line)
        return "added", conf
    conf = _sway_config_path()
    os.makedirs(os.path.dirname(conf), exist_ok=True)
    have_system = os.path.exists(SWAY_SYSTEM_CONFIG)
    seed = ("include %s\n" % SWAY_SYSTEM_CONFIG) if have_system else ""
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write("# w11: wxrandr writes the layout it applies here (AGENTS.md route 2).\n" + seed + line)
    return ("created" if have_system else "created_bare"), conf


def persist_sway_layout(targets: list, fresh: list):
    """--persistent's file half: one `output ...` line per touched output into our file, and the `include`
    that pulls it into sway's config at the next start.  Nothing here reloads the running session -- the live
    apply already did that -- so the note names what the flag buys and what it does not."""
    by = {o.name: o for o in fresh}
    lines = {}
    for t in targets:
        if not t.changed:
            continue
        o = by.get(t.name)
        if o is not None:
            lines[t.name] = _sway_persist_line(o)
    if not lines:
        return
    path = _write_sway_rules(lines)
    how, conf = ensure_sway_include(path)
    if how == "added":
        warn(SWAY_INCLUDE_NOTE % (path, conf))
    elif how == "created":
        warn(SWAY_CREATED_NOTE % (conf, SWAY_SYSTEM_CONFIG))
    elif how == "created_bare":
        warn(SWAY_CREATED_BARE_NOTE % (SWAY_SYSTEM_CONFIG, conf))
    warn(SWAY_PERSIST_NOTE % path)


class SwayBackend:
    """The sway/i3 IPC backend in the shape all four of them share: snapshot, predicted_dims, verify, apply,
    close and a name.

    It holds the IPC socket and, when the compositor also speaks zwlr_output_management, the connection whose
    head data enriches a query with what sway IPC does not report (physical mm, preferred flags,
    make/model/serial)."""

    name = "sway"

    def __init__(self, ipc: SwayIPC, wlr: WlrOutputs | None = None):
        self.ipc = ipc
        self.wlr = wlr

    @property
    def sockpath(self) -> str:
        """The IPC socket: what the state file is keyed by in the one
        session that has no wayland socket to key it by."""
        return self.ipc.sockpath

    def snapshot(self, state: State) -> list:
        return snapshot_sway(self.ipc, state, self.wlr)

    def predicted_dims(self, t: Target, state: State) -> tuple:
        return predicted_dims(t, state)

    def verify(self, state: State, targets: list):
        """--dryrun: sway has nothing to validate against ahead of time. RUN_COMMAND is the only request there
        is, and running it would be the apply."""

    def apply(self, state: State, targets: list, persistent: bool = False) -> list:
        """The two-phase RUN_COMMAND apply, and the fresh snapshot it re-reads.

        `--persistent` writes the layout that just landed into `~/.config/sway/w11-outputs.conf` and makes
        sway's config `include` it, so it comes back at the next start (AGENTS.md route 2 -- see
        `persist_sway_layout`).  It is only about the next session: the running one already has the layout
        from the two phases below, so no `swaymsg reload` is sent.  On a session this process does not own --
        root over ssh, or `sudo`, driving the seated user's sway, which the socket scan finds across uids --
        that `~` is the caller's and not theirs, so the file half is skipped and named instead
        (`SWAY_PERSIST_OTHER_USER_NOTE`); the live apply above has already landed either way.  Whose session
        it is comes from the owner of the IPC socket these two phases were sent down, not from a second scan
        that could stop at a different runtime dir -- see `foreign_session_uid`.

        On i3 nothing is sent at all: there is no `output` command to send it to, so the two phases could only
        produce i3's parse error twice over -- and the first phase would already have recorded the modes it
        never applied.  i3 is X11: the X server owns the layout there, which is what the handover hands
        `xrandr`, so --persistent has nothing to write."""
        if self.ipc.dialect() == "i3":
            raise Fatal(I3_NO_APPLY)
        fresh = apply_sway(self.ipc, state, targets)
        if persistent:
            other = foreign_session_uid(self.ipc.sockpath)
            if other is None:
                persist_sway_layout(targets, fresh)
            else:
                warn(SWAY_PERSIST_OTHER_USER_NOTE
                     % (other, os.geteuid(), _seated_config_path(other, "sway", SWAY_CONF_NAME), other))
        return fresh

    def close(self):
        for handle in (self.ipc, self.wlr):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass


# -- shared backend selection -------------------------------------------------

OUTPUT_MANAGER_IFACE = "zwlr_output_manager_v1"


class NoBackend(Fatal):
    """No layout reader answered in this session.  A Fatal subclass so a caller that already handles Fatal is
    unchanged; a distinct type so a caller can turn it into its own Refusal instead, naming what it tried."""

    def __init__(self, tried: list):
        self.tried = list(tried)
        super().__init__("no layout protocol is available in this session (tried %s)\n" % ", ".join(tried))


def pick_backend(conn=None):
    """The one layout reader for this session, returned as a backend object with `.snapshot(state)` and
    `.close()` -- the shape `wxrandr --query` renders and a mirror tool decides from.

    This lifts the reader choice wxrandr's Session makes (`wxrandr/cli.py`, the SwayBackend / KwinOutputs /
    MutterOutputs / HyprOutputs / WlrOutputs chain) into the shared module, so a second tool can read a
    Cinnamon, a KWin or a GNOME layout the same way, over the same route-2 bus/IPC, instead of refusing every
    session without `zwlr_output_manager_v1`.

    The wlr client is kept where it is the answer: any compositor that advertises `zwlr_output_manager_v1`
    (sway, Hyprland, labwc, river, Wayfire, COSMIC, ...) is read through it -- exactly as before, so the
    geometry those desktops already print is unchanged.  Only a session that advertises NO such manager
    falls to the route-2 readers, which is the whole of the gap: KWin's `kde_output_management_v2`, then
    GNOME's `org.gnome.Mutter.DisplayConfig`, then Cinnamon's `org.cinnamon.Muffin.DisplayConfig` on the
    session bus.
    sway's and Hyprland's own IPC are tried last, for a hypothetical wlroots build with the manager compiled
    out; on a real sway or Hyprland the manager above answers first.

    `conn` is a live `w11common.wayland_mini.WlConn` to reuse for the wayland-native readers (the wlr
    floor and KWin); None makes each open its own.  Raises `NoBackend` (a `Fatal`) when none answers --
    which, across every desktop the six readers cover, is only a session that speaks no output protocol
    we know at all."""
    from w11common import session as _session
    tried = []

    # the wlr floor first, wherever it is advertised: zwlr_output_manager_v1
    tried.append(OUTPUT_MANAGER_IFACE)
    if conn is not None:
        if conn.find_global(OUTPUT_MANAGER_IFACE) is not None:
            try:
                return WlrOutputs(conn=conn)
            except (Fatal, OSError):
                pass
    else:
        wlr = wlr_snapshot_safe()
        if wlr is not None:
            return wlr

    # KWin's kde_output_management_v2 (wayland-native: reuse the caller's conn when it already carries it)
    from hacks.display import kwin as _kwin
    tried.append(_kwin.MGMT)
    if conn is not None:
        if conn.find_global(_kwin.MGMT) is not None:
            try:
                return _kwin.KwinOutputs(conn=conn)
            except (Fatal, OSError, RuntimeError, ValueError):
                pass
    else:
        kc = _kwin.probe()
        if kc is not None:
            try:
                return _kwin.KwinOutputs(conn=kc)
            except (Fatal, OSError, RuntimeError, ValueError):
                kc.close()

    # GNOME's DisplayConfig, then Cinnamon's copy (mutter before cinnamon, as wxrandr's AUTO_ORDER has it)
    from hacks.display import mutter as _mutter
    for flavor in (_mutter.MUTTER, _mutter.MUFFIN):
        tried.append("%s (D-Bus)" % flavor.dest)
        bus = _mutter.probe(flavor=flavor)
        if bus is not None:
            try:
                return _mutter.MutterOutputs(bus=bus, flavor=flavor)
            except (_mutter.DBusError, Fatal, OSError, ValueError):
                bus.close()

    # sway / i3 IPC, then Hyprland's -- only reachable on a wlroots build with the manager compiled out
    tried.append("sway/i3 IPC")
    sock = _session.find_sway_socket()
    if sock:
        try:
            return SwayBackend(SwayIPC(sock), wlr_snapshot_safe())
        except (Fatal, OSError):
            pass
    tried.append("Hyprland IPC")
    try:
        hsock = _session.find_hypr_socket()
    except OSError:
        hsock = None
    if hsock:
        try:
            from hacks.display import hypr as _hypr
            return _hypr.HyprOutputs(sock=hsock)
        except (Fatal, OSError, ImportError):
            pass

    raise NoBackend(tried)


# -- query rendering ----------------------------------------------------------

def fmt_refresh_col(hz: float) -> str:
    return "%6.2f" % hz


def render_screen_line(screen_num: int, outputs, fb=None) -> str:
    x0, y0, x1, y1 = layout_box(outputs)
    cur_w, cur_h = x1 - x0, y1 - y0
    if fb:
        cur_w, cur_h = fb
    return ("Screen %d: minimum %d x %d, current %d x %d, maximum %d x %d"
            % (screen_num, MIN_WIDTH, MIN_HEIGHT, cur_w, cur_h,
               MAX_WIDTH, MAX_HEIGHT))


def _mode_ids(outputs) -> dict:
    """Stable fabricated mode xids for verbose output (0x41, 0x42, ...)."""
    ids = {}
    nxt = 0x41
    for o in outputs:
        for m in o.modes:
            key = (m.display_name, m.w, m.h, m.refresh_mhz)
            if key not in ids:
                ids[key] = nxt
                nxt += 1
    return ids


def mode_xid(ids: dict, m: Mode) -> int:
    return ids.get((m.display_name, m.w, m.h, m.refresh_mhz), 0)


def render_output_header(o: OutputState, primary: str | None, verbose=False, ids=None) -> str:
    line = o.name + " connected"
    if primary == o.name:
        line += " primary"
    if o.active:
        line += " %dx%d+%d+%d" % (o.w, o.h, o.x, o.y)
        if verbose and o.current is not None and ids is not None:
            line += " (0x%x)" % mode_xid(ids, o.current)
        rot, refl = RANDR_VIEW.get(o.transform, ("normal", "normal"))
        # xrandr's rotation field carries the reflection bits too: any reflection makes the whole
        # rotation+reflection phrase print (`normal X axis`), xrandr.c:3758
        if rot != "normal" or refl != "normal" or verbose:
            line += " " + rot
            if refl != "normal":
                line += REFLECTION_SUFFIX[refl]
    line += " (normal left inverted right x axis y axis)"
    if o.active:
        line += " %dmm x %dmm" % (o.mm_w, o.mm_h)
    return line


def render_mode_table(o: OutputState) -> list:
    """Grouped mode rows: `   %-12s` + per-mode ` %6.2f` + */space + +/space.
    Trailing spaces are real (oracle capture)."""
    lines = []
    groups = []
    seen = {}
    for m in o.modes:
        gname = m.display_name
        if gname in seen:
            groups[seen[gname]][1].append(m)
        else:
            seen[gname] = len(groups)
            groups.append((gname, [m]))
    for gname, modes in groups:
        row = "   %-12s" % gname
        for m in modes:
            cur = "*" if (o.current is not None and m is o.current) else " "
            pref = "+" if m.preferred else " "
            row += " %s%s%s" % (fmt_refresh_col(m.refresh_hz), cur, pref)
        lines.append(row)
    return lines


def render_verbose_block(o: OutputState, state: State, crtc_index) -> list:
    """Per-output verbose block. Fields with no Wayland source are printed honestly (identity transform is the
    compositor default; gamma/brightness come from our holder records, not a degenerate XWayland ramp)."""
    g = state.gamma().get(o.name, {})
    gam = g.get("gamma", [1.0, 1.0, 1.0])
    bright = g.get("brightness", 1.0)
    # only report holder values while the holder is actually alive: after it is killed externally (kill -9,
    # compositor restart) the compositor restored the neutral ramp, so stale 0.50 would be a lie.
    pid = g.get("pid")
    if pid is not None:
        if procs.proc_starttime(pid) != g.get("start"):
            gam, bright = [1.0, 1.0, 1.0], 1.0
    lines = [
        "\tIdentifier: 0x%x" % o.ident,
        "\tTimestamp:  0",
        "\tSubpixel:   %s" % o.subpixel,
        "\tGamma:      %#.2g:%#.2g:%#.2g" % tuple(gam),
        "\tBrightness: %#.2g" % bright,
        "\tClones:    ",
    ]
    if o.active and crtc_index is not None:
        lines.append("\tCRTC:       %d" % crtc_index)
    lines.append("\tCRTCs:      %d" % (crtc_index if crtc_index is not None else 0))
    lines += [
        "\tTransform:  %f %f %f" % (1.0, 0.0, 0.0),
        "\t            %f %f %f" % (0.0, 1.0, 0.0),
        "\t            %f %f %f" % (0.0, 0.0, 1.0),
        "\t           filter: ",
    ]
    lines += render_prop_block(o)
    return lines


def render_prop_block(o: OutputState) -> list:
    """--prop block: only properties honestly derivable from the compositor
    (trailing space after values matches xrandr's value printer)."""
    return ["\tnon-desktop: %d " % (1 if o.non_desktop else 0), "\t\tsupported: 0, 1"]


def render_verbose_mode(m: Mode, ids: dict, current: bool) -> list:
    """print_verbose_mode (xrandr.c:593). Custom modes carry a real modeline; compositor modes only expose
    WxH+refresh, so their timings print as the degenerate blanking-free modeline (total == display)."""
    if m.timings:
        hss, hse, htot, vss, vse, vtot = m.timings
        clock = m.clock_mhz
    else:
        hss = hse = htot = m.w
        vss = vse = vtot = m.h
        clock = m.refresh_hz * htot * vtot / 1e6
    head = "  %s (0x%x) %6.3fMHz" % (m.display_name, mode_xid(ids, m), clock)
    for f in m.flags:
        fl = f.lower()
        word = {"+hsync": "+HSync", "-hsync": "-HSync", "+vsync": "+VSync",
                "-vsync": "-VSync", "+csync": "+CSync", "-csync": "-CSync",
                "csync": "CSync", "interlace": "Interlace",
                "doublescan": "DoubleScan"}.get(fl)
        if word:
            head += " " + word
    if current:
        head += " *current"
    if m.preferred:
        head += " +preferred"
    hclock = (clock * 1e6 / htot / 1000.0) if htot else 0.0
    vclock = mode_refresh_hz(clock, htot, vtot, m.flags)
    return [
        head,
        "        h: width  %4d start %4d end %4d total %4d skew %4d "
        "clock %6.2fKHz" % (m.w, hss, hse, htot, 0, hclock),
        "        v: height %4d start %4d end %4d total %4d           "
        "clock %6.2fHz" % (m.h, vss, vse, vtot, vclock),
    ]


def render_query(outputs, state: State, screen_num=0, verbose=False, props=False, fb=None) -> list:
    lines = [render_screen_line(screen_num, outputs, fb)]
    ids = _mode_ids(outputs)
    crtc = 0
    for o in outputs:
        idx = crtc if o.active else None
        if o.active:
            crtc += 1
        lines.append(render_output_header(o, state.primary, verbose, ids))
        if verbose:
            lines += render_verbose_block(o, state, idx)
        elif props:
            lines += render_prop_block(o)
        if verbose:
            for m in o.modes:
                lines += render_verbose_mode(m, ids, m is o.current)
        else:
            lines += render_mode_table(o)
    return lines


def render_monitors(outputs, state: State, primary_first: bool = False) -> list:
    """RandR 1.5 monitor listing (xrandr.c:4030). Every enabled output is one automatic monitor; primary comes
    from the state file; mm are the physical size when known, else synthesized exactly like XWayland (96dpi,
    round-half-even). The X server lists the primary monitor first (rrmonitor.c) — only observable where the
    compositor has a real primary XWayland knows about (Mutter), hence opt-in."""
    act = [o for o in outputs if o.active]
    if primary_first and state.primary:
        act.sort(key=lambda o: o.name != state.primary)
    lines = ["Monitors: %d" % len(act)]
    for i, o in enumerate(act):
        star = "*" if state.primary == o.name else ""
        mm_w = o.mm_w or synth_mm(o.w)
        mm_h = o.mm_h or synth_mm(o.h)
        lines.append(" %d: +%s%s %d/%dx%d/%d+%d+%d  %s" % (
            i, star, o.name, o.w, mm_w, o.h, mm_h, o.x, o.y, o.name))
    return lines


def render_providers(outputs, compositor_name="sway") -> list:
    """One synthesized provider for the compositor (documented invention: Wayland has no GPU provider objects;
    cap 0xb mirrors a typical primary GPU: Source Output, Sink Output, Sink Offload)."""
    n = len(outputs)
    return ["Providers: number : 1",
            "Provider 0: id: 0x1 cap: 0xb, Source Output, Sink Output, "
            "Sink Offload crtcs: %d outputs: %d associated providers: 0 "
            "name:%s" % (n, n, compositor_name)]


# -- RandR 1.0 rendering (q1 path) --------------------------------------------

def q1_sizes(outputs) -> list:
    """The RandR-1.0 size list: the first output's modes, server order."""
    if not outputs:
        return []
    o = outputs[0]
    sizes = []
    seen = {}
    for m in o.modes:
        key = (m.w, m.h)
        if key in seen:
            if m.refresh_hz and round(m.refresh_hz) not in sizes[seen[key]][2]:
                sizes[seen[key]][2].append(round(m.refresh_hz))
            continue
        seen[key] = len(sizes)
        rates = [round(m.refresh_hz)] if m.refresh_hz else []
        sizes.append([m.w, m.h, rates])
    return sizes


def render_q1(outputs, state: State) -> list:
    o = outputs[0] if outputs else None
    sizes = q1_sizes(outputs)
    cur_idx = 0
    cur_rate = 0
    if o is not None and o.current is not None:
        for i, (w, h, rates) in enumerate(sizes):
            if (w, h) == (o.current.w, o.current.h):
                cur_idx = i
                cur_rate = round(o.current.refresh_hz)
    mm_w = screen_mm(o.current.w) if o and o.current else 0
    mm_h = screen_mm(o.current.h) if o and o.current else 0
    lines = [" SZ:    Pixels          Physical       Refresh"]
    for i, (w, h, rates) in enumerate(sizes):
        row = "%c%-2d %5d x %-5d  (%4dmm x%4dmm )" % ("*" if i == cur_idx else " ", i, w, h, mm_w, mm_h)
        if rates:
            row += "  "
        for r in rates:
            row += "%c%-4d" % ("*" if i == cur_idx and r == cur_rate else " ", r)
        lines.append(row)
    return lines


def render_q1_state(o: OutputState) -> list:
    rot, refl = RANDR_VIEW.get(o.transform, ("normal", "normal"))
    refl_word = {"normal": "none", "x": "X axis", "y": "Y axis", "xy": "X and Y axis"}[refl]
    return ["Current rotation - %s" % rot,
            "Current reflection - %s" % refl_word,
            "Rotations possible - normal left inverted right ",
            "Reflections possible - X Axis Y Axis"]
