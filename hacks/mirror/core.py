"""Detection, policy, output model and the wl-mirror argv.

wmirror drives the external `wl-mirror` -- an unprivileged wlroots
screen-copy client, packaged in Ubuntu universe -- and owns its lifetime.
It exists for the two pictures output geometry alone cannot produce on
wlroots (both measured on sway 1.11, see docs/WMIRROR.md):

  * a REGION of one output shown on another: no layout expresses it;
  * a whole output shown on a differently-shaped one: two outputs sharing a
    position show the same pixels, but the smaller one CROPS -- wlroots'
    zwlr_output_management has no replication request to fix that the way
    KWin's set_replication_source does.

Everything else stays where it was. Two outputs of the same logical size at
the same position already mirror on sway, byte-identical, with no helper at
all, so wmirror refuses that case by name and points at `wxrandr --same-as`.

wmirror never changes an output. It starts a client window and watches it.
"""

import contextlib
import dataclasses
import fcntl
import os
import re
import shutil
import subprocess
import time

from w11common import distro, session
from w11common.errors import CmdError
from hacks.display import core as wxcore

HELPER = "wl-mirror"

#: How each family's install line is introduced. The Debian row is today's bytes, which docs/WMIRROR.md and
#: two tests pin; the other three exist because `apt install wl-mirror` was printed on Fedora, Arch and a
#: NixOS box with no apt at all [M recon2/fedora.md, arch.md, nixos.md].
_HINT_WHERE = {"debian": "on Ubuntu/Debian", "fedora": "on Fedora",
               "arch": "on Arch", "nixos": "on NixOS"}


def install_hint() -> str:
    """`on Ubuntu/Debian: sudo apt install wl-mirror`, and the same sentence for whatever family this box is.

    A function and not a constant: `w11common.distro` reads /etc/os-release, and a value frozen at import time
    would be this process's first answer for the life of the interpreter -- which is exactly what the tests
    that plant an os-release file need not to be."""
    fam = distro.family()
    return "%s: sudo %s" % (_HINT_WHERE.get(fam, _HINT_WHERE["debian"]),
                            distro.hint("wl-mirror", fam))

# The capture protocols wl-mirror can actually use. export-dmabuf is an optimisation on top of these (it is what
# `auto` picks for a whole output, and it cannot serve a region at all), never a substitute: a compositor with
# only export-dmabuf could not do --region, so it is not in this list.
SCREENCOPY = "zwlr_screencopy_manager_v1"
EXTCOPY = "ext_image_copy_capture_manager_v1"
CAPTURE_GLOBALS = (SCREENCOPY, EXTCOPY)

OUTPUT_MANAGER = "zwlr_output_manager_v1"

SCALINGS = ("fit", "cover", "exact")
DEFAULT_SCALING = "fit"


class Refusal(Exception):
    """One refusal, as the lines the CLI prints. Never a traceback."""

    def __init__(self, lines):
        if isinstance(lines, str):
            lines = [lines]
        super().__init__(lines[0])
        self.lines = list(lines)


# -- output model -------------------------------------------------------------
#
# There is no model here. An output is a wxcore.OutputState, read by wxcore.snapshot_wlr from the
# zwlr_output_manager events -- the very call `wxrandr --query` renders -- so the two can never disagree about a
# rectangle, and `.geom()` prints the same words on both sides.

def by_name(outputs, name):
    for o in outputs:
        if o.name == name:
            return o
    return None


def rects_overlap(a, b) -> bool:
    return not (a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y)


def rect_inside(region, o) -> bool:
    x, y, w, h = region
    return (x >= o.x and y >= o.y and x + w <= o.x + o.w and y + h <= o.y + o.h)


# -- region -------------------------------------------------------------------

_REGION_RE = re.compile(r"^(\d+)x(\d+)([-+]\d+)([-+]\d+)$")
REGION_FORM = "WxH+X+Y (layout coordinates, e.g. 500x300+1400+100)"


def parse_region(text: str):
    """`WxH+X+Y` -> (x, y, w, h).

    X11 geometry order, the one `xrandr --fb`/`--pos` and `wwmctl -g` speak, in LAYOUT coordinates -- the same
    numbers `slurp` prints and the same ones `wxrandr --query` shows next to each output, so a region is copied
    from one and pasted here."""
    m = _REGION_RE.match((text or "").strip())
    if not m:
        raise Refusal(["--region takes " + REGION_FORM, "got: %s" % text])
    w, h, x, y = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    if w <= 0 or h <= 0:
        raise Refusal(["--region width and height must both be > 0", "got: %s" % text])
    return (x, y, w, h)


def fmt_region(region) -> str:
    x, y, w, h = region
    return "%dx%d%+d%+d" % (w, h, x, y)


def slurp_region(region) -> str:
    """wl-mirror's region syntax, which is slurp's: `<x>,<y> <w>x<h>`.

    The output name is deliberately NOT appended: wl-mirror takes the output from the positional argument then,
    so its "region and argument output differ" path can never be reached (src/options.c)."""
    x, y, w, h = region
    return "%d,%d %dx%d" % (x, y, w, h)


# -- the helper's command line ------------------------------------------------

def build_argv(source: str, target: str, region=None,
               scaling: str = DEFAULT_SCALING, helper: str = HELPER) -> list:
    """The wl-mirror invocation for one mirror.

    `--fullscreen-output T` implies --fullscreen (upstream man page), so the window opens fullscreen on the
    target and nowhere else. Options come first: wl-mirror stops parsing at the first non-`-` argument."""
    argv = [helper, "--fullscreen-output", target]
    if scaling:
        argv += ["--scaling", scaling]
    if region:
        argv += ["--region", slurp_region(region)]
    argv.append(source)
    return argv


def find_helper(path=None):
    """The wl-mirror binary, or None. `path` overrides $PATH (tests)."""
    return shutil.which(HELPER, path=path)


def helper_version(binary: str):
    """`wl-mirror --version`'s first line, or None. Never raises."""
    try:
        # errors="replace" is what makes "never raises" true: text=True decodes strict, so a helper whose banner
        # is not the locale's encoding (or is not text at all) raised UnicodeDecodeError out of subprocess.run,
        # past the except below, and out of `wmirror --check`.
        out = subprocess.run([binary, "--version"], capture_output=True,
                             timeout=5, text=True, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "") + (out.stderr or "")
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else None


def missing_helper_lines() -> list:
    return ["wl-mirror is not installed (no `%s` on PATH)" % HELPER, install_hint()]


# -- compositor detection -----------------------------------------------------

def no_session_lines() -> list:
    """What to say when there is no Wayland session to mirror on."""
    from w11common import passthrough
    # respect_override=False: W11_PASSTHROUGH says what to do about handing over to an X11 original, and
    # wmirror has no original to hand over to (warandr/randr.py reasons the same way).
    if passthrough.session_kind(respect_override=False) == "x11":
        return ["this is an X11 session: there is no wl-mirror here",
                "X11 mirrors whole outputs with `xrandr --output B "
                "--same-as A`; a region is not done here yet -- it needs an "
                "X capture client of our own (XShm off the root window), "
                "which is a tool this toolbox has not written"]
    return ["cannot find a Wayland session to mirror on " "(no wayland socket)"]


def open_conn(wayland_socket=None):
    """A WlConn to the compositor, or Refusal naming what is missing."""
    from w11common.wayland_mini import WlConn
    if wayland_socket is None:
        hit = session.find_wayland_socket()
        if hit is None:
            raise Refusal(no_session_lines())
        wayland_socket = hit[2]
    try:
        conn = WlConn(wayland_socket)
    except OSError as e:
        raise Refusal(["cannot connect to the compositor at %s: %s" % (wayland_socket, e)])
    return conn


def capture_support(conn) -> list:
    """[(interface, version)] of the capture protocols wl-mirror can use."""
    have = []
    for iface in CAPTURE_GLOBALS:
        hit = conn.find_global(iface)
        if hit:
            have.append((iface, hit[1]))
    return have


def no_capture_lines() -> list:
    """Why there is nothing to mirror here, in three lines.

    The second one used to read `wl-mirror needs wlroots ... or a compositor with ext-image-copy-capture-v1`,
    which put the second protocol in a footnote. It is not a footnote: `wmirror --check` passed on COSMIC on
    `ext_image_copy_capture_manager_v1` alone, with no screencopy manager in cosmic-comp's 53 globals at all
    [M recon2/cosmic.md §3], and labwc 0.9.3 and sway 1.12 publish both. The code path already accepted
    extcopy on its own; only these words denied it."""
    return ["this compositor advertises neither %s nor %s, so wl-mirror "
            "cannot capture here" % (SCREENCOPY, EXTCOPY),
            "wl-mirror needs a compositor with %s (sway, Hyprland, labwc, "
            "Wayfire, ...) or %s (COSMIC, labwc, sway 1.12)" % (SCREENCOPY, EXTCOPY),
            "on GNOME and KDE the route is the desktop portal's ScreenCast "
            "(AGENTS.md route 4), which asks the user for permission once "
            "per session and is not wired up here yet"]


def require_capture(conn) -> list:
    have = capture_support(conn)
    if not have:
        raise Refusal(no_capture_lines())
    return have


def read_outputs(conn) -> list:
    """The live layout, or Refusal. Uses wxrandr's own wlr client, so
    wmirror and `wxrandr --query` can never disagree about the geometry."""
    try:
        wlr = wxcore.WlrOutputs(conn=conn)
    except wxcore.Fatal:
        raise Refusal(["this compositor does not advertise %s, so wmirror "
                       "cannot read the output layout" % OUTPUT_MANAGER,
                       "not yet here: the route is the compositor's own "
                       "display bus or IPC, which wxrandr already speaks on "
                       "Mutter, KWin, Hyprland and sway (AGENTS.md route 2), "
                       "at the cost of one layout reader per compositor"])
    except OSError as e:
        raise Refusal(["lost the compositor connection: %s" % e])
    return wxcore.snapshot_wlr(wlr)


# -- policy -------------------------------------------------------------------

RUN, DONE, REFUSE = "run", "done", "refuse"


@dataclasses.dataclass
class Decision:
    """`RUN` (start the helper), `DONE` (the picture is already on the
    screen -- do nothing, exit 0) or `REFUSE` (exit 1)."""
    verdict: str
    lines: list = dataclasses.field(default_factory=list)


def _same_as_hint(source: str, target: str) -> str:
    return "wxrandr --output %s --same-as %s" % (target, source)


def decide(outputs, source: str, target: str, region=None,
           keep_layout: bool = False, running=None) -> Decision:
    """Is the helper needed at all, and may it be started?

    The policy, in one sentence: run wl-mirror only for what the layout cannot express -- a region, or a whole
    output onto one of a different logical size -- and never where the two outputs share pixels, because a
    fullscreen window on the target is drawn on the source too."""
    running = running or {}
    if source == target:
        return Decision(REFUSE, ["%s cannot mirror itself" % source])

    src = by_name(outputs, source)
    dst = by_name(outputs, target)
    known = ", ".join(o.name for o in outputs) or "none"
    for role, name, o in (("source", source, src), ("target", target, dst)):
        if o is None:
            return Decision(REFUSE, ["no output named %s (the %s)"
                                     % (name, role),
                                     "outputs here: %s" % known])
        if not o.active:
            return Decision(REFUSE, ["%s (the %s) is off" % (name, role),
                                     "turn it on first: wxrandr --output "
                                     "%s --auto" % name])

    if region is not None and not rect_inside(region, src):
        return Decision(REFUSE, [
            "the region %s is not inside %s, which is %s"
            % (fmt_region(region), source, src.geom()),
            "wl-mirror would silently clamp it to what fits, so wmirror "
            "asks for one that fits instead"])

    # the self-capture guard. Over a shared rectangle sway draws BOTH outputs' windows on both heads (measured),
    # so a mirror window fullscreen on the target lands on the source too and wl-mirror ends up capturing its
    # own picture.
    if rects_overlap(src, dst):
        if src.rect() == dst.rect() and region is None:
            return Decision(DONE, [
                "%s already shows %s: both are %s, and a shared rectangle "
                "on wlroots is a complete mirror" % (target, source,
                                                     src.geom()),
                "no helper started"])
        return Decision(REFUSE, [
            "%s (%s) and %s (%s) share pixels" % (source, src.geom(),
                                                  target, dst.geom()),
            "a fullscreen mirror window on %s would be drawn on %s as well, "
            "so wl-mirror would capture itself" % (target, source),
            "give %s a place of its own (wxrandr --output %s --right-of %s), "
            "or mirror with the layout alone: %s"
            % (target, target, source, _same_as_hint(source, target))])

    loop = running.get(source)
    if loop and loop.get("source") == target:
        return Decision(REFUSE, [
            "%s is already mirroring %s: the two would capture each other"
            % (source, target),
            "stop that one first: wmirror --stop %s" % source])

    if target in running:
        rec = running[target]
        return Decision(REFUSE, [
            "%s is already mirroring %s" % (target, rec.get("source", "?")),
            "replace it with --replace, or stop it: wmirror --stop %s"
            % target])

    if region is None and (src.w, src.h) == (dst.w, dst.h) and not keep_layout:
        return Decision(REFUSE, [
            "%s and %s are both %dx%d: the layout mirrors them byte for "
            "byte, with no helper and no cost" % (source, target,
                                                  src.w, src.h),
            _same_as_hint(source, target),
            "--keep-layout runs wl-mirror anyway, so %s keeps its own place "
            "in the layout" % target])

    return Decision(RUN)


def watch_reason(outputs, source: str, target: str, region=None, src_rect=None):
    """Why a running mirror must stop, or None. Evaluated by the supervisor on every output change the
    compositor announces.

    The region rules are the start-time refusal, applied for as long as the mirror lives. A region is a
    rectangle of the LAYOUT, resolved against the source once, when wl-mirror starts: move or resize that output
    afterwards and the same rectangle names different pixels, or pixels that are no longer there at all -- and
    wl-mirror says nothing, it clamps. A mirror that refuses to start on a region outside its source must not
    keep running when the layout puts it there."""
    src = by_name(outputs, source)
    dst = by_name(outputs, target)
    if src is None:
        return "source output %s is gone" % source
    if dst is None:
        return "target output %s is gone" % target
    if not src.active:
        return "source output %s was turned off" % source
    if not dst.active:
        return "target output %s was turned off" % target
    if rects_overlap(src, dst):
        return ("%s and %s now share pixels; the mirror would capture " "itself" % (source, target))
    if region is not None:
        if src_rect is not None and tuple(src_rect) != src.rect():
            return ("%s moved or changed size (%dx%d+%d+%d -> %s); the "
                    "region %s no longer names the pixels this mirror was "
                    "started on"
                    % ((source,) + _rect_wh(src_rect) + (src.geom(),
                                                         fmt_region(region))))
        if not rect_inside(region, src):
            return ("the region %s is no longer inside %s (%s)" % (fmt_region(region), source, src.geom()))
    return None


def _rect_wh(rect) -> tuple:
    x, y, w, h = rect
    return (w, h, x, y)


# -- state --------------------------------------------------------------------

def state_path() -> str:
    """This tool's own file, in session.runtime_dir(), next to wxrandr's and shaped the same way. It is a
    separate file on purpose: these records are ours, not the layout cache's, and the layout cache must not have
    to know about them. A runtime dir we cannot have degrades to the 0.2 name in shared /tmp rather than failing
    the command."""
    try:
        return os.path.join(session.runtime_dir(), "wmirror-state.json")
    except CmdError:
        return "/tmp/wmirror-state-%d.json" % os.getuid()


#: How long a command waits for another wmirror to finish before going
#: ahead without the lock. A start holds it for about a second.
LOCK_SECONDS = 8.0


def lock_path() -> str:
    return state_path() + ".start.lock"


@contextlib.contextmanager
def state_lock(timeout: float = LOCK_SECONDS):
    """Serialise the commands that CHANGE the records, for the whole of read-decide-start-write.

    Without it two starts on one target both read an empty file, both spawn a helper, and the second write drops
    the first record -- leaving a wl-mirror fullscreen on somebody's screen that `--list` cannot see and
    `--stop` cannot end (measured: two interleaved starts, two helpers, one record). `State.save`'s own lock
    cannot close that window: it is taken after the helper already exists.

    `fcntl.lockf`, not `flock`, and on a file of its own. A POSIX record lock belongs to the process, so the
    supervisor we fork does NOT inherit it -- with flock the mirror would hold the lock for its whole life
    whenever an exception skipped the unlock (measured both ways). The file is its own because closing any fd to
    a file drops that process's POSIX locks on it, and `State.save` opens and closes its lock file on every
    write.

    A lock we cannot get is not a reason to refuse: after `timeout` we go ahead unlocked, exactly as State does
    when locking is unavailable."""
    fd = None
    try:
        fd = os.open(lock_path(), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
    except OSError:
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.lockf(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def load_state():
    """wxrandr's State (its locking, its three-way merge, its refusal to trust a file that is not ours) over
    wmirror's own path, keyed by the compositor socket like every other per-session store here."""
    hit = session.find_wayland_socket()
    key = hit[2] if hit else "?"
    return wxcore.State(key, path=state_path())


def records(state) -> dict:
    """The `mirrors` container, coerced like State's own sub-dicts: the file is plain JSON and hand-editable,
    and a value of the wrong type must not become a TypeError somewhere else."""
    d = state.d.get("mirrors")
    if not isinstance(d, dict):
        d = state.d["mirrors"] = {}
    return d


def recorded(target: str, rec: dict) -> bool:
    """Is that record really on disk?

    The one thing a start must not do is leave a helper nobody can find. If the file could not be written -- a
    full or read-only XDG_RUNTIME_DIR, State's own refusal to trust what it found there -- the mirror is stopped
    again rather than left painting with no way to end it."""
    try:
        on_disk = records(load_state()).get(target)
    except Exception:
        return False
    return (isinstance(on_disk, dict)
            and on_disk.get("pid") == rec.get("pid")
            and on_disk.get("helper_pid") == rec.get("helper_pid"))


def fmt_record(target: str, rec: dict) -> str:
    """One `--list` line -- also what a successful start prints."""
    bits = ["%s <- %s" % (target, rec.get("source", "?"))]
    region = rec.get("region")
    if region:
        bits.append("region %s" % fmt_region(region))
    bits.append("scaling %s" % (rec.get("scaling") or DEFAULT_SCALING))
    bits.append("%s pid %s" % (HELPER, rec.get("helper_pid", "?")))
    if rec.get("orphan"):
        bits.append("(supervisor gone)")
    return "  ".join(bits)
